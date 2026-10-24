"""Lossless, paged access to every captured SQLite table and cell.

No user SQL is executed. Integers and real numbers use exact strings. Text and
blobs retain their original bytes, with bounded reads for large cells. Snapshot-
bound row references intentionally are not cross-refresh synchronization tokens.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import tempfile
import uuid
from pathlib import Path

from .errors import QQError
from .util import canonical, cursor_decode, cursor_encode, digest, limit_value, opaque, readonly, regular_file, atomic_json

INLINE_BYTES = 4096
MAX_CHUNK = 1024 * 1024


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def ddl_arguments(value):
    parts, start, depth, delimiter = [], 0, 0, None
    i = 0
    while i < len(value):
        char = value[i]
        if delimiter:
            if char == delimiter:
                if i + 1 < len(value) and value[i + 1] == delimiter:
                    i += 1
                else:
                    delimiter = None
        elif char in "'\"`[":
            delimiter = ']' if char == '[' else char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(value[start:i].strip())
            start = i + 1
        i += 1
    parts.append(value[start:].strip())
    return parts


def unquote(value):
    value = value.strip()
    if value[:1] in ("'", '"', '`') and value[-1:] == value[:1]:
        return value[1:-1].replace(value[:1] * 2, value[:1])
    if value.startswith('[') and value.endswith(']'):
        return value[1:-1]
    return value


def fts_description(sql):
    match = re.search(r"\bUSING\s+fts5\s*\((.*)\)\s*$", sql or '', re.I | re.S)
    if not match:
        return None
    options, columns = {}, []
    for part in ddl_arguments(match.group(1)):
        option = re.match(r"^(\w+)\s*=\s*(.*)$", part, re.S)
        if option:
            options[option[1].lower()] = unquote(option[2])
        else:
            column = re.sub(r"\s+UNINDEXED\s*$", '', part, flags=re.I)
            columns.append(unquote(column))
    return options, columns


def exact_cell(value, kind):
    """Independent of row references: suitable for identity and revision hashes."""
    if kind == 'null':
        return {'type': 'null', 'value': None}
    if kind == 'integer':
        return {'type': 'integer', 'value': str(value)}
    if kind == 'real':
        return {'type': 'real', 'value': float(value).hex()}
    raw = bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else str(value).encode('utf-8')
    result = {'type': kind, 'length': len(raw), 'sha256': digest(raw)}
    return result


def bind_value(value):
    if isinstance(value, dict):
        kind = value.get('type')
        if kind == 'null':
            return None, '?'
        if kind == 'integer':
            try:
                number = int(value['value'])
                if not -(2 ** 63) <= number < 2 ** 63:
                    raise ValueError()
                return number, '?'
            except (ValueError, TypeError, KeyError):
                raise QQError('invalid_argument', 'invalid exact integer filter') from None
        if kind == 'real':
            try:
                return float.fromhex(value['value']), '?'
            except (ValueError, TypeError, KeyError):
                raise QQError('invalid_argument', 'invalid exact real filter') from None
        if kind in ('blob', 'text') and value.get('encoding') == 'base64':
            try:
                data = base64.b64decode(value['data'], validate=True)
                return data, 'CAST(? AS TEXT)' if kind == 'text' else '?'
            except (ValueError, TypeError, KeyError):
                raise QQError('invalid_argument', 'invalid base64 filter') from None
        if kind == 'text' and isinstance(value.get('value'), str):
            return value['value'], '?'
        raise QQError('invalid_argument', 'unsupported typed equality filter')
    if value is None or isinstance(value, str) or type(value) in (int, float):
        if type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
            raise QQError('invalid_argument', 'integer filter is outside SQLite range')
        return value, '?'
    raise QQError('invalid_argument', 'equality values must be scalars or exact typed cells')


class DataBrowser:
    def __init__(self, source):
        self.source = source
        self.root = source.root / 'data'
        self._stats = {}

    def _path(self, database):
        self.source._unchanged()
        if not isinstance(database, str) or database not in self.source.manifest['databases']:
            raise QQError('not_found', 'database is not part of this snapshot')
        if Path(database).name != database or database in ('.', '..'):
            raise QQError('invalid_path', 'invalid snapshot database name')
        state = self.source.manifest['databases'][database]
        if state.get('state') == 'unavailable':
            raise QQError('database_unavailable', 'database is unavailable in this snapshot',
                          {'database': database, 'reason': state.get('reason')})
        path = regular_file(self.root / database)
        if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise QQError('invalid_snapshot', 'raw snapshot has an active SQLite sidecar')
        info = path.stat()
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if database in self._stats and self._stats[database] != signature:
            raise QQError('source_changed', 'raw snapshot database changed')
        self._stats[database] = signature
        return path

    def list_databases(self):
        self.source._unchanged()
        result = []
        for name, info in sorted(self.source.manifest['databases'].items()):
            state = info.get('state', 'ready')
            if state == 'unavailable':
                result.append({'database': name, 'state': state, 'reason': info.get('reason'),
                    'size_bytes': info.get('source_size_bytes'), 'table_count': None, 'snapshot_id': self.source.snapshot_id})
                continue
            path = self._path(name)
            entry = {'database': name, 'state': state, 'size_bytes': path.stat().st_size,
                     'snapshot_id': self.source.snapshot_id}
            if state == 'ready':
                with readonly(path) as connection:
                    entry['table_count'] = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            else:
                entry['table_count'] = 0
            result.append(entry)
        return result

    def _describe(self, connection, table):
        if not isinstance(table, str):
            raise QQError('invalid_argument', 'table must be a string')
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            raise QQError('not_found', 'table is not part of this snapshot database')
        sql = row[0] or ''
        target, logical, mode = table, None, 'table'
        parsed = fts_description(sql)
        if parsed:
            options, logical = parsed
            if options.get('content'):
                target, mode = options['content'], 'external_content_proxy'
            elif 'content' not in options:
                target, mode = table + '_content', 'internal_content_proxy'
            else:
                target, mode = table + '_docsize', 'contentless_index_proxy'
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (target,)).fetchone():
            raise QQError('unsupported_table', 'virtual table backing content is absent')
        try:
            columns = connection.execute('PRAGMA table_xinfo(' + quote(target) + ')').fetchall()
        except Exception:
            raise QQError('unsupported_table', 'table requires an unavailable QQ SQLite extension') from None
        physical = [row[1] for row in columns]
        if mode == 'contentless_index_proxy':
            # FTS5 contentless reads have real rowids and NULL logical values.
            # The document-size shadow table retains exactly those rowids.
            alias = '__qq_contentless_rowid'
            while alias in logical:
                alias += '_'
            projection = 'id AS ' + quote(alias) + ''.join(',NULL AS ' + quote(n) for n in logical)
            return {'table': table, 'backing_table': target, 'access_mode': mode, 'schema_sql': sql,
                'columns': [{'name': n, 'storage_column': None, 'declared_type': ''} for n in logical],
                '_mapping': {n: n for n in logical}, '_order': [alias], '_rowid': alias, '_pk': [],
                '_from': '(SELECT ' + projection + ' FROM ' + quote(target) + ')'}
        if logical is None:
            names = physical
            mapping = {name: name for name in names}
        elif mode == 'internal_content_proxy':
            names = logical
            mapping = {name: f'c{i}' for i, name in enumerate(names)}
        else:
            names = logical
            mapping = {name: name for name in names}
        # SQLite folds ASCII identifier case, including hidden rowid aliases.
        fold = lambda s: s.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
        physical_by_name = {fold(n): n for n in physical}
        mapping = {name: physical_by_name.get(fold(actual), actual) for name, actual in mapping.items()}
        if any(name not in physical for name in mapping.values()):
            raise QQError('unsupported_table', 'virtual table content mapping is incompatible')
        rowid = next((name for name in ('rowid', '_rowid_', 'oid') if name not in physical_by_name), None)
        if rowid:
            try:
                connection.execute(f'SELECT {rowid} FROM {quote(target)} LIMIT 0')
            except Exception:
                rowid = None
        pk = [r[1] for r in sorted(columns, key=lambda c: c[5]) if r[5]]
        order = [rowid] if rowid else pk or physical
        return {'table': table, 'backing_table': target, 'access_mode': mode, 'schema_sql': sql,
                'columns': [{'name': n, 'storage_column': mapping[n],
                             'declared_type': next(r[2] for r in columns if r[1] == mapping[n])} for n in names],
                '_mapping': mapping, '_order': order, '_rowid': rowid, '_pk': pk}

    def list_tables(self, database):
        path = self._path(database)
        if self.source.manifest['databases'][database].get('state') == 'uninitialized':
            return []
        result = []
        with readonly(path) as connection:
            names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for name in names:
                try:
                    description = self._describe(connection, name)
                    count = connection.execute('SELECT count(*) FROM ' + quote(description['backing_table'])).fetchone()[0]
                    result.append({k: v for k, v in description.items() if not k.startswith('_')} | {'row_count': count, 'accessible': True})
                except QQError as error:
                    result.append({'table': name, 'accessible': False, 'row_count': None,
                                   'reason': error.code, 'message': error.message})
        self._path(database)
        return result

    def list_datasets(self, category=None, include_internal=False):
        if category is not None and not isinstance(category, str):
            raise QQError('invalid_argument', 'category must be a string')
        if not isinstance(include_internal, bool):
            raise QQError('invalid_argument', 'include_internal must be boolean')
        domains = {'profile_info.db': 'contacts', 'group_info.db': 'groups',
            'collection.db': 'favorites', 'emoji.db': 'emoticons', 'rich_media.db': 'resources',
            'files_in_chat.db': 'resources', 'file_assistant.db': 'resources',
            'nt_flash_transfer.db': 'resources', 'guild1.db': 'channels', 'guild_msg.db': 'channels',
            'settings.db': 'settings', 'misc.db': 'misc'}
        result = []
        for database in self.list_databases():
            name = database['database']
            domain = domains.get(name, 'search_indexes' if '_fts' in name else 'messages' if name == 'nt_msg.db' else 'storage')
            if category and domain != category:
                continue
            if database['state'] == 'unavailable':
                result.append({'dataset_id': opaque(self.source.source_id, 'dataset', name), 'category': domain,
                               **database, 'accessible': False, 'internal': False})
                continue
            for table in self.list_tables(name):
                internal = '_fts' in table['table'] or table['table'].startswith('sqlite_')
                if internal and not include_internal:
                    continue
                result.append({'dataset_id': opaque(self.source.source_id, 'dataset', name + ':' + table['table']),
                    'category': domain, 'database': name, **table, 'internal': internal})
        return result

    def read_dataset(self, dataset_id, **params):
        if not isinstance(dataset_id, str):
            raise QQError('invalid_argument', 'dataset_id must be a string')
        for dataset in self.list_datasets(include_internal=True):
            if dataset['dataset_id'] == dataset_id:
                if dataset.get('state') == 'unavailable':
                    self._path(dataset['database'])
                return self.read_records(dataset['database'], dataset['table'], **params)
        raise QQError('not_found', 'dataset does not exist in this source')

    @staticmethod
    def _where(description, equals):
        if equals is None:
            return '1', []
        if not isinstance(equals, dict) or set(equals) - description['_mapping'].keys():
            raise QQError('invalid_argument', 'equals must map existing columns to exact values')
        expressions, params = [], []
        for name, value in sorted(equals.items()):
            bound, expression = bind_value(value)
            expressions.append(quote(description['_mapping'][name]) + ' IS ' + expression)
            params.append(bound)
        return ' AND '.join(expressions) or '1', params

    def _row_ref(self, database, table, locator):
        return cursor_encode({'kind': 'raw_row', 'source_id': self.source.source_id,
            'snapshot_id': self.source.snapshot_id, 'database': database, 'table': table,
            'locator': locator})

    def _encode(self, value, kind, row_ref, column):
        result = exact_cell(value, kind)
        if kind not in ('text', 'blob'):
            return result
        raw = bytes(value) if isinstance(value, bytes) else str(value).encode('utf-8')
        if len(raw) <= INLINE_BYTES:
            if kind == 'text':
                try:
                    return {**result, 'encoding': 'utf8', 'value': raw.decode('utf-8')}
                except UnicodeDecodeError:
                    pass
            return {**result, 'encoding': 'base64', 'data': base64.b64encode(raw).decode()}
        return {**result, 'encoding': 'reference', 'cell_ref': cursor_encode({
            'kind': 'raw_cell', 'row_ref': row_ref, 'column': column,
            'sha256': result['sha256'], 'length': len(raw)})}

    def read_records(self, database, table, *, equals=None, limit=100, cursor=None):
        limit_value(limit)
        path = self._path(database)
        if self.source.manifest['databases'][database].get('state') == 'uninitialized':
            raise QQError('database_uninitialized', 'QQ has not created SQLite data pages for this database')
        offset = 0
        filter_hash = digest(equals)
        if cursor:
            token = cursor_decode(cursor)
            expected = {'kind': 'raw_records', 'source_id': self.source.source_id,
                        'snapshot_id': self.source.snapshot_id, 'database': database, 'table': table, 'filters': filter_hash}
            if any(token.get(k) != v for k, v in expected.items()) or type(token.get('offset')) is not int or token['offset'] < 0:
                raise QQError('invalid_cursor', 'record cursor belongs to another source, snapshot, table or filter')
            offset = token['offset']
        with readonly(path) as connection:
            description = self._describe(connection, table)
            where, params = self._where(description, equals)
            names = list(description['_mapping'])
            actual = list(description['_mapping'].values())
            selections = [quote(n) for n in actual] + ['typeof(' + quote(n) + ')' for n in actual]
            identity = description['_rowid']
            key_columns = description['_pk'] or list(description['_mapping'].values())
            selections += [identity] if identity else [quote(n) for n in key_columns] + ['typeof(' + quote(n) + ')' for n in key_columns]
            order = ','.join(quote(n) for n in description['_order'])
            connection.text_factory = bytes
            records = connection.execute('SELECT ' + ','.join(selections) + ' FROM ' + description.get('_from', quote(description['backing_table'])) +
                ' WHERE ' + where + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', (*params, limit + 1, offset)).fetchall()
            result = []
            n = len(names)
            for position, row in enumerate(records[:limit], offset):
                if identity:
                    locator = {'rowid': str(row[2 * n])}
                    stable = [locator['rowid']]
                else:
                    key_count = len(key_columns)
                    key_values = [exact_cell(row[2 * n + j], row[2 * n + key_count + j].decode()) for j in range(key_count)]
                    locator = {'key_hash': digest(key_values), 'columns': key_columns}
                    stable = [locator['key_hash']]
                    if not description['_pk']:
                        stable.append(position)
                ref = self._row_ref(database, table, locator)
                values = {name: self._encode(row[i], row[i + n].decode(), ref, name) for i, name in enumerate(names)}
                canonical_values = {name: exact_cell(row[i], row[i + n].decode()) for i, name in enumerate(names)}
                result.append({'id': opaque(self.source.source_id, 'record', canonical([database, table, stable])),
                    'row_ref': ref, 'values': values, 'revision': digest(canonical_values)})
        self._path(database)
        next_cursor = None
        if len(records) > limit:
            next_cursor = cursor_encode({'kind': 'raw_records', 'source_id': self.source.source_id,
                'snapshot_id': self.source.snapshot_id, 'database': database, 'table': table,
                'filters': filter_hash, 'offset': offset + limit})
        return {'database': database, 'table': table, 'access_mode': description['access_mode'],
            'snapshot_id': self.source.snapshot_id, 'rows': result, 'next_cursor': next_cursor}

    def read_cell(self, cell_ref, offset=0, length=65536):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_CHUNK:
            raise QQError('invalid_argument', 'cell offset must be nonnegative and length 1–1048576')
        token = cursor_decode(cell_ref)
        if token.get('kind') != 'raw_cell' or not isinstance(token.get('row_ref'), str):
            raise QQError('invalid_cursor', 'invalid cell reference')
        row = cursor_decode(token['row_ref'])
        if (row.get('kind') != 'raw_row' or row.get('source_id') != self.source.source_id or
                row.get('snapshot_id') != self.source.snapshot_id or not isinstance(row.get('locator'), dict)):
            raise QQError('invalid_cursor', 'cell reference belongs to another source or snapshot')
        database, table, column = row.get('database'), row.get('table'), token.get('column')
        path = self._path(database)
        with readonly(path) as connection:
            description = self._describe(connection, table)
            if column not in description['_mapping']:
                raise QQError('invalid_argument', 'cell column does not exist')
            locator = row['locator']
            if description['_rowid'] and set(locator) == {'rowid'}:
                try:
                    rowid = int(locator['rowid'])
                    if not -(2 ** 63) <= rowid < 2 ** 63:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise QQError('invalid_cursor', 'invalid row identity') from None
                where, params = description['_rowid'] + '=?', [rowid]
            else:
                expected_columns = description['_pk'] or list(description['_mapping'].values())
                if locator.get('columns') != expected_columns or not isinstance(locator.get('key_hash'), str):
                    raise QQError('invalid_cursor', 'invalid composite row identity')
                def row_hash(*args):
                    return digest([exact_cell(args[i + 1], args[i]) for i in range(0, len(args), 2)])
                connection.create_function('qq_exact_row_hash', -1, row_hash, deterministic=True)
                expressions = []
                for name in expected_columns:
                    q = quote(name)
                    expressions.extend(['typeof(' + q + ')', f"CASE WHEN typeof({q}) IN ('text','blob') THEN CAST({q} AS BLOB) ELSE {q} END"])
                where, params = 'qq_exact_row_hash(' + ','.join(expressions) + ')=?', [locator['key_hash']]
            col = quote(description['_mapping'][column])
            connection.text_factory = bytes
            target = description.get('_from', quote(description['backing_table']))
            query = f'SELECT typeof({col}),length(CAST({col} AS BLOB)),substr(CAST({col} AS BLOB),?,?) FROM {target} WHERE {where}'
            query += ' LIMIT 1'
            found = connection.execute(query, (offset + 1, length, *params)).fetchone()
            if found is None:
                raise QQError('not_found', 'referenced row no longer exists')
            if found[0] not in (b'text', b'blob') or found[1] != token.get('length'):
                raise QQError('source_changed', 'referenced cell type or length changed')
            if offset > found[1]:
                raise QQError('invalid_argument', 'offset exceeds cell size')
            data = found[2]
        self._path(database)
        return {'encoding': 'base64', 'data': base64.b64encode(data).decode(), 'offset': offset,
                'length': len(data), 'total_bytes': found[1], 'sha256': token.get('sha256'),
                'chunk_sha256': digest(data), 'next_offset': offset + len(data) if offset + len(data) < found[1] else None}

    def materialize_record(self, record):
        for cell in record['values'].values():
            if cell.get('encoding') != 'reference':
                continue
            chunks, position = [], 0
            while True:
                chunk = self.read_cell(cell['cell_ref'], position, MAX_CHUNK)
                chunks.append(base64.b64decode(chunk['data']))
                position = chunk['next_offset']
                if position is None:
                    break
            raw = b''.join(chunks)
            if digest(raw) != cell['sha256']:
                raise QQError('integrity_failed', 'exported cell digest differs from its snapshot')
            cell.pop('cell_ref')
            cell.update(encoding='base64', data=base64.b64encode(raw).decode())
        return record

    def export_records(self, database, table, output, equals=None):
        root = Path(output).expanduser().absolute()
        if root.is_symlink() or root == self.source.root or self.source.root in root.parents or root in self.source.root.parents:
            raise QQError('invalid_path', 'raw export must not overlap its snapshot or use a symbolic link')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = root / ('qq-records-' + uuid.uuid4().hex[:16])
        count = 0
        with tempfile.TemporaryDirectory(prefix='.export-', dir=root) as temporary:
            work = Path(temporary)
            fd = os.open(work / 'records.jsonl', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                cursor = None
                while True:
                    page = self.read_records(database, table, equals=equals, limit=500, cursor=cursor)
                    for record in page['rows']:
                        # Export is self-contained: materialize referenced cells.
                        self.materialize_record(record)
                        stream.write(canonical(record) + '\n')
                        count += 1
                    cursor = page['next_cursor']
                    if cursor is None:
                        break
            manifest = {'source_id': self.source.source_id, 'snapshot_id': self.source.snapshot_id,
                        'database': database, 'table': table, 'count': count, 'lossless': True,
                        'privacy': {'visibility': 'private', 'trust': 'untrusted'}}
            atomic_json(work / 'manifest.json', manifest)
            os.rename(work, destination)
        return {**manifest, 'output': str(destination)}
