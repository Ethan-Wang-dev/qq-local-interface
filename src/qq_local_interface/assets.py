"""Account-local cache inventory and bounded, version-checked binary access."""
from __future__ import annotations

import base64
import ctypes
import json
import mimetypes
import os
import shutil
import stat
import sys
from pathlib import Path

from .errors import QQError
from .util import cursor_decode, cursor_encode, digest, limit_value, opaque


def file_signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def media_type(path, prefix):
    if prefix.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image', 'image/png'
    if prefix.startswith(b'\xff\xd8\xff'):
        return 'image', 'image/jpeg'
    if prefix.startswith((b'GIF87a', b'GIF89a')):
        return 'image', 'image/gif'
    if prefix.startswith(b'RIFF') and prefix[8:12] == b'WEBP':
        return 'image', 'image/webp'
    if prefix[4:8] == b'ftyp':
        return 'video', 'video/mp4'
    if prefix.startswith((b'#!AMR', b'\x02#!SILK', b'#!SILK')):
        return 'voice', 'audio/amr' if prefix.startswith(b'#!AMR') else 'audio/silk'
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    kind = 'image' if mime.startswith('image/') else 'video' if mime.startswith('video/') else 'voice' if mime.startswith('audio/') else 'file'
    return kind, mime


def inventory_assets(account_root, source_id):
    entries, roots, skipped = [], [], 0
    account_root = Path(account_root)
    for name in ('nt_data', 'nt_temp'):
        root = account_root / name
        if not root.is_dir() or root.is_symlink():
            continue
        root = root.resolve()
        roots.append(str(root))
        for directory, subdirs, files in os.walk(root, followlinks=False):
            kept = [d for d in subdirs if not (Path(directory) / d).is_symlink()]
            skipped += len(subdirs) - len(kept)
            subdirs[:] = kept
            for filename in files:
                path = Path(directory) / filename
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        skipped += 1
                        continue
                    with path.open('rb') as stream:
                        prefix = stream.read(64)
                    relative = name + '/' + path.relative_to(root).as_posix()
                    kind, mime = media_type(path, prefix)
                    item = {'id': opaque(source_id, 'asset', relative), 'source_id': source_id,
                            'relative_path': relative, 'name': filename, 'kind': kind,
                            'mime_type': mime, 'size_bytes': info.st_size,
                            'local_path': str(path), 'signature': file_signature(info)}
                    item['revision'] = digest(item)
                    entries.append(item)
                except (FileNotFoundError, PermissionError, OSError):
                    skipped += 1
    entries.sort(key=lambda item: item['id'])
    return entries, {'roots': roots, 'skipped_entries': skipped,
                     'fingerprint': digest([(i['id'], i['revision']) for i in entries])}


def snapshot_assets(entries, destination):
    """Detach cached bytes too; APFS clonefile avoids copying unchanged extents."""
    destination = Path(destination)
    directory = destination / 'assets'
    directory.mkdir(mode=0o700)
    clone = None
    if sys.platform == 'darwin':
        library = ctypes.CDLL(None, use_errno=True)
        clone = getattr(library, 'clonefile', None)
        if clone:
            clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
            clone.restype = ctypes.c_int
    for item in entries:
        source = Path(item['local_path'])
        target = directory / item['id']
        for attempt in range(3):
            target.unlink(missing_ok=True)
            try:
                before = source.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise QQError('source_busy', 'cache entry is no longer a regular file')
                if clone is None or clone(os.fsencode(source), os.fsencode(target), 0) != 0:
                    shutil.copyfile(source, target, follow_symlinks=False)
                if file_signature(source.lstat()) != file_signature(before):
                    raise QQError('source_busy', 'QQ cache changed during copy; retry sync')
                with target.open('rb') as stream:
                    kind, mime = media_type(source, stream.read(64))
                item.update(signature=file_signature(before), size_bytes=before.st_size, kind=kind, mime_type=mime)
                item['revision'] = digest({k: v for k, v in item.items() if k != 'revision'})
                os.chmod(target, 0o400)
                item['snapshot_path'] = 'assets/' + item['id']
                item['origin_signature'] = item['signature']
                item['signature'] = file_signature(target.stat())
                break
            except OSError:
                target.unlink(missing_ok=True)
                if attempt == 2:
                    raise QQError('source_busy', 'QQ cache disappeared during capture; retry sync') from None
            except QQError:
                target.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                # Retry this entry using a freshly observed source signature.
    return digest([(i['id'], i['revision']) for i in entries])


