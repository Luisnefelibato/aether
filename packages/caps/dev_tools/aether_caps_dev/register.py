from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec


def register_dev(tools: ToolRegistry, settings: Settings) -> None:
    async def git_status(args: dict[str, Any]) -> str:
        cwd = str(args.get("cwd") or ".")

        def _run():
            return subprocess.run(
                ["git", "status", "--short", "--branch"],
                cwd=cwd,
                capture_output=True,
                text=True,
            )

        completed = await asyncio.to_thread(_run)
        return json.dumps(
            {
                "ok": completed.returncode == 0,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )

    async def run_command(args: dict[str, Any]) -> str:
        cmd = str(args.get("command") or "")
        cwd = str(args.get("cwd") or ".")
        completed = await asyncio.to_thread(
            subprocess.run,
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=int(args.get("timeout") or 120),
        )
        return json.dumps(
            {
                "ok": completed.returncode == 0,
                "stdout": completed.stdout[-6000:],
                "stderr": completed.stderr[-2000:],
            }
        )

    async def open_in_cursor(args: dict[str, Any]) -> str:
        path = str(Path(str(args.get("path") or ".")).expanduser().resolve())
        reuse = bool(args.get("reuse_window", True))
        cmd = ["cursor"]
        if reuse:
            cmd.append("-r")
        cmd.append(path)
        try:
            subprocess.Popen(cmd, shell=False)
            verified = await _wait_for_cursor()
            return json.dumps(
                {
                    "ok": verified,
                    "verified": verified,
                    "message": f"Cursor abierto en {path}" if verified else "",
                    "error": None if verified else "No pude confirmar que Cursor se abriera",
                    "path": path,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    async def write_snippet(args: dict[str, Any]) -> str:
        path = Path(str(args.get("path") or "")).expanduser()
        content = str(args.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({"ok": True, "message": f"Snippet en {path}"})

    async def cursor_send_agent(args: dict[str, Any]) -> str:
        """Open project and send prompt to Cursor Agent via official SDK."""
        prompt = str(
            args.get("prompt")
            or args.get("task")
            or args.get("message")
            or ""
        ).strip()
        acceptance = str(args.get("acceptance_criteria") or "").strip()
        role = str(args.get("role") or "developer").strip()
        path = str(
            Path(str(args.get("path") or args.get("cwd") or ".")).expanduser().resolve()
        )
        model = str(args.get("model") or "composer-2.5")
        if not prompt:
            return json.dumps({"ok": False, "error": "prompt vacío"})
        delegated_prompt = (
            f"Role: {role}\nTask: {prompt}\n"
            "Work autonomously in the local repository. Inspect before editing, "
            "implement the task, run proportionate verification, and report exact results."
        )
        if acceptance:
            delegated_prompt += f"\nAcceptance criteria:\n{acceptance}"

        try:
            subprocess.Popen(["cursor", "-r", path], shell=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"no pude abrir Cursor: {exc}"})

        api_key = (settings.cursor_api_key or os.getenv("CURSOR_API_KEY", "")).strip()
        if not api_key:
            return json.dumps(
                {
                    "ok": False,
                    "verified": False,
                    "error": (
                        "Falta CURSOR_API_KEY en .env. Créala en "
                        "https://cursor.com/dashboard/api y pégala ahí."
                    ),
                }
            )
        from aether_supervisor.cursor_delegate import CursorDelegate

        result = await CursorDelegate(api_key, model=model).delegate(
            delegated_prompt, path
        )
        result.setdefault("path", path)
        result.setdefault("mode", "cursor_sdk")
        if result.get("ok") and not result.get("message"):
            result["message"] = "El agente de Cursor completó la misión"
        return json.dumps(result, ensure_ascii=False)

    tools.register(
        ToolSpec(
            "dev.git_status",
            "Obtiene git status del repo.",
            {"type": "object", "properties": {"cwd": {"type": "string"}}},
            git_status,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "dev.run_command",
            "Ejecuta un comando de desarrollo (L2).",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            run_command,
            "L2",
        )
    )
    tools.register(
        ToolSpec(
            "dev.open_editor",
            "Abre una ruta en Cursor.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "reuse_window": {"type": "boolean"},
                },
                "required": ["path"],
            },
            open_in_cursor,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "cursor.open_project",
            "Activa Cursor abriendo un proyecto/carpeta.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "reuse_window": {"type": "boolean"},
                },
                "required": ["path"],
            },
            open_in_cursor,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "cursor.send_agent",
            "Abre un proyecto en Cursor y envía un prompt al agente vía Cursor SDK "
            "(requiere CURSOR_API_KEY).",
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "task": {"type": "string"},
                    "message": {"type": "string"},
                    "path": {"type": "string"},
                    "cwd": {"type": "string"},
                    "model": {"type": "string"},
                    "role": {"type": "string"},
                    "acceptance_criteria": {"type": "string"},
                },
            },
            cursor_send_agent,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "agent.delegate_developer",
            "Delega una misión compleja de desarrollo a un agente Cursor autónomo y "
            "espera su resultado terminal antes de informar éxito.",
            {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "path": {"type": "string"},
                    "acceptance_criteria": {"type": "string"},
                    "model": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["task", "path"],
            },
            cursor_send_agent,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "dev.write_file",
            "Escribe un archivo de código.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            write_snippet,
            "L1",
        )
    )


async def _wait_for_cursor(timeout: float = 6.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            import psutil

            if any(
                str(proc.info.get("name") or "").lower() == "cursor.exe"
                for proc in psutil.process_iter(["name"])
            ):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    return False
