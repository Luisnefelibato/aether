from __future__ import annotations

import asyncio
import json
from typing import Any

from aether_core.tools import ToolRegistry, ToolSpec

from .service import HealthMonitor


def register_health(
    tools: ToolRegistry, settings: Any, monitor: HealthMonitor | None = None
) -> HealthMonitor:
    health = monitor or HealthMonitor(settings)
    health.open()

    for method in ("snapshot", "recent", "alerts"):
        async def handler(args: dict[str, Any], name: str = method) -> str:
            fn = getattr(health, name)
            result = (
                await asyncio.to_thread(fn, int(args.get("limit", 20)))
                if name != "snapshot"
                else await asyncio.to_thread(fn)
            )
            return json.dumps({"ok": True, "verified": True, "result": result})

        tools.register(
            ToolSpec(
                f"health.{method}",
                f"Salud local: {method}.",
                {
                    "type": "object",
                    "properties": (
                        {} if method == "snapshot" else {"limit": {"type": "integer"}}
                    ),
                },
                handler,
                "L0",
            )
        )
    return health
