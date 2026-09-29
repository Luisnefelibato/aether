from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class CursorDelegateResult:
    ok: bool
    verified: bool
    status: str
    agent_id: str | None = None
    run_id: str | None = None
    summary: str = ""
    error: str | None = None
    error_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "status": self.status,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "summary": self.summary,
            "error": self.error,
            "error_kind": self.error_kind,
            "runtime": "local",
        }


class CursorDelegate:
    """Owns one local Cursor SDK agent for one delegated mission."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "composer-2.5",
        sdk: Any | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model
        self._sdk = sdk

    def _load_sdk(self) -> Any:
        if self._sdk is None:
            import cursor_sdk  # type: ignore[import-not-found]

            self._sdk = cursor_sdk
        return self._sdk

    async def delegate(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        path = str(Path(cwd).expanduser().resolve())
        if not self.api_key:
            return CursorDelegateResult(
                False,
                False,
                "startup_failed",
                error="CURSOR_API_KEY is required",
                error_kind="startup_failure",
            ).as_dict()

        agent: Any = None
        owner: Any = None
        run: Any = None
        run_id: str | None = None
        agent_id: str | None = None
        await self._notify(on_progress, "starting", {"cwd": path})
        try:
            sdk = self._load_sdk()
            try:
                owner = await asyncio.to_thread(self._create_agent, sdk, path)
                agent = await asyncio.to_thread(self._enter, owner)
                agent_id = self._identifier(agent, "agent_id", "id", "agentId")
                run = await asyncio.to_thread(agent.send, prompt)
                run_id = self._identifier(run, "id", "run_id", "runId")
            except Exception as exc:  # SDK errors here mean no usable run started.
                return CursorDelegateResult(
                    False,
                    False,
                    "startup_failed",
                    agent_id=agent_id,
                    error=str(exc),
                    error_kind="startup_failure",
                ).as_dict()

            await self._notify(
                on_progress,
                "running",
                {"agent_id": agent_id, "run_id": run_id},
            )
            try:
                result = await asyncio.to_thread(run.wait)
            except asyncio.CancelledError:
                await self._cancel_run(run)
                raise
            except Exception as exc:
                return CursorDelegateResult(
                    False,
                    False,
                    "run_failed",
                    agent_id=agent_id,
                    run_id=run_id,
                    error=str(exc),
                    error_kind="run_failure",
                ).as_dict()

            status = self._status(result)
            succeeded = status in {"finished", "success", "completed", "ok"}
            summary = self._summary(result)
            output = CursorDelegateResult(
                succeeded,
                succeeded,
                status,
                agent_id=agent_id,
                run_id=run_id or self._identifier(result, "id", "run_id", "runId"),
                summary=summary,
                error=None if succeeded else f"Cursor run ended with status {status}",
                error_kind=None if succeeded else "run_failure",
            ).as_dict()
            await self._notify(on_progress, "finished", output)
            return output
        except asyncio.CancelledError:
            await self._notify(
                on_progress,
                "cancelled",
                {"agent_id": agent_id, "run_id": run_id},
            )
            raise
        except (ImportError, ModuleNotFoundError) as exc:
            return CursorDelegateResult(
                False,
                False,
                "startup_failed",
                error=f"cursor-sdk is not installed: {exc}",
                error_kind="startup_failure",
            ).as_dict()
        finally:
            if owner is not None:
                await asyncio.to_thread(self._exit, owner, agent)

    def _create_agent(self, sdk: Any, cwd: str) -> Any:
        local = sdk.LocalAgentOptions(cwd=cwd)
        try:
            return sdk.Agent.create(
                model=self.model,
                api_key=self.api_key,
                local=local,
            )
        except TypeError:
            options = sdk.AgentOptions(
                api_key=self.api_key,
                model=self.model,
                local=local,
            )
            return sdk.Agent.create(options)

    @staticmethod
    def _enter(owner: Any) -> Any:
        enter = getattr(owner, "__enter__", None)
        return enter() if enter is not None else owner

    @staticmethod
    def _exit(owner: Any, agent: Any) -> None:
        exit_method = getattr(owner, "__exit__", None)
        if exit_method is not None:
            exit_method(None, None, None)
            return
        close = getattr(agent, "close", None)
        if close is not None:
            close()

    @staticmethod
    async def _cancel_run(run: Any) -> None:
        cancel = getattr(run, "cancel", None)
        if cancel is not None:
            await asyncio.to_thread(cancel)

    @staticmethod
    async def _notify(
        callback: ProgressCallback | None,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        if callback is None:
            return
        result = callback(phase, payload)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _identifier(value: Any, *names: str) -> str | None:
        for name in names:
            found = getattr(value, name, None)
            if found:
                return str(found)
        return None

    @staticmethod
    def _status(result: Any) -> str:
        value = getattr(result, "status", "error")
        return str(getattr(value, "value", value)).lower().split(".")[-1]

    @staticmethod
    def _summary(result: Any) -> str:
        value = getattr(result, "result", None)
        if value is None:
            value = getattr(result, "text", "")
        return str(value or "")[:4000]
