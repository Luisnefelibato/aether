from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


def _cfg(settings: Any, name: str, default: Any) -> Any:
    aliases = {
        "trash_path": "trash_dir",
        "max_files": "max_index_files",
    }
    for section_name in ("files_intel", "files"):
        section = getattr(settings, section_name, None)
        if section is None:
            continue
        if hasattr(section, name):
            return getattr(section, name)
        alias = aliases.get(name)
        if alias and hasattr(section, alias):
            return getattr(section, alias)
    return getattr(settings, name, default)


class FilesIntel:
    """Bounded local file inventory and reversible file operations."""

    def __init__(self, settings: Any = None, *, root: Path | str | None = None) -> None:
        configured_root = root or _cfg(settings, "root", Path.cwd())
        self.root = Path(configured_root).expanduser().resolve()
        db_value = _cfg(settings, "sqlite_path", self.root / ".aether-files.sqlite3")
        trash_value = _cfg(settings, "trash_path", self.root / ".aether-trash")
        self.db_path = Path(db_value).expanduser().resolve()
        self.trash_path = Path(trash_value).expanduser().resolve()
        self.max_files = int(_cfg(settings, "max_files", 10_000))
        self.max_file_bytes = int(_cfg(settings, "max_file_bytes", 256 * 1024 * 1024))
        self.max_total_bytes = int(_cfg(settings, "max_total_bytes", 2 * 1024**3))
        self.max_depth = int(_cfg(settings, "max_depth", 8))
        roots = _cfg(settings, "index_roots", None)
        self.roots = [Path(item).expanduser().resolve() for item in (roots or [self.root])]
        excludes = _cfg(settings, "exclude_globs", ["**/.git/**", "**/node_modules/**", "**/.venv/**"])
        self.exclude_globs = list(excludes or [])
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL,
                sha256 TEXT NOT NULL, indexed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_files_hash ON files(sha256);
            CREATE TABLE IF NOT EXISTS trash (
                id TEXT PRIMARY KEY, original_path TEXT NOT NULL,
                trash_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                trashed_at REAL NOT NULL, restored_at REAL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def hash_file(path: Path | str) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def index(self, roots: Iterable[Path | str] | None = None) -> dict[str, int]:
        conn = self._require_conn()
        candidates = [self._safe(item) for item in (roots or self.roots)]
        indexed = total = skipped = 0
        seen: set[str] = set()
        for base in candidates:
            if not base.exists():
                skipped += 1
                continue
            paths = [base] if base.is_file() else base.rglob("*")
            for path in paths:
                if indexed >= self.max_files:
                    skipped += 1
                    continue
                if not path.is_file() or path == self.db_path or self.trash_path in path.parents:
                    continue
                try:
                    relative = path.relative_to(base if base.is_dir() else base.parent)
                    if len(relative.parts) > self.max_depth:
                        skipped += 1
                        continue
                except ValueError:
                    pass
                if self._excluded(path):
                    skipped += 1
                    continue
                size = path.stat().st_size
                if size > self.max_file_bytes or total + size > self.max_total_bytes:
                    skipped += 1
                    continue
                resolved = str(path.resolve())
                seen.add(resolved)
                conn.execute(
                    """
                    INSERT INTO files(path,size,mtime,sha256,indexed_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET size=excluded.size,
                        mtime=excluded.mtime,sha256=excluded.sha256,
                        indexed_at=excluded.indexed_at
                    """,
                    (resolved, size, path.stat().st_mtime, self.hash_file(path), time.time()),
                )
                indexed += 1
                total += size
        if not roots:
            rows = conn.execute("SELECT path FROM files").fetchall()
            for row in rows:
                if row["path"] not in seen:
                    conn.execute("DELETE FROM files WHERE path=?", (row["path"],))
        conn.commit()
        return {"indexed": indexed, "bytes": total, "skipped": skipped}

    def search(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._require_conn()
        cap = max(1, min(limit, 1000))
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT * FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
            (f"%{escaped}%", cap),
        ).fetchall()
        hits = [{**dict(row), "match": "path"} for row in rows]
        if len(hits) >= cap or not query.strip():
            return hits
        seen = {item["path"] for item in hits}
        needle = query.casefold()
        remaining = cap - len(hits)
        text_suffixes = {
            ".txt",
            ".md",
            ".py",
            ".json",
            ".csv",
            ".log",
            ".yml",
            ".yaml",
            ".toml",
            ".ini",
            ".html",
            ".css",
            ".js",
            ".ts",
        }
        candidates = conn.execute(
            "SELECT * FROM files WHERE size <= ? ORDER BY path",
            (512 * 1024,),
        ).fetchall()
        for row in candidates:
            path = Path(row["path"])
            if row["path"] in seen or path.suffix.lower() not in text_suffixes:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
            except OSError:
                continue
            if needle not in text.casefold():
                continue
            hits.append({**dict(row), "match": "content"})
            seen.add(row["path"])
            remaining -= 1
            if remaining <= 0:
                break
        return hits

    def duplicates(self) -> list[list[dict[str, Any]]]:
        conn = self._require_conn()
        hashes = conn.execute(
            "SELECT sha256 FROM files GROUP BY sha256 HAVING COUNT(*) > 1"
        ).fetchall()
        return [
            [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM files WHERE sha256=? ORDER BY path", (item["sha256"],)
                )
            ]
            for item in hashes
        ]

    def copy(self, source: Path | str, destination: Path | str) -> dict[str, Any]:
        src, dst = self._safe(source), self._safe(destination)
        self._check_file(src)
        if dst.exists():
            raise FileExistsError(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return self._verified_result(src, dst, "copy")

    def move(self, source: Path | str, destination: Path | str) -> dict[str, Any]:
        src, dst = self._safe(source), self._safe(destination)
        self._check_file(src)
        if dst.exists():
            raise FileExistsError(dst)
        expected = self.hash_file(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        actual = self.hash_file(dst)
        if expected != actual:
            raise IOError("SHA256 mismatch after move")
        return {"ok": True, "operation": "move", "path": str(dst), "sha256": actual}

    def trash(self, path: Path | str) -> dict[str, Any]:
        conn = self._require_conn()
        src = self._safe(path)
        self._check_file(src)
        digest, trash_id = self.hash_file(src), str(uuid4())
        target = self.trash_path / f"{trash_id}-{src.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
        if self.hash_file(target) != digest:
            raise IOError("SHA256 mismatch after trash")
        conn.execute(
            "INSERT INTO trash(id,original_path,trash_path,sha256,trashed_at) "
            "VALUES(?,?,?,?,?)",
            (trash_id, str(src), str(target), digest, time.time()),
        )
        conn.commit()
        return {"ok": True, "trash_id": trash_id, "sha256": digest}

    def restore(self, trash_id: str) -> dict[str, Any]:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM trash WHERE id=? AND restored_at IS NULL", (trash_id,)
        ).fetchone()
        if row is None:
            raise KeyError(trash_id)
        source, target = Path(row["trash_path"]), self._safe(row["original_path"])
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        digest = self.hash_file(target)
        if digest != row["sha256"]:
            raise IOError("SHA256 mismatch after restore")
        conn.execute("UPDATE trash SET restored_at=? WHERE id=?", (time.time(), trash_id))
        conn.commit()
        return {"ok": True, "path": str(target), "sha256": digest}

    def _excluded(self, path: Path) -> bool:
        text = str(path).replace("\\", "/")
        for pattern in self.exclude_globs:
            needle = pattern.replace("**/", "").replace("**", "").strip("/")
            if needle and needle.rstrip("/") in text:
                return True
        return False

    def _safe(self, path: Path | str) -> Path:
        value = Path(path).expanduser()
        if not value.is_absolute():
            value = self.root / value
        resolved = value.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PermissionError(f"path outside configured root: {resolved}")
        return resolved

    def _check_file(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > self.max_file_bytes:
            raise ValueError("file exceeds configured byte limit")

    def _verified_result(self, source: Path, target: Path, operation: str) -> dict[str, Any]:
        expected, actual = self.hash_file(source), self.hash_file(target)
        if expected != actual:
            target.unlink(missing_ok=True)
            raise IOError(f"SHA256 mismatch after {operation}")
        return {"ok": True, "operation": operation, "path": str(target), "sha256": actual}

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("FilesIntel.open() must be called first")
        return self._conn
