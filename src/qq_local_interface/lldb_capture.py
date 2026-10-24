"""LLDB command module. Keeps only authenticated keys, never a heap dump.

Loaded inside Apple's LLDB Python, so dependencies are Python's standard library.
The caller creates the request directory privately before this command runs.
"""
import base64
import collections
import hashlib
import hmac
import json
import os
import re
import shlex
import struct
import time

try:
    import lldb
except ImportError:  # The HMAC helper is also checked by the normal test suite.
    lldb = None


def verified(page, encryption_key):
    salt = bytes(v ^ 0x3A for v in page[:16])
    for kdf in ("sha512", "sha1", "sha256"):
        mac_key = hashlib.pbkdf2_hmac(kdf, encryption_key, salt, 2, 32)
        for page_size in (4096, 1024, 2048, 512):
            if len(page) < page_size:
                continue
            for algorithm, reserved, size in (("sha1", 48, 20), ("sha256", 48, 32), ("sha512", 80, 64)):
                end = page_size - reserved
                actual = hmac.new(mac_key, page[16:end + 16] + struct.pack("<I", 1), algorithm).digest()
                if hmac.compare_digest(actual, page[end + 16:end + 16 + size]):
                    return True
    return False


def probe_context(read_memory, context, page, deadline, checked, visited):
    """Follow a bounded codec pointer graph; only return a page-authenticated key."""
    queue = [(context, 0)]
    while queue and time.monotonic() < deadline:
        data, depth = queue.pop()
        for offset in range(0, max(0, len(data) - 31), 8):
            candidate = data[offset:offset + 32]
            if candidate not in checked:
                checked.add(candidate)
                if verified(page, candidate):
                    return candidate
        if depth >= 2:
            continue
        for offset in range(0, max(0, len(data) - 7), 8):
            pointer = struct.unpack_from('<Q', data, offset)[0]
            if pointer < 4096 or pointer >= 2 ** 48 or pointer in visited:
                continue
            visited.add(pointer)
            pointed = read_memory(pointer, 128)
            if pointed:
                queue.append((pointed, depth + 1))
    return None


