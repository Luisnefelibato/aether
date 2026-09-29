from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from aether_caps_vision import coordinates
from aether_caps_vision import capture as capture_module


class _Shot:
    size = (1600, 900)
    bgra = bytes((30, 20, 10, 255)) * (1600 * 900)


class _Mss:
    monitors = [
        {"left": -1280, "top": 0, "width": 2880, "height": 900},
        {"left": 0, "top": 0, "width": 1600, "height": 900},
    ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def grab(self, monitor):
        assert monitor["left"] == 0
        return _Shot()


def test_capture_contains_multimonitor_geometry(monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=_Mss))
    monkeypatch.setattr(
        capture_module,
        "active_window_info",
        lambda: {"hwnd": 7, "title": "Fixture", "bbox": {"left": 10, "top": 20}},
    )

    result = capture_module.capture_screen(1)

    assert result["bbox"] == {
        "left": 0,
        "top": 0,
        "width": 1600,
        "height": 900,
        "right": 1600,
        "bottom": 900,
    }
    assert result["original_resolution"] == {"width": 1600, "height": 900}
    assert result["thumbnail_resolution"] == {"width": 1280, "height": 720}
    assert result["thumbnail_scale"] == 0.8
    assert result["monitors"][0]["left"] == -1280
    assert result["active_window"]["title"] == "Fixture"
    assert Image.open(__import__("io").BytesIO(__import__("base64").b64decode(result["full_b64"]))).size == (
        1280,
        720,
    )


def test_persist_capture_writes_and_prunes(tmp_path):
    capture = {
        "full_b64": __import__("base64").b64encode(b"png-bytes").decode("ascii"),
        "sha256": "abc123",
    }
    path = capture_module.persist_capture(capture, tmp_path, ttl_h=24)
    assert path
    assert Path(path).read_bytes() == b"png-bytes"
    stale = tmp_path / "old.png"
    stale.write_bytes(b"old")
    os.utime(stale, (0, 0))
    capture_module.persist_capture(capture, tmp_path, ttl_h=1)
    assert not stale.exists()


def test_coordinate_roundtrip_with_negative_monitor_origin():
    bbox = {"left": -1280, "top": 100, "width": 1280, "height": 720}
    thumbnail = coordinates.original_to_thumbnail(-640, 460, 0.5, bbox)
    assert thumbnail == (320, 180)
    assert coordinates.thumbnail_to_original(*thumbnail, 0.5, bbox) == (-640, 460)


def test_parse_kimi_bbox_rejects_low_confidence():
    from aether_caps_vision.grounding import parse_kimi_bbox

    capture = {
        "thumbnail_scale": 1.0,
        "bbox": {"left": 0, "top": 0, "width": 100, "height": 100},
    }
    grounded = parse_kimi_bbox(
        '{"left":10,"top":20,"width":30,"height":40,"confidence":0.9}',
        capture,
        label="OK",
        min_confidence=0.6,
    )
    assert grounded.method == "kimi"
    assert grounded.point == {"x": 25, "y": 40}
    try:
        parse_kimi_bbox(
            '{"left":10,"top":20,"width":30,"height":40,"confidence":0.1}',
            capture,
            label="NO",
            min_confidence=0.6,
        )
    except ValueError:
        return
    raise AssertionError("low confidence should fail")
