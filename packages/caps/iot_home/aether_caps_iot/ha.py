from __future__ import annotations

import os
from typing import Any

import httpx

from aether_core.config import Settings

CONTROLLABLE = {
    "light",
    "switch",
    "fan",
    "lock",
    "cover",
    "climate",
    "media_player",
    "scene",
    "script",
    "automation",
    "input_boolean",
    "input_button",
    "button",
    "siren",
    "alarm_control_panel",
    "vacuum",
    "water_heater",
    "humidifier",
    "remote",
}

READ_DOMAINS = CONTROLLABLE | {
    "sensor",
    "binary_sensor",
    "device_tracker",
    "person",
    "sun",
    "weather",
}

ACTION_ALIASES = {
    "on": "turn_on",
    "off": "turn_off",
    "toggle": "toggle",
    "open": "open_cover",
    "close": "close_cover",
    "lock": "lock",
    "unlock": "unlock",
    "play": "media_play",
    "pause": "media_pause",
    "stop": "media_stop",
}


def credentials(settings: Settings) -> tuple[str, str]:
    url = (
        settings.iot.home_assistant_url
        or os.getenv("HOME_ASSISTANT_URL", "")
    ).strip().rstrip("/")
    token = (
        settings.iot.home_assistant_token
        or os.getenv("HOME_ASSISTANT_TOKEN", "")
    ).strip()
    return url, token


def resolve_entity_id(entity_id: str) -> str:
    raw = str(entity_id or "").strip()
    aliases = {
        "oficina": "input_boolean.luz_oficina",
        "luz oficina": "input_boolean.luz_oficina",
        "luz_oficina": "input_boolean.luz_oficina",
        "light.oficina": "input_boolean.luz_oficina",
        "light.luz_oficina": "input_boolean.luz_oficina",
        "sala": "input_boolean.luz_sala",
        "luz sala": "input_boolean.luz_sala",
        "luz_sala": "input_boolean.luz_sala",
        "light.sala": "input_boolean.luz_sala",
        "cuarto": "input_boolean.luz_cuarto",
        "habitacion": "input_boolean.luz_cuarto",
        "habitación": "input_boolean.luz_cuarto",
        "luz cuarto": "input_boolean.luz_cuarto",
        "luz_cuarto": "input_boolean.luz_cuarto",
        "light.cuarto": "input_boolean.luz_cuarto",
    }
    key = " ".join(raw.replace("_", " ").casefold().split())
    if key in aliases:
        return aliases[key]
    if raw.casefold() in aliases:
        return aliases[raw.casefold()]
    return raw


def resolve_service(domain: str, action: str) -> tuple[str, str | None]:
    raw = str(action or "toggle").strip()
    service = ACTION_ALIASES.get(raw.casefold(), raw)
    expected: str | None = None
    if domain == "lock":
        if service in {"turn_on", "on", "lock"}:
            service = "lock"
            expected = "locked"
        elif service in {"turn_off", "off", "unlock"}:
            service = "unlock"
            expected = "unlocked"
    elif domain == "cover":
        if service in {"turn_on", "on", "open", "open_cover"}:
            service = "open_cover"
            expected = "open"
        elif service in {"turn_off", "off", "close", "close_cover"}:
            service = "close_cover"
            expected = "closed"
    elif domain in {"scene", "script"}:
        service = "turn_on"
        expected = None
    elif domain == "alarm_control_panel":
        expected = None
    elif service == "turn_on":
        expected = "on"
    elif service == "turn_off":
        expected = "off"
    return service, expected


def service_data(args: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    extra = args.get("data")
    if isinstance(extra, dict):
        data.update(extra)
    for key in (
        "brightness",
        "brightness_pct",
        "color_temp",
        "hs_color",
        "rgb_color",
        "temperature",
        "hvac_mode",
        "fan_mode",
        "position",
        "tilt_position",
        "volume_level",
        "source",
    ):
        if args.get(key) is not None:
            data[key] = args[key]
    return data


class HomeAssistant:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client

    def configured(self) -> tuple[str, str]:
        return credentials(self.settings)

    async def request(
        self, method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        url, token = self.configured()
        if not url or not token:
            raise RuntimeError("Home Assistant URL/token no configurados")
        headers = {"Authorization": f"Bearer {token}"}
        if self.client is not None:
            response = await self.client.request(
                method, f"{url}{path}", headers=headers, json=payload
            )
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method, f"{url}{path}", headers=headers, json=payload
                )
        response.raise_for_status()
        return response

    async def ping(self) -> dict[str, Any]:
        body = (await self.request("GET", "/api/")).json()
        running = body.get("message") == "API running."
        return {
            "ok": running,
            "verified": running,
            "status": "connected" if running else "error",
            "mode": "home_assistant",
            "message": body.get("message"),
        }

    async def states(self, domain: str = "") -> list[dict[str, Any]]:
        payload = (await self.request("GET", "/api/states")).json()
        wanted = {domain} if domain else READ_DOMAINS
        devices: list[dict[str, Any]] = []
        for state in payload:
            entity_id = str(state.get("entity_id") or "")
            entity_domain = entity_id.partition(".")[0]
            include = entity_domain in wanted
            if domain == "light" and entity_id.startswith("input_boolean.luz_"):
                include = True
            if not include:
                continue
            devices.append(
                {
                    "entity_id": entity_id,
                    "state": state.get("state"),
                    "name": (state.get("attributes") or {}).get("friendly_name"),
                    "controllable": entity_domain in CONTROLLABLE,
                }
            )
        return devices

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        entity_id = resolve_entity_id(entity_id)
        body = (await self.request("GET", f"/api/states/{entity_id}")).json()
        return {
            "entity_id": entity_id,
            "state": body.get("state"),
            "attributes": body.get("attributes") or {},
        }

    async def call(
        self,
        entity_id: str,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entity_id = resolve_entity_id(entity_id)
        domain = entity_id.partition(".")[0]
        if domain not in CONTROLLABLE:
            return {
                "ok": False,
                "verified": False,
                "error": f"{entity_id} no es controlable ({domain})",
            }
        service, expected = resolve_service(domain, action)
        payload = {"entity_id": entity_id, **(extra or {})}
        before = None
        try:
            before = await self.get_state(entity_id)
        except httpx.HTTPError:
            before = None
        await self.request(
            "POST",
            f"/api/services/{domain}/{service}",
            payload=payload,
        )
        after = await self.get_state(entity_id)
        current = after.get("state")
        if domain in {"scene", "script"}:
            verified = True
        elif service == "toggle" and before is not None:
            verified = current != before.get("state")
        elif expected is not None:
            verified = current == expected
        else:
            verified = current not in {None, "unavailable", "unknown"}
        return {
            "ok": verified,
            "verified": verified,
            "mode": "home_assistant",
            "entity_id": entity_id,
            "service": service,
            "state": current,
            "before": None if before is None else before.get("state"),
            "attributes": after.get("attributes") or {},
            "error": None if verified else f"Estado posterior inesperado: {current}",
            "message": f"{entity_id} está {current}" if verified else "",
        }
