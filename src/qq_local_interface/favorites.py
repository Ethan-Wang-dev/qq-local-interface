"""QQ collection semantics, preserving the distinction between previews and detail.

Field numbers are observed protocol facts; see docs/favorites.md for sources.
No QQ client code or third-party generated schema is included here.
"""
from __future__ import annotations

import base64
import json
import struct
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from . import SCHEMA_VERSION
from .protobuf import MAX_BYTES, wire, wire_fields
from .util import digest, opaque, readonly

KINDS = {1: 'text', 2: 'link', 3: 'gallery', 4: 'audio', 5: 'video', 6: 'file', 7: 'location', 8: 'rich_media'}


def scalar(fields, number):
    return fields.get(number, [None])[0]


def text(fields, number):
    value = scalar(fields, number)
    if not isinstance(value, bytes):
        return None
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        return None


def integer(fields, number):
    value = scalar(fields, number)
    return str(value) if isinstance(value, int) else None


def message(raw):
    return wire(raw, allow_padding=True) if isinstance(raw, bytes) else {}


def envelope(raw, number):
    fields = message(raw)
    value = scalar(fields, number)
    return message(value) if isinstance(value, bytes) else fields


def nested(fields, number):
    return message(scalar(fields, number))


def milliseconds(value):
    try:
        value = int(value)
        if value <= 0:
            return None
        result = datetime.fromtimestamp(value // 1000, timezone.utc) + timedelta(milliseconds=value % 1000)
        return result.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def image(fields):
    return {'name': text(fields, 180553), 'url': text(fields, 180550), 'local_path': text(fields, 180561),
        'width': scalar(fields, 180555), 'height': scalar(fields, 180556),
        'size_bytes': integer(fields, 180557), 'availability': 'metadata_only'}


def file_info(fields):
    return {'name': text(fields, 180604), 'local_path': text(fields, 180610),
        'size_bytes': integer(fields, 180605), 'native_file_id': text(fields, 180603), 'availability': 'metadata_only'}


def preview(raw, depth=0, budget=None):
    """Bounded generic field tree; original bytes stay in the lossless raw API."""
    budget = [2048] if budget is None else budget
    if depth > 8 or not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        return []
    try:
        fields = wire_fields(raw, allow_padding=True)
    except ValueError:
        return []
    result = []
    for number, kind, value in fields:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        item = {'number': number, 'wire_type': kind}
        if kind == 0:
            item['value'] = str(value)
        else:
            item.update(length=len(value), sha256=digest(value))
            if len(value) <= 4096:
                item.update(encoding='base64', data=base64.b64encode(value).decode())
            else:
                item.update(encoding='raw_reference', access='favorites.get(include_raw=true)')
            if kind == 2:
                try:
                    decoded = value.decode()
                    if all(ord(c) >= 32 or c in '\t\r\n' for c in decoded):
                        item['text'] = decoded
                    else:
                        item['fields'] = preview(value, depth + 1, budget)
                except UnicodeDecodeError:
                    item['fields'] = preview(value, depth + 1, budget)
        result.append(item)
    return result


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def body_content(raw_data):
    if not isinstance(raw_data, str) or len(raw_data.encode()) > MAX_BYTES:
        return None
    result = {'raw_data': raw_data, 'sha256': digest(raw_data.encode()), 'format': 'text', 'text': raw_data}
    if raw_data.lstrip().startswith('<'):
        parser = VisibleText()
        parser.feed(raw_data)
        result.update(format='html_or_xml', text=''.join(parser.parts).strip())
    elif raw_data.lstrip().startswith(('{', '[')):
        # Preserve the original JSON exactly; format recognition is not a claim
        # that every proprietary rich-text node has been rendered.
        result.update(format='json', text=None)
    return result


def summary(fields, content_type):
    part = nested(fields, 180649 + content_type)
    result = {'title': None, 'text': None, 'url': None, 'attachments': []}
    if content_type == 1:
        result['text'] = text(part, 180750)
    elif content_type == 2:
        result.update(title=text(part, 180851), text=text(part, 180853), url=text(part, 180850))
    elif content_type == 3:
        result['attachments'] = [image(message(v)) for v in part.get(180950, [])]
    elif content_type == 4:
        result.update(text=text(part, 181051), duration_ms=integer(part, 181050))
    elif content_type == 5:
        result.update(title=text(part, 181150), duration_seconds=integer(part, 181151))
        for field, factory in ((181155, image), (181157, file_info)):
            if scalar(part, field) is not None:
                result['attachments'].append(factory(nested(part, field)))
    elif content_type == 6:
        result['attachments'] = [file_info(nested(part, field)) for field in (181250, 181251) if scalar(part, field) is not None]
    elif content_type == 7:
        result.update(title=text(part, 181350), text=text(part, 181354), note=text(part, 181355))
        for field, name in ((181351, 'latitude'), (181352, 'longitude')):
            value = scalar(part, field)
            if isinstance(value, bytes) and len(value) in (4, 8):
                result[name] = struct.unpack('<f' if len(value) == 4 else '<d', value)[0].hex()
    elif content_type == 8:
        result.update(title=text(part, 181450), text=text(part, 181452), url=text(part, 181455),
                      subtitle=text(part, 181451), publisher=text(part, 181456))
        result['attachments'] = [image(message(v)) for v in part.get(181453, [])]
    return result


def normalize(row, source_id, detail=None, extra=None):
    row = dict(row)
    sid = str(row['180001'])
    kind = int(row.get('180002') or 0)
    errors = []
    try:
        author = envelope(row.get('180004'), 180004)
        fields = envelope(row.get('180015'), 180015)
        decoded = summary(fields, kind)
    except (ValueError, TypeError, RecursionError):
        author, decoded = {}, {'title': None, 'text': None, 'url': None, 'attachments': []}
        errors.append('invalid_summary_protobuf')
    item = {'schema_version': SCHEMA_VERSION, 'source_id': source_id, 'resource_type': 'favorite',
        'id': opaque(source_id, 'favorite', sid), 'native_id': sid, 'kind': KINDS.get(kind, 'unknown'),
        'native_type': str(kind), 'native_category': str(row.get('180008')) if row.get('180008') is not None else None,
        'created_at': milliseconds(row.get('180009')), 'collected_at': milliseconds(row.get('180010')),
        'modified_at': milliseconds(row.get('180012')), 'title': decoded['title'], 'summary_text': decoded['text'],
        'url': decoded['url'], 'attachments': decoded['attachments'],
        'author': {'uid': text(author, 18506), 'qq_number': integer(author, 180501) or integer(author, 18501),
                   'display_name': text(author, 180503), 'group_id': integer(author, 18504), 'group_name': text(author, 18505)},
        'detail': {'state': 'not_cached', 'content': None, 'source': None},
        'native_fields': {'author': preview(row.get('180004')), 'summary': preview(row.get('180015'))},
        'provenance': {'database': 'collection.db', 'table': 'collection_list_info_table', 'native_id_column': '180001'},
        'warnings': errors}
    if detail is not None:
        detail = dict(detail)
        try:
            raw_data = None
            for column, field in (('180701', 180800), ('180704', 181100), ('180708', 181500)):
                value = detail.get(column)
                if value:
                    content = envelope(value, int(column))
                    raw_data = text(content, field)
                    item['native_fields'].setdefault('detail', {})[column] = preview(value)
                    if raw_data is not None:
                        break
            body = body_content(raw_data)
            stale = bool(detail.get('180720') and row.get('180011') and int(detail['180720']) < int(row['180011']))
            item['detail'] = {'state': 'stale' if stale else 'cached' if body is not None else 'raw_only',
                'content': body, 'source': 'collection.db', 'record_time_ms': str(detail.get('180720') or 0)}
        except (ValueError, TypeError, RecursionError):
            item['detail']['state'] = 'decode_error'
    if extra and isinstance(extra.get('raw_data'), str):
        body = body_content(extra['raw_data'])
        if body is not None:
            item['detail'] = {'state': 'cached', 'source': 'weiyun_authenticated_capture', 'content': body,
                'fetched_at': extra.get('fetched_at'), 'raw_response_sha256': extra.get('raw_response_sha256')}
    item['revision'] = digest(item)
    return item


def load_favorites(data_dir, source_id, extra_details=None):
    path = data_dir / 'collection.db'
    if not path.exists() or path.stat().st_size <= 1024:
        return []
    with readonly(path) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'collection_list_info_table' not in tables:
            return []
        details = {str(r['180700']): r for r in connection.execute('SELECT * FROM content_list_info_table')} if 'content_list_info_table' in tables else {}
        return [normalize(row, source_id, details.get(str(row['180001'])), (extra_details or {}).get(str(row['180001'])))
                for row in connection.execute('SELECT * FROM collection_list_info_table')]
