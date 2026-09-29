from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

SupervisorCallback = Callable[[dict[str, Any], dict[str, Any]], Any | Awaitable[Any]]


class PersistentScheduler:
    """Persistent interval routines whose steps execute only through a supervisor."""

    def __init__(
        self, settings: Any = None, supervisor: SupervisorCallback | None = None
    ) -> None:
        config = getattr(settings, "scheduler", settings)
        self.db_path = Path(
            getattr(config, "sqlite_path", Path.cwd() / ".aether-scheduler.sqlite3")
        ).resolve()
        self.poll_seconds = max(0.05, float(getattr(config, "poll_seconds", 1.0)))
        self.max_missed_catchup = max(1, int(getattr(config, "max_missed_catchup", 3)))
        self.timezone = str(getattr(config, "timezone", "America/Bogota") or "America/Bogota")
        self.supervisor = supervisor
        self._conn: sqlite3.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS routines (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                interval_seconds REAL NOT NULL, enabled INTEGER NOT NULL,
                next_run REAL NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS routine_steps (
                id TEXT PRIMARY KEY, routine_id TEXT NOT NULL,
                position INTEGER NOT NULL, action TEXT NOT NULL,
                arguments TEXT NOT NULL, UNIQUE(routine_id, position),
                FOREIGN KEY(routine_id) REFERENCES routines(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS routine_runs (
                id TEXT PRIMARY KEY, routine_id TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL,
                status TEXT NOT NULL, error TEXT
            );
            """
        )
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(routines)").fetchall()
        }
        if "cron_expr" not in columns:
            self._conn.execute("ALTER TABLE routines ADD COLUMN cron_expr TEXT")
        if "timezone" not in columns:
            self._conn.execute("ALTER TABLE routines ADD COLUMN timezone TEXT")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    async def close(self) -> None:
        await self.stop()
        if self._conn:
            self._conn.close()
            self._conn = None

    async def create_routine(
        self,
        name: str,
        interval_seconds: float,
        steps: list[dict[str, Any]],
        *,
        enabled: bool = True,
        run_at: float | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        cron = (cron_expr or "").strip() or None
        if not cron and interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive unless cron_expr is set")
        if cron:
            interval_seconds = max(float(interval_seconds), 60.0)
        if not steps:
            raise ValueError("routine requires at least one step")
        tz = timezone or self.timezone
        conn = self._db()
        routine_id, now = str(uuid4()), time.time()
        next_run = run_at or self._next_after(
            {
                "interval_seconds": interval_seconds,
                "cron_expr": cron,
                "timezone": tz,
            },
            now,
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT id FROM routines WHERE name=?", (name,)
            ).fetchone()
            if existing:
                routine_id = existing["id"]
                conn.execute(
                    """
                    UPDATE routines SET interval_seconds=?,enabled=?,next_run=?,
                        cron_expr=?,timezone=? WHERE id=?
                    """,
                    (interval_seconds, int(enabled), next_run, cron, tz, routine_id),
                )
                conn.execute("DELETE FROM routine_steps WHERE routine_id=?", (routine_id,))
            else:
                conn.execute(
                    """
                    INSERT INTO routines(
                        id,name,interval_seconds,enabled,next_run,created_at,cron_expr,timezone
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        routine_id,
                        name,
                        interval_seconds,
                        int(enabled),
                        next_run,
                        now,
                        cron,
                        tz,
                    ),
                )
            for position, step in enumerate(steps):
                action = str(step.get("action") or step.get("tool") or "")
                if not action:
                    raise ValueError(f"step {position} requires action")
                conn.execute(
                    "INSERT INTO routine_steps VALUES(?,?,?,?,?)",
                    (
                        str(uuid4()),
                        routine_id,
                        position,
                        action,
                        json.dumps(step.get("arguments", step.get("args", {}))),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return await self.get_routine(routine_id)

    async def get_routine(self, routine_id: str) -> dict[str, Any]:
        conn = self._db()
        row = conn.execute("SELECT * FROM routines WHERE id=?", (routine_id,)).fetchone()
        if row is None:
            raise KeyError(routine_id)
        steps = conn.execute(
            "SELECT * FROM routine_steps WHERE routine_id=? ORDER BY position",
            (routine_id,),
        ).fetchall()
        return {
            **dict(row),
            "enabled": bool(row["enabled"]),
            "cron_expr": row["cron_expr"] if "cron_expr" in row.keys() else None,
            "timezone": row["timezone"] if "timezone" in row.keys() else self.timezone,
            "steps": [
                {**dict(step), "arguments": json.loads(step["arguments"])}
                for step in steps
            ],
        }

    async def list_routines(self) -> list[dict[str, Any]]:
        rows = self._db().execute("SELECT id FROM routines ORDER BY name").fetchall()
        return [await self.get_routine(row["id"]) for row in rows]

    async def set_enabled(self, routine_id: str, enabled: bool) -> None:
        conn = self._db()
        conn.execute(
            "UPDATE routines SET enabled=?,next_run=? WHERE id=?",
            (int(enabled), time.time(), routine_id),
        )
        conn.commit()

    async def run_due(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        rows = self._db().execute(
            "SELECT id FROM routines WHERE enabled=1 AND next_run<=? ORDER BY next_run",
            (current,),
        ).fetchall()
        return [await self.run_routine(row["id"], scheduled_at=current) for row in rows]

    async def run_routine(
        self, routine_id: str, *, scheduled_at: float | None = None
    ) -> dict[str, Any]:
        if self.supervisor is None:
            raise RuntimeError("scheduler requires a supervisor callback")
        routine = await self.get_routine(routine_id)
        run_id, started = str(uuid4()), time.time()
        conn = self._db()
        conn.execute(
            "INSERT INTO routine_runs VALUES(?,?,?,NULL,'running',NULL)",
            (run_id, routine_id, started),
        )
        conn.commit()
        results, status, error = [], "completed", None
        try:
            for step in routine["steps"]:
                result = self.supervisor(routine, step)
                if inspect.isawaitable(result):
                    result = await result
                results.append(result)
        except Exception as exc:
            status, error = "failed", str(exc)
        finally:
            next_run = self._next_after(routine, scheduled_at or time.time())
            skipped = 0
            now = time.time()
            while next_run <= now and skipped < self.max_missed_catchup:
                next_run = self._next_after(routine, next_run)
                skipped += 1
            if next_run <= now:
                next_run = self._next_after(routine, now)
            conn.execute(
                "UPDATE routines SET next_run=? WHERE id=?", (next_run, routine_id)
            )
            conn.execute(
                "UPDATE routine_runs SET finished_at=?,status=?,error=? WHERE id=?",
                (time.time(), status, error, run_id),
            )
            conn.commit()
        return {"id": run_id, "status": status, "error": error, "results": results}

    async def seed_defaults(self) -> list[str]:
        """Insert disabled built-in routines once; never re-enable user changes."""
        created: list[str] = []
        defaults = (
            (
                "health_hourly",
                3600.0,
                [{"action": "health.snapshot", "arguments": {}}],
                None,
            ),
            (
                "memory_daily_consolidate",
                86400.0,
                [{"action": "memory.consolidate", "arguments": {"limit": 40}}],
                "15 3 * * *",
            ),
            (
                "files_index_daily",
                86400.0,
                [{"action": "files.index", "arguments": {}}],
                "0 4 * * *",
            ),
            (
                "morning_health",
                86400.0,
                [{"action": "health.snapshot", "arguments": {}}],
                "0 8 * * *",
            ),
            (
                "backup_weekly",
                604800.0,
                [{"action": "backup.run", "arguments": {"set_name": "default"}}],
                "0 5 * * 0",
            ),
        )
        conn = self._db()
        for name, interval, steps, cron in defaults:
            existing = conn.execute(
                "SELECT id FROM routines WHERE name=?", (name,)
            ).fetchone()
            if existing:
                continue
            await self.create_routine(
                name, interval, steps, enabled=False, cron_expr=cron
            )
            created.append(name)
        return created

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="aether-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_due()
            except Exception:
                # Keep the scheduler alive; individual run failures are persisted.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    def _next_after(self, routine: dict[str, Any], after: float) -> float:
        cron = str(routine.get("cron_expr") or "").strip()
        if cron:
            from datetime import datetime
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            from croniter import croniter

            tz_name = str(routine.get("timezone") or self.timezone)
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = datetime.now().astimezone().tzinfo
            moment = datetime.fromtimestamp(after, tz)
            return croniter(cron, moment).get_next(datetime).timestamp()
        interval = float(routine.get("interval_seconds") or 0)
        if interval <= 0:
            raise ValueError("routine is missing interval_seconds")
        return after + interval

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("PersistentScheduler.open() must be called first")
        return self._conn
