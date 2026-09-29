from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


def _get(settings: Any, name: str, default: Any) -> Any:
    section = getattr(settings, "backup", settings)
    aliases = {"output_path": "default_dest", "max_bytes": "max_set_size_gb"}
    if hasattr(section, name):
        value = getattr(section, name)
        if name == "max_bytes" and isinstance(value, (int, float)) and value < 1024:
            return int(value * 1024**3)
        return value
    alias = aliases.get(name)
    if alias and hasattr(section, alias):
        value = getattr(section, alias)
        if name == "max_bytes":
            return int(float(value) * 1024**3)
        return value
    return getattr(settings, name, default)


class BackupService:
    def __init__(self, settings: Any = None, *, root: Path | str | None = None) -> None:
        self.root = Path(root or _get(settings, "root", Path.cwd())).resolve()
        self.output = Path(_get(settings, "output_path", self.root / "backups")).resolve()
        self.db_path = Path(_get(settings, "sqlite_path", self.output / "backups.sqlite3")).resolve()
        self.max_files = int(_get(settings, "max_files", 20_000))
        self.max_bytes = int(_get(settings, "max_bytes", 4 * 1024**3))
        self.exclude_names = {
            ".git",
            ".venv",
            "node_modules",
            "browser_profile",
            "__pycache__",
        }
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS backup_sets (
                name TEXT PRIMARY KEY, paths TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backup_runs (
                id TEXT PRIMARY KEY, set_name TEXT NOT NULL, archive TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                created_at REAL NOT NULL, verified_at REAL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def create_set(self, name: str, paths: Iterable[Path | str]) -> dict[str, Any]:
        conn = self._db()
        normalized = [str(self._safe(path)) for path in paths]
        conn.execute(
            "INSERT INTO backup_sets(name,paths,created_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET paths=excluded.paths",
            (name, json.dumps(normalized), time.time()),
        )
        conn.commit()
        return {"name": name, "paths": normalized}

    def run(self, set_name: str) -> dict[str, Any]:
        conn = self._db()
        row = conn.execute("SELECT paths FROM backup_sets WHERE name=?", (set_name,)).fetchone()
        if row is None:
            raise KeyError(set_name)
        run_id = str(uuid4())
        archive = self.output / f"{set_name}-{run_id}.zip"
        manifest: dict[str, Any] = {"version": 1, "set": set_name, "files": []}
        count = total = 0
        try:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for configured in json.loads(row["paths"]):
                    source = Path(configured)
                    candidates = [source] if source.is_file() else source.rglob("*")
                    for path in candidates:
                        if not path.is_file() or self.output in path.parents:
                            continue
                        if any(part in self.exclude_names for part in path.parts):
                            continue
                        size = path.stat().st_size
                        count += 1
                        total += size
                        if count > self.max_files or total > self.max_bytes:
                            raise ValueError("backup limits exceeded")
                        relative = path.relative_to(self.root).as_posix()
                        digest = self._hash(path)
                        zf.write(path, relative)
                        manifest["files"].append(
                            {"path": relative, "size": size, "sha256": digest}
                        )
                manifest_bytes = json.dumps(
                    manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
                zf.writestr("MANIFEST.json", manifest_bytes)
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            status = "complete"
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        conn.execute(
            "INSERT INTO backup_runs VALUES(?,?,?,?,?,?,NULL)",
            (run_id, set_name, str(archive), manifest_sha, status, time.time()),
        )
        conn.commit()
        return {
            "id": run_id,
            "archive": str(archive),
            "manifest_sha256": manifest_sha,
            "files": count,
            "bytes": total,
            "status": status,
        }

    def verify(self, run_or_archive: str | Path) -> dict[str, Any]:
        conn = self._db()
        row = conn.execute(
            "SELECT * FROM backup_runs WHERE id=?", (str(run_or_archive),)
        ).fetchone()
        archive = Path(row["archive"]) if row else Path(run_or_archive)
        errors: list[str] = []
        with zipfile.ZipFile(archive, "r") as zf:
            manifest_bytes = zf.read("MANIFEST.json")
            manifest = json.loads(manifest_bytes)
            expected_manifest = row["manifest_sha256"] if row else None
            actual_manifest = hashlib.sha256(manifest_bytes).hexdigest()
            if expected_manifest and expected_manifest != actual_manifest:
                errors.append("manifest SHA256 mismatch")
            for item in manifest["files"]:
                digest = hashlib.sha256(zf.read(item["path"])).hexdigest()
                if digest != item["sha256"]:
                    errors.append(f"SHA256 mismatch: {item['path']}")
        ok = not errors
        if row and ok:
            conn.execute(
                "UPDATE backup_runs SET status='verified',verified_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            conn.commit()
        return {"ok": ok, "files": len(manifest["files"]), "errors": errors}

    def restore(self, run_or_archive: str | Path, destination: Path | str) -> dict[str, Any]:
        """Explicit restore only; callers must invoke this operation themselves."""
        verification = self.verify(run_or_archive)
        if not verification["ok"]:
            raise IOError("backup verification failed")
        row = self._db().execute(
            "SELECT archive FROM backup_runs WHERE id=?", (str(run_or_archive),)
        ).fetchone()
        archive = Path(row["archive"]) if row else Path(run_or_archive)
        target = Path(destination).resolve()
        target.mkdir(parents=True, exist_ok=True)
        restored = 0
        with zipfile.ZipFile(archive, "r") as zf:
            manifest = json.loads(zf.read("MANIFEST.json"))
            for item in manifest["files"]:
                output = (target / item["path"]).resolve()
                if output != target and target not in output.parents:
                    raise ValueError("unsafe archive path")
                if output.exists():
                    raise FileExistsError(output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(zf.read(item["path"]))
                restored += 1
        return {"ok": True, "restored": restored, "destination": str(target)}

    def _safe(self, path: Path | str) -> Path:
        value = Path(path)
        if not value.is_absolute():
            value = self.root / value
        value = value.resolve()
        if value != self.root and self.root not in value.parents:
            raise PermissionError(value)
        if not value.exists():
            raise FileNotFoundError(value)
        return value

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("BackupService.open() must be called first")
        return self._conn
