from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import PASSPHRASE, SALT, encrypt_file, encrypt_wal, fixture, text_body, independent_checksum
from qq_local_interface import PROTOCOL_VERSION
from qq_local_interface.crypto import DatabaseKey, decrypt_database, first_page, verify_key, wal_plan
from qq_local_interface.errors import QQError
from qq_local_interface.lldb_capture import verified
from qq_local_interface.protobuf import decode_body
from qq_local_interface.protocol import QQProtocol
from qq_local_interface.source import QQSource
from qq_local_interface.util import atomic_json
from qq_local_interface.vault import QQVault


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = DatabaseKey("passphrase", PASSPHRASE.encode())

    def tearDown(self):
        self.temp.cleanup()

    def test_decrypt_preserves_real_sqlite_reserved_space(self):
        for reserve in (48, 80):
            with self.subTest(reserve=reserve):
                plain = fixture(self.root / str(reserve), reserve=reserve)
                encrypted, result = self.root / f"{reserve}.encrypted", self.root / f"{reserve}.clear"
                encrypt_file(plain, encrypted)
                self.assertTrue(verify_key(encrypted, self.key))
                decrypt_database(encrypted, result, self.key)
                self.assertEqual(result.read_bytes()[20], reserve)
                with sqlite3.connect(result) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM c2c_msg_table').fetchone()[0], 3)
                    self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_wrong_key_and_corrupt_page_do_not_publish_output(self):
        plain = fixture(self.root / "source")
        encrypted, output = self.root / "encrypted", self.root / "clear"
        encrypt_file(plain, encrypted)
        with self.assertRaises(QQError) as error:
            decrypt_database(encrypted, output, DatabaseKey("passphrase", b"wrong"))
        self.assertEqual(error.exception.code, "authentication_failed")
        self.assertFalse(output.exists())
        data = bytearray(encrypted.read_bytes())
        data[1024 + 4096 + 20] ^= 1
        encrypted.write_bytes(data)
        with self.assertRaises(QQError):
            decrypt_database(encrypted, output, self.key)
        self.assertFalse(output.exists())

    def test_lldb_key_oracle_matches_authenticated_page(self):
        import hashlib
        plain = fixture(self.root / "source")
        encrypted = self.root / "encrypted"
        encrypt_file(plain, encrypted)
        key = hashlib.pbkdf2_hmac("sha512", PASSPHRASE.encode(), SALT, 4000, 32)
        self.assertTrue(verified(first_page(encrypted), key))
        self.assertFalse(verified(first_page(encrypted), bytes(32)))

    def test_wal_contains_real_committed_rows_for_plain_and_encrypted_snapshots(self):
        source = fixture(self.root / "source")
        conn = sqlite3.connect(source)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            conn.execute('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?)',
                         (44, 'u_peer', 'u_peer', 222, 222, 1700000044, 'Fixture Peer', text_body('WAL-only row')))
            conn.commit()
            wal = Path(str(source) + '-wal')
            self.assertGreater(wal_plan(wal, 4096)['committed_frames'], 0)
            for encrypted in (False, True):
                with self.subTest(encrypted=encrypted):
                    if encrypted:
                        src, log = self.root / 'cipher.db', self.root / 'cipher.db-wal'
                        encrypt_file(source, src)
                        encrypt_wal(wal, log)
                    else:
                        src, log = source, wal
                    output = self.root / f'output-{encrypted}.db'
                    decrypt_database(src, output, self.key if encrypted else None, log)
                    with sqlite3.connect(output) as target:
                        self.assertEqual(target.execute('SELECT count(*) FROM c2c_msg_table').fetchone()[0], 4)
                        self.assertEqual(target.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            conn.close()

    def test_wal_uncommitted_tail_is_not_published(self):
        import struct
        source = fixture(self.root / 'source')
        connection = sqlite3.connect(source)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA wal_autocheckpoint=0')
            connection.execute('UPDATE c2c_msg_table SET "40800"=? WHERE "40001"=1', (text_body('committed'),))
            connection.commit()
            raw = Path(str(source) + '-wal').read_bytes()
            plan = wal_plan(Path(str(source) + '-wal'), 4096)
            expected_frames = plan['committed_frames']
            header = raw[:32]
            last = raw[-4120:]
            endian = '<' if struct.unpack('>I', header[:4])[0] == 0x377F0682 else '>'
            # Append an authenticated-checksum frame with no commit marker.
            frame = last[:4] + bytes(4) + header[16:24]
            checksum = independent_checksum(frame[:8] + last[24:], struct.unpack('>II', last[16:24]), endian)
            detached = self.root / 'tail.wal'
            detached.write_bytes(raw + frame + struct.pack('>II', *checksum) + last[24:])
            actual = wal_plan(detached, 4096)
            self.assertTrue(actual['ignored_tail'])
            self.assertEqual(actual['committed_frames'], expected_frames)
            target = self.root / 'committed.db'
            decrypt_database(source, target, wal=detached)
            with sqlite3.connect(target) as c:
                self.assertEqual(c.execute('SELECT count(*) FROM c2c_msg_table').fetchone()[0], 3)
                self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            connection.close()


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        fixture(self.input)
        self.vault = QQVault(self.root / "vault", "test-qq")
        self.vault.configure(self.input, account_uin="111", account_uid="u_self")
        self.vault.refresh()
        self.source = self.fresh()

    def tearDown(self):
        self.temp.cleanup()

    def fresh(self):
        return QQSource(self.vault.current()[0])

    def request(self, operation, params=None, **envelope):
        return QQProtocol(vault=self.vault).handle({"protocol_version": PROTOCOL_VERSION,
            "request_id": "test", "operation": operation, "params": params or {}, **envelope})

    def test_contacts_conversations_and_partial_membership(self):
        contacts = self.source.list_contacts()
        self.assertEqual(len(contacts), 2)
        self.assertEqual(next(x for x in contacts if x['qq_number'] == '222')['friend_status'], 'friend')
        chats = self.source.list_conversations(kinds=['group'])
        members = self.source.list_group_members(chats[0]['id'])
        self.assertFalse(members['complete'])
        self.assertEqual(len(members['members']), 1)

    def test_read_filters_page_and_large_message_id(self):
        page = self.source.read_items(limit=2)
        second = self.source.read_items(limit=2, cursor=page['next_cursor'])
        self.assertEqual(len({i['id'] for i in page['items'] + second['items']}), 4)
        group = next(i for i in second['items'] if i['kind'] == 'text' and i['text'] == 'group text')
        self.assertEqual(group['provenance']['local_id'], '9007199254740993')
        self.assertEqual(len(self.source.read_items(directions=['outgoing'])['items']), 2)
        self.assertEqual(len(self.source.read_items(start='2023-11-14T22:13:20Z', end='2023-11-14T22:13:21Z')['items']), 1)

    def test_cursor_cannot_cross_filter_or_new_snapshot(self):
        cursor = self.source.read_items(limit=1)['next_cursor']
        with self.assertRaises(QQError):
            self.source.read_items(query='test', cursor=cursor)
        self.vault.refresh('full')
        with self.assertRaises(QQError):
            self.fresh().read_items(cursor=cursor)
        self.assertEqual(len(self.source.read_items(cursor=cursor)['items']), 3)

    def test_no_change_sync_reuses_generation(self):
        result = self.vault.refresh()
        self.assertFalse(result['changed'])
        self.assertEqual(result['snapshot_id'], self.source.snapshot_id)

    def test_status_change_watermark_supports_initial_read_then_updates(self):
        cursor = self.source.status()['change_cursor']
        self.assertEqual(self.source.changes(cursor=cursor)['changes'], [])
        self.source.read_items(limit=5000)
        with sqlite3.connect(self.input / 'nt_msg.db') as c:
            c.execute('UPDATE c2c_msg_table SET "40800"=? WHERE "40001"=1', (text_body('after bootstrap'),))
        self.vault.refresh()
        changes = self.fresh().changes(cursor=cursor)['changes']
        messages = [event for event in changes if event['entity_type'] == 'messages']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['op'], 'updated')
        self.assertEqual(len([e for e in changes if e['entity_type'] == 'databases']), 1)

    def test_subsecond_time_bound_and_unicode_casefold(self):
        page = self.source.read_items(start='2023-11-14T22:13:20.5Z', end='2023-11-14T22:13:21.5Z')
        self.assertEqual([i['text'] for i in page['items']], ['test message 1'])
        with sqlite3.connect(self.input / 'nt_msg.db') as c:
            c.execute('UPDATE c2c_msg_table SET "40800"=? WHERE "40001"=1', (text_body('Straße 中文'),))
        self.vault.refresh()
        self.assertEqual(len(self.fresh().search_items('STRASSE')['items']), 1)

    def test_normalizer_upgrade_rebuilds_index_without_redecrypting(self):
        snapshot, manifest = self.vault.current()
        manifest.pop('normalizer_version')
        atomic_json(snapshot / 'manifest.json', manifest)
        result = self.vault.refresh()
        self.assertTrue(result['changed'])
        self.assertEqual(result['updated_databases'], [])
        self.assertEqual(len(result['reused_databases']), 2)

    def test_sync_lock_prevents_two_writers(self):
        with self.vault._writer():
            with self.assertRaises(QQError) as error:
                self.vault.refresh()
            self.assertEqual(error.exception.code, 'sync_busy')

    def test_hot_rollback_journal_keeps_last_good_snapshot(self):
        (self.input / 'nt_msg.db-journal').write_bytes(b'active journal')
        with self.assertRaises(QQError) as error:
            self.vault.refresh()
        self.assertEqual(error.exception.code, 'source_busy')
        self.assertEqual(self.vault.current()[1]['generation'], self.source.snapshot_id)

    def test_changes_survive_refresh_and_track_updates_and_missing_without_deletion(self):
        cursor = self.source.changes(limit=5000)['cursor']
        with sqlite3.connect(self.input / 'nt_msg.db') as c:
            c.execute('UPDATE c2c_msg_table SET "40800"=? WHERE "40001"=1', (text_body('updated body'),))
            c.execute('DELETE FROM c2c_msg_table WHERE "40001"=2')
            c.execute('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?)',
                (55, 'u_peer', 'u_peer', 222, 222, 1700000055, 'Fixture Peer', text_body('new body')))
        result = self.vault.refresh()
        self.assertEqual(result['reused_databases'], ['profile_info.db'])
        self.assertEqual(self.source.read_items()['items'][0]['text'], 'test message 0')
        changes = self.fresh().changes(cursor=cursor, limit=5000)['changes']
        message_changes = [x for x in changes if x['entity_type'] == 'messages']
        self.assertEqual({x['op'] for x in message_changes}, {'added', 'updated', 'missing'})
        self.assertFalse(next(x for x in message_changes if x['op'] == 'missing')['is_deletion'])

    def test_cursor_rejects_another_account(self):
        other = QQVault(self.root / 'vault', 'other')
        other.configure(self.input, account_uin='999')
        other.refresh()
        cursor = self.source.changes()['cursor']
        with self.assertRaises(QQError):
            QQSource(other.current()[0]).changes(cursor=cursor)
        with self.assertRaises(QQError):
            self.vault.configure(self.input, account_uin='999')

    def test_failed_refresh_keeps_previous_snapshot(self):
        (self.input / 'nt_msg.db').write_bytes(b'invalid database')
        with self.assertRaises(QQError):
            self.vault.refresh()
        self.assertEqual(self.vault.current()[1]['generation'], self.source.snapshot_id)
        self.assertEqual(len(self.fresh().read_items()['items']), 4)

    def test_protocol_rejects_bad_requests_and_continues(self):
        protocol = QQProtocol(vault=self.vault)
        self.assertEqual(protocol.handle_line('{')['error']['code'], 'invalid_json')
        self.assertTrue(self.request('status')['ok'])
        self.assertEqual(self.request('status', source_id='wrong')['error']['code'], 'source_mismatch')
        self.assertEqual(self.request('messages.read', {'limit': True})['error']['code'], 'invalid_argument')
        self.assertEqual(self.request('messages.read', {'conversation_ids': []})['error']['code'], 'invalid_argument')
        self.assertEqual(self.request('status', {'unknown': 1})['error']['code'], 'invalid_argument')
        self.assertEqual(self.request('messages.search')['error']['code'], 'invalid_argument')
        self.assertEqual(self.request('messages.read', {'start': '2026-01-01'})['error']['code'], 'invalid_argument')

    def test_unsupported_protobuf_is_explicit(self):
        self.assertEqual(decode_body(b'\xff')['quality']['status'], 'decode_error')
        self.assertEqual(decode_body(b'\x08\x01')['quality']['status'], 'unsupported')

    def test_export_is_private_complete_and_diffable(self):
        first = self.source.export_bundle(self.root / 'exports')
        directory = Path(first['output'])
        self.assertEqual(len((directory / 'items.jsonl').read_text().splitlines()), 4)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'items.jsonl').stat().st_mode & 0o777, 0o600)
        second = self.source.export_bundle(self.root / 'exports', previous=directory)
        self.assertEqual(second['counts']['added'], 0)
        self.assertNotEqual(second['output'], first['output'])
        with self.assertRaises(QQError):
            self.source.export_bundle(self.source.root / 'inside')

    def test_full_export_crosses_page_boundaries(self):
        large = self.root / 'large'
        fixture(large, count=1107)
        vault = QQVault(self.root / 'vault', 'large')
        vault.configure(large, account_uin='111', account_uid='u_self')
        vault.refresh()
        source = QQSource(vault.current()[0])
        result = source.export_bundle(self.root / 'exports')
        self.assertEqual(result['counts']['items'], 1108)


if __name__ == '__main__':
    unittest.main()
