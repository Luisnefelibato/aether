from __future__ import annotations

import json
from typing import Any

from aether_core.tools import ToolRegistry, ToolSpec

from .scheduler import PersistentScheduler


def register_scheduler(tools: ToolRegistry, scheduler: PersistentScheduler) -> None:
    async def create(args: dict[str, Any]) -> str:
        routine = await scheduler.create_routine(
            str(args["name"]),
            float(args.get("interval_seconds") or 0),
            list(args.get("steps") or []),
            enabled=bool(args.get("enabled", True)),
            cron_expr=str(args["cron_expr"]) if args.get("cron_expr") else None,
            timezone=str(args["timezone"]) if args.get("timezone") else None,
        )
        return json.dumps({"ok": True, "verified": True, "routine": routine}, default=str)

    async def list_routines(_args: dict[str, Any]) -> str:
        routines = await scheduler.list_routines()
        return json.dumps({"ok": True, "verified": True, "routines": routines}, default=str)

    async def run_now(args: dict[str, Any]) -> str:
        result = await scheduler.run_routine(str(args["routine_id"]))
        ok = result.get("status") == "completed"
        return json.dumps({"ok": ok, "verified": ok, **result}, default=str)

    async def enable(args: dict[str, Any]) -> str:
        await scheduler.set_enabled(str(args["routine_id"]), bool(args.get("enabled", True)))
        return json.dumps({"ok": True, "verified": True})

    tools.register(
        ToolSpec(
            "routine.create",
            "Crea una rutina periódica cuyos pasos se ejecutan vía supervisor.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "interval_seconds": {"type": "number"},
                    "cron_expr": {"type": "string"},
                    "timezone": {"type": "string"},
                    "steps": {"type": "array"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["name", "steps"],
            },
            create,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "routine.list",
            "Lista rutinas persistentes.",
            {"type": "object", "properties": {}},
            list_routines,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "routine.run_now",
            "Ejecuta una rutina inmediatamente.",
            {
                "type": "object",
                "properties": {"routine_id": {"type": "string"}},
                "required": ["routine_id"],
            },
            run_now,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "routine.enable",
            "Activa o pausa una rutina.",
            {
                "type": "object",
                "properties": {
                    "routine_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["routine_id"],
            },
            enable,
            "L0",
        )
    )
