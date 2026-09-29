from __future__ import annotations

import base64
import hashlib
import io
import time
from pathlib import Path
from typing import Any

MAX_THUMBNAIL = (1280, 720)


def _dpi_info() -> dict[str, float | int]:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        return {"dpi": dpi, "scale": dpi / 96.0}
    except Exception:  # noqa: BLE001
        return {"dpi": 96, "scale": 1.0}


def active_window_info() -> dict[str, Any] | None:
    try:
        from aether_caps_desktop.windows import get_active_window

        return get_active_window()
    except Exception:  # noqa: BLE001
        return None


def _monitor_dict(monitor: Any, index: int) -> dict[str, int]:
    return {
        "index": index,
        "left": int(monitor["left"]),
        "top": int(monitor["top"]),
        "width": int(monitor["width"]),
        "height": int(monitor["height"]),
    }


def capture_screen(
    monitor: int | str | None = 1,
    *,
    thumbnail_size: tuple[int, int] = MAX_THUMBNAIL,
    include_image: bool = True,
) -> dict[str, Any]:
    """Capture one monitor or the full virtual desktop with geometry metadata."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitors = [_monitor_dict(mon, index) for index, mon in enumerate(sct.monitors)]
        if not monitors:
            raise RuntimeError("no monitors available")
        if monitor in (None, "all", 0, "0"):
            selected_index = 0
        else:
            selected_index = int(monitor)
            if selected_index < 0 or selected_index >= len(monitors):
                raise ValueError(f"monitor must be between 0 and {len(monitors) - 1}")
        selected = monitors[selected_index]
        shot = sct.grab(selected)

    original = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    thumb = original.copy()
    thumb.thumbnail(thumbnail_size)
    scale_x = thumb.width / original.width
    scale_y = thumb.height / original.height
    # Pillow preserves aspect ratio; keep both for exactness and one convenient scale.
    buffer = io.BytesIO()
    thumb.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    bbox = {
        "left": selected["left"],
        "top": selected["top"],
        "width": selected["width"],
        "height": selected["height"],
        "right": selected["left"] + selected["width"],
        "bottom": selected["top"] + selected["height"],
    }
    result: dict[str, Any] = {
        "ok": True,
        "mime": "image/png",
        "monitor": selected_index,
        "bbox": bbox,
        "original_resolution": {"width": original.width, "height": original.height},
        "thumbnail_resolution": {"width": thumb.width, "height": thumb.height},
        "thumbnail_scale": scale_x,
        "thumbnail_scale_x": scale_x,
        "thumbnail_scale_y": scale_y,
        "monitors": monitors,
        "active_window": active_window_info(),
        "dpi": _dpi_info(),
        "sha256": hashlib.sha256(image_bytes).hexdigest(),
        "message": "Captura lista",
    }
    if include_image:
        result["image_b64"] = encoded[:120] + "..."
        result["full_b64"] = encoded
    return result


def changed_pixels(
    before_b64: str, after_b64: str, *, threshold: int = 12
) -> dict[str, float | bool]:
    """Compare two PNG thumbnails and return a bounded visual change score."""
    from PIL import Image, ImageChops, ImageStat

    before = Image.open(io.BytesIO(base64.b64decode(before_b64))).convert("RGB")
    after = Image.open(io.BytesIO(base64.b64decode(after_b64))).convert("RGB")
    if before.size != after.size:
        after = after.resize(before.size)
    diff = ImageChops.difference(before, after)
    extrema = diff.convert("L").point(lambda value: 255 if value > threshold else 0)
    changed_ratio = ImageStat.Stat(extrema).mean[0] / 255.0
    phash_delta = 0
    try:
        import imagehash

        phash_delta = int(imagehash.phash(before) - imagehash.phash(after))
    except Exception:  # noqa: BLE001
        phash_delta = 0
    return {
        "changed": changed_ratio > 0.0001 or phash_delta > 4,
        "changed_ratio": changed_ratio,
        "phash_delta": phash_delta,
    }


def persist_capture(
    capture: dict[str, Any],
    directory: str | Path,
    *,
    ttl_h: int = 24,
) -> str | None:
    """Write PNG evidence to disk and prune captures older than ttl_h."""
    encoded = capture.get("full_b64")
    if not encoded:
        return None
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max(1, int(ttl_h)) * 3600
    for stale in dest.glob("*.png"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            continue
    sha = str(capture.get("sha256") or hashlib.sha256(encoded.encode()).hexdigest())
    path = dest / f"{sha}.png"
    if not path.exists():
        path.write_bytes(base64.b64decode(encoded))
    capture["path"] = str(path)
    return str(path)


def list_monitors() -> dict[str, Any]:
    import mss

    with mss.mss() as sct:
        monitors = [_monitor_dict(mon, index) for index, mon in enumerate(sct.monitors)]
    return {
        "ok": True,
        "verified": True,
        "monitors": monitors,
        "active_window": active_window_info(),
        "dpi": _dpi_info(),
        "message": f"{max(0, len(monitors) - 1)} monitor(es) detectado(s)",
    }


def evidence(capture: dict[str, Any]) -> dict[str, Any]:
    """Public capture metadata for tools/LLM — never the raw screenshot."""
    preview = str(capture.get("image_b64") or capture.get("full_b64") or "")
    if len(preview) > 120:
        preview = preview[:120] + "..."
    return {
        "mime": capture.get("mime"),
        "sha256": capture.get("sha256"),
        "path": capture.get("path"),
        "monitor": capture.get("monitor"),
        "bbox": capture.get("bbox"),
        "original_resolution": capture.get("original_resolution"),
        "thumbnail_resolution": capture.get("thumbnail_resolution"),
        "thumbnail_scale": capture.get("thumbnail_scale"),
        "active_window": capture.get("active_window"),
        "dpi": capture.get("dpi"),
        "image_b64": preview,
    }
