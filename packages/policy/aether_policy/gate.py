from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aether_core.config import Settings
from aether_core.tools import ToolRegistry


@dataclass
class PolicyDecision:
    allowed: bool
    needs_confirm: bool
    level: str
    reason: str = ""


# Only these still ask for oral confirm even in trusted mode.
ALWAYS_CONFIRM = {
    "mail.send",
    "gmail.send",
    "gmail.delete",
    "calendar.delete_event_google",
    "calendar.publish_event",
    "browser.drive_upload",
    "drive.share",
    "messaging.send_telegram",
    "messaging.send_whatsapp",
    "messaging.delete_message",
    "messaging.telegram_send_draft",
    "messaging.whatsapp_send_draft",
    "messaging.telegram_delete_message",
    "fs.trash",
    "files.trash",
    "files.restore",
    "backup.restore",
    "commerce.checkout",
}

# Hard deny (never run without explicit future allowlist).
ALWAYS_DENY = {
    "system.shutdown",
    "system.format",
}

SENSITIVE_TITLE_RE = re.compile(
    r"(?i)(credential|windows security|user account control|\buac\b|"
    r"\bpassword\b|\botp\b|\b2fa\b|authenticator)"
)
VISUAL_RATE_TOOLS = {
    "desktop.click_target",
    "desktop.send_keys",
    "desktop.click",
    "vision.act_and_verify",
}


def _sensitive_ui(arguments: dict[str, Any]) -> bool:
    target = arguments.get("target") if isinstance(arguments.get("target"), dict) else {}
    texts = [
        str(arguments.get("window") or ""),
        str(arguments.get("title") or ""),
        str(target.get("window") or ""),
        str(target.get("label") or ""),
        str(target.get("name") or ""),
    ]
    return any(SENSITIVE_TITLE_RE.search(text) for text in texts if text)


def _sensitive_effect(name: str) -> bool:
    stem = name.rsplit(".", 1)[-1].lower()
    if stem in {"send_keys", "send_agent"}:
        return False
    if stem in {"send", "trash", "restore", "checkout", "share", "publish"}:
        return True
    return stem.startswith(
        ("send_", "delete", "share_", "publish_", "restore_", "trash_")
    )


class PermissionGate:
    def __init__(self, settings: Settings, tools: ToolRegistry) -> None:
        self.settings = settings
        self.tools = tools
        self._blocked = [re.compile(p) for p in settings.policy.blocked_patterns]
        self.trusted = bool(getattr(settings.policy, "trusted_mode", True))
        self._visual_times: deque[float] = deque()

    def evaluate(self, name: str, arguments: dict[str, Any]) -> PolicyDecision:
        level = self.tools.level_for(name)
        blob = json.dumps(arguments, ensure_ascii=False)
        always_deny = ALWAYS_DENY | set(self.settings.policy.always_deny_tools)
        always_confirm = ALWAYS_CONFIRM | set(
            self.settings.policy.always_confirm_tools
        )

        if name in always_deny:
            return PolicyDecision(False, False, "L3", "Acción denegada por política")

        for rx in self._blocked:
            if rx.search(blob):
                return PolicyDecision(False, False, "L3", "Patrón bloqueado por política")

        if level == "L3":
            return PolicyDecision(False, False, "L3", "Acción L3 bloqueada")

        # Destructive path keywords still need confirm
        cmd = str(arguments.get("command") or "")
        if re.search(r"(?i)\b(rm\s+-rf|del\s+/[sf]|format\s+|shutdown\b|Remove-Item\s+-Recurse)\b", cmd):
            return PolicyDecision(True, True, "L2", "Comando destructivo — confirmación oral")

        if name in always_confirm or _sensitive_effect(name):
            return PolicyDecision(
                True,
                True,
                "L2",
                "La acción envía, publica, comparte, borra, compra o restaura",
            )

        if name in VISUAL_RATE_TOOLS and _sensitive_ui(arguments):
            return PolicyDecision(
                False, False, "L3", "UI de credenciales o 2FA bloqueada"
            )

        if name in VISUAL_RATE_TOOLS and not self._visual_allowed():
            return PolicyDecision(
                False,
                False,
                "L2",
                "Límite de acciones visuales por minuto alcanzado",
            )

        entity = str(arguments.get("entity_id") or arguments.get("device") or "")
        critical_prefixes = tuple(self.settings.policy.critical_entity_prefixes)
        if name.startswith("home.") and entity.startswith(critical_prefixes):
            return PolicyDecision(
                True, True, "L2", "Dispositivo crítico requiere confirmación"
            )

        if name == "desktop.open_app" and not self.trusted:
            app = str(arguments.get("name") or arguments.get("app") or "").lower()
            if not self._app_allowed(app):
                return PolicyDecision(False, False, "L2", "Aplicación no permitida")

        if name.startswith(("fs.", "backup.", "files.")):
            path = str(
                arguments.get("path")
                or arguments.get("folder_path")
                or arguments.get("source")
                or ""
            )
            if path and not self._path_allowed(path):
                return PolicyDecision(False, False, "L2", "Ruta fuera de la política")

        # Trusted owner: full local control without asking for normal ops
        if self.trusted:
            return PolicyDecision(True, False, level, "trusted_mode")

        # Legacy non-trusted path
        needs = level in self.settings.policy.require_oral_confirm_for
        if level == "L1":
            needs = False
        if name.startswith("shell.") or name in {"dev.run_command"}:
            needs = True
            level = "L2"
        return PolicyDecision(True, needs, level, "")

    def _visual_allowed(self) -> bool:
        limit = int(self.settings.policy.max_visual_actions_per_minute or 30)
        now = time.monotonic()
        while self._visual_times and now - self._visual_times[0] > 60:
            self._visual_times.popleft()
        if len(self._visual_times) >= max(1, limit):
            return False
        self._visual_times.append(now)
        return True

    def _app_allowed(self, app: str) -> bool:
        whitelist = [a.lower() for a in self.settings.policy.app_whitelist]
        return any(w in app for w in whitelist)

    def _path_allowed(self, path: str) -> bool:
        if self.trusted:
            return True
        expanded = Path(path).expanduser().resolve()
        for allowed in self.settings.policy.path_whitelist:
            base = Path(allowed).expanduser().resolve()
            try:
                expanded.relative_to(base)
                return True
            except ValueError:
                continue
        try:
            from aether_core.config import ROOT

            expanded.relative_to(ROOT.resolve())
            return True
        except ValueError:
            return False
