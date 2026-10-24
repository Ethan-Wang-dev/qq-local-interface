"""Account-isolated, immutable generations with an atomic current pointer."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .crypto import DatabaseKey, decrypt_database, first_page, guild_key, verify_key
from .errors import QQError
from .index import NORMALIZER_VERSION, build_index
from .assets import inventory_assets, snapshot_assets
from .platforms import DEFAULT_HOME
from .util import atomic_json, digest, file_digest, load_json, private_dir, regular_file, utc_now, validate_source_id

DATABASES = ("nt_msg.db", "profile_info.db", "group_info.db", "collection.db", "files_in_chat.db", "rich_media.db", "emoji.db")


class QQVault:
    def __init__(self, root=DEFAULT_HOME, source_id="qq-local"):
        self.root = Path(root).expanduser().absolute()
        self.source_id = validate_source_id(source_id)
        self.home = self.root / "sources" / self.source_id
        self.config_file = self.home / "config.json"

    def configure(self, database_dir, *, account_uin, account_uid=None, key_file=None, exclude_databases=None):
        path = Path(database_dir).expanduser()
        if path.is_symlink() or not path.is_dir() or not (path / "nt_msg.db").is_file():
            raise QQError("invalid_argument", "database_dir must contain a regular nt_msg.db")
        if not isinstance(account_uin, str) or not account_uin.isdigit():
            raise QQError("invalid_argument", "account_uin must be a QQ number string")
        private_dir(self.home)
        if self.config_file.exists():
            config = self.config()
            if config["account"]["uin"] != account_uin or Path(config["database_dir"]) != path.resolve():
                raise QQError("source_conflict", "use a different source_id for another QQ account or data directory")
        else:
            config = {"version": 1, "source_id": self.source_id, "origin": uuid.uuid4().hex,
                      "database_dir": str(path.resolve()), "account": {"uin": account_uin, "uid": account_uid},
                      "databases": list(DATABASES), "database_selection": "all",
                      "key_file": str((self.home / "keys.json").absolute())}
        if key_file:
            config["key_file"] = str(Path(key_file).expanduser().resolve())
        if exclude_databases is not None:
            if (not isinstance(exclude_databases, list) or any(not isinstance(n, str) or
                    not re.fullmatch(r'[A-Za-z0-9_.-]+\.db', n) for n in exclude_databases) or 'nt_msg.db' in exclude_databases):
                raise QQError('invalid_argument', 'excluded databases must be database filenames and cannot include nt_msg.db')
            config['exclude_databases'] = sorted(set(exclude_databases))
        atomic_json(self.config_file, config)
        return self.status()

    def config(self):
        if not self.config_file.exists():
            raise QQError("not_configured", "configure this QQ source first")
        result = load_json(self.config_file)
        if result.get("source_id") != self.source_id:
            raise QQError("source_mismatch", "vault source_id does not match its configuration")
        return result

    def current(self):
        pointer = self.home / "current.json"
        if not pointer.exists():
            raise QQError("snapshot_required", "run sync before querying this QQ source")
        generation = load_json(pointer).get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", generation):
            raise QQError("invalid_snapshot", "invalid current generation")
        snapshot = self.home / "generations" / generation
        if snapshot.is_symlink() or not snapshot.is_dir():
            raise QQError("invalid_snapshot", "current snapshot directory is unavailable")
        manifest = load_json(snapshot / "manifest.json")
        if manifest.get("source_id") != self.source_id or manifest.get("schema_version") != SCHEMA_VERSION:
            raise QQError("source_mismatch", "snapshot source or schema does not match")
        return snapshot, manifest

    def status(self):
        result = {"source_id": self.source_id, "configured": self.config_file.exists(), "snapshot_available": False}
        if result["configured"]:
            config = self.config()
            result.update({"account_suffix": config["account"]["uin"][-4:],
                           "key_file_available": Path(config["key_file"]).is_file(),
                           'excluded_databases': config.get('exclude_databases', [])})
        try:
            _, manifest = self.current()
            result.update({"snapshot_available": True, "snapshot_id": manifest["generation"],
                           "captured_at": manifest["captured_at"], "counts": manifest["counts"]})
            result['coverage'] = manifest.get('coverage', {'complete': False, 'reason': 'legacy_snapshot_requires_refresh'})
        except QQError as error:
            if error.code != "snapshot_required":
                raise
        health = self.home / "last-check.json"
        if health.exists():
            result["last_check"] = load_json(health)
        return result

    @contextmanager
    def _writer(self):
        private_dir(self.home)
        path = self.home / ".sync.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QQError("sync_busy", "another refresh is already running for this source") from None
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _signature(path):
        if not path.exists():
            return None
        info = regular_file(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _copy_stable(self, source, directory, names):
        paths = [source / (name + suffix) for name in names for suffix in ("", "-wal", "-journal")]
        for _ in range(3):
            before = {path: self._signature(path) for path in paths}
            if before[source / "nt_msg.db"] is None:
                raise QQError("not_found", "selected account nt_msg.db is missing")
            for name in names:
                journal = source / (name + "-journal")
                if before[journal] and before[journal][2]:
                    with journal.open("rb") as stream:
                        if any(stream.read(28)):
                            raise QQError("source_busy", "QQ has an active rollback journal; retry after the transaction completes")
            fingerprints, available = {}, []
            for name in names:
                path = source / name
                if before[path] is None:
                    if name == "nt_msg.db":
                        raise QQError("invalid_database", "QQ message database has no data pages yet")
                    continue
                if before[path][2] <= 1024:
                    if name == "nt_msg.db":
                        raise QQError("invalid_database", "QQ message database has no data pages yet")
                available.append(name)
                parts = {}
                for suffix in ("", "-wal"):
                    src, dst = source / (name + suffix), directory / (name + suffix)
                    dst.unlink(missing_ok=True)
                    if before[src] is not None:
                        shutil.copyfile(src, dst)
                        os.chmod(dst, 0o600)
                        parts[suffix or "main"] = file_digest(dst)
                    else:
                        parts[suffix or "main"] = None
                fingerprints[name] = parts
            after = {path: self._signature(path) for path in paths}
            if before == after:
                return available, fingerprints
        raise QQError("source_busy", "QQ files changed during capture; the previous snapshot remains current")

    @staticmethod
    def _keys(path):
        path = regular_file(path)
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise QQError("insecure_key_file", "key file must be owned by the current user and have mode 0600")
        return load_json(path)

    def _decrypt(self, source, output, keys, account, wal=None):
        page = first_page(source)
        key = None
        if page:
            record = keys.get('keys_by_salt', {}).get(page[:16].hex()) or keys.get('databases', {}).get(source.name) or keys.get('default')
            if record is not None:
                key = DatabaseKey.from_record(record)
            elif source.name.startswith('gpro_') or source.name == 'guild1.db':
                candidate = guild_key(source, account['uin'])
                if verify_key(source, candidate):
                    key = candidate
            if key is None:
                raise QQError('key_required', 'no authenticated key is stored for a selected database', {'database': source.name})
        return decrypt_database(source, output, key, wal)

    def refresh(self, mode="incremental", allow_partial=False):
        if mode not in ("full", "incremental"):
            raise QQError("invalid_argument", "sync mode must be full or incremental")
        if type(allow_partial) is not bool:
            raise QQError('invalid_argument', 'allow_partial must be boolean')
        with self._writer():
            config = self.config()
            source = Path(config["database_dir"])
            if source.is_symlink():
                raise QQError("invalid_path", "QQ database directory must not be a symbolic link")
            discovered = sorted(f.name for f in source.glob('*.db') if f.is_file())
            excluded = config.get('exclude_databases', [])
            names = [n for n in (discovered if config.get('database_selection') == 'all' else config['databases']) if n not in excluded]
            if "nt_msg.db" not in names or any(not re.fullmatch(r"[A-Za-z0-9_.-]+\.db", name) for name in names):
                raise QQError("invalid_argument", "invalid database selection")
            old_dir, old_manifest = None, None
            try:
                old_dir, old_manifest = self.current()
            except QQError as error:
                if error.code != "snapshot_required":
                    raise
            generations = private_dir(self.home / "generations")
            with tempfile.TemporaryDirectory(prefix=".sync-", dir=self.home) as temporary:
                work = Path(temporary)
                encrypted = private_dir(work / "encrypted")
                available, fingerprints = self._copy_stable(source, encrypted, names)
                if config.get('database_selection') == 'all' and set(names) != {f.name for f in source.glob('*.db') if f.is_file() and f.name not in excluded}:
                    raise QQError('source_busy', 'QQ database inventory changed during capture; retry sync')
                asset_entries, asset_state = inventory_assets(source.parent, self.source_id)
                keys_revision = file_digest(Path(config['key_file'])) if Path(config['key_file']).is_file() else None
                if (mode == "incremental" and old_manifest and fingerprints == old_manifest["inputs"]
                        and old_manifest.get("normalizer_version") == NORMALIZER_VERSION
                        and old_manifest.get('keys_revision') == keys_revision
                        and old_manifest.get('selection', {}).get('excluded', []) == excluded
                        and (allow_partial or all(d.get('state', 'ready') != 'unavailable' for d in old_manifest['databases'].values()))
                        and old_manifest.get('assets', {}).get('fingerprint') == asset_state['fingerprint']):
                    checked = {"at": utc_now(), "ok": True, "changed": False}
                    atomic_json(self.home / "last-check.json", checked)
                    return {"source_id": self.source_id, "changed": False, "snapshot_id": old_manifest["generation"],
                            "counts": old_manifest["counts"], "coverage": old_manifest.get('coverage'), "checked_at": checked["at"]}
                generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ_") + uuid.uuid4().hex[:8]
                staged = private_dir(work / "published")
                data = private_dir(staged / "data")
                keys = self._keys(config['key_file']) if Path(config['key_file']).exists() else {}
                details, updated, reused = {}, [], []
                for name in available:
                    output = data / name
                    if (mode == "incremental" and old_manifest and fingerprints[name] == old_manifest["inputs"].get(name)
                            and old_manifest['databases'][name].get('state') != 'unavailable'):
                        shutil.copyfile(regular_file(old_dir / "data" / name), output)
                        reused.append(name)
                        details[name] = {'state': 'ready', **old_manifest["databases"][name]}
                    elif (encrypted / name).stat().st_size <= 1024:
                        shutil.copyfile(encrypted / name, output)
                        details[name] = {"state": "uninitialized", "encrypted": True, "pages": 0, "wal_frames": 0}
                        updated.append(name)
                    else:
                        wal = encrypted / (name + "-wal")
                        try:
                            details[name] = {'state': 'ready', **self._decrypt(encrypted / name, output, keys,
                                config['account'], wal if wal.exists() else None)}
                        except QQError as error:
                            if not allow_partial or name == 'nt_msg.db' or error.code not in ('key_required', 'authentication_failed', 'unsupported_cipher'):
                                raise
                            details[name] = {'state': 'unavailable', 'reason': error.code,
                                             'source_size_bytes': (encrypted / name).stat().st_size}
                        updated.append(name)
                    if output.exists():
                        os.chmod(output, 0o400)
                asset_state['fingerprint'] = snapshot_assets(asset_entries, staged)
                counts = build_index(data, staged / "records.db", self.source_id, config["account"], generation,
                                     old_dir / "records.db" if old_dir else None,
                                     inputs={n: {'files': f, 'state': details[n]['state']} for n, f in fingerprints.items()},
                                     asset_entries=asset_entries)
                counts.update(databases=len(available), assets=len(asset_entries))
                coverage = {'complete': all(d['state'] != 'unavailable' for d in details.values()),
                            'scope': 'selected_local_databases', 'excluded': excluded,
                            'ready': sum(d['state'] == 'ready' for d in details.values()),
                            'uninitialized': sum(d['state'] == 'uninitialized' for d in details.values()),
                            'unavailable': [{'database': n, 'reason': d['reason']} for n, d in details.items() if d['state'] == 'unavailable']}
                manifest = {"schema_version": SCHEMA_VERSION, "source_id": self.source_id,
                    "origin": config["origin"], "generation": generation, "captured_at": utc_now(),
                    "normalizer_version": NORMALIZER_VERSION,
                    "assets": asset_state,
                    "coverage": coverage,
                    "keys_revision": keys_revision,
                    'selection': {'discovered': discovered, 'selected': names, 'excluded': excluded},
                    "inputs": fingerprints, "databases": details, "counts": counts,
                    "mode": mode, "updated_databases": updated, "reused_databases": reused}
                atomic_json(staged / "manifest.json", manifest)
                os.chmod(staged / "records.db", 0o400)
                os.rename(staged, generations / generation)
                atomic_json(self.home / "current.json", {"generation": generation})
                atomic_json(self.home / "last-check.json", {"at": utc_now(), "ok": True, "changed": True, 'coverage': coverage})
                return {"source_id": self.source_id, "changed": True, "snapshot_id": generation,
                        "captured_at": manifest["captured_at"], "updated_databases": updated,
                        "reused_databases": reused, "counts": counts, "coverage": coverage}
