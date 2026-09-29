from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec
from aether_caps_desktop.input import click_target, send_keys
from aether_caps_desktop.windows import focus_window, list_windows


def register_desktop(tools: ToolRegistry, settings: Settings) -> None:
    app_aliases = {
        "bloc de notas": "notepad.exe",
        "notepad": "notepad.exe",
        "calculadora": "calc.exe",
        "calculator": "calc.exe",
        "explorador": "explorer.exe",
        "explorer": "explorer.exe",
        "terminal": "wt.exe",
        "windows terminal": "wt.exe",
        "powershell": "powershell.exe",
        "cmd": "cmd.exe",
        "cursor": "cursor.exe",
        "visual studio code": "code.exe",
        "vscode": "code.exe",
        "chrome": "chrome.exe",
        "edge": "msedge.exe",
        "firefox": "firefox.exe",
        "spotify": "spotify:",
        "whatsapp": "whatsapp:",
        "whatsapp web": "whatsapp:",
        "whatsapp escritorio": "whatsapp:",
        "whatsapp web la app": "whatsapp:",
    }
    protocol_processes = {
        "spotify:": ["Spotify.exe"],
        "whatsapp:": ["WhatsApp.Root.exe", "WhatsApp.exe"],
    }

    async def open_app(args: dict[str, Any]) -> str:
        name = str(args.get("name") or args.get("app") or "").strip()
        if not name:
            return json.dumps({"ok": False, "error": "name required"})

        executable = app_aliases.get(name.lower(), name)
        try:
            if os.name == "nt" and executable in protocol_processes:
                process_names = protocol_processes[executable]
                launch = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", executable, None, None, 1
                )
                verified = int(launch) > 32 and await _wait_for_any_process(
                    process_names, timeout=10.0
                )
                executable = process_names[0]
            elif os.name == "nt" and (
                Path(executable).exists() or shutil.which(executable)
            ):
                process = subprocess.Popen(
                    [executable],
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                verified = await _wait_for_process(executable, process.pid)
            else:
                before = _matching_processes(executable)
                launch = subprocess.run(
                    ["cmd", "/c", "start", "", executable],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                verified = launch.returncode == 0 and await _wait_for_named_process(
                    executable, before
                )

            if not verified:
                return json.dumps(
                    {
                        "ok": False,
                        "verified": False,
                        "error": f"No pude confirmar que {name} se abriera",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": True,
                    "verified": True,
                    "message": f"{name} está abierto",
                    "executable": executable,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    async def open_whatsapp(_args: dict[str, Any]) -> str:
        result = await open_app({"name": "whatsapp"})
        parsed = json.loads(result)
        if parsed.get("ok") is True:
            parsed["message"] = "WhatsApp de escritorio está abierto"
            parsed["app_type"] = "desktop"
        return json.dumps(parsed, ensure_ascii=False)

    async def spotify_play(args: dict[str, Any]) -> str:
        query = str(args.get("query") or args.get("song") or "").strip()
        if not query:
            return json.dumps(
                {"ok": False, "verified": False, "error": "Falta la canción"},
                ensure_ascii=False,
            )
        if os.name != "nt":
            return json.dumps(
                {"ok": False, "verified": False, "error": "Solo disponible en Windows"}
            )
        try:
            uri = f"spotify:search:{quote(query)}"
            launch = ctypes.windll.shell32.ShellExecuteW(
                None, "open", uri, None, None, 1
            )
            if int(launch) <= 32 or not await _wait_for_named_process(
                "Spotify.exe", set(), timeout=12.0
            ):
                return json.dumps(
                    {
                        "ok": False,
                        "verified": False,
                        "error": "Spotify de escritorio no está instalado o no respondió",
                    },
                    ensure_ascii=False,
                )

            await asyncio.sleep(2.5)
            hwnd = _foreground_process_window("Spotify.exe")
            if not hwnd:
                return json.dumps(
                    {
                        "ok": False,
                        "verified": False,
                        "error": "Spotify abrió, pero no pude controlar su ventana",
                    },
                    ensure_ascii=False,
                )
            before_title = _window_title(hwnd)
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            await asyncio.sleep(0.5)

            from pynput.keyboard import Controller, Key

            keyboard = Controller()
            keyboard.press(Key.ctrl)
            keyboard.press("k")
            keyboard.release("k")
            keyboard.release(Key.ctrl)
            await asyncio.sleep(0.4)
            keyboard.type(query)
            await asyncio.sleep(1.5)
            keyboard.press(Key.enter)
            keyboard.release(Key.enter)
            await asyncio.sleep(1.2)
            keyboard.press(Key.tab)
            keyboard.release(Key.tab)
            keyboard.press(Key.enter)
            keyboard.release(Key.enter)
            await asyncio.sleep(2.0)

            after_title = _window_title(hwnd)
            generic = {"", "spotify", "spotify premium", before_title.lower()}
            playing = after_title.lower() not in generic
            if not playing:
                return json.dumps(
                    {
                        "ok": False,
                        "verified": False,
                        "error": (
                            "Spotify mostró la búsqueda, pero no pude verificar "
                            "que iniciara la canción"
                        ),
                        "window_title": after_title,
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": True,
                    "verified": True,
                    "message": f"Reproduciendo en Spotify: {after_title}",
                    "query": query,
                    "window_title": after_title,
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"ok": False, "verified": False, "error": str(exc)},
                ensure_ascii=False,
            )

    async def open_path(args: dict[str, Any]) -> str:
        path = Path(str(args.get("path") or "")).expanduser().resolve()
        if not path.exists():
            return json.dumps(
                {"ok": False, "verified": False, "error": f"No existe: {path}"},
                ensure_ascii=False,
            )
        try:
            if os.name == "nt":
                result = ctypes.windll.shell32.ShellExecuteW(
                    None,
                    "open",
                    str(path),
                    None,
                    str(path.parent),
                    1,
                )
                verified = int(result) > 32
            else:
                process = subprocess.Popen(["xdg-open", str(path)])
                verified = process.poll() in (None, 0)
            if not verified:
                return json.dumps(
                    {
                        "ok": False,
                        "verified": False,
                        "error": f"El sistema rechazó abrir: {path}",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": True,
                    "verified": True,
                    "message": f"El sistema aceptó abrir: {path}",
                    "path": str(path),
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"ok": False, "verified": False, "error": str(exc)},
                ensure_ascii=False,
            )

    async def list_processes(_args: dict[str, Any]) -> str:
        try:
            import psutil

            titles = []
            for p in psutil.process_iter(["pid", "name"]):
                info = p.info
                titles.append({"pid": info.get("pid"), "name": info.get("name")})
            return json.dumps({"ok": True, "processes": titles[:80]})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    async def windows_list(args: dict[str, Any]) -> str:
        try:
            windows = list_windows(
                title=str(args.get("title") or "") or None,
                limit=max(1, min(int(args.get("limit") or 100), 500)),
            )
            return json.dumps({"ok": True, "windows": windows}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    async def window_focus(args: dict[str, Any]) -> str:
        try:
            result = focus_window(
                hwnd=int(args["hwnd"]) if args.get("hwnd") is not None else None,
                title=str(args.get("title") or "") or None,
                pid=int(args["pid"]) if args.get("pid") is not None else None,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"ok": False, "verified": False, "error": str(exc)},
                ensure_ascii=False,
            )

    async def input_send_keys(args: dict[str, Any]) -> str:
        return json.dumps(await send_keys(args), ensure_ascii=False)

    async def input_click(args: dict[str, Any]) -> str:
        return json.dumps(await click_target(args), ensure_ascii=False)

    async def read_clipboard(_args: dict[str, Any]) -> str:
        try:
            import pyperclip

            return json.dumps({"ok": True, "text": pyperclip.paste()[:4000]})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    async def write_clipboard(args: dict[str, Any]) -> str:
        text = str(args.get("text") or "")
        try:
            import pyperclip

            pyperclip.copy(text)
            return json.dumps({"ok": True, "message": "Portapapeles actualizado"})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    async def read_file(args: dict[str, Any]) -> str:
        path = Path(str(args.get("path") or "")).expanduser()
        if not path.exists():
            return json.dumps({"ok": False, "error": "no existe"})
        data = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({"ok": True, "content": data[:8000]})

    async def write_file(args: dict[str, Any]) -> str:
        path = Path(str(args.get("path") or "")).expanduser()
        content = str(args.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verified = path.exists() and path.read_text(encoding="utf-8") == content
        return json.dumps(
            {
                "ok": verified,
                "verified": verified,
                "message": f"Escrito y verificado: {path}" if verified else "",
                "error": None if verified else f"No pude verificar la escritura: {path}",
            },
            ensure_ascii=False,
        )

    async def list_dir(args: dict[str, Any]) -> str:
        path = Path(str(args.get("path") or ".")).expanduser()
        if not path.exists():
            return json.dumps({"ok": False, "error": "no existe"})
        entries = sorted(os.listdir(path))[:200]
        return json.dumps({"ok": True, "entries": entries})

    async def shell_run(args: dict[str, Any]) -> str:
        cmd = str(args.get("command") or "")
        cwd = str(args.get("cwd") or ".")
        timeout = int(args.get("timeout") or settings.supervisor.default_timeout_s)

        def _run():
            completed = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": completed.returncode == 0,
                "verified": completed.returncode == 0,
                "code": completed.returncode,
                "stdout": completed.stdout[-6000:],
                "stderr": completed.stderr[-2000:],
            }

        try:
            result = await asyncio.to_thread(_run)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(exc)})

    async def notify(args: dict[str, Any]) -> str:
        title = str(args.get("title") or "ADAM")
        body = str(args.get("body") or "")
        if os.name == "nt":
            ps = (
                f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; '
                f'Write-Output "{title}: {body}"'
            )
            await asyncio.to_thread(
                subprocess.run,
                ["powershell", "-NoProfile", "-Command", f'Write-Host "{title}: {body}"'],
                capture_output=True,
            )
        return json.dumps({"ok": True, "message": f"{title}: {body}"})

    tools.register(
        ToolSpec(
            "desktop.open_app",
            "Abre una aplicación de Windows y verifica que el proceso exista.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            open_app,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.open_path",
            "Abre un archivo o carpeta existente con la aplicación predeterminada.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            open_path,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.list_processes",
            "Lista procesos en ejecución.",
            {"type": "object", "properties": {}},
            list_processes,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.list_windows",
            "Lista ventanas visibles de Windows con título, proceso, bbox y estado.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
            },
            windows_list,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.focus_window",
            "Enfoca una ventana por hwnd, título o pid y verifica el foco.",
            {
                "type": "object",
                "properties": {
                    "hwnd": {"type": "integer"},
                    "title": {"type": "string"},
                    "pid": {"type": "integer"},
                },
            },
            window_focus,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.send_keys",
            "Envía texto o combinaciones de teclas y verifica un cambio visual.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 2000},
                    "keys": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                        ]
                    },
                    "target": {"type": "string"},
                    "window": {"type": "string"},
                    "settle_seconds": {"type": "number", "minimum": 0, "maximum": 2},
                },
            },
            input_send_keys,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.click_target",
            "Hace clic en un target UIA, bbox o punto seguro y verifica un cambio visual.",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "window": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "bbox": {"type": "object"},
                    "coordinate_space": {
                        "type": "string",
                        "enum": ["screen", "thumbnail"],
                    },
                    "thumbnail_scale": {"type": "number"},
                    "capture_bbox": {"type": "object", "properties": {}},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                    },
                    "clicks": {"type": "integer", "minimum": 1, "maximum": 2},
                    "settle_seconds": {"type": "number", "minimum": 0, "maximum": 2},
                },
            },
            input_click,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.clipboard_read",
            "Lee el portapapeles.",
            {"type": "object", "properties": {}},
            read_clipboard,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.clipboard_write",
            "Escribe texto en el portapapeles.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            write_clipboard,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "fs.read",
            "Lee un archivo de texto.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            read_file,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "fs.write",
            "Escribe un archivo de texto.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            write_file,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "fs.list",
            "Lista archivos de un directorio.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            list_dir,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "shell.run",
            "Ejecuta un comando de shell. Requiere confirmación oral.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
            shell_run,
            "L2",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.notify",
            "Muestra una notificación / mensaje de estado.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["body"],
            },
            notify,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.open_whatsapp",
            "Abre específicamente WhatsApp de escritorio; no abre WhatsApp Web en el navegador.",
            {"type": "object", "properties": {}},
            open_whatsapp,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "desktop.spotify_play",
            "Abre Spotify de escritorio, busca una canción y trata de reproducirla; solo confirma si detecta reproducción.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Canción y opcionalmente artista.",
                    }
                },
                "required": ["query"],
            },
            spotify_play,
            "L1",
        )
    )


