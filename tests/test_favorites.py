"""Realistic collection field envelopes, detail coverage and load-on-open WAL."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import favorite_fixture, fixture, field, encrypt_file
from qq_local_interface.errors import QQError
from qq_local_interface.favorites import body_content, envelope, normalize, summary
from qq_local_interface.protobuf import wire_fields
from qq_local_interface.source import QQSource
from qq_local_interface.vault import QQVault


class FavoriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / 'account' / 'nt_db'
        fixture(self.input)
        favorite_fixture(self.input)
        self.vault = QQVault(self.root / 'vault', 'test-qq')
        self.vault.configure(self.input, account_uin='111', account_uid='u_self')
        self.vault.refresh()
        self.source = QQSource(self.vault.current()[0])

    def tearDown(self):
        self.temp.cleanup()

    def test_summary_type_category_and_millisecond_time_are_distinct(self):
        items = self.source.read_favorites()['items']
        self.assertEqual(len(items), 3)
        first = next(i for i in items if i['native_id'] == 'fixture-cid-0')
        self.assertEqual(first['kind'], 'rich_media')
        self.assertEqual(first['native_type'], '8')
        self.assertEqual(first['native_category'], '2')
        self.assertEqual(first['collected_at'], '2023-11-14T22:13:20.123000Z')
        self.assertEqual(first['author']['qq_number'], '222')
        self.assertEqual(first['author']['display_name'], 'Fixture Author')
        self.assertEqual(first['detail']['content']['text'], 'Complete body 0')
        self.assertNotIn('hidden code', first['detail']['content']['text'])
        missing = next(i for i in items if i['native_id'] == 'fixture-cid-1')
        self.assertEqual(missing['detail']['state'], 'not_cached')
        self.assertIsNone(missing['detail']['content'])
        self.assertEqual(missing['summary_text'], 'Straße preview 1')

    def test_all_collection_union_types_and_unknown_preserve_metadata(self):
        import struct
        cases = [(1, 180650, field(180750, 'plain'), 'text'),
                 (2, 180651, field(180850, 'https://example.com') + field(180851, 'link'), 'link'),
                 (3, 180652, field(180950, field(180553, 'image.png')), 'gallery'),
                 (4, 180653, field(181050, 1200) + field(181051, 'transcript'), 'audio'),
                 (5, 180654, field(181150, 'video') + field(181151, 5), 'video'),
                 (6, 180655, field(181250, field(180604, 'file.zip')), 'file'),
                 (7, 180656, field(181350, 'place') + field(181354, 'address'), 'location'),
                 (8, 180657, field(181450, 'rich') + field(181452, 'brief'), 'rich_media')]
        for native, union, body, expected in cases:
            row = {'180001': str(native), '180002': native, '180008': 99,
                   '180015': field(180015, field(union, body))}
            result = normalize(row, 'synthetic')
            self.assertEqual(result['kind'], expected)
            self.assertEqual(result['native_category'], '99')
            self.assertTrue(result['native_fields']['summary'])
        unknown = normalize({'180001': 'unknown', '180002': 999, '180015': field(180015, field(188888, b'new'))}, 'synthetic')
        self.assertEqual(unknown['kind'], 'unknown')
        self.assertTrue(unknown['native_fields']['summary'])

    def test_padding_parser_does_not_strip_legal_empty_tail_fields(self):
        raw = field(180015, field(180657, field(181450, 'title') + field(181451, b'') + bytes(3)))
        fields = envelope(raw, 180015)
        rich = dict((n, v) for n, _, v in wire_fields(fields[180657][0], allow_padding=True))
        self.assertIn(181451, rich)
        self.assertEqual(rich[181451], b'')
        with self.assertRaises(ValueError):
            wire_fields(fields[180657][0])

    def test_pagination_query_and_snapshot_bound_cursor(self):
        first = self.source.read_favorites(limit=1)
        rest = self.source.read_favorites(limit=2, cursor=first['next_cursor'])
        self.assertEqual(len({i['id'] for i in first['items'] + rest['items']}), 3)
        self.assertIsNone(rest['next_cursor'])
        self.assertEqual(len(self.source.read_favorites(query='STRASSE')['items']), 3)
        with self.assertRaises(QQError):
            self.source.read_favorites(kinds=['text'], cursor=first['next_cursor'])
        self.vault.refresh('full')
        with self.assertRaises(QQError):
            QQSource(self.vault.current()[0]).read_favorites(cursor=first['next_cursor'])

    def test_export_details_coverage_exact_native_rows_and_strict_failure(self):
        export = self.source.export_favorites(self.root / 'out')
        self.assertEqual(export['count'], 3)
        self.assertEqual(export['cached_details'], 1)
        self.assertFalse(export['details_complete_for_selection'])
        path = Path(export['output'])
        items = [json.loads(l) for l in (path / 'favorites.jsonl').read_text().splitlines()]
        native = [json.loads(l) for l in (path / 'native.jsonl').read_text().splitlines()]
        self.assertEqual(len(items), 3)
        self.assertEqual(len(native), 4)
        self.assertEqual((path / 'favorites.jsonl').stat().st_mode & 0o777, 0o600)
        first = next(i for i in items if i['native_id'] == 'fixture-cid-0')
        self.assertEqual(first['detail']['content']['raw_data'], '<p>Complete body 0</p><script>hidden code</script>')
        with self.assertRaises(QQError) as e:
            self.source.export_favorites(self.root / 'strict', require_details=True)
        self.assertEqual(e.exception.code, 'incomplete_data')
        self.assertFalse((self.root / 'strict').exists())
        complete = self.source.export_favorites(self.root / 'complete', query='Complete body', require_details=True)
        self.assertEqual(complete['count'], 1)
        self.assertTrue(complete['details_complete_for_selection'])

    def test_detail_staleness_and_updates_emit_favorite_change(self):
        cursor = self.source.status()['change_cursor']
        with sqlite3.connect(self.input / 'collection.db') as c:
            c.execute('UPDATE collection_list_info_table SET "180011"="180011"+1 WHERE "180001"=?', ('fixture-cid-0',))
        self.vault.refresh()
        fresh = QQSource(self.vault.current()[0])
        item = next(i for i in fresh.read_favorites()['items'] if i['native_id'] == 'fixture-cid-0')
        self.assertEqual(item['detail']['state'], 'stale')
        events = [e for e in fresh.changes(cursor=cursor)['changes'] if e['entity_type'] == 'favorites']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['op'], 'updated')
        raw = fresh.get_favorite(item['id'], include_raw=True)
        self.assertEqual(len(raw['raw']['rows']), 1)
        self.assertEqual(len(raw['detail_raw']['rows']), 1)

    def test_opening_list_writes_only_wal_and_refresh_discovers_it(self):
        c = sqlite3.connect(self.input / 'collection.db')
        try:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA wal_autocheckpoint=0')
            c.execute('INSERT INTO collection_list_info_table SELECT ?,"180002","180004","180008","180009","180010","180011","180012","180015" FROM collection_list_info_table LIMIT 1', ('new-from-wal',))
            c.commit()
            self.assertGreater(Path(str(self.input / 'collection.db') + '-wal').stat().st_size, 0)
            self.vault.refresh()
            self.assertEqual(len(QQSource(self.vault.current()[0]).read_favorites()['items']), 4)
        finally:
            c.close()

    def test_channel_exclusion_is_scoped_and_reversible(self):
        encrypted = self.input / 'guild1.db'
        encrypt_file(self.input / 'nt_msg.db', encrypted)
        self.vault.configure(self.input, account_uin='111', account_uid='u_self', exclude_databases=['guild1.db'])
        result = self.vault.refresh()
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['excluded'], ['guild1.db'])
        source = QQSource(self.vault.current()[0])
        self.assertNotIn('guild1.db', {d['database'] for d in source.list_databases()})
        self.vault.configure(self.input, account_uin='111', exclude_databases=[])
        with self.assertRaises(QQError):
            self.vault.refresh()
        self.assertEqual(self.vault.current()[0], source.root)


if __name__ == '__main__':
    unittest.main()