def asset_path(item, source=None):
    return source.root / item['snapshot_path'] if source is not None and item.get('snapshot_path') else Path(item['local_path'])


def availability(item, source=None):
    try:
        info = asset_path(item, source).lstat()
        if not stat.S_ISREG(info.st_mode):
            return 'unavailable'
        return 'available' if file_signature(info) == item['signature'] else 'changed'
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unavailable'


def list_assets(source, *, kinds=None, query=None, limit=100, cursor=None):
    limit_value(limit)
    source._string_list(kinds, 'kinds')
    if query is not None and not isinstance(query, str):
        raise QQError('invalid_argument', 'query must be a string')
    where, params = ['1'], []
    if kinds:
        where.append('kind IN (' + ','.join('?' for _ in kinds) + ')')
        params.extend(kinds)
    if query:
        where.append('instr(qq_casefold(relative_path),qq_casefold(?))>0')
        params.append(query)
    filters = digest({'kinds': kinds, 'query': query})
    if cursor:
        value = cursor_decode(cursor)
        expected = {'kind': 'assets', 'source_id': source.source_id,
                    'snapshot_id': source.snapshot_id, 'filters': filters}
        if any(value.get(k) != v for k, v in expected.items()) or not isinstance(value.get('after'), str):
            raise QQError('invalid_cursor', 'asset cursor belongs to another snapshot or filter')
        where.append('id>?')
        params.append(value['after'])
    if not source._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'"):
        return {'items': [], 'next_cursor': None, 'available': False, 'reason': 'refresh_required'}
    rows = source._query('SELECT id,payload FROM assets WHERE ' + ' AND '.join(where) + ' ORDER BY id LIMIT ?', (*params, limit + 1))
    items = []
    for row in rows[:limit]:
        item = json.loads(row[1])
        item['availability'] = availability(item, source)
        item.pop('signature')
        items.append(item)
    token = None
    if len(rows) > limit:
        token = cursor_encode({'kind': 'assets', 'source_id': source.source_id,
            'snapshot_id': source.snapshot_id, 'filters': filters, 'after': rows[limit - 1][0]})
    return {'items': items, 'next_cursor': token, 'snapshot_id': source.snapshot_id, 'available': True}


def read_asset(source, asset_id, offset=0, length=65536):
    if not isinstance(asset_id, str) or not asset_id:
        raise QQError('invalid_argument', 'asset_id must be a non-empty string')
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 1048576:
        raise QQError('invalid_argument', 'asset offset must be nonnegative and length 1–1048576')
    rows = source._query('SELECT payload FROM assets WHERE id=?', (asset_id,))
    if not rows:
        raise QQError('not_found', 'local asset is not in this snapshot')
    item = json.loads(rows[0][0])
    path = asset_path(item, source)
    roots = ([source.root / 'assets'] if item.get('snapshot_path') else
             [Path(root) for root in source.manifest.get('assets', {}).get('roots', [])])
    roots = [root.resolve() for root in roots]
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise QQError('asset_missing', 'local cached asset no longer exists') from None
    if path.is_symlink() or not any(root == resolved or root in resolved.parents for root in roots):
        raise QQError('invalid_path', 'asset path is outside the selected account cache')
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        raise QQError('asset_missing', 'local cached asset no longer exists') from None
    except OSError:
        raise QQError('asset_unavailable', 'local cached asset cannot be opened') from None
    try:
        before = os.fstat(fd)
        if file_signature(before) != item['signature']:
            raise QQError('source_changed', 'asset changed since this snapshot; refresh before reading')
        if offset > before.st_size:
            raise QQError('invalid_argument', 'offset exceeds asset size')
        os.lseek(fd, offset, os.SEEK_SET)
        data = os.read(fd, length)
        if file_signature(os.fstat(fd)) != item['signature']:
            raise QQError('source_changed', 'asset changed during read')
    finally:
        os.close(fd)
    return {'asset_id': asset_id, 'encoding': 'base64', 'data': base64.b64encode(data).decode(),
            'offset': offset, 'length': len(data), 'total_bytes': before.st_size,
            'chunk_sha256': digest(data), 'mime_type': item['mime_type'],
            'next_offset': offset + len(data) if offset + len(data) < before.st_size else None}
