from __future__ import annotations

import json
from typing import Any

import pytest

from aether_brain.kimi import KimiPlanner
from aether_core.config import Settings


class FakeKimiPlanner(KimiPlanner):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        settings = Settings(moonshot_api_key="test")
        super().__init__(settings)
        self.responses = responses
        self.required_flags: list[bool] = []

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        require_tool: bool = False,
    ) -> dict[str, Any]:
        self.required_flags.append(require_tool)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_action_turn_forces_tool_and_executes_it():
    planner = FakeKimiPlanner(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {
                                        "name": "desktop_open_app",
                                        "arguments": json.dumps({"name": "notepad"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "Listo.", "tool_calls": []}}]},
        ]
    )

    calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, arguments: dict[str, Any]) -> str:
        calls.append((name, arguments))
        return json.dumps({"ok": True, "verified": True})

    reply, trace = await planner.run_agent_loop(
        [{"role": "user", "content": "abre notepad"}],
        [{"type": "function", "function": {"name": "desktop_open_app"}}],
        execute,
        require_tool=True,
    )

    assert calls == [("desktop_open_app", {"name": "notepad"})]
    assert trace
    assert reply == "Listo."
    assert planner.required_flags[0] is True
    await planner.close()


@pytest.mark.asyncio
async def test_action_never_accepts_unverified_text_only_success():
    planner = FakeKimiPlanner(
        [
            {"choices": [{"message": {"content": "Ya lo abrí.", "tool_calls": []}}]},
            {"choices": [{"message": {"content": "Hecho.", "tool_calls": []}}]},
        ]
    )

    async def execute(_name: str, _arguments: dict[str, Any]) -> str:
        raise AssertionError("No tool should have been called")

    reply, trace = await planner.run_agent_loop(
        [{"role": "user", "content": "abre la aplicación"}],
        [{"type": "function", "function": {"name": "desktop_open_app"}}],
        execute,
        require_tool=True,
    )

    assert trace == []
    assert "No pude ejecutar" in reply
    assert planner.required_flags == [True, True]
    await planner.close()
