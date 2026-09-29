from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


class SupervisorStore:
    """Small async facade over the supervisor's durable SQLite state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> sqlite3.Connection:
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            self._migrate(connection)
            return connection

        self.connection = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self.connection is not None:
            await asyncio.to_thread(self.connection.close)
            self.connection = None

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        # Never rename or recreate jobs: installations may already contain legacy rows.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                args TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id TEXT PRIMARY KEY,
                tool TEXT NOT NULL,
                args TEXT NOT NULL,
                level TEXT,
                result TEXT,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                cwd TEXT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error_kind TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_events (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                mission_id TEXT,
                event TEXT NOT NULL,
                payload TEXT,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cursor_runs (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                job_id TEXT,
                agent_id TEXT,
                run_id TEXT,
                status TEXT NOT NULL,
                error_kind TEXT,
                result TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, ts);
            CREATE INDEX IF NOT EXISTS idx_cursor_mission ON cursor_runs(mission_id);
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, declaration in (
            ("mission_id", "TEXT"),
            ("kind", "TEXT"),
            ("cancellation_requested", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        connection.commit()

    async def execute(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> None:
        connection = self._require_connection()
        async with self._lock:
            def _execute() -> None:
                connection.execute(sql, parameters)
                connection.commit()

            await asyncio.to_thread(_execute)

    async def fetchone(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        async with self._lock:
            row = await asyncio.to_thread(
                lambda: connection.execute(sql, parameters).fetchone()
            )
        return dict(row) if row is not None else None

    async def fetchall(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        async with self._lock:
            rows = await asyncio.to_thread(
                lambda: connection.execute(sql, parameters).fetchall()
            )
        return [dict(row) for row in rows]

    async def list_active_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT id, tool, status, result, created_at, updated_at, mission_id, kind "
            "FROM jobs WHERE status IN ('queued','running','verifying') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )

    async def list_active_missions(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT id, prompt, cwd, status, created_at, updated_at "
            "FROM missions WHERE status IN ('queued','running') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )

    async def recover_orphans(self) -> tuple[int, int]:
        now = time.time()
        connection = self._require_connection()
        async with self._lock:
            def _recover() -> tuple[int, int]:
                missions = connection.execute(
                    "UPDATE missions SET status='orphaned', updated_at=? "
                    "WHERE status='running'",
                    (now,),
                ).rowcount
                jobs = connection.execute(
                    "UPDATE jobs SET status='orphaned', updated_at=? "
                    "WHERE status='running'",
                    (now,),
                ).rowcount
                connection.execute(
                    "UPDATE cursor_runs SET status='orphaned', updated_at=? "
                    "WHERE status='running'",
                    (now,),
                )
                connection.commit()
                return missions, jobs

            return await asyncio.to_thread(_recover)

    async def create_mission(self, prompt: str, cwd: str) -> str:
        mission_id = str(uuid4())
        now = time.time()
        await self.execute(
            "INSERT INTO missions(id,prompt,cwd,status,created_at,updated_at) "
            "VALUES(?,?,?,'queued',?,?)",
            (mission_id, prompt, cwd, now, now),
        )
        return mission_id

    async def update_mission(
        self,
        mission_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error_kind: str | None = None,
    ) -> None:
        await self.execute(
            "UPDATE missions SET status=?, result=?, error_kind=?, updated_at=? "
            "WHERE id=?",
            (
                status,
                json.dumps(result, ensure_ascii=False, default=str)
                if result is not None
                else None,
                error_kind,
                time.time(),
                mission_id,
            ),
        )

    async def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        row = await self.fetchone("SELECT * FROM missions WHERE id=?", (mission_id,))
        if row and row.get("result"):
            try:
                row["result"] = json.loads(row["result"])
            except json.JSONDecodeError:
                pass
        return row

    async def upsert_job(
        self,
        job_id: str,
        tool: str,
        arguments: dict[str, Any],
        status: str,
        result: str | None,
        *,
        mission_id: str | None = None,
        kind: str | None = None,
    ) -> None:
        now = time.time()
        await self.execute(
            """
            INSERT INTO jobs(
                id,tool,args,status,result,created_at,updated_at,mission_id,kind
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, result=excluded.result,
                mission_id=COALESCE(excluded.mission_id,jobs.mission_id),
                kind=COALESCE(excluded.kind,jobs.kind),
                updated_at=excluded.updated_at
            """,
            (
                job_id,
                tool,
                json.dumps(arguments, ensure_ascii=False, default=str),
                status,
                result,
                now,
                now,
                mission_id,
                kind,
            ),
        )

    async def add_event(
        self,
        job_id: str,
        event: str,
        payload: dict[str, Any] | None = None,
        mission_id: str | None = None,
    ) -> None:
        await self.execute(
            "INSERT INTO job_events(id,job_id,mission_id,event,payload,ts) "
            "VALUES(?,?,?,?,?,?)",
            (
                str(uuid4()),
                job_id,
                mission_id,
                event,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                time.time(),
            ),
        )

    async def create_cursor_run(self, mission_id: str, job_id: str) -> str:
        record_id = str(uuid4())
        now = time.time()
        await self.execute(
            "INSERT INTO cursor_runs("
            "id,mission_id,job_id,status,created_at,updated_at"
            ") VALUES(?,?,?,'starting',?,?)",
            (record_id, mission_id, job_id, now, now),
        )
        return record_id

    async def update_cursor_run(
        self,
        record_id: str,
        status: str,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        error_kind: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        await self.execute(
            """
            UPDATE cursor_runs SET status=?,
                agent_id=COALESCE(?,agent_id), run_id=COALESCE(?,run_id),
                error_kind=?, result=?, updated_at=? WHERE id=?
            """,
            (
                status,
                agent_id,
                run_id,
                error_kind,
                json.dumps(result, ensure_ascii=False, default=str)
                if result is not None
                else None,
                time.time(),
                record_id,
            ),
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("supervisor store is not open")
        return self.connection
