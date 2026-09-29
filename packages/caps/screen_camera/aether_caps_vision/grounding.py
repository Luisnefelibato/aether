from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .coordinates import bbox_thumbnail_to_original, point_thumbnail_to_original


@dataclass(frozen=True)
class GroundedTarget:
    method: str
    point: dict[str, int]
    bbox: dict[str, int] | None
    confidence: float
    label: str | None = None
    window: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _center(bbox: Mapping[str, Any]) -> dict[str, int]:
    left = int(bbox.get("left", bbox.get("x", 0)))
    top = int(bbox.get("top", bbox.get("y", 0)))
    return {
        "x": round(left + int(bbox["width"]) / 2),
        "y": round(top + int(bbox["height"]) / 2),
    }


def ground_target(
    target: Mapping[str, Any], capture: Mapping[str, Any] | None = None
) -> GroundedTarget:
    """Resolve an explicit/UIA target without inventing uncertain screen positions."""
    label = str(target.get("label") or target.get("name") or target.get("target") or "").strip()
    window = str(target.get("window") or "").strip() or None
    if label:
        try:
            from aether_caps_desktop.windows import find_uia_target

            uia = find_uia_target(label, window_title=window)
        except Exception:  # noqa: BLE001
            uia = None
        if uia:
            bbox = {key: int(value) for key, value in uia["bbox"].items()}
            return GroundedTarget("uia", _center(bbox), bbox, 1.0, label, window)

    coordinate_space = str(target.get("coordinate_space") or "screen").casefold()
    bbox_value = target.get("bbox")
    if bbox_value:
        bbox = {
            "left": int(bbox_value.get("left", bbox_value.get("x", 0))),
            "top": int(bbox_value.get("top", bbox_value.get("y", 0))),
            "width": int(bbox_value["width"]),
            "height": int(bbox_value["height"]),
        }
        if coordinate_space == "thumbnail":
            if not capture:
                raise ValueError("capture metadata required for thumbnail coordinates")
            bbox = bbox_thumbnail_to_original(
                bbox,
                float(capture["thumbnail_scale"]),
                capture.get("bbox"),
            )
        if bbox["width"] <= 0 or bbox["height"] <= 0:
            raise ValueError("target bbox must have positive dimensions")
        return GroundedTarget(
            "visual_bbox", _center(bbox), bbox, float(target.get("confidence", 1.0)), label or None, window
        )

    if target.get("x") is not None and target.get("y") is not None:
        x, y = float(target["x"]), float(target["y"])
        if coordinate_space == "thumbnail":
            if not capture:
                raise ValueError("capture metadata required for thumbnail coordinates")
            x, y = point_thumbnail_to_original(
                x,
                y,
                float(capture["thumbnail_scale"]),
                capture.get("bbox"),
            )
        return GroundedTarget(
            "point",
            {"x": round(x), "y": round(y)},
            None,
            float(target.get("confidence", 1.0)),
            label or None,
            window,
        )
    raise ValueError("target could not be grounded; provide UIA name, bbox, or x/y")


def parse_kimi_bbox(
    text: str,
    capture: Mapping[str, Any],
    *,
    label: str,
    min_confidence: float = 0.6,
) -> GroundedTarget:
    """Parse a Kimi JSON bbox in thumbnail space; reject low-confidence guesses."""
    import json
    import re

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("kimi grounding returned no JSON")
    data = json.loads(match.group(0))
    confidence = float(data.get("confidence") or 0)
    if confidence < min_confidence:
        raise ValueError(
            f"kimi grounding confidence {confidence} below {min_confidence}"
        )
    bbox = {
        "left": int(data.get("left", data.get("x", 0))),
        "top": int(data.get("top", data.get("y", 0))),
        "width": int(data.get("width") or 0),
        "height": int(data.get("height") or 0),
    }
    if bbox["width"] <= 0 or bbox["height"] <= 0:
        raise ValueError("kimi grounding bbox is empty")
    bbox = bbox_thumbnail_to_original(
        bbox,
        float(capture["thumbnail_scale"]),
        capture.get("bbox"),
    )
    return GroundedTarget("kimi", _center(bbox), bbox, confidence, label, None)
