from __future__ import annotations

import base64
import io
import json
from typing import Any

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec
from aether_caps_vision.capture import (
    capture_screen as capture_screen_data,
    changed_pixels,
    evidence,
    list_monitors as list_monitors_data,
    persist_capture,
)
from aether_caps_vision.grounding import ground_target, parse_kimi_bbox


def register_vision(tools: ToolRegistry, settings: Settings) -> None:
    async def capture_screen(args: dict[str, Any]) -> str:
        try:
            monitor = args.get("monitor", 1)
            max_width = max(64, min(int(args.get("max_width") or 1280), 3840))
            max_height = max(64, min(int(args.get("max_height") or 720), 2160))
            capture = capture_screen_data(
                monitor, thumbnail_size=(max_width, max_height)
            )
            persist_capture(
                capture,
                settings.resolve_path(settings.vision.captures_dir),
                ttl_h=settings.vision.capture_ttl_h,
            )
            public = {k: v for k, v in capture.items() if k != "full_b64"}
            public["verified"] = True
            return json.dumps(public, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    async def describe_screen(args: dict[str, Any]) -> str:
        prompt = str(
            args.get("prompt")
            or "Describe la pantalla de forma breve para un asistente de voz."
        )
        try:
            capture = capture_screen_data(args.get("monitor", 1))
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        persist_capture(
            capture,
            settings.resolve_path(settings.vision.captures_dir),
            ttl_h=settings.vision.capture_ttl_h,
        )
        from aether_brain.kimi import KimiPlanner

        planner = KimiPlanner(settings)
        try:
            description = await planner.describe_image(prompt, capture["full_b64"])
            return json.dumps({"ok": True, "summary": description, "message": description})
        finally:
            await planner.close()

    async def capture_camera(args: dict[str, Any]) -> str:
        try:
            import cv2
            from PIL import Image
        except ImportError as exc:
            return json.dumps({"ok": False, "error": str(exc)})

        index = int(args.get("index") or 0)
        cam = cv2.VideoCapture(index)
        if not cam.isOpened():
            return json.dumps({"ok": False, "error": "cámara no disponible"})
        ok, frame = cam.read()
        cam.release()
        if not ok:
            return json.dumps({"ok": False, "error": "no se pudo capturar"})
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail((1280, 720))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return json.dumps(
            {
                "ok": True,
                "mime": "image/png",
                "message": "Foto lista. La imagen se guardó; no se envía el binario al modelo.",
            }
        )

    async def ground(args: dict[str, Any]) -> str:
        try:
            capture = None
            target = args.get("target")
            if not isinstance(target, dict):
                target = args
            if str(target.get("coordinate_space") or "screen") == "thumbnail":
                capture = capture_screen_data(args.get("monitor", 1))
            grounded = ground_target(target, capture)
            return json.dumps(
                {"ok": True, "grounding": grounded.to_dict()},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"ok": False, "verified": False, "error": str(exc)},
                ensure_ascii=False,
            )

    async def act_and_verify(args: dict[str, Any]) -> str:
        """Observe, ground, act, and independently verify the resulting pixels."""
        before: dict[str, Any] | None = None
        try:
            monitor = args.get("monitor", 1)
            before = capture_screen_data(monitor)
            action = str(args.get("action") or "click").casefold()
            target = args.get("target")
            if not isinstance(target, dict):
                target = {
                    key: args[key]
                    for key in (
                        "name",
                        "label",
                        "window",
                        "x",
                        "y",
                        "bbox",
                        "coordinate_space",
                        "confidence",
                    )
                    if key in args
                }
            grounded = None
            if action == "click" or target:
                try:
                    grounded = ground_target(target, before)
                except ValueError:
                    label = str(
                        (target or {}).get("label")
                        or (target or {}).get("name")
                        or ""
                    ).strip()
                    if not label or not before.get("full_b64"):
                        raise
                    from aether_brain.kimi import KimiPlanner

                    planner = KimiPlanner(settings)
                    try:
                        raw = await planner.describe_image(
                            (
                                f'Localiza el control visible llamado "{label}". '
                                "Responde SOLO JSON "
                                '{"left":int,"top":int,"width":int,"height":int,"confidence":0.0} '
                                "en pixeles del thumbnail. Si no lo ves, confidence 0."
                            ),
                            before["full_b64"],
                        )
                    finally:
                        await planner.close()
                    grounded = parse_kimi_bbox(
                        raw,
                        before,
                        label=label,
                        min_confidence=float(
                            settings.vision.grounding_min_confidence
                        ),
                    )
            if action == "click":
                assert grounded is not None
                action_args: dict[str, Any] = {
                    "x": grounded.point["x"],
                    "y": grounded.point["y"],
                    "button": args.get("button", "left"),
                    "clicks": args.get("clicks", 1),
                    "settle_seconds": args.get("settle_seconds", 0.25),
                }
                tool_name = "desktop.click_target"
            elif action in {"send_keys", "type"}:
                action_args = {
                    key: args[key]
                    for key in ("text", "keys", "settle_seconds")
                    if key in args
                }
                if grounded and grounded.label:
                    action_args["target"] = grounded.label
                if grounded and grounded.window:
                    action_args["window"] = grounded.window
                tool_name = "desktop.send_keys"
            else:
                raise ValueError("action must be click, send_keys, or type")
            if tools.get(tool_name) is None:
                raise RuntimeError(f"required tool is not registered: {tool_name}")
            raw_action = await tools.call(tool_name, action_args)
            action_result = (
                json.loads(raw_action) if isinstance(raw_action, str) else raw_action
            )
            await __import__("asyncio").sleep(
                max(0.05, min(float(args.get("verify_delay", 0.25)), 2.0))
            )
            after = capture_screen_data(monitor)
            dest = settings.resolve_path(settings.vision.captures_dir)
            persist_capture(before, dest, ttl_h=settings.vision.capture_ttl_h)
            persist_capture(after, dest, ttl_h=settings.vision.capture_ttl_h)
            change = changed_pixels(before["full_b64"], after["full_b64"])
            verified = bool(action_result.get("ok")) and bool(change["changed"])
            before_ev = {k: v for k, v in evidence(before).items() if k != "full_b64"}
            after_ev = {k: v for k, v in evidence(after).items() if k != "full_b64"}
            before_ev["path"] = before.get("path")
            after_ev["path"] = after.get("path")
            return json.dumps(
                {
                    "ok": verified,
                    "verified": verified,
                    "action": action,
                    "grounding": grounded.to_dict() if grounded else None,
                    "action_result": action_result,
                    "change": change,
                    "evidence": {"before": before_ev, "after": after_ev},
                    "error": None
                    if verified
                    else (
                        action_result.get("error")
                        or "action produced no verifiable visual change"
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {
                    "ok": False,
                    "verified": False,
                    "error": str(exc),
                    "evidence": {"before": evidence(before)} if before else {},
                },
                ensure_ascii=False,
            )

    async def list_monitors(_args: dict[str, Any]) -> str:
        try:
            return json.dumps(list_monitors_data(), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "verified": False, "error": str(exc)})

    tools.register(
        ToolSpec(
            "vision.list_monitors",
            "Lista monitores, DPI/geometría y ventana activa.",
            {"type": "object", "properties": {}},
            list_monitors,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "vision.capture_screen",
            "Captura la pantalla actual.",
            {
                "type": "object",
                "properties": {
                    "monitor": {
                        "oneOf": [{"type": "integer"}, {"const": "all"}]
                    },
                    "max_width": {"type": "integer", "minimum": 64, "maximum": 3840},
                    "max_height": {"type": "integer", "minimum": 64, "maximum": 2160},
                },
            },
            capture_screen,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "vision.describe_screen",
            "Captura y describe la pantalla con Kimi multimodal.",
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "monitor": {"type": "integer"},
                },
            },
            describe_screen,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "vision.capture_camera",
            "Captura un frame de la cámara.",
            {
                "type": "object",
                "properties": {"index": {"type": "integer"}},
            },
            capture_camera,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "vision.ground_target",
            "Resuelve un target estructurado mediante UIA, bbox o coordenadas.",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "object"},
                    "monitor": {
                        "oneOf": [{"type": "integer"}, {"const": "all"}]
                    },
                },
                "required": ["target"],
            },
            ground,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "vision.act_and_verify",
            "Observa, hace grounding, actúa y verifica el cambio visual con evidencia before/after.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["click", "send_keys", "type"],
                    },
                    "target": {"type": "object"},
                    "text": {"type": "string", "maxLength": 2000},
                    "keys": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                        ]
                    },
                    "monitor": {
                        "oneOf": [{"type": "integer"}, {"const": "all"}]
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                    },
                    "clicks": {"type": "integer", "minimum": 1, "maximum": 2},
                    "verify_delay": {"type": "number", "minimum": 0, "maximum": 2},
                },
                "required": ["action"],
            },
            act_and_verify,
            "L1",
        )
    )
