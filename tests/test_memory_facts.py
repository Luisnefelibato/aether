from pathlib import Path
from types import SimpleNamespace

import pytest

from aether_memory.store import MemoryStore


@pytest.mark.asyncio
async def test_versioned_facts_entities_links_and_summaries(tmp_path: Path):
    settings = SimpleNamespace(
        memory=SimpleNamespace(
            sqlite_path=str(tmp_path / "memory.db"),
            chroma_path=str(tmp_path / "chroma"),
            embed_collection="test",
        ),
        resolve_path=lambda value: Path(value),
    )
    store = MemoryStore(settings)
    await store.open()
    try:
        first = await store.set_fact("user", "city", "Lima")
        second = await store.set_fact("user", "city", "Madrid")
        history = await store.get_facts("user", "city", include_history=True)
        assert first["version"] == 1
        assert second["version"] == 2
        assert sum(fact["active"] for fact in history) == 1
        left = await store.put_entity("Luis", "person")
        right = await store.put_entity("Madrid", "city")
        link = await store.add_link(left["id"], right["id"], "lives_in")
        summary = await store.add_summary("day", "Moved to Madrid")
        assert link["relation"] == "lives_in"
        forgotten = await store.forget_fact("user", "city")
        remaining = await store.get_facts("user", "city")
        assert forgotten["deactivated"] == 1
        assert remaining == []
        await store.add_episode("user", "Me mudé a Madrid")
        consolidated = await store.consolidate(10)
        assert consolidated["ok"] is True
        assert consolidated["summary"]["scope"] == "daily"
    finally:
        await store.close()
