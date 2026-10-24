"""Read-only macOS account discovery. Login credentials are never returned."""
from __future__ import annotations

import hashlib
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .crypto import DatabaseKey, decrypt_database
from .errors import QQError
from .util import opaque, private_dir, readonly

LOGIN_KEY = DatabaseKey("passphrase", b"BD156D6710D54D8782F4")
DEFAULT_HOME = Path.home() / "Library/Application Support/qq-local-interface"


def qq_roots():
    return [Path.home() / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ",
            Path.home() / "Library/Application Support/QQ"]


def doctor():
    import platform
    app = Path("/Applications/QQ.app/Contents/Info.plist")
    info = plistlib.loads(app.read_bytes()) if app.exists() else {}
    return {"system": platform.system(), "architecture": platform.machine(),
            "macos_version": platform.mac_ver()[0],
            "qq_version": info.get("CFBundleShortVersionString"),
            "qq_build": info.get("CFBundleVersion"),
            "qq_running": subprocess.run(["pgrep", "-x", "QQ"], capture_output=True).returncode == 0,
            "lldb": shutil.which("lldb") is not None,
            "local_account_directories": sum(len(list(root.glob("nt_qq_*/nt_db"))) for root in qq_roots() if root.is_dir())}


def resolve_account(uin, private_root=DEFAULT_HOME):
    """Resolve only the explicitly selected account via its local login index."""
    if not isinstance(uin, str) or not uin.isdigit():
        raise QQError("invalid_argument", "QQ account number must contain digits")
    work = private_dir(private_root)
    for root in qq_roots():
        login = root / "global/nt_db/login.db"
        if not login.is_file():
            continue
        with tempfile.TemporaryDirectory(prefix=".account-", dir=work) as directory:
            copied = Path(directory) / "encrypted.db"
            shutil.copyfile(login, copied)
            wal = Path(str(login) + "-wal")
            copied_wal = Path(directory) / "encrypted.db-wal"
            if wal.is_file():
                shutil.copyfile(wal, copied_wal)
            output = Path(directory) / "plain.db"
            decrypt_database(copied, output, LOGIN_KEY, copied_wal if copied_wal.exists() else None)
            with readonly(output) as connection:
                # login_table uses different numbers from profile_info.buddy_list.
                rows = connection.execute('SELECT "1001" AS uid, "1000" AS uin FROM login_table WHERE CAST("1000" AS TEXT)=?', (uin,)).fetchall()
            for row in rows:
                uid = str(row["uid"])
                inner = hashlib.md5(uid.encode()).hexdigest()
                suffix = hashlib.md5((inner + "nt_kernel").encode()).hexdigest()
                directory = root / ("nt_qq_" + suffix) / "nt_db"
                if directory.is_dir() and (directory / "nt_msg.db").is_file():
                    return {"uin": uin, "uid": uid, "database_dir": str(directory),
                            "account_ref": opaque("qq", "account", uid)}
    raise QQError("account_not_found", "selected account has no matching local QQ database directory")
