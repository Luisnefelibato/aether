from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from typing import Any
from uuid import uuid4

from aether_core.config import Settings

logger = logging.getLogger("aether.memory")


class MemoryStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._db_path = settings.resolve_path(settings.memory.sqlite_path)
        self._chroma_path = settings.resolve_path(settings.memory.chroma_path)
        self._conn: sqlite3.Connection | None = None
        self._collection = None

    async def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._connect)
        await asyncio.to_thread(self._init_chroma)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, UNIQUE(name, kind)
            );
            CREATE TABLE IF NOT EXISTS facts (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
                value TEXT NOT NULL, version INTEGER NOT NULL, active INTEGER NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0, source TEXT,
                created_at REAL NOT NULL,
                UNIQUE(subject, predicate, version)
            );
            CREATE INDEX IF NOT EXISTS idx_facts_current
                ON facts(subject, predicate, active);
            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
                relation TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                UNIQUE(source_id, target_id, relation)
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, content TEXT NOT NULL,
                source_from REAL, source_to REAL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_summaries_scope
                ON summaries(scope, created_at DESC);
            """
        )
        conn.commit()
        return conn

    def _init_chroma(self) -> None:
        if os.getenv("AETHER_ENABLE_CHROMA", "").lower() not in {"1", "true", "yes"}:
            self._collection = None
            logger.info("Chroma skipped (set AETHER_ENABLE_CHROMA=1 to enable)")
            return
        try:
            import onnxruntime  # noqa: F401
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            client = chromadb.PersistentClient(
                path=str(self._chroma_path),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._collection = client.get_or_create_collection(
                self.settings.memory.embed_collection
            )
            logger.info("Chroma semantic memory enabled")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chroma disabled (%s); using SQLite episodic memory", exc)
            self._collection = None

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)

    async def add_episode(self, role: str, content: str) -> None:
        assert self._conn is not None
        eid = str(uuid4())
        ts = time.time()

        def _write():
            self._conn.execute(
                "INSERT INTO episodes(id, role, content, ts) VALUES (?, ?, ?, ?)",
                (eid, role, content, ts),
            )
            self._conn.commit()
            if self._collection is not None:
                try:
                    self._collection.add(
                        ids=[eid],
                        documents=[f"{role}: {content}"],
                        metadatas=[{"role": role, "ts": ts}],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Chroma add failed, continuing with SQLite: %s", exc)

        await asyncio.to_thread(_write)

    async def load_history(self, limit: int = 40) -> list[dict[str, str]]:
        """Load recent conversation turns for the LLM context window."""
        assert self._conn is not None

        def _read():
            cur = self._conn.execute(
                "SELECT role, content FROM episodes ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = list(reversed(cur.fetchall()))
            out: list[dict[str, str]] = []
            for role, content in rows:
                if role in {"user", "assistant"}:
                    out.append({"role": role, "content": content})
            return out

        return await asyncio.to_thread(_read)

    async def ensure_identity(self, name: str, address_as: str | None = None) -> None:
        prefs = await self.list_preferences()
        if prefs.get("user_name") != name:
            await self.set_preference("user_name", name)
        alias = address_as or name
        if prefs.get("address_as") != alias:
            await self.set_preference("address_as", alias)
        if prefs.get("owner") != name:
            await self.set_preference("owner", name)
        await self.set_preference("trusted_operator", "true")
        await self.set_preference("assistant_name", "ADAM")
        await self.set_preference("product_name", "ADAM")
        await self.set_preference("assistant_never_alias", "aether")

    async def recall(self, query: str, limit: int = 8) -> list[str]:
        prefs = await self.list_preferences()
        pref_lines = [f"pref {k}={v}" for k, v in prefs.items()]

        # Always include identity first
        identity = []
        if prefs.get("address_as") or prefs.get("user_name"):
            who = prefs.get("address_as") or prefs.get("user_name")
            identity.append(f"El usuario se llama {who}. Trátalo siempre por ese nombre.")

        semantic: list[str] = []
        if self._collection is not None:

            def _query():
                try:
                    res = self._collection.query(query_texts=[query], n_results=limit)
                    docs = (res.get("documents") or [[]])[0]
                    return list(docs)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Chroma query failed: %s", exc)
                    return []

            semantic = await asyncio.to_thread(_query)

        recent = await self._recent(max(limit, 12))
        fact_limit = int(getattr(self.settings.memory, "fact_limit_recall", 12))
        facts = await self.get_facts(limit=fact_limit)
        fact_lines = [
            f"fact {item.get('subject')} {item.get('predicate')}={item.get('value')}"
            for item in facts
        ]
        summaries = await self.list_summaries(limit=3)
        summary_lines = [f"resumen {item.get('scope')}: {item.get('content')}" for item in summaries]
        # Merge recent + semantic, dedupe
        merged: list[str] = []
        for item in identity + pref_lines + fact_lines + summary_lines + recent + semantic:
            if item and item not in merged:
                merged.append(item)
        return merged[: limit + 6]

    async def _recent(self, limit: int) -> list[str]:
        assert self._conn is not None

        def _read():
            cur = self._conn.execute(
                "SELECT role, content FROM episodes ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
            return [f"{r}: {c}" for r, c in reversed(rows)]

        return await asyncio.to_thread(_read)

    async def set_preference(self, key: str, value: str) -> None:
        assert self._conn is not None

        def _write():
            self._conn.execute(
                "INSERT INTO preferences(key, value, ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, value, time.time()),
            )
            self._conn.commit()

        await asyncio.to_thread(_write)

    async def list_preferences(self) -> dict[str, str]:
        assert self._conn is not None

        def _read():
            cur = self._conn.execute("SELECT key, value FROM preferences")
            return {k: v for k, v in cur.fetchall()}

        return await asyncio.to_thread(_read)

    async def put_entity(
        self, name: str, kind: str = "", metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create or update an entity, preserving its stable id."""
        assert self._conn is not None
        now = time.time()

        def _write() -> dict[str, Any]:
            entity_id = str(uuid4())
            self._conn.execute(
                """
                INSERT INTO entities(id,name,kind,metadata,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(name,kind) DO UPDATE SET
                    metadata=excluded.metadata, updated_at=excluded.updated_at
                """,
                (entity_id, name, kind, json.dumps(metadata or {}), now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM entities WHERE name=? AND kind=?", (name, kind)
            ).fetchone()
            return self._row(row)

        return await asyncio.to_thread(_write)

    async def list_entities(self, kind: str | None = None) -> list[dict[str, Any]]:
        assert self._conn is not None

        def _read() -> list[dict[str, Any]]:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT * FROM entities ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM entities WHERE kind=? ORDER BY updated_at DESC",
                    (kind,),
                ).fetchall()
            return [self._row(row) for row in rows]

        return await asyncio.to_thread(_read)

    async def set_fact(
        self,
        subject: str,
        predicate: str,
        value: Any,
        confidence: float = 1.0,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Append a fact version and mark all prior versions inactive."""
        assert self._conn is not None
        encoded = json.dumps(value, ensure_ascii=False)

        def _write() -> dict[str, Any]:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(version),0) FROM facts "
                    "WHERE subject=? AND predicate=?",
                    (subject, predicate),
                ).fetchone()
                version = int(row[0]) + 1
                self._conn.execute(
                    "UPDATE facts SET active=0 WHERE subject=? AND predicate=?",
                    (subject, predicate),
                )
                fact_id = str(uuid4())
                self._conn.execute(
                    """
                    INSERT INTO facts
                    (id,subject,predicate,value,version,active,confidence,source,created_at)
                    VALUES(?,?,?,?,?,1,?,?,?)
                    """,
                    (
                        fact_id,
                        subject,
                        predicate,
                        encoded,
                        version,
                        max(0.0, min(1.0, confidence)),
                        source,
                        time.time(),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self._row(
                self._conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            )

        return await asyncio.to_thread(_write)

    async def get_facts(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        include_history: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        assert self._conn is not None

        def _read() -> list[dict[str, Any]]:
            clauses, params = [], []
            if not include_history:
                clauses.append("active=1")
            if subject is not None:
                clauses.append("subject=?")
                params.append(subject)
            if predicate is not None:
                clauses.append("predicate=?")
                params.append(predicate)
            sql = "SELECT * FROM facts"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(max(1, min(int(limit), 1000)))
            return [self._row(row) for row in self._conn.execute(sql, params)]

        return await asyncio.to_thread(_read)

    async def add_link(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert self._conn is not None

        def _write() -> dict[str, Any]:
            link_id = str(uuid4())
            self._conn.execute(
                """
                INSERT INTO links(id,source_id,target_id,relation,metadata,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_id,target_id,relation) DO UPDATE SET
                    metadata=excluded.metadata
                """,
                (
                    link_id,
                    source_id,
                    target_id,
                    relation,
                    json.dumps(metadata or {}),
                    time.time(),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM links WHERE source_id=? AND target_id=? AND relation=?",
                (source_id, target_id, relation),
            ).fetchone()
            return self._row(row)

        return await asyncio.to_thread(_write)

    async def list_links(self, entity_id: str | None = None) -> list[dict[str, Any]]:
        assert self._conn is not None

        def _read() -> list[dict[str, Any]]:
            if entity_id is None:
                rows = self._conn.execute("SELECT * FROM links").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM links WHERE source_id=? OR target_id=?",
                    (entity_id, entity_id),
                ).fetchall()
            return [self._row(row) for row in rows]

        return await asyncio.to_thread(_read)

    async def add_summary(
        self,
        scope: str,
        content: str,
        source_from: float | None = None,
        source_to: float | None = None,
    ) -> dict[str, Any]:
        assert self._conn is not None

        def _write() -> dict[str, Any]:
            summary_id = str(uuid4())
            self._conn.execute(
                "INSERT INTO summaries VALUES(?,?,?,?,?,?)",
                (summary_id, scope, content, source_from, source_to, time.time()),
            )
            self._conn.commit()
            return self._row(
                self._conn.execute(
                    "SELECT * FROM summaries WHERE id=?", (summary_id,)
                ).fetchone()
            )

        return await asyncio.to_thread(_write)

    async def forget_fact(
        self, subject: str, predicate: str | None = None
    ) -> dict[str, Any]:
        assert self._conn is not None

        def _write() -> dict[str, Any]:
            if predicate:
                cur = self._conn.execute(
                    "UPDATE facts SET active=0 WHERE subject=? AND predicate=? AND active=1",
                    (subject, predicate),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE facts SET active=0 WHERE subject=? AND active=1",
                    (subject,),
                )
            self._conn.commit()
            return {"ok": True, "deactivated": cur.rowcount, "subject": subject}

        return await asyncio.to_thread(_write)

    async def consolidate(self, limit: int = 40) -> dict[str, Any]:
        recent = await self._recent(limit)
        if not recent:
            return {"ok": True, "summary": None, "message": "No hay episodios que consolidar"}
        lines = recent[-limit:]
        content = " | ".join(lines)[:1500]
        summary = await self.add_summary(
            "daily",
            content,
            source_from=None,
            source_to=time.time(),
        )
        return {"ok": True, "summary": summary}

    async def list_summaries(
        self, scope: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        assert self._conn is not None

        def _read() -> list[dict[str, Any]]:
            if scope is None:
                rows = self._conn.execute(
                    "SELECT * FROM summaries ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM summaries WHERE scope=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (scope, limit),
                ).fetchall()
            return [self._row(row) for row in rows]

        return await asyncio.to_thread(_read)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("metadata", "value"):
            if key in result:
                try:
                    result[key] = json.loads(result[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        if "active" in result:
            result["active"] = bool(result["active"])
        return result
