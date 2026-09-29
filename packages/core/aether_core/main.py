from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

# Allow `python packages/core/aether_core/main.py` during development.
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aether_audio.pipeline import AudioPipeline
from aether_brain.kimi import KimiPlanner
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
from aether_core.config import ROOT, load_settings
from aether_core.events import EventBus
from aether_core.hud_server import HudServer
from aether_core.session import SessionDirector
from aether_core.tools import ToolRegistry
from aether_memory.store import MemoryStore
from aether_memory.tools import register_memory_tools
from aether_policy.gate import PermissionGate
from aether_scheduler.scheduler import PersistentScheduler
from aether_scheduler.tools import register_scheduler
from aether_supervisor.manager import ProcessSupervisor
from aether_voice.eleven import ElevenLabsTTS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aether")


def _ensure_data_dirs(settings) -> None:
    settings.resolve_path(settings.memory.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.memory.chroma_path).mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.files.trash_dir).mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.backup.default_dest).mkdir(parents=True, exist_ok=True)
    settings.resolve_path(settings.vision.captures_dir).mkdir(parents=True, exist_ok=True)
    (ROOT / "data").mkdir(parents=True, exist_ok=True)


def _resolved(settings, relative: str) -> str:
    return str(settings.resolve_path(relative))


async def run() -> None:
    settings = load_settings()
    _ensure_data_dirs(settings)

    bus = EventBus()
    tools = ToolRegistry()
    memory = MemoryStore(settings)
    await memory.open()
    register_memory_tools(tools, memory)

    policy = PermissionGate(settings, tools)
    supervisor = ProcessSupervisor(settings, tools, bus)
    await supervisor.open()

    async def _run_step(_routine: dict, step: dict) -> str:
        return await supervisor.run_tool(
            str(step.get("action") or step.get("tool") or ""),
            dict(step.get("arguments") or step.get("args") or {}),
        )

    db = _resolved(settings, settings.memory.sqlite_path)
    scheduler = PersistentScheduler(
        SimpleNamespace(sqlite_path=db, poll_seconds=float(settings.scheduler.tick_seconds)),
        supervisor=_run_step,
    )
    await scheduler.open()

    files_cfg = SimpleNamespace(
        sqlite_path=db,
        trash_path=_resolved(settings, settings.files.trash_dir),
        root=str(Path.home()),
        index_roots=[str(Path(p).expanduser()) for p in settings.files.index_roots],
        exclude_globs=list(settings.files.exclude_globs),
        max_files=settings.files.max_index_files,
        max_depth=settings.files.max_depth,
    )
    health_cfg = SimpleNamespace(
        sqlite_path=db,
        cpu_warn_percent=settings.health.cpu_warn_percent,
        memory_warn_percent=settings.health.memory_warn_percent,
        disks=settings.health.disks,
    )
    backup_cfg = SimpleNamespace(
        sqlite_path=db,
        default_dest=_resolved(settings, settings.backup.default_dest),
        output_path=_resolved(settings, settings.backup.default_dest),
        max_set_size_gb=settings.backup.max_set_size_gb,
    )

    caps = settings.capabilities
    if caps.desktop_win:
        register_desktop(tools, settings)
    if caps.dev_tools:
        register_dev(tools, settings)
    if caps.browser:
        register_browser(tools, settings)
    if caps.iot_home:
        register_iot(tools, settings)
    if caps.calendar_mail:
        register_life(tools, settings)
    if caps.screen_camera:
        register_vision(tools, settings)
    if getattr(caps, "files_intel", True):
        register_files_intel(tools, files_cfg)
    if getattr(caps, "backup", True):
        register_backup(tools, backup_cfg)
    if getattr(caps, "health", True):
        register_health(tools, health_cfg)
    if getattr(caps, "messaging", True):
        register_messaging(tools, settings)
    if getattr(caps, "integrations", True):
        register_integrations(tools, settings)
    if getattr(caps, "scheduler", True):
        register_scheduler(tools, scheduler)
        await scheduler.seed_defaults()
        await scheduler.start()

    brain = KimiPlanner(settings)
    voice = ElevenLabsTTS(settings, bus)
    session = SessionDirector(
        settings, bus, brain, voice, memory, policy, supervisor, tools
    )
    await session.start()

    hud = HudServer(bus, settings.hud.ws_host, settings.hud.ws_port)
    await hud.start()

    audio = AudioPipeline(settings, bus)
    await audio.start()

    logger.info(
        "ADAM online — wake=%s ptt=%s tools=%d cursor_api=%s",
        settings.audio.wake_enabled,
        settings.audio.ptt_hotkey,
        len(tools.names()),
        "yes" if settings.cursor_api_key else "MISSING",
    )
    await bus.publish("ready", tools=tools.names())

    stop = asyncio.Event()

    def _handle_sig(*_args):
        stop.set()

    try:
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, _handle_sig)
            except NotImplementedError:
                signal.signal(sig, lambda *_: stop.set())
    except Exception:  # noqa: BLE001
        pass

    if "--text" in sys.argv:
        asyncio.create_task(_text_repl(bus))

    await stop.wait()
    await audio.stop()
    await hud.stop()
    await scheduler.close()
    await supervisor.close()
    await memory.close()
    await brain.close()


async def _text_repl(bus: EventBus) -> None:
    """Developer escape hatch — product UX remains voice-first."""
    loop = asyncio.get_running_loop()
    print("ADAM text debug mode. Type a command (or 'quit').")
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        text = line.strip()
        if text.lower() in {"quit", "exit"}:
            break
        if text:
            await bus.publish("utterance", text=text)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
