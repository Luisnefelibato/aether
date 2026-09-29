from __future__ import annotations

import json

import pytest

from aether_core.config import Settings
from aether_core.tools import ToolRegistry
from aether_caps_dev.register import register_dev


@pytest.mark.asyncio
async def test_dev_git_status_tool_registered():
    settings = Settings()
    tools = ToolRegistry()
    register_dev(tools, settings)
    assert "dev.git_status" in tools.names()
    result = json.loads(await tools.call("dev.git_status", {"cwd": "."}))
    assert "ok" in result
