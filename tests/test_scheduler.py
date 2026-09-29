import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "scheduler"))

from aether_scheduler import PersistentScheduler


@pytest.mark.asyncio
async def test_scheduler_persists_and_runs_steps_via_supervisor(tmp_path: Path):
    calls = []

    async def supervisor(routine, step):
        calls.append((routine["name"], step["action"], step["arguments"]))
        return "done"

    scheduler = PersistentScheduler(
        SimpleNamespace(sqlite_path=tmp_path / "scheduler.db", poll_seconds=0.01),
        supervisor,
    )
    await scheduler.open()
    try:
        routine = await scheduler.create_routine(
            "morning",
            60,
            [{"action": "health.snapshot", "arguments": {"quiet": True}}],
            run_at=time.time() - 1,
        )
        runs = await scheduler.run_due()
        assert runs[0]["status"] == "completed"
        assert calls == [("morning", "health.snapshot", {"quiet": True})]
        assert (await scheduler.get_routine(routine["id"]))["steps"]
        seeded = await scheduler.seed_defaults()
        assert "health_hourly" in seeded
        again = await scheduler.seed_defaults()
        assert again == []
        names = {item["name"] for item in await scheduler.list_routines()}
        assert "memory_daily_consolidate" in names
        assert "morning_health" in names
        assert "backup_weekly" in names
    finally:
        await scheduler.close()


@pytest.mark.asyncio
async def test_scheduler_accepts_cron_expression(tmp_path: Path):
    async def supervisor(routine, step):
        return "done"

    scheduler = PersistentScheduler(
        SimpleNamespace(sqlite_path=tmp_path / "scheduler.db", poll_seconds=0.01),
        supervisor,
    )
    await scheduler.open()
    try:
        routine = await scheduler.create_routine(
            "breakfast",
            0,
            [{"action": "health.snapshot", "arguments": {}}],
            enabled=False,
            cron_expr="0 8 * * *",
        )
        assert routine["cron_expr"] == "0 8 * * *"
        assert routine["next_run"] > time.time()
    finally:
        await scheduler.close()