def _process_name(executable: str) -> str:
    return Path(executable).name.lower()


def _matching_processes(executable: str) -> set[int]:
    try:
        import psutil

        expected = _process_name(executable)
        return {
            int(proc.info["pid"])
            for proc in psutil.process_iter(["pid", "name"])
            if str(proc.info.get("name") or "").lower() == expected
        }
    except Exception:  # noqa: BLE001
        return set()


async def _wait_for_process(executable: str, pid: int, timeout: float = 5.0) -> bool:
    expected = _process_name(executable)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            import psutil

            proc = psutil.Process(pid)
            if proc.is_running() and (
                not expected or proc.name().lower() == expected
            ):
                return True
        except Exception:  # noqa: BLE001
            pass
        if _matching_processes(executable):
            return True
        await asyncio.sleep(0.2)
    return False


async def _wait_for_named_process(
    executable: str, before: set[int], timeout: float = 6.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _matching_processes(executable)
        if current and (current - before or current == before):
            return True
        await asyncio.sleep(0.25)
    return False


async def _wait_for_any_process(
    process_names: list[str], timeout: float = 6.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(_matching_processes(name) for name in process_names):
            return True
        await asyncio.sleep(0.25)
    return False


def _foreground_process_window(process_name: str) -> int:
    try:
        import psutil

        pids = {
            int(proc.info["pid"])
            for proc in psutil.process_iter(["pid", "name"])
            if str(proc.info.get("name") or "").lower() == process_name.lower()
        }
    except Exception:  # noqa: BLE001
        return 0
    matches: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_proc
    def callback(hwnd, _lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids and _window_title(int(hwnd)):
            matches.append(int(hwnd))
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return matches[0] if matches else 0


def _window_title(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()
