"""Every operation and MCP tool runs against an isolated synthetic account."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import fixture, favorite_fixture
from qq_local_interface import PROTOCOL_VERSION
from qq_local_interface.protocol import OPERATIONS, QQProtocol
from qq_local_interface.source import QQSource
from qq_local_interface.vault import QQVault

REPOSITORY = Path(__file__).resolve().parents[1]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        incoming = self.root / 'account' / 'nt_db'
        fixture(incoming)
        favorite_fixture(incoming)
        with sqlite3.connect(incoming / 'misc.db') as c:
            c.execute('CREATE TABLE binary_data(id INTEGER PRIMARY KEY,payload BLOB)')
            c.execute('INSERT INTO binary_data VALUES(1,?)', (bytes(range(256)) * 50,))
        cache = incoming.parent / 'nt_data'
        cache.mkdir()
        (cache / 'test.amr').write_bytes(b'#!AMR\n' + bytes(50))
        self.vault = QQVault(self.root / 'vault', 'test-qq')
        self.vault.configure(incoming, account_uin='111', account_uid='u_self')
        self.vault.refresh()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, operation, params=None):
        return {'protocol_version': PROTOCOL_VERSION, 'request_id': 'transport', 'operation': operation, 'params': params or {}}

    def cases(self):
        source = QQSource(self.vault.current()[0])
        message = source.read_items(limit=1)['items'][0]['id']
        group = source.list_conversations(kinds=['group'])[0]['id']
        dataset = next(d for d in source.list_datasets() if d['database'] == 'misc.db' and d['table'] == 'binary_data')
        ref = source.read_records('misc.db', 'binary_data')['rows'][0]['values']['payload']['cell_ref']
        asset = source.list_assets()['items'][0]['id']
        favorite_id = source.read_favorites(limit=1)['items'][0]['id']
        return {
            'status': {}, 'sync.status': {}, 'sync.refresh': {},
            'contacts.list': {}, 'contacts.read': {'limit': 1},
            'conversations.list': {}, 'conversations.read': {'limit': 1},
            'groups.members': {'conversation_id': group, 'limit': 1},
            'messages.read': {'limit': 1, 'directions': ['outgoing'], 'qualities': ['complete'], 'kinds': ['text']},
            'messages.search': {'query': 'test', 'directions': ['incoming'], 'qualities': ['complete'], 'kinds': ['text']},
            'messages.export': {'output': str(self.root / 'exports'), 'query': 'test', 'kinds': ['text'], 'directions': ['outgoing']},
            'changes.list': {'limit': 1}, 'messages.get': {'item_id': message, 'include_raw': True},
            'databases.list': {}, 'tables.list': {'database': 'misc.db'},
            'records.read': {'database': 'misc.db', 'table': 'binary_data'},
            'records.export': {'database': 'misc.db', 'table': 'binary_data', 'output': str(self.root / 'exports')},
            'cells.read': {'cell_ref': ref, 'offset': 13, 'length': 731},
            'assets.list': {}, 'assets.read': {'asset_id': asset},
            'datasets.list': {}, 'datasets.read': {'dataset_id': dataset['dataset_id']},
            'favorites.read': {'limit': 1}, 'favorites.get': {'favorite_id': favorite_id, 'include_raw': True},
            'favorites.export': {'output': str(self.root / 'exports')},
        }

    def test_all_operations_and_machine_readable_contracts(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest('optional contract verification requires jsonschema')
        operation_schema = Draft202012Validator(json.loads((REPOSITORY / 'schemas/qq.local.operations.v1.json').read_text()))
        envelope_schema = Draft202012Validator(json.loads((REPOSITORY / 'schemas/qq.local.protocol.v1.json').read_text()))
        record_schema = Draft202012Validator(json.loads((REPOSITORY / 'schemas/qq.local.data.v1.json').read_text()))
        protocol = QQProtocol(vault=self.vault)
        cases = self.cases()
        self.assertEqual(set(cases), set(OPERATIONS))
        for operation, params in cases.items():
            with self.subTest(operation=operation):
                request = self.request(operation, params)
                operation_schema.validate(request)
                response = protocol.handle(request)
                self.assertTrue(response['ok'], response.get('error'))
                envelope_schema.validate(response)
                if operation in ('records.read', 'datasets.read'):
                    for row in response['data']['rows']:
                        record_schema.validate(row)
                bad = protocol.handle(self.request(operation, {**params, 'unexpected_parameter': True}))
                self.assertFalse(bad['ok'])
                self.assertEqual(bad['error']['code'], 'invalid_argument')

    def test_ndjson_and_direct_cli_from_another_working_directory(self):
        cases = self.cases()
        requests = [self.request(op, p) for op, p in cases.items()]
        # Absolute PYTHONPATH supports source checkouts; the actual installed
        # account is additionally checked without PYTHONPATH by integration QA.
        env = {**os.environ, 'PYTHONPATH': str(REPOSITORY / 'src')}
        prefix = [sys.executable, '-m', 'qq_local_interface', '--vault', str(self.vault.root), '--source-id', 'test-qq']
        result = subprocess.run(prefix + ['rpc'], input='\n'.join(json.dumps(r) for r in requests) + '\n',
            cwd='/tmp', env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), len(requests))
        self.assertTrue(all(r['ok'] for r in responses))
        direct = subprocess.run(prefix + ['records', 'misc.db', 'binary_data', '--equals', '{"id":1}'],
            cwd='/tmp', env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(direct.returncode, 0)
        self.assertEqual(len(json.loads(direct.stdout)['data']['rows']), 1)

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP SDK is not installed')
    def test_all_mcp_tools_over_real_stdio_transport(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cases = self.cases()
        tool_operations = {'qq_status': 'status', 'qq_sync_refresh': 'sync.refresh',
            'qq_contacts': 'contacts.list', 'qq_conversations': 'conversations.list',
            'qq_contacts_page': 'contacts.read', 'qq_conversations_page': 'conversations.read',
            'qq_group_members': 'groups.members', 'qq_messages': 'messages.read', 'qq_search': 'messages.search',
            'qq_changes': 'changes.list', 'qq_message': 'messages.get', 'qq_databases': 'databases.list',
            'qq_tables': 'tables.list', 'qq_records': 'records.read', 'qq_cell': 'cells.read',
            'qq_assets': 'assets.list', 'qq_asset': 'assets.read', 'qq_datasets': 'datasets.list',
            'qq_dataset': 'datasets.read', 'qq_export_records': 'records.export', 'qq_export': 'messages.export',
            'qq_favorites': 'favorites.read', 'qq_favorite': 'favorites.get', 'qq_export_favorites': 'favorites.export'}

        async def exercise():
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'qq_local_interface', '--vault', str(self.vault.root), '--source-id', 'test-qq', 'mcp'],
                env={**os.environ, 'PYTHONPATH': str(REPOSITORY / 'src')}, cwd='/tmp')
            with tempfile.TemporaryFile(mode='w+') as errors:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = (await client.list_tools()).tools
                        self.assertEqual({t.name for t in tools}, set(tool_operations))
                        for tool, operation in tool_operations.items():
                            result = await client.call_tool(tool, cases[operation])
                            self.assertFalse(result.isError, tool)
                            response = result.structuredContent
                            if response is None:
                                response = json.loads(next(c.text for c in result.content if c.type == 'text'))
                            self.assertTrue(response['ok'], {'tool': tool, 'error': response.get('error')})
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
