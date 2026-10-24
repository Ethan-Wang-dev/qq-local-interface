"""Normalize detached QQ SQLite files into our own query and change index."""
from __future__ import annotations

import json
import base64
import os
import sqlite3
from pathlib import Path

from . import SCHEMA_VERSION
from .errors import QQError
from .protobuf import decode_body, links_in
from .favorites import load_favorites
from .util import canonical, digest, opaque, readonly, timestamp

DDL = """
CREATE TABLE actors(id TEXT PRIMARY KEY, name TEXT, kind TEXT, payload TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE conversations(id TEXT PRIMARY KEY, name TEXT, kind TEXT, payload TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, author_id TEXT,
 created_at TEXT, kind TEXT, direction TEXT, text TEXT, quality TEXT,
 payload TEXT NOT NULL, revision TEXT NOT NULL);
CREATE INDEX messages_chat_time ON messages(conversation_id,created_at,id);
CREATE INDEX messages_time ON messages(created_at,id);
CREATE INDEX messages_author ON messages(author_id,created_at,id);
CREATE TABLE memberships(conversation_id TEXT, actor_id TEXT, payload TEXT NOT NULL,
 id TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(conversation_id,actor_id));
CREATE TABLE databases(id TEXT PRIMARY KEY, name TEXT, payload TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE assets(id TEXT PRIMARY KEY, relative_path TEXT, kind TEXT, payload TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE favorites(id TEXT PRIMARY KEY,native_id TEXT UNIQUE,kind TEXT,collected_at TEXT,search_text TEXT,payload TEXT NOT NULL,revision TEXT NOT NULL);
CREATE TABLE changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, generation TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, op TEXT NOT NULL,
 revision TEXT, payload TEXT NOT NULL);
"""

NORMALIZER_VERSION = 8


def rows(connection, table):
    escaped = table.replace('"', '""')
    return connection.execute(f'SELECT * FROM "{escaped}"')


def tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def take(row, *columns):
    keys = row.keys()
    for column in columns:
        value = row[column] if column in keys else None
        if value is not None and value != "" and value != 0:
            return value
    return None


def identifier(value):
    return str(value) if value is not None and value != "" and value != 0 else None


def normalized_hash(row):
    return digest({k: {"sha256": digest(bytes(v))} if isinstance(v, (bytes, bytearray)) else v for k, v in dict(row).items()})


