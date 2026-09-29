from __future__ import annotations

from typing import Any, Mapping, Sequence


def _bbox(value: Mapping[str, Any] | Sequence[int]) -> tuple[int, int, int, int]:
    if isinstance(value, Mapping):
        left = int(value.get("left", value.get("x", 0)))
        top = int(value.get("top", value.get("y", 0)))
        width = int(value.get("width", 0))
        height = int(value.get("height", 0))
        return left, top, width, height
    if len(value) != 4:
        raise ValueError("bbox must have four values")
    return tuple(int(part) for part in value)  # type: ignore[return-value]


def point_original_to_thumbnail(
    x: float, y: float, scale: float, bbox: Mapping[str, Any] | Sequence[int] | None = None
) -> tuple[int, int]:
    """Convert an absolute desktop point to capture-thumbnail coordinates."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    left, top = (0, 0) if bbox is None else _bbox(bbox)[:2]
    return round((x - left) * scale), round((y - top) * scale)


def point_thumbnail_to_original(
    x: float, y: float, scale: float, bbox: Mapping[str, Any] | Sequence[int] | None = None
) -> tuple[int, int]:
    """Convert capture-thumbnail coordinates to absolute desktop coordinates."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    left, top = (0, 0) if bbox is None else _bbox(bbox)[:2]
    return round(x / scale + left), round(y / scale + top)


def bbox_original_to_thumbnail(
    target: Mapping[str, Any] | Sequence[int],
    scale: float,
    capture_bbox: Mapping[str, Any] | Sequence[int] | None = None,
) -> dict[str, int]:
    left, top, width, height = _bbox(target)
    x, y = point_original_to_thumbnail(left, top, scale, capture_bbox)
    return {
        "left": x,
        "top": y,
        "width": round(width * scale),
        "height": round(height * scale),
    }


def bbox_thumbnail_to_original(
    target: Mapping[str, Any] | Sequence[int],
    scale: float,
    capture_bbox: Mapping[str, Any] | Sequence[int] | None = None,
) -> dict[str, int]:
    left, top, width, height = _bbox(target)
    x, y = point_thumbnail_to_original(left, top, scale, capture_bbox)
    return {
        "left": x,
        "top": y,
        "width": round(width / scale),
        "height": round(height / scale),
    }


# Short aliases kept useful for callers and tests.
original_to_thumbnail = point_original_to_thumbnail
thumbnail_to_original = point_thumbnail_to_original
