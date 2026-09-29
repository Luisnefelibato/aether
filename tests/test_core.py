from __future__ import annotations

import json

import pytest

from aether_core.config import Settings
from aether_core.session import SessionDirector
from aether_core.tools import ToolRegistry, ToolSpec, normalize_json_schema
from aether_policy.gate import PermissionGate
from aether_supervisor.manager import ProcessSupervisor


def test_normalize_json_schema_adds_enum_type():
    schema = {
        "type": "object",
        "properties": {"button": {"enum": ["left", "right"]}},
    }
    out = normalize_json_schema(schema)
    assert out["properties"]["button"]["type"] == "string"


def test_policy_trusted_no_confirm_for_shell():
    settings = Settings()
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "shell.run",
            "run",
            {"type": "object", "properties": {"command": {"type": "string"}}},
            lambda a: {"ok": True},
            "L2",
        )
    )
    gate = PermissionGate(settings, tools)
    d = gate.evaluate("shell.run", {"command": "echo hi"})
    assert d.allowed is True
    assert d.needs_confirm is False


def test_policy_l2_needs_confirm():
    settings = Settings()
    settings.policy.trusted_mode = False
    settings.policy.require_oral_confirm_for = ["L2"]
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "shell.run",
            "run",
            {"type": "object", "properties": {"command": {"type": "string"}}},
            lambda a: {"ok": True},
            "L2",
        )
    )
    gate = PermissionGate(settings, tools)
    d = gate.evaluate("shell.run", {"command": "echo hi"})
    assert d.allowed is True
    assert d.needs_confirm is True
    assert d.level == "L2"


def test_policy_blocks_secrets():
    settings = Settings()
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "desktop.notify",
            "notify",
            {"type": "object", "properties": {}},
            lambda a: {},
            "L0",
        )
    )
    gate = PermissionGate(settings, tools)
    d = gate.evaluate("desktop.notify", {"body": "api_key=secret"})
    assert d.allowed is False


def test_policy_confirms_sensitive_external_effects():
    settings = Settings()
    tools = ToolRegistry()
    for name in (
        "browser.drive_upload",
        "messaging.send_telegram",
        "fs.trash",
        "commerce.checkout",
    ):
        tools.register(
            ToolSpec(
                name,
                "sensitive",
                {"type": "object", "properties": {}},
                lambda a: {"ok": True},
                "L2",
            )
        )
    gate = PermissionGate(settings, tools)
    for name in tools.names():
        decision = gate.evaluate(name, {})
        assert decision.allowed is True
        assert decision.needs_confirm is True


def test_policy_confirms_critical_home_entities_only():
    settings = Settings()
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "home.set_device",
            "home",
            {"type": "object", "properties": {}},
            lambda a: {"ok": True},
            "L1",
        )
    )
    gate = PermissionGate(settings, tools)
    assert gate.evaluate(
        "home.set_device", {"entity_id": "lock.front_door", "action": "unlock"}
    ).needs_confirm
    assert not gate.evaluate(
        "home.set_device", {"entity_id": "light.office", "action": "on"}
    ).needs_confirm


def test_policy_does_not_confirm_send_keys_or_cursor():
    settings = Settings()
    tools = ToolRegistry()
    for name in ("desktop.send_keys", "cursor.send_agent"):
        tools.register(
            ToolSpec(name, "ok", {"type": "object", "properties": {}}, lambda a: {"ok": True}, "L1")
        )
    gate = PermissionGate(settings, tools)
    assert gate.evaluate("desktop.send_keys", {"keys": "a"}).needs_confirm is False
    assert gate.evaluate("cursor.send_agent", {"prompt": "fix tests"}).needs_confirm is False


def test_policy_blocks_credential_ui():
    settings = Settings()
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "vision.act_and_verify",
            "ok",
            {"type": "object", "properties": {}},
            lambda a: {"ok": True},
            "L1",
        )
    )
    gate = PermissionGate(settings, tools)
    denied = gate.evaluate(
        "vision.act_and_verify",
        {"action": "click", "target": {"window": "Windows Security"}},
    )
    assert denied.allowed is False


