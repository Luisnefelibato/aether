from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "caps" / "messaging"))

from aether_caps_messaging import (
    DraftStore,
    TelegramBotAdapter,
    WhatsAppCloudAdapter,
)


def test_sqlite_drafts_create_and_delete(tmp_path: Path) -> None:
    store = DraftStore(tmp_path / "drafts.db")
    draft = store.create("telegram", "123", "hola")

    assert store.get(draft["id"])["body"] == "hola"
    assert store.delete(draft["id"]) is True
    assert store.get(draft["id"]) is None


@pytest.mark.asyncio
async def test_telegram_send_and_delete_mock_http() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramBotAdapter("bot-token", client=client)
        sent = await adapter.send("123", "hola")
        deleted = await adapter.delete("123", sent["message_id"])

    assert sent["ok"] is True
    assert sent["message_id"] == "42"
    assert deleted["ok"] is True
    assert methods == ["sendMessage", "deleteMessage"]


@pytest.mark.asyncio
async def test_telegram_without_token_never_fakes_send() -> None:
    sent = await TelegramBotAdapter("").send("123", "hola")

    assert sent["ok"] is False
    assert sent["verified"] is False
    assert sent["status"] == "needs_login"


@pytest.mark.asyncio
async def test_whatsapp_send_mock_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "messages": [{"id": "wamid.123"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WhatsAppCloudAdapter("token", "phone-id", client=client)
        sent = await adapter.send("15551234567", "hola")

    assert sent["ok"] is True
    assert sent["verified"] is True
    assert sent["message_id"] == "wamid.123"


@pytest.mark.asyncio
async def test_whatsapp_optional_fallback_is_explicit() -> None:
    adapter = WhatsAppCloudAdapter("", "")
    sent = await adapter.send("15551234567", "hola")

    assert adapter.status()["status"] == "needs_oauth"
    assert sent["ok"] is False
    assert sent["verified"] is False
