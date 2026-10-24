"""Bounded QQNT element decoding with an exact raw-cell access path.

Element numbers follow the primary QQNT type definitions. Unknown fields are
retained as typed previews and the original database cell remains readable.
"""
from __future__ import annotations

import base64
import json
import re
import struct
import zlib

from .util import digest

MAX_BYTES = 4 * 1024 * 1024
ELEMENT_KINDS = {
    0: "unsupported", 1: "text", 2: "image", 3: "file", 4: "voice",
    5: "video", 6: "sticker", 7: "quote", 8: "system", 9: "wallet",
    10: "card", 11: "sticker", 12: "gift", 13: "forward", 14: "markdown",
    15: "image", 16: "forward", 17: "keyboard", 18: "gift",
    19: "calendar", 20: "game", 21: "call", 22: "feed",
    23: "online_file", 24: "bubble", 25: "activity", 26: "tofu",
    27: "sticker", 28: "location", 29: "task", 30: "folder",
    43: "recommendation", 44: "action_bar",
}


def wire_fields(data, *, allow_padding=False):
    if len(data) > MAX_BYTES:
        raise ValueError("protobuf message exceeds size limit")
    result, offset = [], 0

    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError("truncated protobuf varint")
            part = data[offset]
            offset += 1
            if shift == 63 and part > 1:
                raise ValueError("protobuf varint exceeds uint64")
            value |= (part & 127) << shift
            if part < 128:
                return value
        raise ValueError("oversized protobuf varint")

    while offset < len(data):
        if allow_padding and data[offset] == 0 and not any(data[offset:]):
            break
        tag = varint()
        number, kind = tag >> 3, tag & 7
        if not number or number > 0x1FFFFFFF:
            raise ValueError("invalid protobuf field")
        if kind == 0:
            value = varint()
        elif kind == 2:
            length = varint()
            if length > len(data) - offset:
                raise ValueError("truncated protobuf field")
            value = data[offset:offset + length]
            offset += length
        elif kind in (1, 5):
            length = 8 if kind == 1 else 4
            if offset + length > len(data):
                raise ValueError("truncated protobuf fixed field")
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError("unsupported protobuf wire type")
        result.append((number, kind, value))
        if len(result) > 10000:
            raise ValueError("protobuf field count exceeds limit")
    return result


def wire(data, *, allow_padding=False):
    result = {}
    for number, _, value in wire_fields(data, allow_padding=allow_padding):
        result.setdefault(number, []).append(value)
    return result


def text_field(fields, number):
    value = fields.get(number, [None])[0]
    if not isinstance(value, bytes):
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def first(fields, number):
    return fields.get(number, [None])[0]


def native_fields(segment):
    """Preserve order and wire types; large bytes are available in the raw cell."""
    result = []
    for number, kind, value in wire_fields(segment):
        entry = {"number": number, "wire_type": kind}
        if kind == 0:
            entry["value"] = str(value)
        else:
            entry.update(length=len(value), sha256=digest(value))
            if len(value) <= 4096:
                entry.update(encoding="base64", data=base64.b64encode(value).decode())
                if kind == 2:
                    try:
                        text = value.decode("utf-8")
                        if all(ord(c) >= 32 or c in "\n\t\r" for c in text):
                            entry["text"] = text
                    except UnicodeDecodeError:
                        pass
            else:
                entry.update(encoding="raw_cell_reference", access="messages.get(include_raw=true)")
        result.append(entry)
    return result


def json_card(data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        return None
    try:
        if data[:1] not in (b"{", b"["):
            inflater = zlib.decompressobj()
            data = inflater.decompress(data, MAX_BYTES + 1)
            if len(data) > MAX_BYTES or inflater.unconsumed_tail or not inflater.eof:
                return None
        parsed = json.loads(data)
        if not isinstance(parsed, (dict, list)):
            return None
        # JSON decoder recursion limits vary across Python versions. Bound the
        # resulting tree explicitly so later serialization and indexing remain
        # safe even when a newer decoder accepts very deep input.
        pending, visited = [(parsed, 0)], 0
        while pending:
            value, depth = pending.pop()
            visited += 1
            if depth > 64 or visited > 10000:
                return None
            if isinstance(value, (dict, list)) and visited + len(pending) + len(value) > 10000:
                return None
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            pending.extend((child, depth + 1) for child in children)
        return parsed
    except (ValueError, TypeError, zlib.error, UnicodeDecodeError, RecursionError):
        return None


def strings_in(value, depth=0):
    if depth > 16:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v, depth + 1)]
    if isinstance(value, list):
        return [s for v in value for s in strings_in(v, depth + 1)]
    return []


