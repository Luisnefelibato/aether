from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
for package in ("integrations", "calendar_mail", "iot_home"):
    sys.path.insert(0, str(ROOT / "packages" / "caps" / package))

from aether_caps_integrations import GoogleAdapter, SpotifyAdapter, register_integrations
from aether_caps_life.register import GoogleCalendarMailAdapter, register_life
from aether_caps_iot.register import register_iot
from aether_core.config import Settings
from aether_core.tools import ToolRegistry


def test_google_status_has_manual_oauth_structure(tmp_path: Path) -> None:
    settings = Settings()
    settings.browser.user_data_dir = str(tmp_path / "empty-profile")
    adapter = GoogleAdapter(
        settings,
        environ={
            "GOOGLE_CLIENT_ID": "client-id",
            "GOOGLE_REDIRECT_URI": "http://localhost/callback",
        },
    )

    status = adapter.status().to_dict()

    assert status["status"] == "needs_oauth"
    assert status["onboarding"]["manual_login_required"] is True
    assert status["onboarding"]["automates_credentials_or_2fa"] is False
    assert "accounts.google.com" in status["onboarding"]["authorization_url"]


def test_google_status_reads_token_file(tmp_path: Path) -> None:
    token_path = tmp_path / "google_tokens.json"
    token_path.write_text('{"access_token": "file-token"}', encoding="utf-8")
    settings = Settings()
    settings.google.token_path = str(token_path)
    settings.browser.user_data_dir = str(tmp_path / "empty-profile")
    adapter = GoogleAdapter(settings, environ={})
    assert adapter.status().status == "connected"


@pytest.mark.asyncio
async def test_spotify_status_and_control_mock_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"id": "tester"})
        if request.url.path == "/v1/me/player/pause":
            return httpx.Response(204)
        if request.url.path == "/v1/me/player":
            return httpx.Response(200, json={"is_playing": False})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SpotifyAdapter("token", client=client)
        status = await adapter.status()
        controlled = await adapter.control("pause")

    assert status.status == "connected"
    assert controlled["ok"] is True
    assert controlled["verified"] is True


@pytest.mark.asyncio
async def test_google_mail_adapter_mock_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        assert request.url.path.endswith("/messages/send")
        return httpx.Response(200, json={"id": "gmail-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarMailAdapter("token", client=client)
        sent = await adapter.send_mail(
            {"to": "person@example.com", "subject": "Hola", "body": "Prueba"}
        )

    assert sent["id"] == "gmail-1"


@pytest.mark.asyncio
async def test_local_mail_stub_never_reports_send_success(tmp_path: Path) -> None:
    settings = Settings()
    settings.memory.sqlite_path = str(tmp_path / "life.db")
    settings.calendar_mail.provider = "local_stub"
    tools = ToolRegistry()
    register_life(tools, settings)

    draft = json.loads(
        await tools.call(
            "mail.draft",
            {"to": "person@example.com", "subject": "Hola", "body": "Prueba"},
        )
    )
    sent = json.loads(await tools.call("mail.send", {"id": draft["id"]}))

    assert sent["ok"] is False
    assert sent["verified"] is False
    assert sent["stub"] is True


@pytest.mark.asyncio
async def test_home_assistant_verifies_state_after_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "entity_id": "lock.front_door",
                "state": "unlocked",
                "attributes": {},
            },
        )

    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("aether_caps_iot.ha.httpx.AsyncClient", client_factory)
    settings = Settings()
    settings.iot.home_assistant_url = "http://home-assistant"
    settings.iot.home_assistant_token = "token"
    tools = ToolRegistry()
    register_iot(tools, settings)

    result = json.loads(
        await tools.call(
            "home.set_device", {"entity_id": "lock.front_door", "action": "unlock"}
        )
    )

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["critical"] is True
    assert result["service"] == "unlock"
    assert ("POST", "/api/services/lock/unlock") in requests
    assert ("GET", "/api/states/lock.front_door") in requests


def test_home_entity_aliases():
    from aether_caps_iot.ha import resolve_entity_id

    assert resolve_entity_id("oficina") == "input_boolean.luz_oficina"
    assert resolve_entity_id("luz sala") == "input_boolean.luz_sala"
    assert resolve_entity_id("lock.front_door") == "lock.front_door"


@pytest.mark.asyncio
async def test_integration_onboard_never_automates_credentials() -> None:
    tools = ToolRegistry()
    register_integrations(tools, Settings())
    payload = json.loads(await tools.call("integration.onboard", {"service": "google"}))
    assert payload["ok"] is True
    assert payload["onboarding"]["automates_credentials_or_2fa"] is False
    status = json.loads(await tools.call("integration.status", {"service": "home"}))
    assert status["statuses"][0]["service"] == "home"
    assert status["statuses"][0]["status"] == "needs_login"
    assert "stub" not in (status["statuses"][0].get("mode") or "")


@pytest.mark.asyncio
async def test_home_assistant_light_verifies_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content.decode() or "{}"))
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={"entity_id": "light.office", "state": "on", "attributes": {"brightness": 128}},
        )

    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("aether_caps_iot.ha.httpx.AsyncClient", client_factory)
    settings = Settings()
    settings.iot.home_assistant_url = "http://home-assistant"
    settings.iot.home_assistant_token = "token"
    tools = ToolRegistry()
    register_iot(tools, settings)
    result = json.loads(
        await tools.call(
            "home.set_device",
            {"entity_id": "light.office", "action": "on", "brightness": 128},
        )
    )
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["state"] == "on"
    assert bodies[0]["brightness"] == 128
