"""Exhaustively compare API data with detached SQLite and local cache bytes.

The private report contains counts, hashes and failure locations, never chat
contents or keys. Optional exports are private and contain the real records.
Run with the project's Python: tools/audit_coverage.py [--export-tables].
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import struct
import time
from collections import Counter
from pathlib import Path

from qq_local_interface.errors import QQError
from qq_local_interface.protobuf import wire
from qq_local_interface.source import QQSource
from qq_local_interface.util import atomic_json, utc_now
from qq_local_interface.vault import QQVault


def q(name):
    return '"' + name.replace('"', '""') + '"'


def reference_layout(connection, table):
    """Derive native columns independently of DataBrowser's descriptor."""
    sql = connection.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0] or ''
    if re.search(r'\bUSING\s+fts5\s*\(', sql, re.I):
        # Use a separate SQLite engine to parse the declared FTS columns. QQ's
        # custom tokenizer is irrelevant to the declared content projection.
        compatible = re.sub(r"tokenize\s*=\s*('[^']*'|\"[^\"]*\")", "tokenize='unicode61'", sql, flags=re.I)
        with sqlite3.connect(':memory:') as parser:
            parser.execute(compatible)
            columns = [r[1] for r in parser.execute('PRAGMA table_info(' + q(table) + ')')]
        content = re.search(r"\bcontent\s*=\s*('[^']*'|\"[^\"]*\"|\w+)", sql, re.I)
        if content:
            backing = content[1].strip("'\"")
            if backing:
                return columns, 'SELECT ' + ','.join(q(n) for n in columns) + ' FROM ' + q(backing)
            return columns, 'SELECT ' + ','.join('NULL AS ' + q(c) for c in columns) + ' FROM ' + q(table + '_docsize')
        return columns, 'SELECT ' + ','.join(q('c' + str(i)) + ' AS ' + q(c) for i, c in enumerate(columns)) + ' FROM ' + q(table + '_content')
    columns = [r[1] for r in connection.execute('PRAGMA table_xinfo(' + q(table) + ')')]
    return columns, 'SELECT ' + ','.join(q(n) for n in columns) + ' FROM ' + q(table)


def primitive(value, kind):
    if isinstance(kind, bytes):
        kind = kind.decode()
    if kind == 'null':
        return ['null', None]
    if kind == 'integer':
        return ['integer', str(value)]
    if kind == 'real':
        return ['real', struct.pack('>d', value).hex()]
    return [kind, len(value), hashlib.sha256(value).hexdigest()]


def row_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def api_cell(source, cell, totals):
    kind = cell['type']
    totals['cells'] += 1
    if kind == 'null':
        return ['null', None]
    if kind == 'integer':
        return ['integer', str(int(cell['value']))]
    if kind == 'real':
        return ['real', struct.pack('>d', float.fromhex(cell['value'])).hex()]
    if cell['encoding'] == 'utf8':
        raw = cell['value'].encode('utf-8')
    elif cell['encoding'] == 'base64':
        raw = base64.b64decode(cell['data'], validate=True)
    else:
        totals['referenced_cells'] += 1
        result, offset = bytearray(), 0
        while True:
            chunk = source.read_cell(cell['cell_ref'], offset, 65521)
            part = base64.b64decode(chunk['data'], validate=True)
            if hashlib.sha256(part).hexdigest() != chunk['chunk_sha256']:
                raise ValueError('cell chunk checksum mismatch')
            result.extend(part)
            offset = chunk['next_offset']
            if offset is None:
                break
        raw = bytes(result)
    if len(raw) != cell['length'] or hashlib.sha256(raw).hexdigest() != cell['sha256']:
        raise ValueError('cell byte checksum mismatch')
    return primitive(raw, kind)


def audit_table(source, database, table, totals, export_root=None):
    name = table['table']
    with sqlite3.connect((source.root / 'data' / database).as_uri() + '?immutable=1&mode=ro', uri=True) as connection:
        columns, statement = reference_layout(connection, name)
        # typeof is evaluated against the same independent projection, which
        # avoids JSON precision changes and invalid UTF-8 replacement.
        connection.text_factory = bytes
        sql = 'SELECT *, ' + ','.join('typeof(' + q(c) + ')' for c in columns) + ' FROM (' + statement + ')'
        reference = Counter()
        for row in connection.execute(sql):
            reference[row_hash({n: primitive(row[i], row[i + len(columns)]) for i, n in enumerate(columns)})] += 1
    actual, cursor, ids, count = Counter(), None, set(), 0
    while True:
        page = source.read_records(database, name, limit=73, cursor=cursor)
        for row in page['rows']:
            if list(row['values']) != columns or row['id'] in ids:
                raise ValueError('column set or row identity mismatch')
            ids.add(row['id'])
            actual[row_hash({n: api_cell(source, row['values'][n], totals) for n in columns})] += 1
            count += 1
        cursor = page['next_cursor']
        if cursor is None:
            break
    if actual != reference or count != table['row_count']:
        raise ValueError('native/API row multiset or row count mismatch')
    if export_root:
        exported = source.export_records(database, name, export_root)
        out = Counter()
        with (Path(exported['output']) / 'records.jsonl').open() as stream:
            for line in stream:
                row = json.loads(line)
                out[row_hash({n: api_cell(source, row['values'][n], Counter()) for n in columns})] += 1
        if out != reference or exported['count'] != count:
            raise ValueError('export/native record mismatch')
        totals['exported_tables'] += 1
        totals['exported_rows'] += count
    totals['tables'] += 1
    totals['rows'] += count
    return {'table': name, 'rows': count, 'access_mode': table['access_mode'], 'passed': True}


