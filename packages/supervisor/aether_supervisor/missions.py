from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from aether_core.events import EventBus

from .cursor_delegate import CursorDelegate
from .storage import SupervisorStore
from .verification import Check, VerificationRunner


@dataclass(slots=True)
class MissionRequest:
    id: str
    prompt: str
    cwd: str
    model: str
    verification_commands: tuple[str, ...] = ()
    checks: tuple[Check, ...] = field(default_factory=tuple)


class MissionWorker:
    """Durable background dispatcher with tracked, cancellable mission tasks."""

    def __init__(
        self,
        store: SupervisorStore,
        bus: EventBus,
        *,
        api_key: str,
        max_concurrency: int = 1,
        delegate_factory: Any = CursorDelegate,
        verification_runner: VerificationRunner | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.api_key = api_key
        self.delegate_factory = delegate_factory
        self.verification_runner = verification_runner or VerificationRunner()
        self._queue: asyncio.Queue[MissionRequest | None] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._dispatcher: asyncio.Task[None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._completions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._queued: set[str] = set()
        self._closing = False

    @property
    def tasks(self) -> dict[str, asyncio.Task[None]]:
        return dict(self._tasks)

    async def start(self) -> None:
        if self._dispatcher is None or self._dispatcher.done():
            self._closing = False
            self._dispatcher = asyncio.create_task(
                self._dispatch(), name="aether-mission-dispatcher"
            )

    async def submit(
        self,
        prompt: str,
        *,
        cwd: str | Path = ".",
        model: str = "composer-2.5",
        verification_commands: list[str] | tuple[str, ...] = (),
        checks: list[Check] | tuple[Check, ...] = (),
    ) -> str:
        if self._closing:
            raise RuntimeError("mission worker is closing")
        if self._dispatcher is None:
            await self.start()
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("mission prompt cannot be empty")
        path = str(Path(cwd).expanduser().resolve())
        mission_id = await self.store.create_mission(normalized_prompt, path)
        request = MissionRequest(
            mission_id,
            normalized_prompt,
            path,
            model,
            tuple(verification_commands),
            tuple(checks),
        )
        self._completions[mission_id] = asyncio.get_running_loop().create_future()
        self._queued.add(mission_id)
        await self._queue.put(request)
        await self.bus.publish("mission_submitted", id=mission_id, cwd=path)
        return mission_id

    async def cancel(self, mission_id: str) -> bool:
        task = self._tasks.get(mission_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True
        if mission_id in self._queued:
            self._queued.remove(mission_id)
            result = self._cancel_result()
            await self.store.update_mission(mission_id, "cancelled", result)
            self._complete(mission_id, result)
            await self.bus.publish("mission_cancelled", id=mission_id)
            return True
        return False

    async def wait(
        self, mission_id: str, timeout: float | None = None
    ) -> dict[str, Any]:
        future = self._completions.get(mission_id)
        if future is None:
            mission = await self.store.get_mission(mission_id)
            if mission is None:
                raise KeyError(mission_id)
            if mission["status"] in {
                "done",
                "error",
                "cancelled",
                "orphaned",
            }:
                result = mission.get("result")
                return result if isinstance(result, dict) else mission
            raise RuntimeError("mission is not owned by this worker")
        return await asyncio.wait_for(asyncio.shield(future), timeout)

    async def close(self) -> None:
        self._closing = True
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None

    async def _dispatch(self) -> None:
        while True:
            request = await self._queue.get()
            if request is None:
                return
            if request.id not in self._queued:
                self._queue.task_done()
                continue
            self._queued.remove(request.id)
            task = asyncio.create_task(
                self._run_guarded(request),
                name=f"aether-mission-{request.id}",
            )
            self._tasks[request.id] = task
            task.add_done_callback(
                lambda completed, mission_id=request.id: self._task_done(
                    mission_id, completed
                )
            )
            self._queue.task_done()

    async def _run_guarded(self, request: MissionRequest) -> None:
        async with self._semaphore:
            await self._run(request)

    async def _run(self, request: MissionRequest) -> None:
        job_id = str(uuid4())
        cursor_record_id = await self.store.create_cursor_run(request.id, job_id)
        arguments = {"prompt": request.prompt, "cwd": request.cwd, "model": request.model}
        await self.store.update_mission(request.id, "running")
        await self.store.upsert_job(
            job_id,
            "cursor.delegate",
            arguments,
            "running",
            None,
            mission_id=request.id,
            kind="cursor",
        )
        await self.store.add_event(job_id, "started", arguments, request.id)
        await self.bus.publish("mission_start", id=request.id, job_id=job_id)

        async def progress(phase: str, payload: dict[str, Any]) -> None:
            status = "running" if phase in {"starting", "running"} else phase
            await self.store.update_cursor_run(
                cursor_record_id,
                status,
                agent_id=payload.get("agent_id"),
                run_id=payload.get("run_id"),
            )
            await self.store.add_event(job_id, phase, payload, request.id)
            await self.bus.publish(
                "mission_progress",
                id=request.id,
                job_id=job_id,
                phase=phase,
                **payload,
            )

        delegate = self.delegate_factory(
            self.api_key,
            model=request.model,
        )
        try:
            result = await delegate.delegate(
                request.prompt, request.cwd, on_progress=progress
            )
            result["git"] = await self.verification_runner.git_status(request.cwd)
            if result.get("ok") and (
                request.verification_commands or request.checks
            ):
                verification = await self.verification_runner.run(
                    commands=request.verification_commands,
                    checks=request.checks,
                    cwd=request.cwd,
                )
                result["verification"] = verification
                result["verified"] = verification["verified"]
                result["ok"] = bool(result["ok"] and verification["ok"])
                if not result["ok"]:
                    result["error"] = verification["error"]
                    result["error_kind"] = "verification_failure"

            status = "done" if result.get("ok") and result.get("verified") else "error"
            serialized = json.dumps(result, ensure_ascii=False, default=str)
            await self.store.update_cursor_run(
                cursor_record_id,
                status,
                agent_id=result.get("agent_id"),
                run_id=result.get("run_id"),
                error_kind=result.get("error_kind"),
                result=result,
            )
            await self.store.upsert_job(
                job_id,
                "cursor.delegate",
                arguments,
                status,
                serialized,
                mission_id=request.id,
                kind="cursor",
            )
            await self.store.update_mission(
                request.id, status, result, result.get("error_kind")
            )
            await self.store.add_event(job_id, status, result, request.id)
            self._complete(request.id, result)
            await self.bus.publish(
                "mission_done" if status == "done" else "mission_error",
                id=request.id,
                job_id=job_id,
                result=result,
            )
        except asyncio.CancelledError:
            result = self._cancel_result()
            await self.store.update_cursor_run(
                cursor_record_id,
                "cancelled",
                error_kind="cancelled",
                result=result,
            )
            await self.store.upsert_job(
                job_id,
                "cursor.delegate",
                arguments,
                "cancelled",
                json.dumps(result),
                mission_id=request.id,
                kind="cursor",
            )
            await self.store.update_mission(request.id, "cancelled", result, "cancelled")
            await self.store.add_event(job_id, "cancelled", result, request.id)
            self._complete(request.id, result)
            await self.bus.publish(
                "mission_cancelled", id=request.id, job_id=job_id
            )
            raise

    def _task_done(self, mission_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(mission_id, None)
        if not task.cancelled() and task.exception() is not None:
            result = {
                "ok": False,
                "verified": False,
                "error": str(task.exception()),
                "error_kind": "worker_failure",
            }
            self._complete(mission_id, result)

    def _complete(self, mission_id: str, result: dict[str, Any]) -> None:
        future = self._completions.get(mission_id)
        if future is not None and not future.done():
            future.set_result(result)

    @staticmethod
    def _cancel_result() -> dict[str, Any]:
        return {
            "ok": False,
            "verified": False,
            "status": "cancelled",
            "error": "cancelled",
            "error_kind": "cancelled",
        }