def test_policy_visual_rate_limit():
    settings = Settings()
    settings.policy.max_visual_actions_per_minute = 2
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            "vision.act_and_verify",
            "ok",
            {"type": "object", "properties": {}},
            lambda a: {"ok": True},
            "L1",
        )
    )
    gate = PermissionGate(settings, tools)
    assert gate.evaluate("vision.act_and_verify", {}).allowed is True
    assert gate.evaluate("vision.act_and_verify", {}).allowed is True
    denied = gate.evaluate("vision.act_and_verify", {})
    assert denied.allowed is False


def test_status_query_detection():
    assert SessionDirector._is_status_query("qué estás haciendo")
    assert SessionDirector._is_status_query("estado de los trabajos")
    assert not SessionDirector._is_status_query("abre el bloc de notas")
    assert SessionDirector._is_action_request("abre el bloc de notas")
    assert SessionDirector._is_action_request(
        "quiero que revises el proyecto y ejecutes las pruebas"
    )
    assert SessionDirector._is_action_request("dime qué procesos están activos")
    assert SessionDirector._is_action_request("open Cursor")
    assert SessionDirector._is_action_request("corre las pruebas")
    assert SessionDirector._is_action_request("ábrelo")
    assert SessionDirector._is_action_request("sube esta carpeta a mi Drive")
    assert SessionDirector._is_action_request("reproduce una canción en Spotify")
    assert not SessionDirector._is_action_request("¿cómo estás?")
    assert not SessionDirector._is_action_request("explícame qué es un proceso")


def test_unverified_result_is_failure():
    trace = [
        {
            "name": "desktop.open_app",
            "result": json.dumps({"ok": True, "verified": False}),
        }
    ]
    assert SessionDirector._first_failed_tool(trace) is trace[0]
    assert not ProcessSupervisor._result_succeeded(trace[0]["result"])
    assert not ProcessSupervisor._result_succeeded("plain text")
    assert not ProcessSupervisor._result_succeeded(json.dumps({"message": "done"}))
    assert ProcessSupervisor._result_succeeded(
        json.dumps({"ok": True, "verified": True})
    )
    assert SessionDirector._first_failed_tool(
        [{"name": "tool", "result": "plain text"}]
    )
    assert SessionDirector._first_failed_tool(
        [{"name": "tool", "result": json.dumps({"message": "done"})}]
    )


@pytest.mark.asyncio
async def test_desktop_list_and_notify():
    from aether_caps_desktop.register import register_desktop

    settings = Settings()
    tools = ToolRegistry()
    register_desktop(tools, settings)
    result = await tools.call("desktop.notify", {"body": "hola"})
    data = json.loads(result)
    assert data["ok"] is True
    assert "desktop.open_whatsapp" in tools.names()
    assert "desktop.spotify_play" in tools.names()


def test_browser_service_tools_registered():
    from aether_caps_browser.register import register_browser

    tools = ToolRegistry()
    register_browser(tools, Settings())
    assert "browser.click_text" in tools.names()
    assert "browser.youtube_play" in tools.names()
    assert "browser.drive_upload" in tools.names()


@pytest.mark.asyncio
async def test_calendar_stub():
    from aether_caps_life.register import register_life

    settings = Settings()
    tools = ToolRegistry()
    register_life(tools, settings)
    created = json.loads(
        await tools.call("calendar.create_event", {"title": "Demo ADAM"})
    )
    assert created["ok"] is True
    listed = json.loads(await tools.call("calendar.list_events", {"limit": 5}))
    assert listed["ok"] is True
    assert any(e["title"] == "Demo ADAM" for e in listed["events"])


@pytest.mark.asyncio
async def test_iot_stub():
    from aether_caps_iot.register import register_iot

    settings = Settings()
    tools = ToolRegistry()
    register_iot(tools, settings)
    out = json.loads(
        await tools.call(
            "home.set_device", {"entity_id": "light.bedroom", "action": "on"}
        )
    )
    assert out["ok"] is False
    assert out["verified"] is False
    assert out["status"] == "needs_login"
    listed = json.loads(await tools.call("home.list_devices", {}))
    assert listed["ok"] is False
    scene = json.loads(await tools.call("home.scene", {"name": "noche"}))
    assert scene["ok"] is False
    assert scene["verified"] is False
