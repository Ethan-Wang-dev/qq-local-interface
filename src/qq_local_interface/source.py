"""Read-only normalized snapshot API, with stable IDs and explicit cursors."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from . import SCHEMA_VERSION
from .errors import QQError
from .data import DataBrowser
from .util import atomic_json, canonical, cursor_decode, cursor_encode, digest, limit_value, load_json, private_dir, readonly, regular_file, time_bound


class QQSource:
    def __init__(self, snapshot, source_id=None):
        root = Path(snapshot).expanduser().absolute()
        if root.is_symlink() or not root.is_dir():
            raise QQError("invalid_snapshot", "snapshot must be a detached generation directory")
        self.manifest = load_json(root / "manifest.json")
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            raise QQError("unsupported_schema", "unsupported QQ snapshot schema")
        self.source_id = self.manifest["source_id"]
        if source_id is not None and source_id != self.source_id:
            raise QQError("source_mismatch", "source_id does not match snapshot")
        self.root = root
        self.database = regular_file(root / "records.db")
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(self.database) + suffix).exists():
                raise QQError("invalid_snapshot", "snapshot has an active SQLite sidecar")
        self.snapshot_id = self.manifest["generation"]
        self.origin = self.manifest["origin"]
        self._signature = self._file_signature()
        self.data = DataBrowser(self)

    def _file_signature(self):
        info = self.database.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _unchanged(self):
        if self._file_signature() != self._signature:
            raise QQError("source_changed", "snapshot was modified; reopen a stable generation")

    def _query(self, sql, params=()):
        self._unchanged()
        with readonly(self.database) as connection:
            result = connection.execute(sql, params).fetchall()
        self._unchanged()
        return result

    def status(self):
        self._unchanged()
        position = self._query("SELECT coalesce(max(seq),0) FROM changes")[0][0]
        change_cursor = cursor_encode({"kind": "changes", "source_id": self.source_id,
            "origin": self.origin, "filters": digest(None), "after": position})
        return {"schema_version": SCHEMA_VERSION, "source_id": self.source_id,
                "source_kind": "qq_mac_decrypted_vault", "snapshot_id": self.snapshot_id,
                "captured_at": self.manifest["captured_at"], "counts": self.manifest["counts"],
                "coverage": self.manifest.get('coverage', {'complete': False, 'reason': 'legacy_snapshot_requires_refresh'}),
                "change_cursor": change_cursor,
                "capabilities": ["contacts", "conversations", "messages", "search", "group_members", "changes", "export",
                                 "databases", "tables", "datasets", "lossless_records", "binary_cells", "local_assets", 'favorites'],
                "limitations": ["group_membership_may_be_incomplete", "media_content_requires_local_cache", "unsupported_elements_have_raw_access",
                                "snapshot_reads_do_not_refresh_qq", "no_message_sending", "no_source_database_writes"]}

    def list_contacts(self, query=None, limit=1000):
        limit_value(limit)
        if query is not None and not isinstance(query, str):
            raise QQError("invalid_argument", "query must be a string")
        items = [json.loads(row[0]) for row in self._query("SELECT payload FROM actors ORDER BY coalesce(name,''),id")]
        if query:
            needle = query.casefold()
            items = [i for i in items if any(needle in str(i.get(k) or "").casefold() for k in ("display_name", "qq_number", "uid"))]
        return items[:limit]

    def list_conversations(self, query=None, kinds=None, limit=1000):
        limit_value(limit)
        if kinds is not None and (not isinstance(kinds, list) or any(k not in ("person", "group") for k in kinds)):
            raise QQError("invalid_argument", "conversation kinds must be person or group")
        if query is not None and not isinstance(query, str):
            raise QQError("invalid_argument", "query must be a string")
        items = [json.loads(row[0]) for row in self._query("SELECT payload FROM conversations ORDER BY kind,coalesce(name,''),id")]
        if kinds is not None:
            items = [i for i in items if i["kind"] in kinds]
        if query:
            items = [i for i in items if query.casefold() in str(i.get("display_name") or i["native_id"]).casefold()]
        return items[:limit]

    def _entities_page(self, table, *, query=None, kinds=None, limit=100, cursor=None):
        limit_value(limit)
        if query is not None and not isinstance(query, str):
            raise QQError('invalid_argument', 'query must be a string')
        if kinds is not None and (table != 'conversations' or not isinstance(kinds, list) or not kinds or any(k not in ('person', 'group') for k in kinds)):
            raise QQError('invalid_argument', 'invalid conversation kinds')
        filters = digest({'query': query, 'kinds': kinds})
        where, params = ['1'], []
        if cursor:
            token = cursor_decode(cursor)
            expected = {'kind': table, 'source_id': self.source_id, 'snapshot_id': self.snapshot_id, 'filters': filters}
            if any(token.get(k) != v for k, v in expected.items()) or not isinstance(token.get('after'), str):
                raise QQError('invalid_cursor', 'entity cursor belongs to another source, snapshot or filter')
            where.append('id>?')
            params.append(token['after'])
        if query:
            fields = ["coalesce(name,'')"] + (["json_extract(payload,'$.uid')", "json_extract(payload,'$.qq_number')"] if table == 'actors' else ["json_extract(payload,'$.native_id')"])
            where.append('(' + ' OR '.join('instr(qq_casefold(coalesce(' + f + ",'')),qq_casefold(?))>0" for f in fields) + ')')
            params.extend([query] * len(fields))
        if kinds:
            where.append('kind IN (' + ','.join('?' for _ in kinds) + ')')
            params.extend(kinds)
        rows = self._query('SELECT id,payload FROM ' + table + ' WHERE ' + ' AND '.join(where) + ' ORDER BY id LIMIT ?', (*params, limit + 1))
        next_cursor = cursor_encode({'kind': table, 'source_id': self.source_id, 'snapshot_id': self.snapshot_id,
            'filters': filters, 'after': rows[limit - 1][0]}) if len(rows) > limit else None
        return {'items': [json.loads(r[1]) for r in rows[:limit]], 'next_cursor': next_cursor, 'snapshot_id': self.snapshot_id}

    def read_contacts(self, **params):
        return self._entities_page('actors', **params)

    def read_conversations(self, **params):
        return self._entities_page('conversations', **params)

    def list_group_members(self, conversation_id, limit=1000, cursor=None):
        limit_value(limit)
        if not isinstance(conversation_id, str):
            raise QQError("invalid_argument", "conversation_id must be a string")
        match = self._query("SELECT kind FROM conversations WHERE id=?", (conversation_id,))
        if not match or match[0][0] != "group":
            raise QQError("not_found", "group conversation not found")
        after = ''
        if cursor:
            token = cursor_decode(cursor)
            expected = {'kind': 'members', 'source_id': self.source_id, 'snapshot_id': self.snapshot_id, 'conversation_id': conversation_id}
            if any(token.get(k) != v for k, v in expected.items()) or not isinstance(token.get('after'), str):
                raise QQError('invalid_cursor', 'member cursor belongs to another source, snapshot or group')
            after = token['after']
        rows = self._query("SELECT m.payload,a.payload,m.actor_id FROM memberships m JOIN actors a ON a.id=m.actor_id WHERE conversation_id=? AND m.actor_id>? ORDER BY m.actor_id LIMIT ?",
                           (conversation_id, after, limit + 1))
        next_cursor = cursor_encode({'kind': 'members', 'source_id': self.source_id, 'snapshot_id': self.snapshot_id,
            'conversation_id': conversation_id, 'after': rows[limit - 1][2]}) if len(rows) > limit else None
        return {"conversation_id": conversation_id,
                "members": [{**json.loads(r[0]), "actor": json.loads(r[1])} for r in rows[:limit]],
                "complete": False, "truncated": len(rows) > limit,
                "next_cursor": next_cursor, "snapshot_id": self.snapshot_id,
                "reason": "local_index_and_observed_messages_are_not_an_authoritative_member_roster"}

    @staticmethod
    def _string_list(value, name, allow_empty=False):
        if value is not None and (not isinstance(value, list) or any(not isinstance(i, str) or not i for i in value) or (not value and not allow_empty)):
            raise QQError("invalid_argument", name + " must be a non-empty string array")

    def _message_filter(self, conversation_ids=None, start=None, end=None, author_ids=None, kinds=None, directions=None, qualities=None, query=None):
        values = {"conversation_ids": conversation_ids, "author_ids": author_ids, "kinds": kinds,
                  "directions": directions, "qualities": qualities}
        for name, value in values.items():
            self._string_list(value, name)
        for name, allowed in (("directions", {"incoming", "outgoing", "unknown"}),
                              ("qualities", {"complete", "metadata_only", "unsupported", "decode_error"})):
            if values[name] and set(values[name]) - allowed:
                raise QQError("invalid_argument", "invalid " + name)
        if query is not None and not isinstance(query, str):
            raise QQError("invalid_argument", "query must be a string")
        start, end = time_bound(start), time_bound(end)
        if start and end and start >= end:
            raise QQError("invalid_argument", "start must precede end")
        conditions, params = [], []
        for key, column in (("conversation_ids", "conversation_id"), ("author_ids", "author_id"),
                            ("kinds", "kind"), ("directions", "direction"), ("qualities", "quality")):
            if values[key] is not None:
                conditions.append(column + " IN (" + ",".join("?" for _ in values[key]) + ")")
                params.extend(values[key])
        if start:
            conditions.append("created_at>=?")
            params.append(start)
        if end:
            conditions.append("created_at<?")
            params.append(end)
        if query:
            conditions.append("instr(qq_casefold(text),qq_casefold(?))>0")
            params.append(query)
        if conversation_ids:
            known = {r[0] for r in self._query("SELECT id FROM conversations")}
            if set(conversation_ids) - known:
                raise QQError("not_found", "a requested conversation does not exist")
        normalized = {**values, "start": start, "end": end, "query": query or None}
        return " AND ".join(conditions) or "1", params, digest(normalized)

    def read_items(self, conversation_ids=None, *, start=None, end=None, author_ids=None, kinds=None,
                   directions=None, qualities=None, query=None, limit=100, cursor=None):
        limit_value(limit)
        where, params, filters = self._message_filter(conversation_ids, start, end, author_ids, kinds, directions, qualities, query)
        if cursor:
            decoded = cursor_decode(cursor)
            expected = {"kind": "messages", "source_id": self.source_id, "snapshot_id": self.snapshot_id, "filters": filters}
            if any(decoded.get(k) != v for k, v in expected.items()) or not isinstance(decoded.get("after"), list) or len(decoded["after"]) != 2:
                raise QQError("invalid_cursor", "cursor belongs to another source, snapshot or filter")
            where += " AND (coalesce(created_at,''),id)>(?,?)"
            params.extend(decoded["after"])
        rows = self._query("SELECT payload,coalesce(created_at,''),id FROM messages WHERE " + where + " ORDER BY coalesce(created_at,''),id LIMIT ?", (*params, limit + 1))
        next_cursor = None
        if len(rows) > limit:
            row = rows[limit - 1]
            next_cursor = cursor_encode({"kind": "messages", "source_id": self.source_id, "snapshot_id": self.snapshot_id,
                                         "filters": filters, "after": [row[1], row[2]]})
        return {"items": [json.loads(row[0]) for row in rows[:limit]], "next_cursor": next_cursor,
                "source_id": self.source_id, "snapshot_id": self.snapshot_id}

    def search_items(self, query, **filters):
        if not isinstance(query, str) or not query.strip():
            raise QQError("invalid_argument", "search query must be non-empty")
        return self.read_items(query=query, **filters)

    def list_databases(self):
        return self.data.list_databases()

    def list_datasets(self, category=None, include_internal=False):
        return self.data.list_datasets(category, include_internal)

    def read_dataset(self, dataset_id, **params):
        return self.data.read_dataset(dataset_id, **params)

    def list_tables(self, database):
        return self.data.list_tables(database)

    def read_records(self, database, table, **params):
        return self.data.read_records(database, table, **params)

    def read_cell(self, cell_ref, offset=0, length=65536):
        return self.data.read_cell(cell_ref, offset, length)

    def export_records(self, database, table, output, equals=None):
        return self.data.export_records(database, table, output, equals)

    def get_message(self, item_id, include_raw=False):
        if not isinstance(item_id, str) or not item_id:
            raise QQError('invalid_argument', 'item_id must be a non-empty string')
        if not isinstance(include_raw, bool):
            raise QQError('invalid_argument', 'include_raw must be boolean')
        rows = self._query('SELECT payload FROM messages WHERE id=?', (item_id,))
        if not rows:
            raise QQError('not_found', 'message not found')
        item = json.loads(rows[0][0])
        result = {'item': item, 'snapshot_id': self.snapshot_id}
        if include_raw:
            provenance = item['provenance']
            result['raw'] = self.read_records(provenance['database'], provenance['table'],
                equals={'40001': {'type': 'integer', 'value': provenance['local_id']}}, limit=2)
        return result

    def list_assets(self, **params):
        from .assets import list_assets
        return list_assets(self, **params)

    def read_asset(self, asset_id, offset=0, length=65536):
        from .assets import read_asset
        return read_asset(self, asset_id, offset, length)

    def read_favorites(self, *, query=None, kinds=None, limit=100, cursor=None):
        limit_value(limit)
        self._string_list(kinds, 'kinds')
        if query is not None and not isinstance(query, str):
            raise QQError('invalid_argument', 'query must be a string')
        if not self._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='favorites'"):
            raise QQError('refresh_required', 'refresh this source to enable the favorites index')
        filters = digest({'query': query, 'kinds': kinds})
        where, params = ['1'], []
        if query:
            where.append('instr(qq_casefold(search_text),qq_casefold(?))>0')
            params.append(query)
        if kinds:
            where.append('kind IN (' + ','.join('?' for _ in kinds) + ')')
            params.extend(kinds)
        if cursor:
            token = cursor_decode(cursor)
            expected = {'kind': 'favorites', 'source_id': self.source_id, 'snapshot_id': self.snapshot_id, 'filters': filters}
            if any(token.get(k) != v for k, v in expected.items()) or not isinstance(token.get('after'), str):
                raise QQError('invalid_cursor', 'favorite cursor belongs to another source, snapshot or filter')
            where.append('id>?')
            params.append(token['after'])
        rows = self._query('SELECT id,payload FROM favorites WHERE ' + ' AND '.join(where) + ' ORDER BY id LIMIT ?', (*params, limit + 1))
        next_cursor = cursor_encode({'kind': 'favorites', 'source_id': self.source_id, 'snapshot_id': self.snapshot_id,
            'filters': filters, 'after': rows[limit - 1][0]}) if len(rows) > limit else None
        return {'items': [json.loads(r[1]) for r in rows[:limit]], 'next_cursor': next_cursor, 'snapshot_id': self.snapshot_id,
                'scope': 'locally_loaded_favorites', 'online_completeness_verified': False}

    def get_favorite(self, favorite_id, include_raw=False):
        if not isinstance(favorite_id, str) or not favorite_id or type(include_raw) is not bool:
            raise QQError('invalid_argument', 'favorite_id must be a string and include_raw must be boolean')
        rows = self._query('SELECT payload FROM favorites WHERE id=?', (favorite_id,))
        if not rows:
            raise QQError('not_found', 'favorite does not exist in this snapshot')
        item = json.loads(rows[0][0])
        result = {'item': item, 'snapshot_id': self.snapshot_id}
        if include_raw:
            result['raw'] = self.read_records('collection.db', 'collection_list_info_table', equals={'180001': item['native_id']}, limit=2)
            tables = {t['table'] for t in self.list_tables('collection.db')}
            result['detail_raw'] = self.read_records('collection.db', 'content_list_info_table', equals={'180700': item['native_id']}, limit=2) if 'content_list_info_table' in tables else None
        return result

    def export_favorites(self, output, *, query=None, kinds=None, require_details=False):
        if type(require_details) is not bool:
            raise QQError('invalid_argument', 'require_details must be boolean')
        root = Path(output).expanduser().absolute()
        if root.is_symlink() or root == self.root or self.root in root.parents or root in self.root.parents:
            raise QQError('invalid_path', 'favorites export must not overlap its snapshot')
        items, cursor = [], None
        while True:
            page = self.read_favorites(query=query, kinds=kinds, limit=1000, cursor=cursor)
            items.extend(page['items'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        cached = sum(i['detail']['state'] == 'cached' for i in items)
        if require_details and cached != len(items):
            raise QQError('incomplete_data', 'some selected favorite details are not cached', {'items': len(items), 'cached_details': cached})
        private_dir(root)
        destination = root / ('qq-favorites-' + uuid.uuid4().hex[:16])
        with tempfile.TemporaryDirectory(prefix='.export-', dir=root) as temp:
            staging = Path(temp)
            def stream(name):
                return os.fdopen(os.open(staging / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8')
            with stream('favorites.jsonl') as out, stream('native.jsonl') as raw_out:
                for item in items:
                    out.write(canonical(item) + '\n')
                    raw = self.get_favorite(item['id'], include_raw=True)
                    for key in ('raw', 'detail_raw'):
                        for record in (raw.get(key) or {}).get('rows', []):
                            native = self.data.materialize_record(record)
                            raw_out.write(canonical({'favorite_id': item['id'], 'part': key, 'record': native}) + '\n')
            manifest = {'schema_version': SCHEMA_VERSION, 'source_id': self.source_id, 'snapshot_id': self.snapshot_id,
                'count': len(items), 'cached_details': cached, 'missing_or_stale_details': len(items) - cached,
                'details_complete_for_selection': cached == len(items), 'online_completeness_verified': False,
                'scope': 'locally_loaded_favorites', 'privacy': {'visibility': 'private', 'trust': 'untrusted'}}
            atomic_json(staging / 'manifest.json', manifest)
            os.rename(staging, destination)
        return {**manifest, 'output': str(destination)}

    def changes(self, cursor=None, limit=100, entity_types=None):
        limit_value(limit)
        allowed = {"actors", "conversations", "messages", "memberships", "databases", "assets", 'favorites'}
        self._string_list(entity_types, "entity_types")
        if entity_types and set(entity_types) - allowed:
            raise QQError("invalid_argument", "unsupported change entity type")
        position = 0
        filters = digest(sorted(entity_types) if entity_types else None)
        if cursor:
            value = cursor_decode(cursor)
            if (value.get("kind") != "changes" or value.get("source_id") != self.source_id or
                    value.get("origin") != self.origin or value.get("filters") != filters or
                    type(value.get("after")) is not int or value["after"] < 0):
                raise QQError("invalid_cursor", "change cursor belongs to another source, origin or filter")
            position = value["after"]
        maximum = self._query("SELECT coalesce(max(seq),0) FROM changes")[0][0]
        if position > maximum:
            raise QQError("invalid_cursor", "change cursor is ahead of this snapshot")
        where, params = "seq>?", [position]
        if entity_types:
            where += " AND entity_type IN (" + ",".join("?" for _ in entity_types) + ")"
            params.extend(entity_types)
        rows = self._query("SELECT seq,payload FROM changes WHERE " + where + " ORDER BY seq LIMIT ?", (*params, limit + 1))
        visible = rows[:limit]
        more = len(rows) > limit
        after = visible[-1][0] if more else maximum
        token = cursor_encode({"kind": "changes", "source_id": self.source_id, "origin": self.origin,
                               "filters": filters, "after": after})
        return {"changes": [{**json.loads(row[1]), "sequence": str(row[0])} for row in visible],
                "cursor": token, "next_cursor": token if more else None,
                "source_id": self.source_id, "snapshot_id": self.snapshot_id}

    def export_bundle(self, output, conversation_ids=None, *, previous=None, **filters):
        root = Path(output).expanduser().absolute()
        if root == self.root or self.root in root.parents or root in self.root.parents:
            raise QQError("invalid_path", "export must not overlap the snapshot")
        if root.is_symlink():
            raise QQError("invalid_path", "export root must not be a symbolic link")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        normalized_filters = {"conversation_ids": conversation_ids, **filters}
        filter_hash = digest(normalized_filters)
        old = {}
        if previous:
            prior = Path(previous)
            manifest = load_json(prior / "manifest.json")
            if manifest.get("source_id") != self.source_id or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("filters_hash") != filter_hash:
                raise QQError("source_mismatch", "previous export must use the same source, schema and filters")
            with regular_file(prior / "items.jsonl").open() as stream:
                old = {item["id"]: item["revision"] for line in stream if line.strip() for item in [json.loads(line)]}
        self._message_filter(conversation_ids=conversation_ids, **filters)
        destination = root / ("qq-export-" + uuid.uuid4().hex[:16])
        selected, authors, added, updated = set(), set(), 0, 0
        count = 0
        with tempfile.TemporaryDirectory(prefix=".export-", dir=root) as temporary:
            staging = Path(temporary)

            def open_private(name):
                fd = os.open(staging / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                return os.fdopen(fd, "w", encoding="utf-8")

            with open_private("items.jsonl") as items, open_private("changes.jsonl") as changes:
                cursor = None
                while True:
                    page = self.read_items(conversation_ids, **filters, limit=1000, cursor=cursor)
                    for item in page["items"]:
                        items.write(canonical(item) + "\n")
                        selected.add(item["conversation_id"])
                        if item["author_id"]:
                            authors.add(item["author_id"])
                        prior_revision = old.pop(item["id"], None)
                        if prior_revision != item["revision"]:
                            op = "added" if prior_revision is None else "updated"
                            added += op == "added"
                            updated += op == "updated"
                            changes.write(canonical({"op": op, "item": item}) + "\n")
                        count += 1
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
            with open_private("conversations.jsonl") as stream:
                for row in self._query("SELECT payload FROM conversations ORDER BY id"):
                    item = json.loads(row[0])
                    if item["id"] in selected:
                        if item.get("peer_actor_id"):
                            authors.add(item["peer_actor_id"])
                        stream.write(canonical(item) + "\n")
            with open_private("actors.jsonl") as stream:
                for row in self._query("SELECT payload FROM actors ORDER BY id"):
                    item = json.loads(row[0])
                    if item["id"] in authors:
                        stream.write(canonical(item) + "\n")
            manifest = {"schema_version": SCHEMA_VERSION, "source_id": self.source_id,
                "snapshot_id": self.snapshot_id, "filters_hash": filter_hash,
                "privacy": {"visibility": "private", "storage": "local", "trust": "untrusted"},
                "counts": {"items": count, "added": added, "updated": updated, "missing_from_previous": len(old)}}
            from .util import atomic_json
            atomic_json(staging / "manifest.json", manifest)
            self._unchanged()
            os.rename(staging, destination)
        return {**manifest, "output": str(destination)}
