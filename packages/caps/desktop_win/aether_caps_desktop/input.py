from __future__ import annotations

import asyncio
from typing import Any, Iterable

MAX_TEXT_LENGTH = 2000
MAX_KEY_STROKES = 100


def _capture() -> dict[str, Any]:
    from aether_caps_vision.capture import capture_screen

    return capture_screen("all")


def _compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    from aether_caps_vision.capture import changed_pixels

    return changed_pixels(before["full_b64"], after["full_b64"])


def _evidence(capture: dict[str, Any]) -> dict[str, Any]:
    from aether_caps_vision.capture import evidence

    return evidence(capture)


def _key(name: str, key_type: Any) -> Any:
    aliases = {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "win": "cmd",
        "windows": "cmd",
        "enter": "enter",
        "return": "enter",
        "tab": "tab",
        "esc": "esc",
        "escape": "esc",
        "space": "space",
        "backspace": "backspace",
        "delete": "delete",
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "home": "home",
        "end": "end",
        "pageup": "page_up",
        "pagedown": "page_down",
    }
    attribute = aliases.get(name.casefold())
    if attribute and hasattr(key_type, attribute):
        return getattr(key_type, attribute)
    if len(name) == 1:
        return name
    if name.casefold().startswith("f") and name[1:].isdigit():
        attribute = name.casefold()
        if hasattr(key_type, attribute):
            return getattr(key_type, attribute)
    raise ValueError(f"unsupported key: {name}")


def _send_sequence(keys: Iterable[str]) -> None:
    from pynput.keyboard import Controller, Key

    keyboard = Controller()
    for expression in keys:
        parts = [part.strip() for part in expression.split("+") if part.strip()]
        if not parts:
            continue
        resolved = [_key(part, Key) for part in parts]
        for item in resolved:
            keyboard.press(item)
        for item in reversed(resolved):
            keyboard.release(item)


async def send_keys(args: dict[str, Any]) -> dict[str, Any]:
    text = args.get("text")
    raw_keys = args.get("keys", [])
    keys = [raw_keys] if isinstance(raw_keys, str) else list(raw_keys or [])
    if text is None and not keys:
        return {"ok": False, "verified": False, "error": "text or keys required"}
    text = None if text is None else str(text)
    if text is not None and len(text) > MAX_TEXT_LENGTH:
        return {"ok": False, "verified": False, "error": "text exceeds safe limit"}
    if len(keys) > MAX_KEY_STROKES:
        return {"ok": False, "verified": False, "error": "too many key strokes"}

    before = _capture()
    method = "keyboard"
    try:
        target_name = str(args.get("target") or "").strip()
        if target_name:
            from .windows import find_uia_target

            target = find_uia_target(
                target_name, window_title=str(args.get("window") or "") or None
            )
            wrapper = target and target.get("_wrapper")
            if wrapper is not None:
                wrapper.set_focus()
                if text:
                    wrapper.type_keys(text, with_spaces=True, set_foreground=True)
                method = "uia"
            elif text:
                from pynput.keyboard import Controller

                Controller().type(text)
        elif text:
            from pynput.keyboard import Controller

            Controller().type(text)
        if keys:
            _send_sequence(str(item) for item in keys)
        await asyncio.sleep(max(0.05, min(float(args.get("settle_seconds", 0.35)), 2.0)))
        after = _capture()
        comparison = _compare(before, after)
        verified = bool(comparison["changed"])
        return {
            "ok": verified,
            "verified": verified,
            "method": method,
            "change": comparison,
            "evidence": {"before": _evidence(before), "after": _evidence(after)},
            "error": None if verified else "no visual change detected",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "verified": False,
            "error": str(exc),
            "evidence": {"before": _evidence(before)},
        }


def _point_from_args(args: dict[str, Any]) -> tuple[int, int, dict[str, Any] | None]:
    from .windows import find_uia_target

    target_name = str(args.get("target") or args.get("name") or "").strip()
    if target_name:
        uia = find_uia_target(
            target_name, window_title=str(args.get("window") or "") or None
        )
        if uia:
            bbox = uia["bbox"]
            return (
                int(bbox["left"] + bbox["width"] / 2),
                int(bbox["top"] + bbox["height"] / 2),
                uia,
            )
    bbox = args.get("bbox")
    if bbox:
        x = float(bbox.get("left", bbox.get("x", 0))) + float(bbox["width"]) / 2
        y = float(bbox.get("top", bbox.get("y", 0))) + float(bbox["height"]) / 2
    elif args.get("x") is not None and args.get("y") is not None:
        x, y = float(args["x"]), float(args["y"])
    else:
        raise ValueError("target, bbox, or x/y required")
    if str(args.get("coordinate_space") or "screen") == "thumbnail":
        from aether_caps_vision.coordinates import point_thumbnail_to_original

        scale = float(args.get("thumbnail_scale") or args.get("scale") or 0)
        capture_bbox = args.get("capture_bbox")
        x, y = point_thumbnail_to_original(x, y, scale, capture_bbox)
    return round(x), round(y), None


async def click_target(args: dict[str, Any]) -> dict[str, Any]:
    before = _capture()
    try:
        x, y, uia = _point_from_args(args)
        from .windows import virtual_screen_bbox

        bounds = virtual_screen_bbox()
        right = bounds["left"] + bounds["width"]
        bottom = bounds["top"] + bounds["height"]
        if not (bounds["left"] <= x < right and bounds["top"] <= y < bottom):
            raise ValueError("click target is outside the virtual desktop")
        method = "uia" if uia else "mouse"
        wrapper = uia and uia.get("_wrapper")
        if wrapper is not None:
            wrapper.click_input()
        else:
            from pynput.mouse import Button, Controller

            button_name = str(args.get("button") or "left").casefold()
            button = getattr(Button, button_name, None)
            if button is None:
                raise ValueError("button must be left, right, or middle")
            clicks = int(args.get("clicks") or 1)
            if clicks < 1 or clicks > 2:
                raise ValueError("clicks must be 1 or 2")
            mouse = Controller()
            mouse.position = (x, y)
            mouse.click(button, clicks)
        await asyncio.sleep(max(0.05, min(float(args.get("settle_seconds", 0.4)), 2.0)))
        after = _capture()
        comparison = _compare(before, after)
        verified = bool(comparison["changed"])
        clean_target = {key: value for key, value in (uia or {}).items() if key != "_wrapper"}
        return {
            "ok": verified,
            "verified": verified,
            "method": method,
            "point": {"x": x, "y": y},
            "target": clean_target or None,
            "change": comparison,
            "evidence": {"before": _evidence(before), "after": _evidence(after)},
            "error": None if verified else "no visual change detected",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "verified": False,
            "error": str(exc),
            "evidence": {"before": _evidence(before)},
        }
