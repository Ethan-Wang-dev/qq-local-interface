"""QQ's SQLCipher page profile, including authenticated committed WAL frames.

This implementation reads detached copies only. It preserves SQLite's reserved
page bytes; clearing byte 20 would change b-tree usable sizes and corrupt reads.
Format references are documented in docs/sources.md.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import QQError
from .util import regular_file

PAGE_SIZE = 4096
HEADER_SIZE = 1024
RESERVED = 48
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(repr=False)
class DatabaseKey:
    kind: str
    value: bytes = field(repr=False)

    @classmethod
    def from_record(cls, record):
        try:
            kind = record["kind"]
            value = record["value"]
            if kind == "passphrase" and isinstance(value, str) and value:
                return cls(kind, value.encode("utf-8"))
            if kind == "raw" and isinstance(value, str):
                data = bytes.fromhex(value)
                if len(data) == 32:
                    return cls(kind, data)
        except (ValueError, TypeError, KeyError):
            pass
        raise QQError("invalid_key", "key record must contain a passphrase or 32-byte raw key")


class PageCodec:
    def __init__(self, key, salt, hmac_algorithm="sha1", kdf_algorithm="sha512", iterations=4000, page_size=4096):
        if len(salt) != 16:
            raise QQError("invalid_database", "invalid SQLCipher salt length")
        self.encryption_key = key.value if key.kind == "raw" else hashlib.pbkdf2_hmac(kdf_algorithm, key.value, salt, iterations, 32)
        masked = bytes(x ^ 0x3A for x in salt)
        self.hmac_key = hashlib.pbkdf2_hmac(kdf_algorithm, self.encryption_key, masked, 2, 32)
        self.kdf_algorithm = kdf_algorithm
        self.iterations = iterations
        self.page_size = page_size
        if hmac_algorithm not in ("sha1", "sha256", "sha512"):
            raise QQError("unsupported_cipher", "unsupported page HMAC algorithm")
        self.hmac_algorithm = hmac_algorithm
        self.mac_size = hashlib.new(hmac_algorithm).digest_size
        self.reserved = ((16 + self.mac_size + 15) // 16) * 16

    def verify(self, page, page_number):
        if len(page) != self.page_size:
            return False
        start = 16 if page_number == 1 else 0
        end = self.page_size - self.reserved
        expected = hmac.new(self.hmac_key, page[start:end + 16] + struct.pack("<I", page_number), self.hmac_algorithm).digest()
        return hmac.compare_digest(expected, page[end + 16:end + 16 + self.mac_size])

    def decrypt(self, page, page_number):
        if not self.verify(page, page_number):
            raise QQError("authentication_failed", "database page authentication failed", {"page": page_number})
        start = 16 if page_number == 1 else 0
        end = self.page_size - self.reserved
        dec = Cipher(algorithms.AES(self.encryption_key), modes.CBC(page[end:end + 16])).decryptor()
        plain = dec.update(page[start:end]) + dec.finalize()
        result = bytearray((SQLITE_MAGIC if start else b"") + plain + bytes(self.reserved))
        if page_number == 1:
            if int.from_bytes(result[16:18], 'big') != self.page_size or result[20] < self.reserved:
                raise QQError("unsupported_cipher", "authenticated page has an unsupported SQLite page layout")
            result[18:20] = b"\x01\x01"  # A detached snapshot has no active WAL.
        return bytes(result)


def first_page(path):
    with regular_file(path).open("rb") as stream:
        prefix = stream.read(HEADER_SIZE)
        if prefix.startswith(SQLITE_MAGIC):
            return None
        page = stream.read(PAGE_SIZE)
    if len(page) < 512:
        raise QQError("invalid_database", "QQ database must include its 1024-byte header and a complete page")
    return page


def verify_key(path, key):
    page = first_page(path)
    return page is not None and detect_codec(key, page) is not None


def detect_codec(key, page):
    for size in (4096, 1024, 2048, 512):
        if len(page) < size:
            continue
        for kdf in ("sha512", "sha1", "sha256"):
            for algorithm in ("sha1", "sha256", "sha512"):
                candidate = PageCodec(key, page[:16], algorithm, kdf, page_size=size)
                if candidate.verify(page[:size], 1):
                    return candidate
    return None


def guild_key(path, uin):
    """Documented QQ guild derivation; callers must still authenticate a page."""
    with regular_file(path).open("rb") as stream:
        header = stream.read(16)
    user = str(uin).encode()
    seed = hashlib.md5((hashlib.md5(user).hexdigest() + hashlib.md5(hashlib.sha1(user).digest()).hexdigest()).encode()).hexdigest()
    value = hashlib.md5((hashlib.md5(header).hexdigest() + seed).encode()).hexdigest()
    return DatabaseKey("passphrase", value.encode())


def wal_checksum(data, previous=(0, 0), endian="<"):
    if len(data) % 8:
        raise QQError("invalid_wal", "WAL checksum input is not aligned")
    values = struct.unpack(endian + "I" * (len(data) // 4), data)
    a, b = previous
    for i in range(0, len(values), 2):
        a = (a + values[i] + b) & 0xFFFFFFFF
        b = (b + values[i + 1] + a) & 0xFFFFFFFF
    return a, b


def wal_plan(path, expected_page_size):
    """Index only the last valid committed transaction; ignore uncommitted tail."""
    if path is None or not Path(path).exists() or Path(path).stat().st_size == 0:
        return {"offsets": {}, "database_pages": None, "committed_frames": 0, "ignored_tail": False}
    with regular_file(path).open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise QQError("invalid_wal", "incomplete WAL header")
        magic, version, size = struct.unpack(">III", header[:12])
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or size != expected_page_size:
            raise QQError("invalid_wal", "unsupported WAL header or page size")
        endian = "<" if magic == 0x377F0682 else ">"
        checksum = wal_checksum(header[:24], endian=endian)
        if checksum != struct.unpack(">II", header[24:32]):
            raise QQError("invalid_wal", "WAL header checksum failed")
        pending, committed = {}, {}
        count = last_commit = 0
        pages = None
        ignored = False
        while True:
            frame = stream.read(24)
            if not frame:
                break
            payload_offset = stream.tell()
            payload = stream.read(size)
            if len(frame) != 24 or len(payload) != size or frame[8:16] != header[16:24]:
                ignored = True
                break
            page_number, database_pages = struct.unpack(">II", frame[:8])
            candidate = wal_checksum(frame[:8] + payload, checksum, endian)
            if page_number == 0 or candidate != struct.unpack(">II", frame[16:24]):
                ignored = True
                break
            checksum = candidate
            count += 1
            pending[page_number] = payload_offset
            if database_pages:
                committed = dict(pending)
                pages, last_commit = database_pages, count
        return {"offsets": committed, "database_pages": pages, "committed_frames": last_commit,
                "ignored_tail": ignored or count != last_commit}


def decrypt_database(source, output, key=None, wal=None):
    source, output = regular_file(source), Path(output)
    if output.exists():
        raise QQError("invalid_path", "decryption output must be a new file")
    page = first_page(source)
    encrypted = page is not None
    if encrypted and key is None:
        raise QQError("key_required", "a matching database key is required")
    codec = None
    if encrypted and key is not None:
        codec = detect_codec(key, page)
        if codec is None:
            raise QQError("authentication_failed", "database key did not authenticate its first page")
    if encrypted:
        size, offset = codec.page_size, HEADER_SIZE
        if (source.stat().st_size - offset) % size:
            raise QQError("invalid_database", "encrypted database contains a partial page")
    else:
        with source.open("rb") as stream:
            head = stream.read(100)
        size = int.from_bytes(head[16:18], "big")
        size = 65536 if size == 1 else size
        if size < 512 or size > 65536 or size & (size - 1):
            raise QQError("invalid_database", "invalid SQLite page size")
        offset = 0
    plan = wal_plan(wal, size)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
            src.seek(offset)
            number = 0
            while raw := src.read(size):
                number += 1
                if len(raw) != size:
                    raise QQError("invalid_database", "partial database page")
                dst.write(codec.decrypt(raw, number) if codec else raw)
            if wal and plan["offsets"]:
                with regular_file(wal).open("rb") as log:
                    for page_number, position in sorted(plan["offsets"].items()):
                        if page_number > plan["database_pages"]:
                            continue
                        log.seek(position)
                        raw = log.read(size)
                        dst.seek((page_number - 1) * size)
                        dst.write(codec.decrypt(raw, page_number) if codec else raw)
                dst.truncate(plan["database_pages"] * size)
            dst.flush()
            os.fsync(dst.fileno())
        # Standard SQLite must see a detached rollback-mode file, with no sidecars.
        with output.open("r+b") as stream:
            stream.seek(18)
            stream.write(b"\x01\x01")
        conn = sqlite3.connect(output.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            result = conn.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise QQError("integrity_failed", "decrypted database failed SQLite quick_check")
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        output.unlink(missing_ok=True)
        raise QQError('integrity_failed', 'decrypted database cannot be read by SQLite') from None
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {"encrypted": encrypted, "cipher_hmac": codec.hmac_algorithm if codec else None,
            "cipher_kdf": codec.kdf_algorithm if codec else None,
            "pages": output.stat().st_size // size,
            "wal_frames": plan["committed_frames"], "wal_ignored_tail": plan["ignored_tail"]}
