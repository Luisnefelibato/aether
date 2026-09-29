from __future__ import annotations

from types import SimpleNamespace

import pytest

from aether_supervisor.cursor_delegate import CursorDelegate


class FakeRun:
    id = "run-42"

    def __init__(self, status="finished"):
        self.status = status

    def wait(self):
        return SimpleNamespace(status=self.status, result="completed")


class FakeAgent:
    agent_id = "agent-42"

    def __init__(self, run):
        self.run = run

    def send(self, prompt):
        return self.run


class AgentOwner:
    def __init__(self, agent):
        self.agent = agent
        self.closed = False

    def __enter__(self):
        return self.agent

    def __exit__(self, *args):
        self.closed = True


def fake_sdk(status="finished", create_error=None):
    owner = AgentOwner(FakeAgent(FakeRun(status)))

    class Agent:
        @staticmethod
        def create(**kwargs):
            if create_error:
                raise create_error
            assert kwargs["local"].cwd
            return owner

    return (
        SimpleNamespace(
            Agent=Agent,
            LocalAgentOptions=lambda cwd: SimpleNamespace(cwd=cwd),
        ),
        owner,
    )


@pytest.mark.asyncio
async def test_cursor_delegate_uses_local_runtime_persists_ids_and_closes(tmp_path):
    sdk, owner = fake_sdk()
    progress = []

    async def capture(phase, payload):
        progress.append((phase, payload))

    result = await CursorDelegate("key", sdk=sdk).delegate(
        "do work", tmp_path, on_progress=capture
    )

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["agent_id"] == "agent-42"
    assert result["run_id"] == "run-42"
    assert result["runtime"] == "local"
    assert owner.closed is True
    assert [phase for phase, _ in progress] == ["starting", "running", "finished"]


@pytest.mark.asyncio
async def test_cursor_delegate_distinguishes_startup_and_run_failure(tmp_path):
    startup_sdk, _ = fake_sdk(create_error=RuntimeError("auth failed"))
    startup = await CursorDelegate("key", sdk=startup_sdk).delegate("x", tmp_path)
    run_sdk, _ = fake_sdk(status="error")
    run = await CursorDelegate("key", sdk=run_sdk).delegate("x", tmp_path)

    assert startup["status"] == "startup_failed"
    assert startup["error_kind"] == "startup_failure"
    assert run["status"] == "error"
    assert run["error_kind"] == "run_failure"
