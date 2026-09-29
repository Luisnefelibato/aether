from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable


Check = Callable[[], bool | Awaitable[bool]]


class VerificationRunner:
    """Runs acceptance commands/checks and always returns the ok/verified contract."""

    def __init__(self, default_timeout_s: float = 300) -> None:
        self.default_timeout_s = default_timeout_s

    async def run(
        self,
        *,
        commands: Iterable[str] = (),
        checks: Iterable[Check] = (),
        cwd: str | Path = ".",
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        command_results: list[dict[str, Any]] = []
        check_results: list[dict[str, Any]] = []
        timeout = timeout_s or self.default_timeout_s

        for command in commands:
            command_results.append(await self._command(command, str(cwd), timeout))

        for index, check in enumerate(checks):
            try:
                value = check()
                passed = bool(await value) if inspect.isawaitable(value) else bool(value)
                check_results.append({"check": index, "ok": passed})
            except Exception as exc:  # noqa: BLE001
                check_results.append({"check": index, "ok": False, "error": str(exc)})

        verified = all(item["ok"] for item in command_results + check_results)
        # No requested verification is successful execution, but not verified evidence.
        if not command_results and not check_results:
            verified = False
        return {
            "ok": verified,
            "verified": verified,
            "commands": command_results,
            "checks": check_results,
            "error": None if verified else "verification failed",
        }

    @staticmethod
    async def _command(command: str, cwd: str, timeout: float) -> dict[str, Any]:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {"command": command, "ok": False, "error": "timeout"}
        return {
            "command": command,
            "ok": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": stdout.decode(errors="replace")[-4000:],
            "stderr": stderr.decode(errors="replace")[-4000:],
        }

    async def git_status(self, cwd: str | Path) -> dict[str, Any]:
        return await self._command("git status --porcelain --branch", str(cwd), 30)

    async def files_exist(self, cwd: str | Path, paths: Iterable[str]) -> dict[str, Any]:
        root = Path(cwd)
        missing = [path for path in paths if not (root / path).exists()]
        ok = not missing
        return {"ok": ok, "verified": ok, "missing": missing}
