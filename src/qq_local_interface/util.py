from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import QQError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    if not isinstance(value, bytes):
        value = canonical(value).encode()
    return hashlib.sha256(value).hexdigest()


def opaque(source_id, kind, identity):
    return kind + "_" + digest([source_id, kind, str(identity)])[:32]


def validate_source_id(source_id):
    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", source_id):
        raise QQError("invalid_argument", "source_id must contain 1–64 letters, digits, dots, underscores or hyphens")
    return source_id


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    try:
        number = int(value)
        if number <= 0:
            return None
        return datetime.fromtimestamp(number, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def time_bound(value):
    if value is None:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        # Python 3.10 accepts only 3/6 fractional digits; normalize RFC 3339
        # fractions so a one-digit boundary retains its actual instant.
        normalized = re.sub(r"\.(\d{1,6})(?=[+-]\d{2}:\d{2}$)",
                            lambda match: "." + match.group(1).ljust(6, "0"), normalized)
        result = datetime.fromisoformat(normalized)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, AttributeError):
        raise QQError("invalid_argument", "time bounds must be ISO 8601 strings with a timezone") from None


def private_dir(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise QQError("invalid_path", "private directories must not be symbolic links")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise QQError("invalid_path", "expected a directory")
    os.chmod(path, 0o700)
    return path


def regular_file(path):
    path = Path(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise QQError("not_found", "required local file is missing") from None
    if not stat.S_ISREG(mode):
        raise QQError("invalid_path", "expected a regular file, not a symbolic link")
    return path


def atomic_json(path, value):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path):
    try:
        return json.loads(regular_file(path).read_text())
    except json.JSONDecodeError:
        raise QQError("invalid_data", "invalid local JSON file") from None


def file_digest(path):
    h = hashlib.sha256()
    with regular_file(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def readonly(path):
    path = regular_file(path).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.create_function("qq_casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
    try:
        yield connection
    finally:
        connection.close()


def cursor_encode(value):
    return base64.urlsafe_b64encode(canonical(value).encode()).decode().rstrip("=")


def cursor_decode(value):
    try:
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise QQError("invalid_cursor", "invalid cursor") from None


def limit_value(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5000:
        raise QQError("invalid_argument", "limit must be an integer between 1 and 5000")
    return value
