from __future__ import annotations

import asyncio
import json
from typing import Any

from aether_core.tools import ToolRegistry, ToolSpec

from .service import BackupService


def register_backup(
    tools: ToolRegistry, settings: Any, service: BackupService | None = None
) -> BackupService:
    backup = service or BackupService(settings)
    backup.open()

    definitions = {
        "create_set": (["name", "paths"], "L1"),
        "run": (["set_name"], "L1"),
        "verify": (["run_or_archive"], "L0"),
        "restore": (["run_or_archive", "destination"], "L2"),
    }
    for method, (required, level) in definitions.items():
        async def handler(args: dict[str, Any], name: str = method) -> str:
            if name == "create_set":
                result = await asyncio.to_thread(backup.create_set, args["name"], args["paths"])
            elif name == "run":
                result = await asyncio.to_thread(backup.run, args["set_name"])
            elif name == "verify":
                result = await asyncio.to_thread(backup.verify, args["run_or_archive"])
            else:
                result = await asyncio.to_thread(
                    backup.restore, args["run_or_archive"], args["destination"]
                )
            return json.dumps(result, ensure_ascii=False)

        properties = {
            key: (
                {"type": "array", "items": {"type": "string"}}
                if key == "paths"
                else {"type": "string"}
            )
            for key in required
        }
        tools.register(
            ToolSpec(
                f"backup.{method}",
                f"Operación de respaldo local: {method}.",
                {"type": "object", "properties": properties, "required": required},
                handler,
                level,
            )
        )
    return backup