def build_index(data_dir, output, source_id, account, generation, previous=None, *, inputs=None, asset_entries=None, favorite_details=None):
    data_dir, output = Path(data_dir), Path(output)
    connection = sqlite3.connect(output)
    connection.executescript(DDL)
    actors, aliases, conversations, memberships = {}, {}, {}, {}
    warnings = []
    assets_by_path, assets_by_name = {}, {}
    for entry in asset_entries or []:
        assets_by_path.setdefault(os.path.realpath(entry['local_path']), []).append(entry['id'])
        assets_by_name.setdefault(entry['name'].casefold(), []).append(entry['id'])

    def link_assets(decoded):
        for attachment in decoded['attachments']:
            matches = {}
            for path in attachment.get('native_paths', []):
                for aid in assets_by_path.get(os.path.realpath(path) if os.path.isabs(path) else path, []):
                    matches[aid] = 'native_path'
            if attachment.get('local_path'):
                for aid in assets_by_path.get(os.path.realpath(attachment['local_path']), []):
                    matches[aid] = 'native_path'
            name = attachment.get('name')
            if name:
                for aid in assets_by_name.get(name.casefold(), []):
                    matches.setdefault(aid, 'filename_candidate')
            for value in attachment.get('native_identifiers', {}).values():
                raw = base64.b64decode(value)
                if len(raw) == 16:
                    marker = raw.hex()
                    for cached, ids in assets_by_name.items():
                        if cached.split('.')[0] == marker:
                            for aid in ids:
                                matches.setdefault(aid, 'identifier_candidate')
            attachment['local_assets'] = [{'asset_id': aid, 'match': reason} for aid, reason in sorted(matches.items())]
            if any(v == 'native_path' for v in matches.values()):
                attachment['availability'] = 'local_snapshot'
            elif matches:
                attachment['availability'] = 'local_cache_candidates'

    def actor(uid=None, uin=None, name=None, friend=None, provenance=None):
        uid, uin = identifier(uid), identifier(uin)
        keys = [f"uid:{uid}" if uid else None, f"uin:{uin}" if uin else None]
        identity = next((aliases[k] for k in keys if k in aliases), None) or keys[0] or keys[1]
        if identity is None:
            return None
        aid = opaque(source_id, "actor", identity)
        item = actors.setdefault(aid, {"schema_version": SCHEMA_VERSION, "source_id": source_id,
            "id": aid, "kind": "person", "uid": uid, "qq_number": uin, "display_name": None,
            "friend_status": "unknown", "provenance": []})
        for key in keys:
            if key:
                aliases[key] = identity
        if uid:
            item["uid"] = uid
        if uin:
            item["qq_number"] = uin
        if name is not None and (item["display_name"] is None or provenance):
            item["display_name"] = str(name)
        if uin == account.get("uin") or uid == account.get("uid"):
            item["friend_status"] = "self"
        elif friend is True:
            item["friend_status"] = "friend"
        if provenance and provenance not in item["provenance"]:
            item["provenance"].append(provenance)
        return aid

    def conversation(kind, peer, name=None, peer_actor=None):
        cid = opaque(source_id, "conversation", kind + ":" + str(peer))
        item = conversations.setdefault(cid, {"schema_version": SCHEMA_VERSION, "source_id": source_id,
            "id": cid, "kind": kind, "display_name": None, "native_id": str(peer),
            "peer_actor_id": peer_actor, "message_count": 0})
        if name is not None:
            item["display_name"] = str(name)
        if peer_actor:
            item["peer_actor_id"] = peer_actor
        return cid

    try:
        actor(account.get("uid"), account.get("uin"))
        profile = data_dir / "profile_info.db"
        if profile.exists() and profile.stat().st_size > 1024:
            with readonly(profile) as raw:
                names = tables(raw)
                for table in ("buddy_list", "profile_info_v6", "profile_info_adelie"):
                    if table in names:
                        for row in rows(raw, table):
                            actor(take(row, "1000", "uid"), take(row, "1002", "uin"),
                                  take(row, "20002", "nickname", "remark"), table == "buddy_list",
                                  {"database": "profile_info.db", "table": table})
        groups = data_dir / "group_info.db"
        if groups.exists() and groups.stat().st_size > 1024:
            with readonly(groups) as raw:
                names = tables(raw)
                if "group_detail_info_ver1" in names:
                    for row in rows(raw, "group_detail_info_ver1"):
                        group = take(row, "60001", "groupCode", "group_id")
                        if group:
                            conversation("group", group, take(row, "60007", "groupName", "name"))
                if "group_member3" in names:
                    for row in rows(raw, "group_member3"):
                        group = take(row, "60001", "group_id")
                        if not group or ("64016" in row.keys() and row["64016"] == 1):
                            continue
                        aid = actor(take(row, "1000", "uid"), take(row, "1002", "uin"),
                                    take(row, "20002", "nickname"))
                        if aid:
                            cid = conversation("group", group)
                            memberships[cid, aid] = {"conversation_id": cid, "actor_id": aid,
                                "display_name": take(row, "64003", "card"), "membership_source": "group_member3",
                                "is_admin": row["64010"] == 1 if "64010" in row.keys() else None,
                                "complete": False}
        message_file = data_dir / "nt_msg.db"
        if not message_file.exists():
            raise QQError("invalid_snapshot", "nt_msg.db is required")
        with readonly(message_file) as raw:
            names = tables(raw)
            selected = [t for t in ("c2c_msg_table", "group_msg_table") if t in names]
            if not selected:
                raise QQError("unsupported_schema", "no supported QQ message tables found")
            for table in selected:
                columns = {r[1] for r in raw.execute(f'PRAGMA table_info("{table}")')}
                if "40001" not in columns or "40050" not in columns:
                    raise QQError("unsupported_schema", "QQ message table lacks required identity or timestamp columns")
                for row in rows(raw, table):
                    group = table == "group_msg_table"
                    aid = actor(take(row, "40020"), take(row, "40033"), take(row, "40093"))
                    if group:
                        peer = take(row, "40030", "40021", "40027")
                        cid = conversation("group", peer if peer is not None else "unknown")
                    else:
                        peer_actor = actor(take(row, "40021"), take(row, "40030"))
                        peer = actors[peer_actor]["uid"] or actors[peer_actor]["qq_number"] if peer_actor else take(row, "40027")
                        cid = conversation("person", peer if peer is not None else "unknown", peer_actor=peer_actor)
                    body = take(row, "40800", "40080")
                    decoded = decode_body(body)
                    link_assets(decoded)
                    author = actors.get(aid, {})
                    is_self = author.get("friend_status") == "self"
                    direction = "outgoing" if is_self else "incoming" if aid else "unknown"
                    local_id = str(row["40001"])
                    mid = opaque(source_id, "item", cid + ":" + local_id)
                    item = {"schema_version": SCHEMA_VERSION, "resource_type": "message", "source_id": source_id,
                        "id": mid, "conversation_id": cid, "author_id": aid,
                        "created_at": timestamp(row["40050"]), "direction": direction,
                        **decoded, "title": next((a["name"] for a in decoded["attachments"] if a["name"]), None),
                        "links": links_in(decoded.get("search_text") or decoded["text"]),
                        "provenance": {"database": "nt_msg.db", "table": table, "local_id": local_id,
                            "native_type": identifier(take(row, "40011")), "raw_row_sha256": normalized_hash(row)}}
                    item["revision"] = digest(item)
                    connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (mid, cid, aid, item["created_at"], item["kind"], direction, decoded.get("search_text") or item["text"],
                         item["quality"]["status"], canonical(item), item["revision"]))
                    conversations[cid]["message_count"] += 1
                    if group and aid and (cid, aid) not in memberships:
                        memberships[cid, aid] = {"conversation_id": cid, "actor_id": aid,
                            "display_name": author.get("display_name"), "membership_source": "observed_messages",
                            "is_admin": None, "complete": False}
        for item in conversations.values():
            peer = actors.get(item.get("peer_actor_id"), {})
            item["display_name"] = item["display_name"] or peer.get("display_name")
        for table, items in (("actors", actors), ("conversations", conversations)):
            for item in items.values():
                item["revision"] = digest(item)
                connection.execute(f"INSERT INTO {table} VALUES(?,?,?,?,?)",
                    (item["id"], item["display_name"], item["kind"], canonical(item), item["revision"]))
        for (cid, aid), item in memberships.items():
            item['id'] = opaque(source_id, 'membership', cid + ':' + aid)
            item['revision'] = digest(item)
            connection.execute("INSERT INTO memberships VALUES(?,?,?,?,?)", (cid, aid, canonical(item), item['id'], item['revision']))
        for name, fingerprint in sorted((inputs or {}).items()):
            item = {'id': opaque(source_id, 'database', name), 'database': name,
                    'source_id': source_id, 'storage_revision': digest(fingerprint)}
            item['revision'] = digest(item)
            connection.execute('INSERT INTO databases VALUES(?,?,?,?)', (item['id'], name, canonical(item), item['revision']))
        for item in asset_entries or []:
            connection.execute('INSERT INTO assets VALUES(?,?,?,?,?)',
                (item['id'], item['relative_path'], item['kind'], canonical(item), item['revision']))
        favorite_items = load_favorites(data_dir, source_id, favorite_details)
        for item in favorite_items:
            link_assets(item)
            item.pop('revision', None)
            item['revision'] = digest(item)
            body = item['detail'].get('content') or {}
            searchable = '\n'.join(str(v) for v in (item['title'], item['summary_text'], item['url'],
                body.get('text'), body.get('raw_data'), item['author'].get('display_name'), item['author'].get('group_name')) if v)
            connection.execute('INSERT INTO favorites VALUES(?,?,?,?,?,?,?)',
                (item['id'], item['native_id'], item['kind'], item['collected_at'], searchable, canonical(item), item['revision']))
        old = sqlite3.connect(previous.as_uri() + "?mode=ro&immutable=1", uri=True) if previous else None
        counts = {"added": 0, "updated": 0, "missing": 0}
        try:
            if old:
                for row in old.execute("SELECT seq,generation,entity_type,entity_id,op,revision,payload FROM changes ORDER BY seq"):
                    connection.execute("INSERT INTO changes VALUES(?,?,?,?,?,?,?)", row)
            for table in ("actors", "conversations", "messages", "memberships", "databases", "assets", 'favorites'):
                old_columns = {r[1] for r in old.execute('PRAGMA table_info(' + table + ')')} if old else set()
                old_revisions = dict(old.execute(f"SELECT id,revision FROM {table}")) if {'id', 'revision'} <= old_columns else {}
                for entity_id, revision, payload in connection.execute(f"SELECT id,revision,payload FROM {table}").fetchall():
                    prior = old_revisions.pop(entity_id, None)
                    if prior != revision:
                        op = "added" if prior is None else "updated"
                        event = {"op": op, "entity_type": table, "entity_id": entity_id,
                                 "generation": generation, "item": json.loads(payload)}
                        connection.execute("INSERT INTO changes(generation,entity_type,entity_id,op,revision,payload) VALUES(?,?,?,?,?,?)",
                            (generation, table, entity_id, op, revision, canonical(event)))
                        counts[op] += 1
                for entity_id, revision in old_revisions.items():
                    event = {"op": "missing", "entity_type": table, "entity_id": entity_id, "generation": generation,
                             "reason": "not_observed_in_current_snapshot", "is_deletion": False}
                    connection.execute("INSERT INTO changes(generation,entity_type,entity_id,op,revision,payload) VALUES(?,?,?,?,?,?)",
                        (generation, table, entity_id, "missing", revision, canonical(event)))
                    counts["missing"] += 1
        finally:
            if old:
                old.close()
        connection.commit()
        return {"actors": len(actors), "conversations": len(conversations),
                "messages": connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                "memberships": len(memberships), 'favorites': len(favorite_items), "changes": counts, "warnings": warnings}
    finally:
        connection.close()
