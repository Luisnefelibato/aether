"""Prove local HA lights toggle via ADAM tools. Prints only ok/state, never tokens."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "packages" / "caps" / "iot_home"))

from aether_caps_iot.register import register_iot
from aether_core.config import load_settings
from aether_core.tools import ToolRegistry


async def main() -> None:
    tools = ToolRegistry()
    register_iot(tools, load_settings())
    ping = json.loads(await tools.call("home.check_connection", {}))
    if not ping.get("ok"):
        raise SystemExit(f"HA no conecta: {ping.get('error') or ping}")
    off = json.loads(
        await tools.call(
            "home.set_device", {"entity_id": "oficina", "action": "off"}
        )
    )
    on = json.loads(
        await tools.call(
            "home.set_device", {"entity_id": "oficina", "action": "on"}
        )
    )
    print(
        json.dumps(
            {
                "connected": True,
                "off_ok": off.get("ok"),
                "off_state": off.get("state"),
                "on_ok": on.get("ok"),
                "on_state": on.get("state"),
            }
        )
    )
    if not (off.get("ok") and on.get("ok") and on.get("state") == "on"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
