from __future__ import annotations

import json
import sqlite3

import pytest

from aether_core.config import Settings
from aether_core.events import EventBus
from aether_core.tools import ToolRegistry, ToolSpec
from aether_supervisor.manager import ProcessSupervisor


@pytest.mark.asyncio
async def test_migrations_preserve_legacy_jobs_and_are_idempotent(tmp_path):
    db = tmp_path / "supervisor.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE jobs(id TEXT PRIMARY KEY, tool TEXT NOT NULL, args TEXT NOT NULL,"
        " status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO jobs VALUES('legacy','old.tool','{}','done','{}',1,1)"
    )
    connection.commit()
    connection.close()

    settings = Settings()
    settings.memory.sqlite_path = str(db)
    supervisor = ProcessSupervisor(settings, ToolRegistry(), EventBus())
    await supervisor.open()
    await supervisor.close()
    await supervisor.open()

    connection = sqlite3.connect(db)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    legacy = connection.execute(
        "SELECT status FROM jobs WHERE id='legacy'"
    ).fetchone()
    connection.close()
    await supervisor.close()

    assert {"missions", "jobs", "job_events", "cursor_runs"} <= tables
    assert legacy == ("done",)


@pytest.mark.asyncio
async def test_run_tool_contract_remains_compatible(tmp_path):
    settings = Settings()
    settings.memory.sqlite_path = str(tmp_path / "tools.db")
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "demo.ok",
            "demo",
            {"type": "object"},
            lambda arguments: {"ok": True, "verified": True, **arguments},
        )
    )
    supervisor = ProcessSupervisor(settings, tools, EventBus())
    await supervisor.open()
    result = json.loads(await supervisor.run_tool("demo.ok", {"value": 7}))
    await supervisor.close()
    assert result == {"ok": True, "verified": True, "value": 7}
