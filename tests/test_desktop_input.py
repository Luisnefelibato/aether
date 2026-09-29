from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from aether_caps_desktop import input as input_module
from aether_caps_desktop import windows


def _capture(color: tuple[int, int, int]) -> dict:
    buffer = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(buffer, format="PNG")
    raw = buffer.getvalue()
    return {
        "mime": "image/png",
        "full_b64": base64.b64encode(raw).decode("ascii"),
        "sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "monitor": 0,
        "bbox": {"left": -100, "top": 0, "width": 200, "height": 100},
        "original_resolution": {"width": 24, "height": 16},
        "thumbnail_resolution": {"width": 24, "height": 16},
        "thumbnail_scale": 1.0,
        "active_window": None,
    }


class _Element:
    clicked = False

    def click_input(self):
        self.clicked = True


@pytest.mark.asyncio
async def test_click_prefers_uia_and_verifies_change(monkeypatch):
    element = _Element()
    captures = iter([_capture((0, 0, 0)), _capture((255, 255, 255))])
    monkeypatch.setattr(input_module, "_capture", lambda: next(captures))
    monkeypatch.setattr(
        windows,
        "find_uia_target",
        lambda *_a, **_k: {
            "method": "uia",
            "bbox": {"left": 10, "top": 20, "width": 40, "height": 20},
            "_wrapper": element,
        },
    )
    monkeypatch.setattr(
        windows,
        "virtual_screen_bbox",
        lambda: {"left": -100, "top": 0, "width": 300, "height": 200},
    )

    result = await input_module.click_target({"target": "Guardar", "settle_seconds": 0})

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["method"] == "uia"
    assert result["point"] == {"x": 30, "y": 30}
    assert element.clicked is True
    assert "_wrapper" not in result["target"]


@pytest.mark.asyncio
async def test_send_keys_reports_unverified_without_visual_change(monkeypatch):
    same = _capture((50, 50, 50))
    monkeypatch.setattr(input_module, "_capture", lambda: same.copy())
    monkeypatch.setattr(input_module, "_send_sequence", lambda _keys: None)

    result = await input_module.send_keys({"keys": ["CTRL+L"], "settle_seconds": 0})

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["error"] == "no visual change detected"


@pytest.mark.asyncio
async def test_click_rejects_point_outside_virtual_desktop(monkeypatch):
    monkeypatch.setattr(input_module, "_capture", lambda: _capture((0, 0, 0)))
    monkeypatch.setattr(windows, "find_uia_target", lambda *_a, **_k: None)
    monkeypatch.setattr(
        windows,
        "virtual_screen_bbox",
        lambda: {"left": 0, "top": 0, "width": 100, "height": 100},
    )

    result = await input_module.click_target({"x": 1000, "y": 1000})

    assert result["ok"] is False
    assert result["verified"] is False
    assert "outside" in result["error"]
