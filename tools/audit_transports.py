"""Exercise installed NDJSON and all MCP tools using the configured account.

Starts children in /tmp without PYTHONPATH. Stores only pass/fail summaries in
the private vault. Export tools write their normal private bundles there too.
"""
import asyncio
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from qq_local_interface import PROTOCOL_VERSION
from qq_local_interface.protocol import OPERATIONS
from qq_local_interface.source import QQSource
from qq_local_interface.util import atomic_json, utc_now
from qq_local_interface.vault import QQVault


def cases(vault):
    source = QQSource(vault.current()[0])
    ref = None
    for database in source.list_databases():
        if database['state'] != 'ready':
            continue
        for table in source.list_tables(database['database']):
            if not table['accessible'] or not table['row_count']:
                continue
            cursor = None
            while True:
                page = source.read_records(database['database'], table['table'], limit=100, cursor=cursor)
                ref = next((v['cell_ref'] for row in page['rows'] for v in row['values'].values() if v.get('encoding') == 'reference'), None)
                cursor = page['next_cursor']
                if ref or cursor is None:
                    break
            if ref:
                break
        if ref:
            break
    if not ref:
        raise RuntimeError('no real large-cell sample available')
    message = source.read_items(limit=1)['items'][0]
    group = source.list_conversations(kinds=['group'], limit=1)[0]
    asset = source.list_assets(limit=1)['items'][0]
    favorite = source.read_favorites(limit=1)['items'][0]
    dataset = next(d for d in source.list_datasets() if d.get('accessible') and d['category'] == 'favorites')
    output = str(vault.home / 'exports' / 'transport-audit')
    return {
        'status': {}, 'sync.status': {}, 'contacts.list': {'limit': 1}, 'contacts.read': {'limit': 1},
        'conversations.list': {'limit': 1}, 'conversations.read': {'limit': 1},
        'groups.members': {'conversation_id': group['id'], 'limit': 1}, 'messages.read': {'limit': 1},
        'messages.search': {'query': 'https://', 'limit': 1},
        'messages.get': {'item_id': message['id'], 'include_raw': True},
        'messages.export': {'output': output, 'conversation_ids': [message['conversation_id']]},
        'changes.list': {'limit': 1}, 'databases.list': {}, 'tables.list': {'database': 'nt_msg.db'},
        'datasets.list': {}, 'datasets.read': {'dataset_id': dataset['dataset_id'], 'limit': 1},
        'records.read': {'database': 'nt_msg.db', 'table': 'group_msg_table', 'limit': 1},
        'records.export': {'database': 'nt_msg.db', 'table': 'group_msg_table', 'output': output},
        'cells.read': {'cell_ref': ref, 'length': 127}, 'assets.list': {'limit': 1},
        'assets.read': {'asset_id': asset['id'], 'length': 127},
        'favorites.read': {'limit': 1}, 'favorites.get': {'favorite_id': favorite['id'], 'include_raw': True},
        'favorites.export': {'output': output},
        'sync.refresh': {'allow_partial': True},
    }


async def mcp_check(vault, env, core_only=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    inputs = cases(vault)
    mapping = {'qq_status': 'status', 'qq_contacts': 'contacts.list', 'qq_conversations': 'conversations.list',
        'qq_contacts_page': 'contacts.read', 'qq_conversations_page': 'conversations.read',
        'qq_group_members': 'groups.members', 'qq_messages': 'messages.read', 'qq_search': 'messages.search',
        'qq_changes': 'changes.list', 'qq_message': 'messages.get', 'qq_databases': 'databases.list',
        'qq_tables': 'tables.list', 'qq_records': 'records.read', 'qq_cell': 'cells.read',
        'qq_assets': 'assets.list', 'qq_asset': 'assets.read', 'qq_datasets': 'datasets.list',
        'qq_dataset': 'datasets.read', 'qq_export_records': 'records.export', 'qq_export': 'messages.export',
        'qq_sync_refresh': 'sync.refresh', 'qq_favorites': 'favorites.read', 'qq_favorite': 'favorites.get',
        'qq_export_favorites': 'favorites.export'}
    checks = []
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'qq_local_interface', '--vault', str(vault.root), '--source-id', vault.source_id, 'mcp'],
        env=env, cwd='/tmp')
    with tempfile.TemporaryFile(mode='w+') as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                advertised = {t.name for t in (await client.list_tools()).tools}
                if advertised != set(mapping):
                    raise RuntimeError('MCP tool catalog mismatch')
                for tool, operation in mapping.items():
                    if core_only and operation.startswith('favorites.'):
                        continue
                    result = await client.call_tool(tool, inputs[operation])
                    response = result.structuredContent
                    if response is None:
                        response = json.loads(next(c.text for c in result.content if c.type == 'text'))
                    checks.append({'tool': tool, 'passed': not result.isError and response.get('ok') is True,
                                   'error': response.get('error', {}).get('code')})
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-only', action='store_true', help='leave optional favorites work deferred')
    args = parser.parse_args()
    vault = QQVault()
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    inputs = cases(vault)
    if set(inputs) != set(OPERATIONS):
        raise RuntimeError('operation catalog mismatch')
    if args.core_only:
        inputs = {op: p for op, p in inputs.items() if not op.startswith('favorites.')}
    requests = [{'protocol_version': PROTOCOL_VERSION, 'request_id': str(i), 'operation': op, 'params': params}
                for i, (op, params) in enumerate(inputs.items())]
    result = subprocess.run([sys.executable, '-m', 'qq_local_interface', '--vault', str(vault.root),
        '--source-id', vault.source_id, 'rpc'], cwd='/tmp', env=env,
        input='\n'.join(json.dumps(r) for r in requests) + '\n', capture_output=True, text=True, timeout=120)
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    checks = [{'operation': request['operation'], 'passed': response.get('ok') is True,
               'error': response.get('error', {}).get('code')} for request, response in zip(requests, responses)]
    ndjson_passed = result.returncode == 0 and len(responses) == len(requests) and all(c['passed'] for c in checks)
    mcp = asyncio.run(mcp_check(vault, env, args.core_only))
    report = {'at': utc_now(), 'working_directory': '/tmp', 'pythonpath_removed': True, 'favorites_deferred': args.core_only,
        'ndjson': checks, 'mcp': mcp, 'passed': ndjson_passed and all(c['passed'] for c in mcp)}
    atomic_json(vault.home / 'transport-report.json', report)
    print(json.dumps({'passed': report['passed'], 'ndjson_operations': len(checks), 'mcp_tools': len(mcp),
                      'failures': [c for c in checks + mcp if not c['passed']]}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
