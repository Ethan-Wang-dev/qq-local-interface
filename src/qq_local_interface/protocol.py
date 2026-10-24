"""One JSON request per line; the same operations back the MCP adapter."""
from __future__ import annotations

import json

from . import PROTOCOL_VERSION, SCHEMA_VERSION
from .errors import QQError
from .source import QQSource

FILTERS = {"conversation_ids", "start", "end", "author_ids", "kinds", "directions", "qualities", "query"}
OPERATIONS = {
    "status": set(), "sync.status": set(), "sync.refresh": {"mode", "allow_partial"},
    "contacts.list": {"query", "limit"}, "conversations.list": {"query", "kinds", "limit"},
    "groups.members": {"conversation_id", "limit", "cursor"},
    "contacts.read": {"query", "limit", "cursor"},
    "conversations.read": {"query", "kinds", "limit", "cursor"},
    "messages.read": FILTERS | {"limit", "cursor"},
    "messages.search": FILTERS | {"limit", "cursor"},
    "messages.export": FILTERS | {"output", "previous"},
    "changes.list": {"cursor", "limit", "entity_types"},
    "messages.get": {"item_id", "include_raw"},
    "databases.list": set(), "tables.list": {"database"},
    "records.read": {"database", "table", "equals", "limit", "cursor"},
    "records.export": {"database", "table", "output", "equals"},
    "cells.read": {"cell_ref", "offset", "length"},
    "assets.list": {"kinds", "query", "limit", "cursor"},
    "assets.read": {"asset_id", "offset", "length"},
    "datasets.list": {"category", "include_internal"},
    "datasets.read": {"dataset_id", "equals", "limit", "cursor"},
    'favorites.read': {'query', 'kinds', 'limit', 'cursor'},
    'favorites.get': {'favorite_id', 'include_raw'},
    'favorites.export': {'output', 'query', 'kinds', 'require_details'},
}


class QQProtocol:
    def __init__(self, vault=None, source=None):
        if (vault is None) == (source is None):
            raise ValueError("provide exactly one of vault and source")
        self.vault = vault
        self.fixed_source = source
        self.source_id = vault.source_id if vault else source.source_id

    def source(self):
        if self.fixed_source:
            return self.fixed_source
        path, _ = self.vault.current()
        return QQSource(path, self.source_id)

    def handle(self, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        operation = request.get("operation") if isinstance(request, dict) else None
        meta = {"protocol_version": PROTOCOL_VERSION, "schema_version": SCHEMA_VERSION,
                "request_id": request_id if isinstance(request_id, str) else None,
                "source_id": self.source_id, "operation": operation if isinstance(operation, str) else None}
        try:
            if not isinstance(request, dict) or set(request) - {"protocol_version", "request_id", "source_id", "operation", "params"}:
                raise QQError("invalid_request", "request must be an object with only documented envelope fields")
            if request.get("protocol_version") != PROTOCOL_VERSION:
                raise QQError("protocol_version_mismatch", "unsupported protocol_version", {"expected": PROTOCOL_VERSION})
            if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
                raise QQError("invalid_request", "request_id must be a non-empty string of at most 128 characters")
            if request.get("source_id", self.source_id) != self.source_id:
                raise QQError("source_mismatch", "request source_id differs from this connector")
            if not isinstance(operation, str) or operation not in OPERATIONS:
                raise QQError("unsupported_operation", "unsupported operation")
            params = request.get("params", {})
            if not isinstance(params, dict) or set(params) - OPERATIONS[operation]:
                raise QQError("invalid_argument", "params contains unsupported fields or is not an object")
            if operation == "sync.status":
                data = self.vault.status() if self.vault else {"sync_available": False, **self.fixed_source.status()}
            elif operation == "sync.refresh":
                if not self.vault:
                    raise QQError("unsupported_operation", "an offline snapshot cannot refresh QQ")
                data = self.vault.refresh(**params)
            else:
                source = self.source()
                meta["snapshot_id"] = source.snapshot_id
                if operation == "status":
                    data = source.status()
                    if self.vault:
                        data["sync"] = self.vault.status()
                elif operation == "contacts.list":
                    data = source.list_contacts(**params)
                elif operation == "conversations.list":
                    data = source.list_conversations(**params)
                elif operation == 'contacts.read':
                    data = source.read_contacts(**params)
                elif operation == 'conversations.read':
                    data = source.read_conversations(**params)
                elif operation == "groups.members":
                    if "conversation_id" not in params:
                        raise QQError("invalid_argument", "conversation_id is required")
                    data = source.list_group_members(**params)
                elif operation == "messages.read":
                    data = source.read_items(**params)
                elif operation == "messages.search":
                    if "query" not in params:
                        raise QQError("invalid_argument", "query is required")
                    data = source.search_items(**params)
                elif operation == "messages.export":
                    if "output" not in params:
                        raise QQError("invalid_argument", "output is required")
                    data = source.export_bundle(**params)
                elif operation == "changes.list":
                    data = source.changes(**params)
                elif operation == "messages.get":
                    data = source.get_message(**params)
                elif operation == "databases.list":
                    data = source.list_databases()
                elif operation == "tables.list":
                    data = source.list_tables(**params)
                elif operation == "records.read":
                    data = source.read_records(**params)
                elif operation == "records.export":
                    data = source.export_records(**params)
                elif operation == "cells.read":
                    data = source.read_cell(**params)
                elif operation == "assets.list":
                    data = source.list_assets(**params)
                elif operation == "assets.read":
                    data = source.read_asset(**params)
                elif operation == "datasets.list":
                    data = source.list_datasets(**params)
                elif operation == "datasets.read":
                    data = source.read_dataset(**params)
                elif operation == 'favorites.read':
                    data = source.read_favorites(**params)
                elif operation == 'favorites.get':
                    data = source.get_favorite(**params)
                elif operation == 'favorites.export':
                    data = source.export_favorites(**params)
            return {"ok": True, "data": data, "meta": meta}
        except QQError as error:
            return {"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details}, "meta": meta}
        except (TypeError, ValueError):
            return {"ok": False, "error": {"code": "invalid_argument", "message": "invalid operation parameters", "details": {}}, "meta": meta}
        except Exception:
            return {"ok": False, "error": {"code": "internal_error", "message": "local QQ operation failed", "details": {}}, "meta": meta}

    def handle_line(self, line):
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"ok": False, "error": {"code": "invalid_json", "message": "request is not valid JSON", "details": {}},
                    "meta": {"protocol_version": PROTOCOL_VERSION, "request_id": None, "source_id": self.source_id}}
        return self.handle(request)
