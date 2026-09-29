"""Live probe of every registered ADAM tool with latency measurement.

Destructive sends/clicks/launches are invoked only via fail-fast arguments,
or skipped when the handler would still start an external agent or app.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aether_caps_backup.register import register_backup
from aether_caps_browser.register import register_browser
from aether_caps_desktop.register import register_desktop
from aether_caps_dev.register import register_dev
from aether_caps_files_intel.register import register_files_intel
from aether_caps_health.register import register_health
from aether_caps_integrations.register import register_integrations
from aether_caps_iot.register import register_iot
from aether_caps_life.register import register_life
from aether_caps_messaging.register import register_messaging
from aether_caps_vision.register import register_vision
from aether_core.config import load_settings
from aether_core.events import EventBus
from aether_core.tools import ToolRegistry
from aether_memory.store import MemoryStore
from aether_memory.tools import register_memory_tools
from aether_scheduler.scheduler import PersistentScheduler
from aether_scheduler.tools import register_scheduler
from aether_supervisor.manager import ProcessSupervisor


SKIP = {
    "cursor.send_agent": "lanza un agente Cursor de pago/largo",
    "agent.delegate_developer": "alias de delegación Cursor",
    "cursor.open_project": "abriría otra ventana de Cursor",
    "dev.open_editor": "abre Cursor (mismo handler que cursor.open_project)",
    "desktop.open_app": "start.exe espera 15s con nombres inventados",
    "desktop.open_whatsapp": "abre WhatsApp Desktop",
    "desktop.spotify_play": "abre Spotify Desktop",
    "browser.youtube_play": "reproduciría YouTube",
    "browser.drive_upload": "subiría un archivo",
    "browser.goto": "abre Chromium persistente (conflicto con perfil vivo)",
    "browser.click": "requiere página abierta",
    "browser.type": "requiere página abierta",
    "browser.click_text": "requiere página abierta",
    "browser.extract": "requiere página abierta",
    "desktop.clipboard_write": "sobrescribe el portapapeles",
}

TIMEOUTS = {
    "files.index": 90.0,
    "vision.describe_screen": 90.0,
    "vision.capture_screen": 20.0,
    "vision.capture_camera": 15.0,
    "integration.status": 25.0,
    "health.snapshot": 20.0,
    "desktop.list_processes": 20.0,
    "desktop.list_windows": 15.0,
    "spotify.search": 15.0,
    "spotify.control": 15.0,
    "calendar.list_events": 15.0,
    "home.check_connection": 15.0,
    "vision.act_and_verify": 25.0,
    "desktop.click_target": 15.0,
    "desktop.send_keys": 15.0,
}


def _parse(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"ok": True, "result": data}
        except json.JSONDecodeError:
            return {"ok": True, "message": raw[:400]}
    return {"ok": True, "result": raw}


def classify(name: str, data: dict, error: str | None) -> str:
    if error == "timeout":
        return "timeout"
    if error:
        return "error"
    status = str(data.get("status") or "")
    if status in {"needs_oauth", "needs_login", "stub"} or data.get("stub"):
        return "needs_config"
    if data.get("ok") and data.get("verified") is False:
        return "ok_unverified"
    if data.get("ok") is False:
        return "expected_fail" if data.get("error") else "error"
    if data.get("ok"):
        return "ok"
    return "ok"


async def build_registry(tmp: Path) -> tuple[ToolRegistry, list]:
    settings = load_settings()
    db = str(tmp / "probe.db")
    settings.memory.sqlite_path = db
    settings.files.trash_dir = str(tmp / "trash")
    settings.backup.default_dest = str(tmp / "backups")
    settings.vision.captures_dir = str(tmp / "captures")
    Path(settings.files.trash_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.backup.default_dest).mkdir(parents=True, exist_ok=True)
    Path(settings.vision.captures_dir).mkdir(parents=True, exist_ok=True)

    fixture = tmp / "docs"
    fixture.mkdir()
    (fixture / "nota.txt").write_text("adam probe nota", encoding="utf-8")
    (fixture / "dup_a.txt").write_text("same", encoding="utf-8")
    (fixture / "dup_b.txt").write_text("same", encoding="utf-8")

    tools = ToolRegistry()
    bus = EventBus()
    memory = MemoryStore(settings)
    await memory.open()
    register_memory_tools(tools, memory)

    supervisor = ProcessSupervisor(settings, tools, bus)
    await supervisor.open()

    async def _run_step(_routine, step):
        return await supervisor.run_tool(
            str(step.get("action") or ""),
            dict(step.get("arguments") or {}),
        )

    scheduler = PersistentScheduler(
        SimpleNamespace(sqlite_path=db, poll_seconds=0.05),
        supervisor=_run_step,
    )
    await scheduler.open()

    files_cfg = SimpleNamespace(
        sqlite_path=db,
        trash_path=str(tmp / "trash"),
        root=str(tmp),
        index_roots=[str(fixture)],
        exclude_globs=[".git", "node_modules", ".venv"],
        max_files=200,
        max_depth=4,
    )
    health_cfg = SimpleNamespace(
        sqlite_path=db,
        cpu_warn_percent=90.0,
        memory_warn_percent=90.0,
        disks=["C:\\"],
    )
    backup_cfg = SimpleNamespace(
        sqlite_path=db,
        default_dest=str(tmp / "backups"),
        output_path=str(tmp / "backups"),
        max_set_size_gb=1.0,
    )

    register_desktop(tools, settings)
    register_dev(tools, settings)
    register_browser(tools, settings)
    register_iot(tools, settings)
    register_life(tools, settings)
    register_vision(tools, settings)
    register_files_intel(tools, files_cfg)
    register_backup(tools, backup_cfg)
    register_health(tools, health_cfg)
    register_messaging(tools, settings)
    register_integrations(tools, settings)
    register_scheduler(tools, scheduler)
    await scheduler.seed_defaults()

    closers = [memory.close, supervisor.close, scheduler.close]
    return tools, closers, fixture, tmp, scheduler


async def probe() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="adam-probe-"))
    tools, closers, fixture, tmp, scheduler = await build_registry(tmp)
    probe_file = tmp / "docs" / "nota.txt"
    copy_dest = tmp / "docs" / "nota_copy.txt"
    write_path = tmp / "fs_write.txt"
    snippet = tmp / "snippet.py"

    calls: dict[str, dict] = {
        "memory.list_preferences": {},
        "memory.set_preference": {"key": "probe_key", "value": "1"},
        "memory.set_fact": {
            "subject": "probe",
            "predicate": "city",
            "value": "Bogota",
            "source": "user",
        },
        "memory.get_facts": {"subject": "probe"},
        "memory.remember_fact": {
            "subject": "probe",
            "predicate": "lang",
            "value": "es",
            "source": "user",
        },
        "memory.recall_facts": {"subject": "probe"},
        "memory.put_entity": {"name": "ADAM-probe", "kind": "project"},
        "memory.list_entities": {"kind": "project"},
        "memory.add_summary": {"scope": "probe", "content": "resumen de prueba"},
        "memory.consolidate": {"limit": 10},
        "memory.forget_fact": {"subject": "probe", "predicate": "lang"},
        "health.snapshot": {},
        "health.recent": {"limit": 5},
        "health.alerts": {"limit": 5},
        "files.index": {},
        "files.search": {"query": "nota", "limit": 10},
        "files.duplicates": {},
        "files.copy": {"source": str(probe_file), "destination": str(copy_dest)},
        "files.move": {
            "source": str(copy_dest),
            "destination": str(tmp / "docs" / "nota_moved.txt"),
        },
        "files.trash": {"path": str(tmp / "docs" / "nota_moved.txt")},
        "files.restore": {"trash_id": "missing-id"},
        "backup.create_set": {"name": "probe", "paths": [str(fixture)]},
        "backup.run": {"set_name": "probe"},
        "backup.verify": {"run_or_archive": "missing"},
        "backup.restore": {
            "run_or_archive": "missing",
            "destination": str(tmp / "restore"),
        },
        "routine.list": {},
        "routine.create": {
            "name": "probe_once",
            "interval_seconds": 3600,
            "enabled": False,
            "steps": [{"action": "health.snapshot", "arguments": {}}],
        },
        "desktop.list_processes": {},
        "desktop.list_windows": {"limit": 20},
        "desktop.notify": {"title": "ADAM probe", "body": "ok"},
        "desktop.clipboard_read": {},
        "desktop.open_path": {"path": str(tmp / "no_existe")},
        "fs.list": {"path": str(fixture)},
        "fs.read": {"path": str(probe_file)},
        "fs.write": {"path": str(write_path), "content": "probe"},
        "shell.run": {"command": "echo adam-probe", "timeout": 10},
        "dev.git_status": {"cwd": str(ROOT)},
        "dev.run_command": {"command": "echo adam-dev", "cwd": str(ROOT), "timeout": 10},
        "dev.write_file": {"path": str(snippet), "content": "print(1)\n"},
        "dev.open_editor": {"path": str(tmp / "missing.py")},
        "vision.list_monitors": {},
        "vision.capture_screen": {"monitor": 1, "max_width": 640, "max_height": 360},
        "vision.capture_camera": {},
        "vision.ground_target": {"x": 10, "y": 10},
        "vision.describe_screen": {"monitor": 1},
        "calendar.list_events": {"limit": 3},
        "calendar.create_event": {"title": "probe local"},
        "mail.draft": {"to": "probe@local.test", "subject": "probe", "body": "x"},
        "mail.list_drafts": {"limit": 5},
        "mail.delete_draft": {"id": "missing"},
        "mail.send": {"id": "missing"},
        "messaging.status": {},
        "messaging.draft_create": {
            "channel": "telegram",
            "to": "probe",
            "body": "hola",
        },
        "messaging.draft_list": {},
        "messaging.draft_delete": {"draft_id": "missing"},
        "messaging.telegram_delete_message": {"chat_id": "0", "message_id": "0"},
        "integration.status": {"service": "all"},
        "integration.onboard": {"service": "google"},
        "spotify.control": {"action": "pause"},
        "spotify.search": {"query": "test", "limit": 1},
        "spotify.queue": {"uri": "spotify:track:probe"},
        "home.check_connection": {},
        "home.list_devices": {},
        "home.get_state": {"entity_id": "light.probe"},
        "desktop.send_keys": {},
        "desktop.click_target": {},
        "vision.act_and_verify": {"action": "click"},
        "desktop.focus_window": {"title": "ventana_inexistente_probe"},
        "desktop.open_app": {"name": "app_inexistente_probe"},
        "home.set_device": {"entity_id": "light.probe", "action": "turn_on"},
        "home.scene": {"name": "probe"},
        "messaging.telegram_send_draft": {"draft_id": "missing"},
        "messaging.whatsapp_send_draft": {"draft_id": "missing"},
    }

    results = []
    created_routine_id = None
    created_entity_id = None
    created_draft_id = None
    trash_id = None

    for name in sorted(tools.names()):
        if name in SKIP:
            results.append(
                {
                    "name": name,
                    "family": name.split(".", 1)[0],
                    "ms": 0,
                    "outcome": "skipped",
                    "ok": False,
                    "verified": False,
                    "detail": SKIP[name],
                }
            )
            continue
        args = dict(calls.get(name) or {})
        if name == "memory.add_link" and created_entity_id:
            args = {
                "source_id": created_entity_id,
                "target_id": created_entity_id,
                "relation": "self",
            }
        if name == "memory.link_entity" and created_entity_id:
            args = {
                "source_id": created_entity_id,
                "target_id": created_entity_id,
                "relation": "self",
            }
        if name == "files.restore" and trash_id:
            args = {"trash_id": trash_id}
        if name == "routine.enable" and created_routine_id:
            args = {"routine_id": created_routine_id, "enabled": False}
        if name == "routine.run_now" and created_routine_id:
            args = {"routine_id": created_routine_id}
        if name == "mail.delete_draft" and created_draft_id:
            args = {"id": created_draft_id}
        if name == "backup.verify":
            args = {"run_or_archive": "probe"}

        timeout = TIMEOUTS.get(name, 20.0)
        started = time.perf_counter()
        error = None
        data: dict = {}
        try:
            raw = await asyncio.wait_for(tools.call(name, args), timeout=timeout)
            data = _parse(raw)
        except asyncio.TimeoutError:
            error = "timeout"
            data = {"ok": False, "error": "timeout"}
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            data = {"ok": False, "error": error}
        ms = round((time.perf_counter() - started) * 1000, 1)
        outcome = classify(name, data, error)
        detail = str(
            data.get("error")
            or data.get("message")
            or data.get("status")
            or data.get("detail")
            or ""
        )[:180]
        if name == "memory.put_entity":
            created_entity_id = (data.get("entity") or {}).get("id")
        if name == "routine.create":
            created_routine_id = (data.get("routine") or {}).get("id")
        if name == "mail.draft":
            created_draft_id = data.get("id")
        if name == "files.trash":
            result = data.get("result") or {}
            trash_id = result.get("id") or result.get("trash_id") or data.get("id")

        results.append(
            {
                "name": name,
                "family": name.split(".", 1)[0],
                "ms": ms,
                "outcome": outcome,
                "ok": bool(data.get("ok")),
                "verified": data.get("verified"),
                "detail": detail,
            }
        )

    # Second pass for tools that needed IDs created later in alpha order
    pending = [
        n
        for n in tools.names()
        if n
        in {
            "memory.add_link",
            "memory.link_entity",
            "routine.enable",
            "routine.run_now",
            "files.restore",
            "mail.delete_draft",
            "backup.verify",
        }
        and any(r["name"] == n and r["outcome"] in {"expected_fail", "error"} for r in results)
    ]
    # Re-run restore if we now have trash_id
    for name in ("files.restore", "routine.enable", "memory.add_link"):
        if name not in tools.names():
            continue
        args = {}
        if name == "files.restore" and trash_id:
            args = {"trash_id": trash_id}
        elif name == "routine.enable" and created_routine_id:
            args = {"routine_id": created_routine_id, "enabled": False}
        elif name == "memory.add_link" and created_entity_id:
            args = {
                "source_id": created_entity_id,
                "target_id": created_entity_id,
                "relation": "self",
            }
        else:
            continue
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(tools.call(name, args), timeout=20)
            data = _parse(raw)
            error = None
        except Exception as exc:  # noqa: BLE001
            data = {"ok": False, "error": str(exc)}
            error = str(exc)
        ms = round((time.perf_counter() - started) * 1000, 1)
        for row in results:
            if row["name"] == name:
                row.update(
                    {
                        "ms": ms,
                        "outcome": classify(name, data, error),
                        "ok": bool(data.get("ok")),
                        "verified": data.get("verified"),
                        "detail": str(data.get("error") or data.get("message") or "")[:180],
                    }
                )

    for closer in closers:
        try:
            await closer()
        except Exception:  # noqa: BLE001
            pass

    executed = [r for r in results if r["outcome"] != "skipped"]
    latencies = [r["ms"] for r in executed]
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else 0
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else 0
    by_outcome: dict[str, int] = {}
    by_family: dict[str, list[float]] = {}
    for row in results:
        by_outcome[row["outcome"]] = by_outcome.get(row["outcome"], 0) + 1
        by_family.setdefault(row["family"], []).append(row["ms"])
    family_stats = [
        {
            "family": family,
            "count": len(ms),
            "mean_ms": round(statistics.mean(ms), 1) if ms else 0,
            "max_ms": round(max(ms), 1) if ms else 0,
        }
        for family, ms in sorted(by_family.items())
    ]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool_count": len(results),
        "executed": len(executed),
        "skipped": len(results) - len(executed),
        "total_s": round(sum(latencies) / 1000, 2),
        "p50_ms": p50,
        "p95_ms": p95,
        "mean_ms": round(statistics.mean(latencies), 1) if latencies else 0,
        "max_ms": round(max(latencies), 1) if latencies else 0,
        "by_outcome": by_outcome,
        "family_stats": family_stats,
        "slowest": sorted(executed, key=lambda r: r["ms"], reverse=True)[:12],
        "results": results,
        "tmp": str(tmp),
    }


def main() -> None:
    report = asyncio.run(probe())
    out = ROOT / "data" / "tool_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
