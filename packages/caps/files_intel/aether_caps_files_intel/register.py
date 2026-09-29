from __future__ import annotations

import asyncio
import json
from typing import Any

from aether_core.tools import ToolRegistry, ToolSpec

from .service import FilesIntel


def register_files_intel(
    tools: ToolRegistry, settings: Any, service: FilesIntel | None = None
) -> FilesIntel:
    files = service or FilesIntel(settings)
    files.open()

    async def call(method: str, args: dict[str, Any]) -> str:
        try:
            fn = getattr(files, method)
            if method == "index":
                value = await asyncio.to_thread(fn, args.get("roots"))
            elif method == "search":
                value = await asyncio.to_thread(fn, str(args.get("query", "")), int(args.get("limit", 100)))
            elif method in {"copy", "move"}:
                value = await asyncio.to_thread(fn, args["source"], args["destination"])
            elif method == "trash":
                value = await asyncio.to_thread(fn, args["path"])
            elif method == "restore":
                value = await asyncio.to_thread(fn, args["trash_id"])
            else:
                value = await asyncio.to_thread(fn)
            if isinstance(value, dict) and value.get("ok") is False:
                return json.dumps({"ok": False, "verified": False, **value}, ensure_ascii=False)
            return json.dumps(
                {"ok": True, "verified": True, "result": value},
                ensure_ascii=False,
                default=str,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "verified": False, "error": str(exc)})

    definitions = {
        "index": ({"roots": {"type": "array", "items": {"type": "string"}}}, []),
        "search": ({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
        "duplicates": ({}, []),
        "copy": ({"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"]),
        "move": ({"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"]),
        "trash": ({"path": {"type": "string"}}, ["path"]),
        "restore": ({"trash_id": {"type": "string"}}, ["trash_id"]),
    }
    for method, (properties, required) in definitions.items():
        async def handler(args: dict[str, Any], name: str = method) -> str:
            return await call(name, args)

        tools.register(
            ToolSpec(
                f"files.{method}",
                f"Operación local de archivos: {method}.",
                {"type": "object", "properties": properties, "required": required},
                handler,
                "L1" if method in {"copy", "move", "trash", "restore"} else "L0",
            )
        )
    return files