def capture(debugger, command, result, internal_dict):
    request_file = shlex.split(command)[0]
    with open(request_file) as stream:
        request = json.load(stream)
    process = debugger.GetSelectedTarget().GetProcess()
    targets = {t["salt"]: base64.b64decode(t["page"]) for t in request["targets"]}
    found = {}
    checked_raw_keys = set()
    salt_addresses = {salt: set() for salt in targets}
    counts = collections.Counter()
    raw_pattern = re.compile(rb"[xX]'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")
    text_pattern = re.compile(rb"(?<![\x20-\x7e])[\x20-\x7e]{16}(?![\x20-\x7e])")
    hex_pattern = re.compile(rb"(?<![a-fA-F0-9])(?:[a-fA-F0-9]{64}|[a-fA-F0-9]{32})(?![a-fA-F0-9])")
    started = time.monotonic()
    deadline = started + 80
    scan_deadline = started + 30
    address, scanned = 0, 0
    region = lldb.SBMemoryRegionInfo()
    try:
        while time.monotonic() < scan_deadline and scanned < 2 * 1024 ** 3:
            if process.GetMemoryRegionInfo(address, region).Fail():
                break
            base, end = region.GetRegionBase(), region.GetRegionEnd()
            if end <= address:
                break
            if region.IsReadable() and region.IsWritable():
                offset, overlap = base, b""
                while offset < end and time.monotonic() < scan_deadline and scanned < 2 * 1024 ** 3:
                    amount = min(1024 * 1024, end - offset)
                    error = lldb.SBError()
                    data = process.ReadMemory(offset, amount, error)
                    if data:
                        chunk = overlap + data
                        for match in raw_pattern.finditer(chunk):
                            salt = match.group(2).decode().lower()
                            key = bytes.fromhex(match.group(1).decode())
                            if salt in targets and salt not in found and verified(targets[salt], key):
                                found[salt] = {"kind": "raw", "value": key.hex()}
                        # Some QQ libraries keep binary codec keys without an
                        # SQL x'key+salt' string. Inspect only bounded windows
                        # around the selected database salt, authenticating
                        # every candidate before keeping it.
                        for salt, page in targets.items():
                            if salt in found:
                                continue
                            patterns = [page[:16], bytes(v ^ 0x3A for v in page[:16])]
                            for pattern in patterns:
                                position = chunk.find(pattern)
                                while position >= 0 and time.monotonic() < deadline:
                                    salt_addresses[salt].add(offset - len(overlap) + position)
                                    left = max(0, position - 128)
                                    right = min(len(chunk) - 32, position + 128)
                                    for start in range(left, right + 1, 8):
                                        key = chunk[start:start + 32]
                                        if key in checked_raw_keys:
                                            continue
                                        checked_raw_keys.add(key)
                                        if verified(page, key):
                                            found[salt] = {"kind": "raw", "value": key.hex()}
                                            break
                                        if time.monotonic() >= deadline:
                                            break
                                    if salt in found:
                                        break
                                    position = chunk.find(pattern, position + 1)
                                if salt in found:
                                    break
                        counts.update(text_pattern.findall(chunk))
                        counts.update(hex_pattern.findall(chunk))
                        overlap = chunk[-2048:]
                        scanned += len(data)
                    offset += amount
                    if len(found) == len(targets):
                        break
            address = end
            if len(found) == len(targets):
                break
        # Raw salt allocations can be far from their cipher key allocations.
        # Find references to those allocations, then inspect bounded pointer
        # neighborhoods in codec contexts. No functions execute in QQ.
        address = 0
        visited = set()
        def read_memory(pointer, amount):
            return process.ReadMemory(pointer, amount, lldb.SBError())
        while len(found) < len(targets) and time.monotonic() < deadline - 10:
            if process.GetMemoryRegionInfo(address, region).Fail():
                break
            base, end = region.GetRegionBase(), region.GetRegionEnd()
            if end <= address:
                break
            if region.IsReadable() and region.IsWritable():
                offset, overlap = base, b''
                while offset < end and time.monotonic() < deadline - 10:
                    amount = min(1024 * 1024, end - offset)
                    data = read_memory(offset, amount)
                    if data:
                        chunk = overlap + data
                        for salt, addresses in salt_addresses.items():
                            if salt in found:
                                continue
                            for pointer in addresses:
                                pattern = struct.pack('<Q', pointer)
                                position = chunk.find(pattern)
                                while position >= 0 and time.monotonic() < deadline - 10:
                                    left = max(0, position - 256)
                                    context = chunk[left:min(len(chunk), position + 256)]
                                    key = probe_context(read_memory, context, targets[salt], deadline - 10, checked_raw_keys, visited)
                                    if key:
                                        found[salt] = {'kind': 'raw', 'value': key.hex()}
                                        break
                                    position = chunk.find(pattern, position + 1)
                                if salt in found:
                                    break
                        overlap = chunk[-512:]
                    offset += amount
                    if len(found) == len(targets):
                        break
            address = end
        # Repeated candidates first, then singletons. Do not exclude alphabetic
        # or numeric keys: those are valid passphrases too.
        for candidate, count in counts.most_common():
            if time.monotonic() >= deadline or len(found) == len(targets):
                break
            for salt, page in targets.items():
                if salt in found:
                    continue
                key = hashlib.pbkdf2_hmac("sha512", candidate, page[:16], 4000, 32)
                if verified(page, key):
                    found[salt] = {"kind": "raw", "value": key.hex()}
        fd = os.open(request["output"], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"version": 1, "keys_by_salt": found, "diagnostic": {
                "bytes_scanned": scanned, "salt_allocations": {s: len(v) for s, v in salt_addresses.items()},
                "raw_candidates": len(checked_raw_keys), "text_candidates": len(counts),
                "elapsed_seconds": round(time.monotonic() - started, 2)}}, stream)
        result.AppendMessage("QQ capture completed; authenticated key count: %d" % len(found))
    finally:
        # Always release QQ, including when a file or memory read fails.
        if process.IsValid():
            process.Detach()


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand("command script add -f lldb_capture.capture qq-capture")
