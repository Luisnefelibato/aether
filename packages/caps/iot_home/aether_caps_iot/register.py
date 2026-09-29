from __future__ import annotations

import json
from typing import Any

import httpx

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec

from .ha import CONTROLLABLE, HomeAssistant, resolve_entity_id, service_data

CRITICAL_DOMAINS = {
    "alarm_control_panel",
    "climate",
    "cover",
    "lock",
    "siren",
    "valve",
}

ONBOARD = {
    "steps": [
        "En Home Assistant: Perfil → Long-Lived Access Tokens → crear token.",
        "Pon HOME_ASSISTANT_URL (ej. http://homeassistant.local:8123) y HOME_ASSISTANT_TOKEN en aether/.env.",
        "Reinicia ADAM. Cerraduras y alarmas piden confirmación oral.",
    ],
    "env": ["HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN"],
    "automates_credentials_or_2fa": False,
}


def _result(ok: bool, **payload: Any) -> str:
    payload.setdefault("verified", ok)
    return json.dumps(
        {"ok": ok, **payload},
        ensure_ascii=False,
        default=str,
    )


def _critical_metadata(entity_id: str) -> dict[str, Any]:
    domain = entity_id.partition(".")[0]
    critical = domain in CRITICAL_DOMAINS
    return {
        "critical": critical,
        "critical_reason": (
            f"El dominio {domain} puede afectar acceso, seguridad o infraestructura"
            if critical
            else None
        ),
    }


def _missing() -> str:
    return _result(
        False,
        status="needs_login",
        mode="home_assistant",
        stub=False,
        error="Home Assistant no configurado. No se envió ningún comando.",
        onboarding=ONBOARD,
    )


def register_iot(tools: ToolRegistry, settings: Settings) -> None:
    ha = HomeAssistant(settings)

    async def check_connection(_args: dict[str, Any]) -> str:
        if not all(ha.configured()):
            return _missing()
        try:
            ping = await ha.ping()
            return _result(bool(ping.get("ok")), **{k: v for k, v in ping.items() if k != "ok"})
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            return _result(False, status="error", mode="home_assistant", error=str(exc))

    async def get_state(args: dict[str, Any]) -> str:
        entity = str(args.get("entity_id") or "")
        if not entity:
            return _result(False, error="entity_id required")
        if not all(ha.configured()):
            return _missing()
        try:
            state = await ha.get_state(entity)
            return _result(True, mode="home_assistant", **state, **_critical_metadata(entity))
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            return _result(
                False,
                status="error",
                mode="home_assistant",
                error=str(exc),
                **_critical_metadata(entity),
            )

    async def list_devices(args: dict[str, Any]) -> str:
        if not all(ha.configured()):
            return _missing()
        try:
            devices = await ha.states(str(args.get("domain") or ""))
            return _result(
                True,
                mode="home_assistant",
                devices=devices,
                count=len(devices),
            )
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            return _result(False, status="error", mode="home_assistant", error=str(exc))

    async def set_device(args: dict[str, Any]) -> str:
        entity = resolve_entity_id(str(args.get("entity_id") or args.get("device") or ""))
        action = str(args.get("action") or "toggle")
        if not entity or "." not in entity:
            return _result(False, error="entity_id válido requerido")
        if not all(ha.configured()):
            return _missing()
        domain = entity.partition(".")[0]
        if domain not in CONTROLLABLE:
            return _result(
                False,
                error=f"{entity} es de solo lectura",
                **_critical_metadata(entity),
            )
        try:
            result = await ha.call(entity, action, service_data(args))
            result.update(_critical_metadata(entity))
            ok = bool(result.pop("ok", False))
            return _result(ok, **result)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            return _result(
                False,
                status="error",
                mode="home_assistant",
                error=str(exc),
                **_critical_metadata(entity),
            )

    async def scene(args: dict[str, Any]) -> str:
        name = str(args.get("name") or "")
        if not name:
            return _result(False, error="name required")
        entity = name if name.startswith("scene.") else f"scene.{name}"
        return await set_device({"entity_id": entity, "action": "turn_on"})

    async def call_service(args: dict[str, Any]) -> str:
        entity = str(args.get("entity_id") or "")
        service = str(args.get("service") or args.get("action") or "")
        if not entity or not service:
            return _result(False, error="entity_id y service son obligatorios")
        return await set_device({**args, "action": service, "entity_id": entity})

    tools.register(
        ToolSpec(
            "home.check_connection",
            "Comprueba autenticación real contra la API de Home Assistant.",
            {"type": "object", "properties": {}},
            check_connection,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "home.get_state",
            "Lee el estado actual de una entidad en Home Assistant.",
            {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            get_state,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "home.list_devices",
            "Lista entidades reales de Home Assistant, opcionalmente por dominio.",
            {
                "type": "object",
                "properties": {"domain": {"type": "string"}},
            },
            list_devices,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "home.set_device",
            "Controla una entidad real, verifica el estado posterior y marca cerraduras/alarmas.",
            {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "action": {"type": "string"},
                    "brightness": {"type": "integer"},
                    "brightness_pct": {"type": "number"},
                    "temperature": {"type": "number"},
                    "hvac_mode": {"type": "string"},
                    "position": {"type": "integer"},
                    "data": {"type": "object"},
                },
                "required": ["entity_id", "action"],
            },
            set_device,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "home.scene",
            "Activa una escena real de Home Assistant y verifica la llamada.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            scene,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "home.call_service",
            "Llama un servicio de Home Assistant (brillo, clima, medios) y verifica el resultado.",
            {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "service": {"type": "string"},
                    "action": {"type": "string"},
                    "data": {"type": "object"},
                },
                "required": ["entity_id"],
            },
            call_service,
            "L1",
        )
    )