def audit_assets(source, totals):
    cursor, result, seen = None, [], set()
    while True:
        page = source.list_assets(limit=79, cursor=cursor)
        for item in page['items']:
            if item['id'] in seen:
                raise ValueError('duplicate asset ID')
            seen.add(item['id'])
            expected, actual, offset = hashlib.sha256(), hashlib.sha256(), 0
            try:
                path = source.root / item['snapshot_path'] if item.get('snapshot_path') else Path(item['local_path'])
                with path.open('rb') as stream:
                    while part := stream.read(1048576):
                        expected.update(part)
                while True:
                    chunk = source.read_asset(item['id'], offset, 1048576)
                    part = base64.b64decode(chunk['data'], validate=True)
                    if hashlib.sha256(part).hexdigest() != chunk['chunk_sha256']:
                        raise ValueError('asset chunk checksum mismatch')
                    actual.update(part)
                    totals['asset_bytes'] += len(part)
                    offset = chunk['next_offset']
                    if offset is None:
                        break
                if actual.digest() != expected.digest():
                    raise ValueError('asset byte mismatch')
                totals['assets'] += 1
            except (QQError, OSError, ValueError) as error:
                result.append({'asset_id': item['id'], 'passed': False, 'error': getattr(error, 'code', type(error).__name__)})
        cursor = page['next_cursor']
        if cursor is None:
            break
    if len(seen) != source.manifest['counts']['assets']:
        raise ValueError('asset inventory count mismatch')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--source-id', default='qq-local')
    parser.add_argument('--export-tables', action='store_true')
    args = parser.parse_args()
    vault = QQVault(args.vault, args.source_id) if args.vault else QQVault(source_id=args.source_id)
    source = QQSource(vault.current()[0])
    started, totals, failures, databases = time.monotonic(), Counter(), [], []
    report = {'at': utc_now(), 'snapshot_id': source.snapshot_id, 'coverage': source.status()['coverage']}
    expected_names = {p.name for p in Path(vault.config()['database_dir']).glob('*.db') if p.is_file()}
    selection = source.manifest.get('selection') or {'discovered': sorted(expected_names), 'selected': list(source.manifest['databases']), 'excluded': []}
    if (expected_names != set(selection['discovered']) or set(selection['selected']) != set(source.manifest['databases']) or
            expected_names - set(selection['selected']) != set(selection['excluded'])):
        failures.append({'check': 'source_database_inventory', 'error': 'database_set_mismatch'})
    export_root = vault.home / 'exports' / ('coverage-' + source.snapshot_id) if args.export_tables else None
    for database in source.list_databases():
        record = {**database, 'tables': []}
        if database['state'] == 'ready':
            for table in source.list_tables(database['database']):
                try:
                    if not table['accessible']:
                        raise ValueError('table inaccessible')
                    record['tables'].append(audit_table(source, database['database'], table, totals, export_root))
                except Exception as error:
                    failure = {'database': database['database'], 'table': table['table'], 'error': getattr(error, 'code', type(error).__name__)}
                    failures.append(failure)
                    record['tables'].append({**failure, 'passed': False})
        databases.append(record)
    failures.extend(audit_assets(source, totals))
    types, qualities, cursor, messages = Counter(), Counter(), None, 0
    while True:
        page = source.read_items(limit=73, cursor=cursor)
        for item in page['items']:
            messages += 1
            qualities[item['quality']['status']] += 1
            types.update(e.get('native_type') for e in item['elements'])
            raw = source.get_message(item['id'], include_raw=True)['raw']['rows'][0]['values']
            body = raw.get('40800') or raw.get('40080')
            if body and body['type'] in ('text', 'blob'):
                native = api_cell(source, body, Counter())
                if native[1:] != [item['raw_body']['length'], item['raw_body']['sha256']]:
                    failures.append({'check': 'message_raw_body', 'item_id': item['id'], 'error': 'digest_mismatch'})
        cursor = page['next_cursor']
        if cursor is None:
            break
    if messages != source.manifest['counts']['messages']:
        failures.append({'check': 'normalized_message_count', 'error': 'count_mismatch'})
    report.update(databases=databases, totals=dict(totals), message_count=messages,
        element_types=dict(types), quality=dict(qualities), failures=failures,
        accessible_data_passed=not failures,
        all_requested_databases_accessible=source.status()['coverage']['complete'],
        all_local_databases_accessible=source.status()['coverage']['complete'] and not selection['excluded'],
        database_selection=selection,
        elapsed_seconds=round(time.monotonic() - started, 2),
        limitations=['online_only_history_not_audited', 'uncached_remote_media_not_available', 'empty_tables_are_not_real_type_samples'])
    atomic_json(vault.home / 'coverage-report.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('databases',)}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