def decode_body(value):
    if isinstance(value, str):
        return {"text": value, "kind": "text", "elements": [{"kind": "text", "text": value}],
                "attachments": [], "quality": {"status": "complete", "reason": None},
                "search_text": value, "raw_body": {"length": len(value.encode()), "sha256": digest(value.encode()),
                "access": "messages.get(include_raw=true)"}}
    if value is not None and not isinstance(value, (bytes, bytearray, memoryview)):
        raw = b""
        storage_error = True
    else:
        raw, storage_error = bytes(value or b""), False
    access = {"length": len(raw), "sha256": digest(raw), "access": "messages.get(include_raw=true)"}
    if not raw and not storage_error:
        return {"text": None, "kind": "unsupported", "elements": [], "attachments": [],
                "quality": {"status": "metadata_only", "reason": "empty_body"},
                "search_text": "", "raw_body": access}
    try:
        if storage_error:
            raise ValueError("unsupported body storage")
        outer = wire(raw)
        segments = outer.get(40800, [])
        if not segments and any(k in outer for k in (45002, 45101, 45402)):
            segments = [raw]
        elements, attachments, searchable = [], [], []
        unsupported = False
        for segment in segments:
            if not isinstance(segment, bytes):
                raise ValueError("invalid message segment")
            fields = wire(segment)
            native_type = first(fields, 45002)
            kind = ELEMENT_KINDS.get(native_type, "unsupported")
            # Legacy rows can omit an explicit type while retaining text.
            if native_type is None and text_field(fields, 45101) is not None:
                kind = "text"
            element = {"kind": kind, "native_type": str(native_type) if native_type is not None else None,
                       "native_fields": native_fields(segment), "raw_sha256": digest(segment)}
            text = text_field(fields, 45101)
            if text is not None:
                element["text"] = text
            if kind in ("image", "file", "voice", "video", "online_file", "folder"):
                attachment = {"kind": kind, "name": text_field(fields, 45402),
                    "size_bytes": first(fields, 45405), "availability": "metadata_only",
                    "native_paths": [v for n in (45403, 45812, 45814) if (v := text_field(fields, n))],
                    "width": first(fields, 45411), "height": first(fields, 45412)}
                for n in (45406, 45503, 45504, 45802, 45803, 45804):
                    data = first(fields, n)
                    if isinstance(data, bytes):
                        attachment.setdefault("native_identifiers", {})[str(n)] = base64.b64encode(data).decode()
                attachments.append(attachment)
                element["attachment"] = attachment
            elif kind == "quote":
                element["target"] = {"local_id": str(first(fields, 47401)) if first(fields, 47401) is not None else None,
                    "sequence": str(first(fields, 47402)) if first(fields, 47402) is not None else None,
                    "timestamp": str(first(fields, 47403)) if first(fields, 47403) is not None else None,
                    "sender_uid": text_field(fields, 40020)}
            elif kind == "sticker":
                element["native_index"] = str(first(fields, 47601)) if first(fields, 47601) is not None else None
                caption = text_field(fields, 47602)
                if caption is not None:
                    element["caption"] = caption
            elif kind == "card":
                card = json_card(first(fields, 47901))
                if card is not None:
                    element["card"] = card
                    searchable.extend(strings_in(card))
                else:
                    element["card_decode_status"] = "raw_fields_available"
            elif kind == "forward":
                element["resource_id"] = text_field(fields, 48601)
                element["native_xml"] = text_field(fields, 48602)
                element["native_uuid"] = text_field(fields, 48603)
            elif kind == "call":
                element["description"] = text_field(fields, 48153)
                element["duration"] = first(fields, 48152)
            if kind == "unsupported":
                unsupported = True
            searchable.extend(e["text"] for e in element["native_fields"] if "text" in e)
            elements.append(element)
        kinds = set(e["kind"] for e in elements)
        kind = next(iter(kinds)) if len(kinds) == 1 else "mixed" if kinds else "unsupported"
        unsupported = unsupported or not elements
        text_complete = bool(elements) and kinds <= {"text"} and all("text" in e for e in elements)
        status = "unsupported" if unsupported else "complete" if text_complete else "metadata_only"
        text = "".join(e["text"] for e in elements if "text" in e) or None
        return {"text": text, "kind": kind, "elements": elements, "attachments": attachments,
                "quality": {"status": status, "reason": "unrecognized_element_type" if unsupported else None},
                "search_text": "\n".join(dict.fromkeys(([text] if text else []) + searchable)),
                "raw_body": access}
    except (ValueError, TypeError):
        return {"text": None, "kind": "unsupported", "elements": [], "attachments": [],
                "quality": {"status": "decode_error", "reason": "invalid_or_oversized_protobuf"},
                "search_text": "", "raw_body": access}


def links_in(text):
    return list(dict.fromkeys(u.rstrip(".,;)]}") for u in re.findall(r'https?://[^\s<>"\']+', text or "")))
