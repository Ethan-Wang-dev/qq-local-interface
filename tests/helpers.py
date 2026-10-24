"""Synthetic fixtures only; no real QQ databases or account identifiers."""
import ctypes
import ctypes.util
import hashlib
import hmac
import os
import sqlite3
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PASSPHRASE = "fixture-only-key!"
SALT = bytes(range(16))


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def field(number, value):
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint((number << 3) | 2) + varint(len(value)) + value


def text_body(text):
    return field(40800, field(45002, 1) + field(45101, text))


def favorite_fixture(directory, count=3, details=1):
    path = Path(directory) / 'collection.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE collection_list_info_table("180001" TEXT PRIMARY KEY,"180002" INTEGER,"180004" BLOB,"180008" INTEGER,"180009" INTEGER,"180010" INTEGER,"180011" INTEGER,"180012" INTEGER,"180015" BLOB)')
        c.execute('CREATE TABLE content_list_info_table("180700" TEXT PRIMARY KEY,"180720" INTEGER,"180701" BLOB,"180704" BLOB,"180708" BLOB)')
        author = field(180004, field(18504, 333) + field(18505, 'Fixture Group') + field(18506, 'u_peer') + field(180501, 222) + field(180503, 'Fixture Author'))
        for i in range(count):
            timestamp_ms = 1700000000123 + i
            body = field(180015, field(180657, field(181450, f'title {i}') + field(181452, f'Straße preview {i}')))
            c.execute('INSERT INTO collection_list_info_table VALUES(?,?,?,?,?,?,?,?,?)',
                (f'fixture-cid-{i}', 8, author, 2 if i == 0 else 1, timestamp_ms, timestamp_ms, timestamp_ms, timestamp_ms, body))
            if i < details:
                raw = f'<p>Complete body {i}</p><script>hidden code</script>'
                c.execute('INSERT INTO content_list_info_table VALUES(?,?,NULL,NULL,?)',
                    (f'fixture-cid-{i}', timestamp_ms, field(180708, field(181500, raw))))
    return path


def create_reserved_database(path, reserve=80, page_size=4096):
    """Ask the real SQLite C engine to create a nonzero-reserve database."""
    library = ctypes.CDLL(ctypes.util.find_library("sqlite3"))
    pointer = ctypes.c_void_p()
    library.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    library.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    library.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.sqlite3_close.argtypes = [ctypes.c_void_p]
    if library.sqlite3_open(os.fsencode(path), ctypes.byref(pointer)):
        raise RuntimeError("fixture SQLite open failed")
    try:
        if library.sqlite3_exec(pointer, f"PRAGMA page_size={page_size};".encode(), None, None, None):
            raise RuntimeError("fixture page size failed")
        value = ctypes.c_int(reserve)
        if library.sqlite3_file_control(pointer, None, 38, ctypes.byref(value)):
            raise RuntimeError("fixture reserve failed")
        sql = b'''CREATE TABLE c2c_msg_table("40001" INTEGER PRIMARY KEY,"40020" TEXT,"40021" TEXT,
          "40030" INTEGER,"40033" INTEGER,"40050" INTEGER,"40093" TEXT,"40800" BLOB);
        CREATE TABLE group_msg_table("40001" INTEGER PRIMARY KEY,"40020" TEXT,"40021" TEXT,
          "40030" INTEGER,"40033" INTEGER,"40050" INTEGER,"40093" TEXT,"40800" BLOB);'''
        if library.sqlite3_exec(pointer, sql, None, None, None):
            raise RuntimeError("fixture schema failed")
    finally:
        library.sqlite3_close(pointer)
    if Path(path).read_bytes()[20] != reserve:
        raise RuntimeError("SQLite fixture did not retain reserved bytes")


def fixture(directory, count=3, reserve=80):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "nt_msg.db"
    create_reserved_database(path, reserve)
    with sqlite3.connect(path) as connection:
        for i in range(count):
            connection.execute('INSERT INTO c2c_msg_table VALUES(?,?,?,?,?,?,?,?)',
                (i + 1, "u_peer" if i % 2 else "u_self", "u_peer", 222, 222 if i % 2 else 111,
                 1700000000 + i, "Fixture Peer" if i % 2 else "Fixture Self", text_body(f"test message {i}")))
        connection.execute('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?,?,?)',
            (9007199254740993, "u_peer", "333", 333, 222, 1700000010, "Fixture Peer", text_body("group text")))
    with sqlite3.connect(directory / "profile_info.db") as connection:
        connection.execute('CREATE TABLE buddy_list("1000" TEXT,"1002" INTEGER,"20002" TEXT)')
        connection.execute('INSERT INTO buddy_list VALUES(?,?,?)', ("u_peer", 222, "Fixture Peer"))
    return path


def encode_page(plain, number, salt=SALT, passphrase=PASSPHRASE):
    """Fixture encoder independent of the production decrypt/verify classes."""
    encryption_key = hashlib.pbkdf2_hmac("sha512", passphrase.encode(), salt, 4000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", encryption_key, bytes(v ^ 58 for v in salt), 2, 32)
    iv = hashlib.sha256(b"fixture iv" + struct.pack("<I", number)).digest()[:16]
    start = 16 if number == 1 else 0
    enc = Cipher(algorithms.AES(encryption_key), modes.CBC(iv)).encryptor()
    body = enc.update(plain[start:4048]) + enc.finalize()
    mac = hmac.new(mac_key, body + iv + struct.pack("<I", number), hashlib.sha1).digest()
    return (salt if number == 1 else b"") + body + iv + mac + bytes(12)


def encrypt_file(source, output):
    with open(source, "rb") as src, open(output, "wb") as dst:
        dst.write(bytes(1024))
        number = 1
        while page := src.read(4096):
            dst.write(encode_page(page, number))
            number += 1


def independent_checksum(data, seed=(0, 0), endian="<"):
    a, b = seed
    for x, y in struct.iter_unpack(endian + "II", data):
        a = (a + x + b) % 2 ** 32
        b = (b + y + a) % 2 ** 32
    return a, b


def encrypt_wal(source, output):
    """Retain real SQLite WAL transactions, re-encrypt pages, recompute checksums."""
    with open(source, "rb") as src, open(output, "wb") as dst:
        header = src.read(32)
        endian = "<" if struct.unpack(">I", header[:4])[0] == 0x377F0682 else ">"
        checksum = independent_checksum(header[:24], endian=endian)
        dst.write(header[:24] + struct.pack(">II", *checksum))
        while frame := src.read(24):
            plain = src.read(4096)
            page_number = struct.unpack(">I", frame[:4])[0]
            page = encode_page(plain, page_number)
            checksum = independent_checksum(frame[:8] + page, checksum, endian)
            dst.write(frame[:16] + struct.pack(">II", *checksum) + page)
