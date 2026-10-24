"""Explicit first-use key capture, separate from ordinary sync and queries."""
from __future__ import annotations

import base64
import os
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path

from .crypto import first_page, DatabaseKey, verify_key
from .errors import QQError
from .util import atomic_json, load_json, private_dir


def capture_keys(database_dir, output, pid=None, timeout=100, exclude_databases=None):
    pids = subprocess.run(["pgrep", "-x", "QQ"], capture_output=True, text=True).stdout.split()
    if pid is None:
        if len(pids) != 1:
            raise QQError("qq_login_required", "log in to the selected QQ account first; exactly one QQ main process is required")
        pid = int(pids[0])
    output = Path(output).expanduser().resolve()
    existing = load_json(output) if output.exists() else {"version": 1}
    targets = []
    for path in sorted(Path(database_dir).glob("*.db")):
        if path.name in (exclude_databases or []):
            continue
        if path.is_file() and path.stat().st_size >= 1536:
            page = first_page(path)
            if page:
                prior = existing.get("keys_by_salt", {}).get(page[:16].hex())
                if prior and verify_key(path, DatabaseKey.from_record(prior)):
                    continue
                targets.append({"salt": page[:16].hex(), "page": base64.b64encode(page).decode()})
    if not targets:
        return {"authenticated_keys": 0, "stored_keys": len(existing.get("keys_by_salt", {})),
                "target_databases": 0, "key_file_saved": output.exists()}
    private_dir(output.parent)
    with tempfile.TemporaryDirectory(prefix=".capture-", dir=output.parent) as directory:
        work = Path(directory)
        request_file, captured = work / "request.json", work / "result.json"
        atomic_json(request_file, {"targets": targets, "output": str(captured)})
        module = Path(__file__).with_name("lldb_capture.py")
        command = ["lldb", "--batch", "-p", str(pid),
                   "-o", "command script import " + shlex.quote(str(module)),
                   "-o", "qq-capture " + shlex.quote(str(request_file)), "-o", "quit"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
            if state.startswith("T"):
                os.kill(pid, signal.SIGCONT)
            raise QQError("debug_access_required", "LLDB could not complete key capture; QQ signing or developer debugging access needs setup") from None
        if not captured.exists():
            atomic_json(output.parent / "capture-diagnostic.json", {"returncode": process.returncode,
                "diagnostic": (stdout + stderr)[-5000:]})
            raise QQError("debug_access_required", "LLDB could not attach or run capture; inspect the private capture diagnostic")
        result = load_json(captured)
        atomic_json(output.parent / "capture-summary.json", {
            "target_databases": len(targets), "authenticated_keys": len(result.get("keys_by_salt", {})),
            **{k: v for k, v in result.get('diagnostic', {}).items() if k != 'salt_allocations'},
            "salt_allocations": sum(result.get('diagnostic', {}).get('salt_allocations', {}).values())})
        if not result.get("keys_by_salt"):
            raise QQError("key_not_found", "no additional authenticated keys found for the remaining databases; existing keys were preserved",
                          {'remaining_databases': len(targets), 'stored_keys': len(existing.get('keys_by_salt', {}))})
        merged = {**existing, "version": 1, "keys_by_salt": {
            **existing.get("keys_by_salt", {}), **result["keys_by_salt"]}}
        atomic_json(output, merged)
        return {"authenticated_keys": len(result["keys_by_salt"]),
                "stored_keys": len(merged["keys_by_salt"]), "target_databases": len(targets), "key_file_saved": True}
