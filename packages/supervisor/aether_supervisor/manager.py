from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from aether_core.config import Settings
from aether_core.events import EventBus
from aether_core.tools import ToolRegistry

from .missions import MissionWorker
from .storage import SupervisorStore

logger = logging.getLogger("aether.supervisor")


class ProcessSupervisor:
    def __init__(self, settings: Settings, tools: ToolRegistry, bus: EventBus) -> None:
        self.settings = settings
        self.tools = tools
        self.bus = bus
        self._sem = asyncio.Semaphore(settings.supervisor.max_concurrent_jobs)
        self._jobs: dict[str, asyncio.Task] = {}
        self._conn: sqlite3.Connection | None = None
        self._db_path = settings.resolve_path(settings.memory.sqlite_path)
        self._store = SupervisorStore(self._db_path)
        self._missions = MissionWorker(
            self._store,
            bus,
            api_key=settings.cursor_api_key,
            max_concurrency=settings.supervisor.max_concurrent_jobs,
        )

    async def open(self) -> None:
        await self._store.open()
        self._conn = self._store.connection
        orphaned_missions, orphaned_jobs = await self._store.recover_orphans()
        await self._missions.start()
        if orphaned_missions or orphaned_jobs:
            await self.bus.publish(
                "jobs_recovered",
                missions=orphaned_missions,
                jobs=orphaned_jobs,
                status="orphaned",
            )

    async def close(self) -> None:
        await self.cancel_all()
        await self._missions.close()
        await self._store.close()
        self._conn = None

    async def run_tool(
        self, name: str, arguments: dict[str, Any], force: bool = False
    ) -> str:
        job_id = str(uuid4())
        await self._persist_job(job_id, name, arguments, "running", None)
        await self.bus.publish("job_start", id=job_id, tool=name)
        task = asyncio.create_task(
            self._execute_tool(job_id, name, arguments),
            name=f"aether-tool-{job_id}",
        )
        self._jobs[job_id] = task
        try:
            return await task
        finally:
            self._jobs.pop(job_id, None)

    async def _execute_tool(
        self, job_id: str, name: str, arguments: dict[str, Any]
    ) -> str:
        async with self._sem:
            try:
                delegated = name.startswith(("cursor.", "agent."))
                timeout = (
                    self.settings.supervisor.delegated_timeout_s
                    if delegated
                    else self.settings.supervisor.default_timeout_s
                )
                result = await asyncio.wait_for(
                    self.tools.call(name, arguments), timeout=timeout
                )
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False, default=str)
                succeeded = self._result_succeeded(result)
                status = "done" if succeeded else "error"
                await self._persist_job(job_id, name, arguments, status, result)
                await self._audit(name, arguments, result)
                if succeeded:
                    await self.bus.publish(
                        "job_done", id=job_id, tool=name, result=result
                    )
                else:
                    await self.bus.publish(
                        "job_error",
                        id=job_id,
                        tool=name,
                        error=self._result_error(result),
                    )
                return result
            except asyncio.CancelledError:
                await self._persist_job(job_id, name, arguments, "cancelled", None)
                await self.bus.publish("job_cancelled", id=job_id, tool=name)
                return json.dumps({"ok": False, "error": "cancelled"})
            except Exception as exc:  # noqa: BLE001
                logger.exception("tool %s failed", name)
                err = json.dumps({"ok": False, "error": str(exc)})
                await self._persist_job(job_id, name, arguments, "error", err)
                await self._audit(name, arguments, err)
                await self.bus.publish("job_error", id=job_id, tool=name, error=str(exc))
                return err

    async def cancel_all(self) -> None:
        tasks = list(self._jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        for mission in await self._store.list_active_missions(limit=50):
            await self._missions.cancel(str(mission["id"]))
        await self.bus.publish("jobs_cancelled")

    async def submit_mission(
        self,
        prompt: str,
        *,
        cwd: str | Path = ".",
        model: str = "composer-2.5",
        verification_commands: list[str] | tuple[str, ...] = (),
        checks: tuple[Any, ...] | list[Any] = (),
    ) -> str:
        """Persist and enqueue a Cursor mission without waiting for completion."""
        return await self._missions.submit(
            prompt,
            cwd=cwd,
            model=model,
            verification_commands=verification_commands,
            checks=checks,
        )

    async def cancel_mission(self, mission_id: str) -> bool:
        return await self._missions.cancel(mission_id)

    async def wait_mission(
        self, mission_id: str, timeout: float | None = None
    ) -> dict[str, Any]:
        return await self._missions.wait(mission_id, timeout)

    async def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        return await self._store.get_mission(mission_id)

    async def snapshot(self) -> dict[str, Any]:
        jobs = await self._store.list_active_jobs()
        missions = await self._store.list_active_missions()
        return {
            "jobs": [
                {
                    "id": row["id"],
                    "tool": row.get("tool"),
                    "status": row.get("status"),
                    "label": row.get("tool") or row.get("kind") or "trabajo",
                    "progress": 0.5 if row.get("status") == "running" else 0.1,
                    "mission_id": row.get("mission_id"),
                }
                for row in jobs
            ],
            "missions": missions,
        }

    async def cancel_job(self, job_id: str) -> bool:
        task = self._jobs.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        job = await self._store.fetchone("SELECT mission_id FROM jobs WHERE id=?", (job_id,))
        if job and job.get("mission_id"):
            return await self.cancel_mission(str(job["mission_id"]))
        return False

    @staticmethod
    def _result_succeeded(result: str) -> bool:
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed, dict):
            return False
        if parsed.get("ok") is not True:
            return False
        return parsed.get("verified") is not False

    @staticmethod
    def _result_error(result: str) -> str:
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return result[:500]
        if isinstance(parsed, dict):
            return str(parsed.get("error") or parsed.get("message") or "tool failed")
        return "tool failed"

    async def _persist_job(
        self,
        job_id: str,
        tool: str,
        args: dict[str, Any],
        status: str,
        result: str | None,
    ) -> None:
        await self._store.upsert_job(job_id, tool, args, status, result)
        await self._store.add_event(
            job_id,
            status,
            {"tool": tool, "result": result},
        )

    async def _audit(self, tool: str, args: dict[str, Any], result: str) -> None:
        level = self.tools.level_for(tool)
        await self._store.execute(
            "INSERT INTO audit(id, tool, args, level, result, ts) VALUES(?,?,?,?,?,?)",
            (
                str(uuid4()),
                tool,
                json.dumps(args, ensure_ascii=False),
                level,
                result[:4000],
                time.time(),
            ),
        )
