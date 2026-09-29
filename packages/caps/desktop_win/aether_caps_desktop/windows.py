from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any


def _require_windows() -> Any:
    if os.name != "nt":
        raise RuntimeError("window control is only available on Windows")
    return ctypes.windll.user32


def _title(user32: Any, hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _rect(user32: Any, hwnd: int) -> dict[str, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"left": 0, "top": 0, "width": 0, "height": 0}
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }


def _process_name(pid: int) -> str | None:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return None


def list_windows(*, title: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    user32 = _require_windows()
    found: list[dict[str, Any]] = []
    active = int(user32.GetForegroundWindow() or 0)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(raw_hwnd: int, _lparam: int) -> bool:
        hwnd = int(raw_hwnd)
        if len(found) >= max(1, min(limit, 500)) or not user32.IsWindowVisible(hwnd):
            return True
        window_title = _title(user32, hwnd)
        if not window_title or (title and title.casefold() not in window_title.casefold()):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(
            {
                "hwnd": hwnd,
                "title": window_title,
                "pid": int(pid.value),
                "process": _process_name(int(pid.value)),
                "bbox": _rect(user32, hwnd),
                "active": hwnd == active,
                "minimized": bool(user32.IsIconic(hwnd)),
            }
        )
        return True

    user32.EnumWindows(callback, 0)
    return found


def get_active_window() -> dict[str, Any] | None:
    user32 = _require_windows()
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {
        "hwnd": hwnd,
        "title": _title(user32, hwnd),
        "pid": int(pid.value),
        "process": _process_name(int(pid.value)),
        "bbox": _rect(user32, hwnd),
    }


def resolve_window(
    *, hwnd: int | None = None, title: str | None = None, pid: int | None = None
) -> dict[str, Any] | None:
    if hwnd:
        return next((item for item in list_windows(limit=500) if item["hwnd"] == hwnd), None)
    matches = list_windows(title=title, limit=500)
    if pid is not None:
        matches = [item for item in matches if item["pid"] == pid]
    if not matches:
        return None
    return next((item for item in matches if item["active"]), matches[0])


def focus_window(
    *, hwnd: int | None = None, title: str | None = None, pid: int | None = None
) -> dict[str, Any]:
    user32 = _require_windows()
    window = resolve_window(hwnd=hwnd, title=title, pid=pid)
    if not window:
        return {"ok": False, "verified": False, "error": "window not found"}
    target = int(window["hwnd"])
    if window.get("minimized"):
        user32.ShowWindow(target, 9)  # SW_RESTORE
    user32.BringWindowToTop(target)
    accepted = bool(user32.SetForegroundWindow(target))
    active = int(user32.GetForegroundWindow() or 0)
    verified = accepted and active == target
    return {
        "ok": verified,
        "verified": verified,
        "window": window,
        "error": None if verified else "Windows did not focus the requested window",
    }


def virtual_screen_bbox() -> dict[str, int]:
    user32 = _require_windows()
    left = int(user32.GetSystemMetrics(76))
    top = int(user32.GetSystemMetrics(77))
    width = int(user32.GetSystemMetrics(78))
    height = int(user32.GetSystemMetrics(79))
    return {"left": left, "top": top, "width": width, "height": height}


def find_uia_target(
    name: str, *, window_title: str | None = None
) -> dict[str, Any] | None:
    """Use pywinauto when installed; absence is an expected fallback condition."""
    try:
        from pywinauto import Desktop
    except ImportError:
        return None
    try:
        root = (
            Desktop(backend="uia").window(title_re=f".*{window_title}.*")
            if window_title
            else Desktop(backend="uia")
        )
        element = root.child_window(title=name)
        wrapper = element.wrapper_object()
        rect = wrapper.rectangle()
        return {
            "method": "uia",
            "name": name,
            "bbox": {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(rect.width()),
                "height": int(rect.height()),
            },
            "_wrapper": wrapper,
        }
    except Exception:  # noqa: BLE001
        return None
