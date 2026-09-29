from __future__ import annotations

import asyncio

import pytest

from aether_core.events import EventBus
from aether_supervisor.missions import MissionWorker
from aether_supervisor.storage import SupervisorStore
from aether_supervisor.verification import VerificationRunner


class SuccessfulDelegate:
    def __init__(self, api_key, *, model):
        self.model = model

    async def delegate(self, prompt, cwd, *, on_progress):
        await on_progress(
            "running", {"agent_id": "agent-1", "run_id": "run-1"}
        )
        return {
            "ok": True,
            "verified": True,
            "status": "finished",
            "agent_id": "agent-1",
            "run_id": "run-1",
            "summary": prompt,
        }


class BlockingDelegate:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    def __init__(self, api_key, *, model):
        pass

    async def delegate(self, prompt, cwd, *, on_progress):
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
async def test_submit_runs_in_background_and_persists_ids(tmp_path):
    store = SupervisorStore(tmp_path / "missions.db")
    await store.open()
    worker = MissionWorker(
        store,
        EventBus(),
        api_key="key",
        delegate_factory=SuccessfulDelegate,
    )
    mission_id = await worker.submit("implement feature", cwd=tmp_path)
    result = await worker.wait(mission_id, timeout=2)
    mission = await store.get_mission(mission_id)
    cursor_run = await store.fetchone(
        "SELECT agent_id,run_id,status FROM cursor_runs WHERE mission_id=?",
        (mission_id,),
    )
    await worker.close()
    await store.close()

    assert result["ok"] is True
    assert mission and mission["status"] == "done"
    assert cursor_run == {
        "agent_id": "agent-1",
        "run_id": "run-1",
        "status": "done",
    }


@pytest.mark.asyncio
async def test_active_mission_is_really_cancelled(tmp_path):
    BlockingDelegate.started = asyncio.Event()
    BlockingDelegate.cancelled = asyncio.Event()
    store = SupervisorStore(tmp_path / "cancel.db")
    await store.open()
    worker = MissionWorker(
        store,
        EventBus(),
        api_key="key",
        delegate_factory=BlockingDelegate,
    )
    mission_id = await worker.submit("block", cwd=tmp_path)
    await asyncio.wait_for(BlockingDelegate.started.wait(), 2)
    assert await worker.cancel(mission_id) is True
    result = await worker.wait(mission_id, timeout=2)
    mission = await store.get_mission(mission_id)
    await worker.close()
    await store.close()

    assert BlockingDelegate.cancelled.is_set()
    assert result["status"] == "cancelled"
    assert mission and mission["status"] == "cancelled"


@pytest.mark.asyncio
async def test_verification_runner_contract(tmp_path):
    runner = VerificationRunner()
    result = await runner.run(
        commands=["python -c \"print('ok')\""],
        checks=[lambda: True],
        cwd=tmp_path,
    )
    assert result["ok"] is True
    assert result["verified"] is True
