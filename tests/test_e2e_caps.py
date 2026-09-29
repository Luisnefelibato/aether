from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aether_core.config import Settings
from aether_core.tools import ToolRegistry
from aether_caps_life.register import register_life
from aether_memory.store import MemoryStore
from aether_memory.tools import register_memory_tools
from aether_policy.gate import PermissionGate


@pytest.mark.asyncio
async def test_e2e_memory_mail_policy_and_confirm(tmp_path: Path):
    settings = Settings()
    settings.memory.sqlite_path = str(tmp_path / "e2e.db")
    tools = ToolRegistry()
    memory = MemoryStore(settings)
    await memory.open()
    try:
        register_memory_tools(tools, memory)
        register_life(tools, settings)
        gate = PermissionGate(settings, tools)

        rejected = json.loads(
            await tools.call(
                "memory.set_fact",
                {
                    "subject": "user",
                    "predicate": "guess",
                    "value": "maybe",
                    "source": "unverified",
                },
            )
        )
        assert rejected["ok"] is False

        stored = json.loads(
            await tools.call(
                "memory.set_fact",
                {
                    "subject": "user",
                    "predicate": "city",
                    "value": "Bogota",
                    "source": "user",
                },
            )
        )
        assert stored["ok"] is True

        entity = json.loads(
            await tools.call(
                "memory.put_entity",
                {"name": "ADAM", "kind": "project"},
            )
        )
        listed = json.loads(await tools.call("memory.list_entities", {"kind": "project"}))
        assert entity["ok"] is True
        assert any(item["name"] == "ADAM" for item in listed["entities"])

        draft = json.loads(
            await tools.call(
                "mail.draft",
                {"to": "a@b.com", "subject": "Hola", "body": "cuerpo"},
            )
        )
        drafts = json.loads(await tools.call("mail.list_drafts", {}))
        assert draft["id"] in {item["id"] for item in drafts["drafts"]}

        send = gate.evaluate("mail.send", {"id": draft["id"]})
        assert send.allowed is True
        assert send.needs_confirm is True
    finally:
        await memory.close()
