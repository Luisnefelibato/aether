from __future__ import annotations

import base64
import io
import json
from importlib import import_module

import pytest
from PIL import Image

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec
from aether_caps_vision.capture import changed_pixels


def _capture(color: tuple[int, int, int]) -> dict:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 20), color).save(buffer, format="PNG")
    raw = buffer.getvalue()
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "ok": True,
        "mime": "image/png",
        "full_b64": encoded,
        "sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "monitor": 1,
        "bbox": {"left": 0, "top": 0, "width": 32, "height": 20},
        "original_resolution": {"width": 32, "height": 20},
        "thumbnail_resolution": {"width": 32, "height": 20},
        "thumbnail_scale": 1.0,
        "active_window": {"title": "Fixture"},
    }


def test_changed_pixels_generated_fixture():
    before = _capture((0, 0, 0))
    after = _capture((255, 255, 255))
    assert changed_pixels(before["full_b64"], after["full_b64"])["changed"] is True
    assert changed_pixels(before["full_b64"], before["full_b64"])["changed"] is False


@pytest.mark.asyncio
async def test_act_and_verify_returns_evidence_and_rejects_no_change(monkeypatch):
    register_module = import_module("aether_caps_vision.register")
    same = _capture((20, 30, 40))
    monkeypatch.setattr(register_module, "capture_screen_data", lambda *_a, **_k: same.copy())

    tools = ToolRegistry()

    async def clicked(_args):
        return json.dumps({"ok": True, "verified": True})

    tools.register(
        ToolSpec(
            "desktop.click_target",
            "fixture",
            {"type": "object", "properties": {}},
            clicked,
            "L1",
        )
    )
    register_module.register_vision(tools, Settings())

    result = json.loads(
        await tools.call(
            "vision.act_and_verify",
            {"action": "click", "target": {"x": 5, "y": 6}, "verify_delay": 0},
        )
    )

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["grounding"]["point"] == {"x": 5, "y": 6}
    assert result["evidence"]["before"]["sha256"]
    assert result["evidence"]["after"]["sha256"]
    assert "full_b64" not in result["evidence"]["before"]
