from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import PROTOCOL_VERSION
from .capture import capture_keys
from .errors import QQError
from .platforms import DEFAULT_HOME, doctor, resolve_account
from .protocol import QQProtocol, OPERATIONS
from .source import QQSource
from .util import canonical
from .vault import QQVault


def parser():
    p = argparse.ArgumentParser(description="macOS QQ sync, private snapshots and Agent interfaces")
    p.add_argument("--vault", type=Path, default=DEFAULT_HOME)
    p.add_argument("--source-id", default="qq-local")
    p.add_argument("--snapshot", type=Path, help="read a fixed offline generation instead of a live vault")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    init = commands.add_parser("init")
    init.add_argument("--account", required=True, help="QQ number of the explicitly selected account")
    init.add_argument("--database-dir", type=Path)
    init.add_argument("--account-uid")
    init.add_argument("--key-file", type=Path)
    selection = init.add_mutually_exclusive_group()
    selection.add_argument('--exclude-database', action='append')
    selection.add_argument('--include-all-databases', action='store_true')
    commands.add_parser("capture-key")
    sync = commands.add_parser("sync")
    sync.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    sync.add_argument('--allow-partial', action='store_true', help='publish accessible databases and explicitly report unavailable auxiliary databases')
    watch = commands.add_parser("watch")
    watch.add_argument("--interval", type=int, default=30)
    watch.add_argument('--allow-partial', action='store_true')
    commands.add_parser("status")
    commands.add_parser("sync-status")
    for name in ("contacts", "conversations"):
        sub = commands.add_parser(name)
        sub.add_argument("--query")
        sub.add_argument("--limit", type=int, default=1000)
        if name == "conversations":
            sub.add_argument("--kind", choices=("person", "group"), action="append")
    members = commands.add_parser("members")
    members.add_argument("conversation_id")
    members.add_argument("--limit", type=int, default=1000)
    members.add_argument('--cursor')
    call = commands.add_parser('call', help='invoke any versioned operation with a JSON params object')
    call.add_argument('operation', choices=sorted(OPERATIONS))
    call.add_argument('--params', default='{}')
    commands.add_parser('databases')
    tables = commands.add_parser('tables')
    tables.add_argument('database')
    datasets = commands.add_parser('datasets')
    datasets.add_argument('--category')
    datasets.add_argument('--include-internal', action='store_true')
    for name in ('records', 'dataset', 'records-export'):
        sub = commands.add_parser(name)
        if name == 'dataset':
            sub.add_argument('dataset_id')
        else:
            sub.add_argument('database')
            sub.add_argument('table')
        sub.add_argument('--equals', help='JSON object with exact equality filters')
        if name == 'records-export':
            sub.add_argument('--output', required=True)
        else:
            sub.add_argument('--limit', type=int, default=100)
            sub.add_argument('--cursor')
    assets = commands.add_parser('assets')
    assets.add_argument('--kind', action='append')
    assets.add_argument('--query')
    assets.add_argument('--limit', type=int, default=100)
    assets.add_argument('--cursor')
    for name, identifier in (('asset', 'asset_id'), ('cell', 'cell_ref')):
        sub = commands.add_parser(name)
        sub.add_argument(identifier)
        sub.add_argument('--offset', type=int, default=0)
        sub.add_argument('--length', type=int, default=65536)
    message = commands.add_parser('message')
    message.add_argument('item_id')
    message.add_argument('--include-raw', action='store_true')
    favorites = commands.add_parser('favorites')
    favorites.add_argument('--query')
    favorites.add_argument('--kind', action='append')
    favorites.add_argument('--limit', type=int, default=100)
    favorites.add_argument('--cursor')
    favorite = commands.add_parser('favorite')
    favorite.add_argument('favorite_id')
    favorite.add_argument('--include-raw', action='store_true')
    favorite_export = commands.add_parser('favorites-export')
    favorite_export.add_argument('--output', required=True)
    favorite_export.add_argument('--query')
    favorite_export.add_argument('--kind', action='append')
    favorite_export.add_argument('--require-details', action='store_true')
    for name in ("messages", "search", "export"):
        sub = commands.add_parser(name)
        if name == "search":
            sub.add_argument("query")
        else:
            sub.add_argument("--query")
        sub.add_argument("--conversation", action="append")
        sub.add_argument("--start")
        sub.add_argument("--end")
        sub.add_argument("--author", action="append")
        sub.add_argument("--kind", action="append")
        if name == "export":
            sub.add_argument("--output", required=True)
            sub.add_argument("--previous")
        else:
            sub.add_argument("--limit", type=int, default=100)
            sub.add_argument("--cursor")
    changes = commands.add_parser("changes")
    changes.add_argument("--cursor")
    changes.add_argument("--limit", type=int, default=100)
    changes.add_argument("--entity-type", action="append", choices=("actors", "conversations", "messages", 'memberships', 'databases', 'assets', 'favorites'))
    commands.add_parser("rpc")
    commands.add_parser("mcp")
    commands.add_parser("agent-config")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    vault = QQVault(args.vault, args.source_id)
    try:
        if args.snapshot and args.command not in {"status", "contacts", "conversations", "members", "messages", "search", "changes", "export", "rpc", 'call', 'databases', 'tables', 'datasets', 'dataset', 'records', 'records-export', 'assets', 'asset', 'cell', 'message', 'favorites', 'favorite', 'favorites-export'}:
            raise QQError("invalid_argument", "this command requires a configured vault, not --snapshot")
        if args.command == "doctor":
            result = doctor()
        elif args.command == "init":
            account = resolve_account(args.account, args.vault) if args.database_dir is None else {
                "uin": args.account, "uid": args.account_uid, "database_dir": str(args.database_dir)}
            result = vault.configure(account["database_dir"], account_uin=account["uin"],
                                     account_uid=account["uid"], key_file=args.key_file,
                                     exclude_databases=[] if args.include_all_databases else args.exclude_database)
        elif args.command == "capture-key":
            config = vault.config()
            result = capture_keys(config["database_dir"], config["key_file"], exclude_databases=config.get('exclude_databases'))
        elif args.command == "sync":
            result = vault.refresh(args.mode, allow_partial=args.allow_partial)
        elif args.command == "watch":
            if args.interval < 5:
                raise QQError("invalid_argument", "watch interval must be at least 5 seconds")
            while True:
                try:
                    print(canonical({"ok": True, "data": vault.refresh(allow_partial=args.allow_partial)}), flush=True)
                except QQError as error:
                    print(canonical({"ok": False, "error": {"code": error.code, "message": error.message}}), flush=True)
                time.sleep(args.interval)
        elif args.command == "mcp":
            from .mcp_server import create_server
            create_server(vault).run(transport="stdio")
            return 0
        elif args.command == "agent-config":
            result = {"mcpServers": {"qq-local": {"command": sys.executable, "args": ["-m", "qq_local_interface",
                "--vault", str(args.vault.expanduser().absolute()), "--source-id", args.source_id, "mcp"]}}}
        else:
            protocol = QQProtocol(source=QQSource(args.snapshot, args.source_id)) if args.snapshot else QQProtocol(vault=vault)
            if args.command == "rpc":
                for line in sys.stdin:
                    if line.strip():
                        print(canonical(protocol.handle_line(line)), flush=True)
                return 0
            mapping = {"status": "status", "sync-status": "sync.status", "contacts": "contacts.list",
                "conversations": "conversations.list", "members": "groups.members", "messages": "messages.read",
                "search": "messages.search", "changes": "changes.list", "export": "messages.export"}
            mapping.update({'databases': 'databases.list', 'tables': 'tables.list', 'datasets': 'datasets.list',
                'dataset': 'datasets.read', 'records': 'records.read', 'records-export': 'records.export',
                'assets': 'assets.list', 'asset': 'assets.read', 'cell': 'cells.read', 'message': 'messages.get',
                'favorites': 'favorites.read', 'favorite': 'favorites.get', 'favorites-export': 'favorites.export'})
            rename = {"conversation": "conversation_ids", "author": "author_ids", "kind": "kinds", "entity_type": "entity_types"}
            params = {rename.get(k, k): v for k, v in vars(args).items() if k not in
                      {"vault", "source_id", "snapshot", "command"} and v is not None}
            operation = mapping.get(args.command)
            try:
                if args.command == 'call':
                    operation, params = args.operation, json.loads(args.params)
                elif 'equals' in params:
                    params['equals'] = json.loads(params['equals'])
            except json.JSONDecodeError:
                raise QQError('invalid_json', 'params or equals must be valid JSON') from None
            result = protocol.handle({"protocol_version": PROTOCOL_VERSION, "request_id": "cli",
                                      "operation": operation, "params": params})
        print(canonical(result))
        return 2 if isinstance(result, dict) and result.get("ok") is False else 0
    except QQError as error:
        print(canonical({"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details}}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
