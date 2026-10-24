"""Regression coverage uses synthetic data and independent byte-level expectations."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from helpers import PASSPHRASE, SALT, create_reserved_database, field, fixture, varint
from qq_local_interface.crypto import DatabaseKey, decrypt_database, first_page, verify_key
from qq_local_interface.errors import QQError
from qq_local_interface.lldb_capture import probe_context
from qq_local_interface.protobuf import decode_body, json_card, wire_fields
from qq_local_interface.source import QQSource
from qq_local_interface.vault import QQVault


class RichBodyTests(unittest.TestCase):
    def body(self, native, *parts):
        return field(40800, field(45002, native) + b''.join(parts))

    def test_observed_element_types_and_preview_regression(self):
        # QQNT's published enum values, independent of the decoder mapping.
        cases = [(1, 'text'), (2, 'image'), (3, 'file'), (4, 'voice'),
                 (5, 'video'), (6, 'sticker'), (7, 'quote'), (8, 'system'),
                 (9, 'wallet'), (10, 'card'), (11, 'sticker'), (16, 'forward')]
        for native, expected in cases:
            with self.subTest(native=native):
                result = decode_body(self.body(native, field(45411, 640), field(45412, 480)))
                self.assertEqual(result['kind'], expected)
                self.assertEqual(result['elements'][0]['native_type'], str(native))
                self.assertNotEqual(result['quality']['status'], 'unsupported')
        result = decode_body(self.body(3, field(45402, 'document.pdf'), field(45405, 9007199254740993), field(45411, 120)))
        self.assertEqual(result['attachments'][0]['kind'], 'file')
        self.assertEqual(result['attachments'][0]['size_bytes'], 9007199254740993)

    def test_text_mixed_order_and_unknown_field_preservation(self):
        raw = self.body(1, field(45101, '第一段')) + self.body(6, field(47601, 23)) + self.body(1, field(45101, '第三段'))
        result = decode_body(raw)
        self.assertEqual([e['kind'] for e in result['elements']], ['text', 'sticker', 'text'])
        self.assertEqual(result['text'], '第一段第三段')
        self.assertEqual(result['kind'], 'mixed')
        unknown = b'\x00\xff' * 7
        result = decode_body(self.body(777, field(49999, unknown)))
        self.assertEqual(result['quality']['status'], 'unsupported')
        self.assertEqual(base64.b64decode(result['elements'][0]['native_fields'][-1]['data']), unknown)
        self.assertEqual(result['raw_body']['sha256'], hashlib.sha256(self.body(777, field(49999, unknown))).hexdigest())

    def test_quote_sequence_sticker_card_and_forward(self):
        result = decode_body(self.body(7, field(47402, 9007199254740993), field(47403, 1700000000)))
        self.assertEqual(result['elements'][0]['target']['sequence'], '9007199254740993')
        self.assertIsNone(result['elements'][0]['target']['local_id'])
        self.assertEqual(decode_body(self.body(6, field(47601, 66)))['elements'][0]['native_index'], '66')
        card = {'title': '测试卡片', 'url': 'https://example.com/item'}
        for raw in (json.dumps(card).encode(), zlib.compress(json.dumps(card).encode())):
            result = decode_body(self.body(10, field(47901, raw)))
            self.assertEqual(result['elements'][0]['card'], card)
            self.assertIn('测试卡片', result['search_text'])
        result = decode_body(self.body(16, field(48601, 'resource'), field(48602, '<msg/>')))
        self.assertEqual(result['elements'][0]['native_xml'], '<msg/>')

    def test_fixed_width_wire_types_overflow_and_limits(self):
        raw = varint(11 << 3 | 1) + bytes(range(8)) + varint(12 << 3 | 5) + bytes(range(4))
        self.assertEqual(wire_fields(raw), [(11, 1, bytes(range(8))), (12, 5, bytes(range(4)))])
        for bad in (b'\x80', b'\x00', b'\x08' + b'\xff' * 10, b'\x0a\x10a', b'\x09a'):
            with self.subTest(bad=bad.hex()):
                with self.assertRaises(ValueError):
                    wire_fields(bad)
        self.assertEqual(decode_body(2 ** 60)['quality']['status'], 'decode_error')
        self.assertIsNone(json_card(zlib.compress(b' ' * (5 * 1024 * 1024))))
        self.assertEqual(decode_body(b'x' * (4 * 1024 * 1024 + 1))['quality']['status'], 'decode_error')

    def test_extremely_nested_card_retains_raw_access_without_aborting_sync(self):
        raw = self.body(10, field(47901, b'[' * 2000 + b'0' + b']' * 2000))
        result = decode_body(raw)
        self.assertEqual(result['kind'], 'card')
        self.assertEqual(result['elements'][0]['card_decode_status'], 'raw_fields_available')
        self.assertEqual(result['raw_body']['sha256'], hashlib.sha256(raw).hexdigest())


class LosslessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / 'account' / 'nt_db'
        fixture(self.input)
        self.big = bytes(range(256)) * 75
        self.long_text = '文本\x00\U0001f642' * 2500
        with sqlite3.connect(self.input / 'misc.db') as c:
            c.execute('CREATE TABLE "odd""table"("id" INTEGER PRIMARY KEY,"nil","int","real","text","blob")')
            c.execute('INSERT INTO "odd""table" VALUES(1,NULL,?,?,?,?)', (-(2 ** 63), 5e-324, self.long_text, self.big))
            c.execute('INSERT INTO "odd""table" VALUES(2,NULL,?,?,CAST(X\'80ff00\' AS TEXT),?)', (2 ** 63 - 1, -1.25, b''))
            c.execute('CREATE TABLE no_rowid(a BLOB,b TEXT,payload BLOB,PRIMARY KEY(a,b)) WITHOUT ROWID')
            c.execute('INSERT INTO no_rowid VALUES(?,CAST(X\'80ff\' AS TEXT),?)', (b'key', self.big))
            c.execute('CREATE TABLE shadowed(rowid TEXT,_rowid_ TEXT,oid TEXT,payload BLOB)')
            c.executemany('INSERT INTO shadowed VALUES(?,?,?,?)', [('r', 's', 't', self.big)] * 2)
            c.execute('CREATE TABLE empty_table(x TEXT)')
            c.execute('CREATE VIRTUAL TABLE internal_fts USING fts5(body)')
            c.execute('INSERT INTO internal_fts VALUES(?)', ('indexed text',))
            c.execute("CREATE VIRTUAL TABLE contentless_fts USING fts5(body,content='')")
            c.execute('INSERT INTO contentless_fts(rowid,body) VALUES(8,?)', ('erased original',))
            c.execute('CREATE TABLE external_content(body TEXT,description TEXT)')
            c.execute('INSERT INTO external_content VALUES(?,?)', ('external body', 'description'))
            c.execute("CREATE VIRTUAL TABLE external_fts USING fts5(body,description UNINDEXED,content='external_content')")
        (self.input / 'uninitialized.db').write_bytes(bytes(1024))
        cache = self.input.parent / 'nt_data'
        cache.mkdir()
        self.media = cache / 'sample'
        self.media_bytes = b'RIFF' + bytes(4) + b'WEBP' + self.big
        self.media.write_bytes(self.media_bytes)
        self.vault = QQVault(self.root / 'vault', 'test-qq')
        self.vault.configure(self.input, account_uin='111', account_uid='u_self')
        self.vault.refresh()
        self.source = QQSource(self.vault.current()[0])

    def tearDown(self):
        self.temp.cleanup()

    def materialize(self, cell, source=None):
        source = source or self.source
        if cell['encoding'] == 'utf8':
            return cell['value'].encode()
        if cell['encoding'] == 'base64':
            return base64.b64decode(cell['data'])
        result, offset = bytearray(), 0
        while True:
            chunk = source.read_cell(cell['cell_ref'], offset, 997)
            part = base64.b64decode(chunk['data'])
            self.assertEqual(hashlib.sha256(part).hexdigest(), chunk['chunk_sha256'])
            result.extend(part)
            offset = chunk['next_offset']
            if offset is None:
                break
        self.assertEqual(hashlib.sha256(result).hexdigest(), cell['sha256'])
        return bytes(result)

    def test_all_sqlite_storage_classes_exact_bytes_and_quoted_identifiers(self):
        first = self.source.read_records('misc.db', 'odd"table', limit=1)
        row = first['rows'][0]['values']
        self.assertEqual(row['nil'], {'type': 'null', 'value': None})
        self.assertEqual(row['int']['value'], '-9223372036854775808')
        self.assertEqual(struct.pack('>d', float.fromhex(row['real']['value'])), struct.pack('>d', 5e-324))
        self.assertEqual(self.materialize(row['text']), self.long_text.encode())
        self.assertEqual(self.materialize(row['blob']), self.big)
        second = self.source.read_records('misc.db', 'odd"table', limit=1, cursor=first['next_cursor'])
        self.assertEqual(second['rows'][0]['values']['int']['value'], '9223372036854775807')
        self.assertEqual(self.materialize(second['rows'][0]['values']['text']), bytes.fromhex('80ff00'))
        self.assertIsNone(second['next_cursor'])

    def test_without_rowid_composite_key_and_shadowed_rowid_duplicates(self):
        for table, count in (('no_rowid', 1), ('shadowed', 2)):
            page = self.source.read_records('misc.db', table)
            self.assertEqual(len(page['rows']), count)
            self.assertEqual(len({r['id'] for r in page['rows']}), count)
            for record in page['rows']:
                self.assertEqual(self.materialize(record['values']['payload']), self.big)

    def test_case_insensitive_hidden_rowid_and_fts_content_columns(self):
        with sqlite3.connect(self.input / 'misc.db') as c:
            c.execute('CREATE TABLE uppercase_aliases(ROWID TEXT,_ROWID_ TEXT,OID TEXT,payload BLOB)')
            c.executemany('INSERT INTO uppercase_aliases VALUES(?,?,?,?)', [('r', 's', 't', self.big)] * 2)
            c.execute("CREATE VIRTUAL TABLE case_fts USING fts5(BODY,Description UNINDEXED,content='external_content')")
        self.vault.refresh()
        fresh = QQSource(self.vault.current()[0])
        rows = fresh.read_records('misc.db', 'uppercase_aliases')['rows']
        self.assertEqual(len({r['id'] for r in rows}), 2)
        self.assertEqual(self.materialize(rows[0]['values']['payload'], fresh), self.big)
        rows = fresh.read_records('misc.db', 'case_fts')['rows']
        self.assertEqual(self.materialize(rows[0]['values']['BODY'], fresh), b'external body')
        self.assertEqual(self.materialize(rows[0]['values']['Description'], fresh), b'description')

    def test_fts_logical_values_match_sqlite(self):
        with sqlite3.connect(self.input / 'misc.db') as c:
            c.text_factory = bytes
            for table, columns in [('internal_fts', ['body']), ('external_fts', ['body', 'description']), ('contentless_fts', ['body'])]:
                expected = c.execute('SELECT ' + ','.join(columns) + ' FROM ' + table).fetchall()
                actual = self.source.read_records('misc.db', table)['rows']
                values = [tuple(self.materialize(r['values'][n]) if r['values'][n]['type'] != 'null' else None for n in columns) for r in actual]
                self.assertEqual(values, expected)

    def test_exact_filters_null_blob_invalid_and_large_text(self):
        filters = [{'nil': None}, {'text': {'type': 'text', 'encoding': 'base64', 'data': base64.b64encode(bytes.fromhex('80ff00')).decode()}},
                   {'blob': {'type': 'blob', 'encoding': 'base64', 'data': base64.b64encode(self.big).decode()}}, {'text': self.long_text}]
        for where, count in zip(filters, (2, 1, 1, 1)):
            self.assertEqual(len(self.source.read_records('misc.db', 'odd"table', equals=where)['rows']), count)

    def test_cursor_and_cell_boundaries_cannot_cross_filter_snapshot_or_source(self):
        first = self.source.read_records('misc.db', 'odd"table', limit=1)
        with self.assertRaises(QQError):
            self.source.read_records('misc.db', 'odd"table', equals={'id': 1}, cursor=first['next_cursor'])
        ref = first['rows'][0]['values']['blob']['cell_ref']
        for offset, length in ((-1, 10), (0, 1048577), (len(self.big) + 1, 1), (True, 1)):
            with self.assertRaises(QQError):
                self.source.read_cell(ref, offset, length)
        self.assertEqual(self.source.read_cell(ref, len(self.big), 1)['length'], 0)
        self.vault.refresh('full')
        fresh = QQSource(self.vault.current()[0])
        with self.assertRaises(QQError):
            fresh.read_cell(ref)
        self.assertEqual(self.materialize(first['rows'][0]['values']['blob']), self.big)

    def test_uninitialized_empty_and_unknown_tables(self):
        self.assertEqual(self.source.list_tables('uninitialized.db'), [])
        self.assertEqual(self.source.read_records('misc.db', 'empty_table')['rows'], [])
        with self.assertRaises(QQError) as e:
            self.source.read_records('uninitialized.db', 'any')
        self.assertEqual(e.exception.code, 'database_uninitialized')
        for database, table in (('misc.db', 'x";DROP TABLE empty_table;--'), ('../misc.db', 'empty_table')):
            with self.assertRaises(QQError):
                self.source.read_records(database, table)

    def test_raw_export_is_self_contained_private_and_complete(self):
        result = self.source.export_records('misc.db', 'odd"table', self.root / 'exports')
        path = Path(result['output'])
        records = [json.loads(l) for l in (path / 'records.jsonl').read_text().splitlines()]
        self.assertEqual(result['count'], 2)
        self.assertEqual(self.materialize(records[0]['values']['blob']), self.big)
        self.assertEqual(self.materialize(records[0]['values']['text']), self.long_text.encode())
        self.assertEqual((path / 'records.jsonl').stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(c.get('encoding') == 'reference' for r in records for c in r['values'].values()))

    def test_assets_byte_fidelity_missing_changed_and_symlink(self):
        items = self.source.list_assets(limit=1)
        self.assertEqual(items['items'][0]['mime_type'], 'image/webp')
        asset_id = items['items'][0]['id']
        result, offset = bytearray(), 0
        while True:
            chunk = self.source.read_asset(asset_id, offset, 1234)
            part = base64.b64decode(chunk['data'])
            self.assertEqual(hashlib.sha256(part).hexdigest(), chunk['chunk_sha256'])
            result.extend(part)
            offset = chunk['next_offset']
            if offset is None:
                break
        self.assertEqual(bytes(result), self.media_bytes)
        self.media.write_bytes(b'changed')
        self.assertEqual(self.source.list_assets()['items'][0]['availability'], 'available')
        self.assertEqual(base64.b64decode(self.source.read_asset(asset_id, length=1048576)['data']), self.media_bytes)
        self.media.unlink()
        self.assertEqual(base64.b64decode(self.source.read_asset(asset_id, length=1048576)['data']), self.media_bytes)
        outside = self.root / 'outside'
        outside.write_bytes(b'private')
        self.media.symlink_to(outside)
        self.assertEqual(base64.b64decode(self.source.read_asset(asset_id, length=1048576)['data']), self.media_bytes)
        captured = self.source.root / 'assets' / asset_id
        captured.unlink()
        captured.symlink_to(outside)
        with self.assertRaises(QQError) as e:
            self.source.read_asset(asset_id)
        self.assertEqual(e.exception.code, 'invalid_path')

    def test_media_only_sync_and_asset_change_events(self):
        cursor = self.source.status()['change_cursor']
        self.media.write_bytes(self.media_bytes + b'new')
        result = self.vault.refresh()
        self.assertTrue(result['changed'])
        self.assertEqual(result['updated_databases'], [])
        fresh = QQSource(self.vault.current()[0])
        events = [e for e in fresh.changes(cursor=cursor)['changes'] if e['entity_type'] == 'assets']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['op'], 'updated')
        self.assertFalse(self.vault.refresh()['changed'])

    def test_entity_and_member_pagination_has_no_silent_truncation(self):
        for method, total in ((self.source.read_contacts, 2), (self.source.read_conversations, 2)):
            items, cursor = [], None
            while True:
                page = method(limit=1, cursor=cursor)
                items.extend(page['items'])
                cursor = page['next_cursor']
                if cursor is None:
                    break
            self.assertEqual(len({r['id'] for r in items}), total)
        self.assertEqual(len(self.source.read_contacts(query='PEER')['items']), 1)
        self.assertEqual(len(self.source.read_conversations(kinds=['group'])['items']), 1)

    def test_group_name_is_not_owner_uid(self):
        with sqlite3.connect(self.input / 'group_info.db') as c:
            c.execute('CREATE TABLE group_detail_info_ver1("60001" INTEGER,"60002" TEXT,"60007" TEXT)')
            c.execute('INSERT INTO group_detail_info_ver1 VALUES(333,?,?)', ('u_owner', 'Actual Group Name'))
        self.vault.refresh()
        fresh = QQSource(self.vault.current()[0])
        self.assertEqual(fresh.list_conversations(kinds=['group'])[0]['display_name'], 'Actual Group Name')

    def test_group_members_have_continuation_and_filter_binding(self):
        with sqlite3.connect(self.input / 'group_info.db') as c:
            c.execute('CREATE TABLE group_member3("60001" INTEGER,"1000" TEXT,"1002" INTEGER,"20002" TEXT,"64016" INTEGER)')
            c.executemany('INSERT INTO group_member3 VALUES(333,?,?,?,0)', [('u_self', 111, 'Self'), ('u_peer', 222, 'Peer')])
        self.vault.refresh()
        fresh = QQSource(self.vault.current()[0])
        cid = fresh.list_conversations(kinds=['group'])[0]['id']
        first = fresh.list_group_members(cid, limit=1)
        self.assertTrue(first['truncated'])
        self.assertIsNotNone(first['next_cursor'])
        last = fresh.list_group_members(cid, limit=1, cursor=first['next_cursor'])
        self.assertFalse(last['truncated'])
        self.assertIsNone(last['next_cursor'])
        self.assertNotEqual(first['members'][0]['actor_id'], last['members'][0]['actor_id'])
        self.assertFalse(last['complete'])

    def test_attachment_native_path_links_to_immutable_asset(self):
        raw = field(40800, field(45002, 3) + field(45402, 'sample') + field(45403, str(self.media)))
        with sqlite3.connect(self.input / 'nt_msg.db') as c:
            c.execute('UPDATE c2c_msg_table SET "40800"=? WHERE "40001"=1', (raw,))
        self.vault.refresh()
        fresh = QQSource(self.vault.current()[0])
        attachment = fresh.read_items(kinds=['file'])['items'][0]['attachments'][0]
        self.assertEqual(attachment['availability'], 'local_snapshot')
        self.assertEqual(attachment['local_assets'][0]['match'], 'native_path')
        self.assertEqual(base64.b64decode(fresh.read_asset(attachment['local_assets'][0]['asset_id'], length=1048576)['data']), self.media_bytes)

    def test_auditor_reference_projections_for_all_fts_modes(self):
        from collections import Counter
        from tools.audit_coverage import audit_table
        tables = {t['table']: t for t in self.source.list_tables('misc.db')}
        for name in ('internal_fts', 'external_fts', 'contentless_fts'):
            self.assertTrue(audit_table(self.source, 'misc.db', tables[name], Counter())['passed'])

    def test_new_auxiliary_types_are_discovered_without_a_whitelist(self):
        for filename, category in [('collection.db', 'favorites'), ('emoji.db', 'emoticons'), ('settings.db', 'settings'), ('guild_msg.db', 'channels')]:
            with sqlite3.connect(self.input / filename) as c:
                c.execute('CREATE TABLE novel_data(id INTEGER PRIMARY KEY,body BLOB)')
                c.execute('INSERT INTO novel_data VALUES(1,?)', (self.big,))
        self.vault.refresh()
        fresh = QQSource(self.vault.current()[0])
        datasets = fresh.list_datasets()
        for filename, category in [('collection.db', 'favorites'), ('emoji.db', 'emoticons'), ('settings.db', 'settings'), ('guild_msg.db', 'channels')]:
            item = next(d for d in datasets if d['database'] == filename and d['table'] == 'novel_data')
            self.assertEqual(item['category'], category)
            result = fresh.read_dataset(item['dataset_id'])
            self.assertEqual(self.materialize(result['rows'][0]['values']['body'], fresh), self.big)

    def test_optional_missing_key_is_explicit_strict_mode_keeps_snapshot(self):
        # A structurally encrypted auxiliary DB without its key must never be
        # silently counted as decoded, and critical message data stays strict.
        from helpers import encrypt_file
        aux = self.input / 'missing_key.db'
        encrypt_file(self.input / 'nt_msg.db', aux)
        with self.assertRaises(QQError):
            self.vault.refresh()
        self.assertEqual(self.vault.current()[0], self.source.root)
        partial = self.vault.refresh(allow_partial=True)
        self.assertFalse(partial['coverage']['complete'])
        fresh = QQSource(self.vault.current()[0])
        item = next(d for d in fresh.list_databases() if d['database'] == 'missing_key.db')
        self.assertEqual(item['state'], 'unavailable')
        with self.assertRaises(QQError) as e:
            fresh.read_records('missing_key.db', 'c2c_msg_table')
        self.assertEqual(e.exception.code, 'database_unavailable')


class CipherProfileTests(unittest.TestCase):
    def test_independent_cipher_profiles_and_small_databases(self):
        # Derive/encrypt independently of PageCodec; SQLite verifies the result.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for size, algorithm, kdf, reserve in [(1024, 'sha1', 'sha1', 48), (2048, 'sha256', 'sha256', 48),
                                                  (4096, 'sha512', 'sha512', 80)]:
                with self.subTest(size=size, hmac=algorithm, kdf=kdf):
                    plain = root / f'{size}.plain'
                    # Use the real C engine with reserved space and page size.
                    create_reserved_database(plain, reserve, page_size=size)
                    raw = plain.read_bytes()
                    key = hashlib.pbkdf2_hmac(kdf, PASSPHRASE.encode(), SALT, 4000, 32)
                    mac_key = hashlib.pbkdf2_hmac(kdf, key, bytes(x ^ 58 for x in SALT), 2, 32)
                    reserved = ((16 + hashlib.new(algorithm).digest_size + 15) // 16) * 16
                    encrypted = root / f'{size}.encrypted'
                    with encrypted.open('wb') as out:
                        out.write(bytes(1024))
                        for number, offset in enumerate(range(0, len(raw), size), 1):
                            page = raw[offset:offset + size]
                            iv, start = bytes([number % 256]) * 16, 16 if number == 1 else 0
                            enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
                            body = enc.update(page[start:size - reserved]) + enc.finalize()
                            mac = hmac.new(mac_key, body + iv + struct.pack('<I', number), algorithm).digest()
                            out.write((SALT if start else b'') + body + iv + mac + bytes(reserved - 16 - len(mac)))
                    record = DatabaseKey('passphrase', PASSPHRASE.encode())
                    self.assertTrue(verify_key(encrypted, record))
                    output = root / f'{size}.out'
                    details = decrypt_database(encrypted, output, record)
                    self.assertEqual(details['cipher_hmac'], algorithm)
                    with sqlite3.connect(output) as c:
                        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    found = probe_context(lambda p, n: {8192: key + bytes(96)}.get(p, b''),
                        struct.pack('<Q', 8192) + bytes(120), first_page(encrypted), time.monotonic() + 5, set(), set())
                    self.assertEqual(found, key)


if __name__ == '__main__':
    unittest.main()
