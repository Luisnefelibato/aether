from __future__ import annotations

import hashlib
import json
import os
import secrets
from base64 import urlsafe_b64encode
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from aether_core.config import Settings
from aether_core.google_creds import load_google_access_token
from aether_core.spotify_creds import load_spotify_access_token
from aether_core.tools import ToolRegistry, ToolSpec

IntegrationState = Literal[
    "connected", "needs_login", "needs_oauth", "stub", "error"
]
VALID_STATES = {"connected", "needs_login", "needs_oauth", "stub", "error"}


@dataclass(frozen=True)
class IntegrationStatus:
    service: str
    status: IntegrationState
    mode: str
    detail: str
    onboarding: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["connected"] = self.status == "connected"
        return data


class GoogleAdapter:
    """Reports Google readiness without entering credentials or handling 2FA."""

    SCOPES = (
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    )

    def __init__(
        self,
        settings: Settings,
        *,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.environ = os.environ if environ is None else environ

    def status(self) -> IntegrationStatus:
        token = self.environ.get("GOOGLE_ACCESS_TOKEN", "").strip()
        if not token:
            token = load_google_access_token(self.settings)
        if token:
            return IntegrationStatus(
                "google",
                "connected",
                "oauth_api",
                "Token OAuth disponible para Calendar/Gmail API.",
            )

        client_id = self.environ.get("GOOGLE_CLIENT_ID", "").strip()
        redirect_uri = self.environ.get("GOOGLE_REDIRECT_URI", "").strip()
        if client_id and redirect_uri:
            query = urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(self.SCOPES),
                    "access_type": "offline",
                    "prompt": "consent",
                    "login_hint": "luisfgomezosp@gmail.com",
                }
            )
            return IntegrationStatus(
                "google",
                "needs_oauth",
                "oauth",
                "Abre la URL y completa login/2FA manualmente; ADAM no captura credenciales.",
                {
                    "authorization_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}",
                    "token_url": "https://oauth2.googleapis.com/token",
                    "scopes": list(self.SCOPES),
                    "manual_login_required": True,
                    "automates_credentials_or_2fa": False,
                },
            )

        profile = self.settings.resolve_path(self.settings.browser.user_data_dir)
        session_present = profile.exists() and any(profile.iterdir())
        return IntegrationStatus(
            "google",
            "connected" if session_present else "needs_login",
            "browser_session",
            (
                "Existe un perfil persistente; el estado exacto de login se valida al navegar."
                if session_present
                else "Inicia sesión manualmente en el perfil persistente del navegador."
            ),
            {
                "profile": str(profile),
                "session_detected": session_present,
                "manual_login_required": not session_present,
                "automates_credentials_or_2fa": False,
            },
        )


class SpotifyAdapter:
    API = "https://api.spotify.com/v1"

    def __init__(
        self,
        access_token: str = "",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token.strip()
        self.client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if self.client is not None:
            response = await self.client.request(
                method, f"{self.API}{path}", headers=headers, **kwargs
            )
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(
                    method, f"{self.API}{path}", headers=headers, **kwargs
                )
        response.raise_for_status()
        return response

    async def status(self) -> IntegrationStatus:
        if not self.access_token:
            client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
            redirect = os.getenv(
                "SPOTIFY_REDIRECT_URI",
                "http://127.0.0.1:8080/",
            )
            onboarding: dict[str, Any] = {
                "fallback_tool": "desktop.spotify_play",
                "required_scopes": [
                    "user-read-playback-state",
                    "user-modify-playback-state",
                ],
                "manual_login_required": True,
                "automates_credentials_or_2fa": False,
            }
            if client_id:
                verifier = secrets.token_urlsafe(64)
                challenge = (
                    urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .rstrip(b"=")
                    .decode()
                )
                pkce_path = Path("data/spotify_pkce.json")
                pkce_path.parent.mkdir(parents=True, exist_ok=True)
                pkce_path.write_text(
                    json.dumps({"verifier": verifier}), encoding="utf-8"
                )
                query = urlencode(
                    {
                        "client_id": client_id,
                        "response_type": "code",
                        "redirect_uri": redirect,
                        "scope": "user-read-playback-state user-modify-playback-state",
                        "code_challenge_method": "S256",
                        "code_challenge": challenge,
                    }
                )
                onboarding["authorization_url"] = (
                    f"https://accounts.spotify.com/authorize?{query}"
                )
            return IntegrationStatus(
                "spotify",
                "needs_oauth",
                "optional_api",
                "Control opcional: configura SPOTIFY_ACCESS_TOKEN; la app de escritorio sigue disponible como fallback.",
                onboarding,
            )
        try:
            response = await self._request("GET", "/me")
            user = response.json()
            return IntegrationStatus(
                "spotify",
                "connected",
                "web_api",
                f"Spotify conectado como {user.get('display_name') or user.get('id') or 'usuario'}.",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return IntegrationStatus(
                "spotify", "error", "web_api", f"Spotify API no respondió: {exc}"
            )

    async def control(self, action: str, device_id: str = "") -> dict[str, Any]:
        if not self.access_token:
            return {
                "ok": False,
                "verified": False,
                "status": "needs_oauth",
                "error": "Spotify Web API no configurada.",
                "fallback": {"tool": "desktop.spotify_play", "supports": "search/play"},
            }
        endpoint = {
            "play": ("PUT", "/me/player/play"),
            "pause": ("PUT", "/me/player/pause"),
            "next": ("POST", "/me/player/next"),
            "previous": ("POST", "/me/player/previous"),
        }.get(action)
        if endpoint is None:
            return {"ok": False, "verified": False, "error": "acción no soportada"}
        params = {"device_id": device_id} if device_id else None
        try:
            await self._request(endpoint[0], endpoint[1], params=params)
            playback = await self._request("GET", "/me/player")
            body = playback.json() if playback.content else {}
            expected = action != "pause"
            verified = (
                bool(body.get("is_playing")) == expected
                if action in {"play", "pause"}
                else playback.status_code == 200
            )
            return {
                "ok": verified,
                "verified": verified,
                "status": "connected",
                "action": action,
                "is_playing": body.get("is_playing"),
                "error": None if verified else "No se pudo verificar el control.",
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "ok": False,
                "verified": False,
                "status": "error",
                "error": str(exc),
            }

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not self.access_token:
            return {
                "ok": False,
                "verified": False,
                "status": "needs_oauth",
                "error": "Spotify Web API no configurada.",
                "fallback": {"tool": "desktop.spotify_play"},
            }
        try:
            response = await self._request(
                "GET",
                "/search",
                params={"q": query, "type": "track", "limit": max(1, min(limit, 10))},
            )
            items = (response.json().get("tracks") or {}).get("items") or []
            tracks = [
                {
                    "name": item.get("name"),
                    "uri": item.get("uri"),
                    "artists": ", ".join(
                        artist.get("name", "") for artist in item.get("artists") or []
                    ),
                }
                for item in items
            ]
            return {"ok": True, "verified": True, "tracks": tracks}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "verified": False, "error": str(exc)}

    async def queue(self, uri: str, device_id: str = "") -> dict[str, Any]:
        if not self.access_token:
            return {
                "ok": False,
                "verified": False,
                "status": "needs_oauth",
                "error": "Spotify Web API no configurada.",
            }
        params: dict[str, str] = {"uri": uri}
        if device_id:
            params["device_id"] = device_id
        try:
            await self._request("POST", "/me/player/queue", params=params)
            return {"ok": True, "verified": True, "queued": uri}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "verified": False, "error": str(exc)}


ONBOARDING = {
    "google": {
        "service": "google",
        "steps": [
            "Crea credenciales OAuth de escritorio en Google Cloud (Gmail + Calendar).",
            "Pon GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET y GOOGLE_REDIRECT_URI en aether/.env.",
            "Abre la URL de integration.status y completa login/2FA tú mismo.",
            "ADAM no escribe contraseñas ni códigos OTP.",
        ],
        "env": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GOOGLE_ACCESS_TOKEN"],
        "automates_credentials_or_2fa": False,
    },
    "spotify": {
        "service": "spotify",
        "steps": [
            "Crea una app en Spotify Developer Dashboard.",
            "Pon SPOTIFY_CLIENT_ID y SPOTIFY_ACCESS_TOKEN en aether/.env, o usa desktop.spotify_play.",
            "ADAM no automatiza el login de Spotify.",
        ],
        "env": ["SPOTIFY_CLIENT_ID", "SPOTIFY_ACCESS_TOKEN"],
        "fallback_tool": "desktop.spotify_play",
        "automates_credentials_or_2fa": False,
    },
    "telegram": {
        "service": "telegram",
        "steps": [
            "Habla con BotFather, crea un bot y copia el token.",
            "Pon TELEGRAM_BOT_TOKEN en aether/.env y reinicia ADAM.",
            "Los envíos siguen pidiendo confirmación oral.",
        ],
        "env": ["TELEGRAM_BOT_TOKEN"],
        "automates_credentials_or_2fa": False,
    },
    "whatsapp": {
        "service": "whatsapp",
        "steps": [
            "WhatsApp personal: usa desktop.open_whatsapp; no hay API de envío para cuentas personales.",
            "WhatsApp Business Cloud (opcional): WHATSAPP_CLOUD_TOKEN y WHATSAPP_PHONE_NUMBER_ID.",
            "ADAM no evade restricciones de WhatsApp personal.",
        ],
        "env": ["WHATSAPP_CLOUD_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"],
        "fallback_tool": "desktop.open_whatsapp",
        "automates_credentials_or_2fa": False,
    },
    "home": {
        "service": "home",
        "steps": [
            "En Home Assistant crea un Long-Lived Access Token.",
            "Pon HOME_ASSISTANT_URL y HOME_ASSISTANT_TOKEN en aether/.env.",
            "Cerraduras y alarmas siempre piden confirmación. Sin token no hay simulación.",
        ],
        "env": ["HOME_ASSISTANT_URL", "HOME_ASSISTANT_TOKEN"],
        "automates_credentials_or_2fa": False,
    },
}


async def _home_status(settings: Settings) -> IntegrationStatus:
    from aether_caps_iot.ha import HomeAssistant, credentials

    url, token = credentials(settings)
    if not url or not token:
        return IntegrationStatus(
            "home",
            "needs_login",
            "home_assistant",
            "Home Assistant no configurado. Sin URL/token no se envía ningún comando.",
            ONBOARDING["home"],
        )
    try:
        ping = await HomeAssistant(settings).ping()
        if ping.get("ok"):
            return IntegrationStatus(
                "home",
                "connected",
                "home_assistant",
                "Home Assistant API respondió; el control es real.",
                ONBOARDING["home"],
            )
        return IntegrationStatus(
            "home",
            "error",
            "home_assistant",
            str(ping.get("message") or "API no confirmó que está en marcha."),
            ONBOARDING["home"],
        )
    except Exception as exc:  # noqa: BLE001
        return IntegrationStatus(
            "home",
            "error",
            "home_assistant",
            f"Token presente pero la API no respondió: {exc}",
            ONBOARDING["home"],
        )


def _token_status(
    service: str, token: str, mode: str, connected_msg: str, missing_msg: str
) -> IntegrationStatus:
    present = bool(token.strip())
    return IntegrationStatus(
        service,
        "connected" if present else "needs_login",
        mode,
        connected_msg if present else missing_msg,
        ONBOARDING.get(service),
    )


def register_integrations(tools: ToolRegistry, settings: Settings) -> None:
    google = GoogleAdapter(settings)
    spotify = SpotifyAdapter(load_spotify_access_token(settings))

    async def integration_status(args: dict[str, Any]) -> str:
        service = str(args.get("service") or "all").lower()
        statuses: list[IntegrationStatus] = []
        if service in {"all", "google"}:
            statuses.append(google.status())
        if service in {"all", "spotify"}:
            statuses.append(await spotify.status())
        if service in {"all", "home", "home_assistant"}:
            statuses.append(await _home_status(settings))
        if service in {"all", "telegram"}:
            statuses.append(
                _token_status(
                    "telegram",
                    os.getenv("TELEGRAM_BOT_TOKEN", "")
                    or settings.messaging.telegram_bot_token,
                    "bot_http",
                    "Telegram Bot token configurado.",
                    "Crea un bot con BotFather y configura TELEGRAM_BOT_TOKEN.",
                )
            )
        if service in {"all", "whatsapp"}:
            token = (
                os.getenv("WHATSAPP_CLOUD_TOKEN", "")
                or settings.messaging.whatsapp_cloud_token
            )
            phone = (
                os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
                or settings.messaging.whatsapp_phone_number_id
            )
            statuses.append(
                _token_status(
                    "whatsapp",
                    f"{token}:{phone}" if token and phone else "",
                    "cloud_business_or_desktop",
                    "WhatsApp Cloud Business configurado.",
                    "Personal: desktop.open_whatsapp. Business: WHATSAPP_CLOUD_TOKEN.",
                )
            )
        if not statuses:
            statuses.append(
                IntegrationStatus(service, "stub", "unsupported", "Servicio no registrado.")
            )
        return json.dumps(
            {
                "ok": True,
                "verified": True,
                "statuses": [item.to_dict() for item in statuses],
                "allowed_states": sorted(VALID_STATES),
            },
            ensure_ascii=False,
        )

    async def integration_onboard(args: dict[str, Any]) -> str:
        service = str(args.get("service") or "").lower()
        guide = ONBOARDING.get(service)
        if guide is None:
            return json.dumps(
                {
                    "ok": False,
                    "verified": False,
                    "error": "Servicio desconocido",
                    "services": sorted(ONBOARDING),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "verified": True,
                "message": f"Onboarding de {service} listo. Completa login tú mismo.",
                "onboarding": guide,
            },
            ensure_ascii=False,
        )

    async def spotify_control(args: dict[str, Any]) -> str:
        result = await spotify.control(
            str(args.get("action") or ""), str(args.get("device_id") or "")
        )
        return json.dumps(result, ensure_ascii=False)

    tools.register(
        ToolSpec(
            "integration.status",
            "Muestra estado y onboarding seguro de Google, Spotify, Telegram, WhatsApp y Home Assistant.",
            {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "enum": [
                            "all",
                            "google",
                            "spotify",
                            "telegram",
                            "whatsapp",
                            "home",
                        ],
                    }
                },
            },
            integration_status,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "integration.onboard",
            "Devuelve pasos de onboarding sin automatizar credenciales ni 2FA.",
            {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "enum": ["google", "spotify", "telegram", "whatsapp", "home"],
                    }
                },
                "required": ["service"],
            },
            integration_onboard,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "spotify.control",
            "Controla reproducción por Spotify Web API y verifica el estado.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "next", "previous"],
                    },
                    "device_id": {"type": "string"},
                },
                "required": ["action"],
            },
            spotify_control,
            "L1",
        )
    )

    async def spotify_search(args: dict[str, Any]) -> str:
        result = await spotify.search(
            str(args.get("query") or ""), int(args.get("limit") or 5)
        )
        return json.dumps(result, ensure_ascii=False)

    async def spotify_queue(args: dict[str, Any]) -> str:
        result = await spotify.queue(
            str(args.get("uri") or ""), str(args.get("device_id") or "")
        )
        return json.dumps(result, ensure_ascii=False)

    tools.register(
        ToolSpec(
            "spotify.search",
            "Busca pistas en Spotify Web API; si no hay OAuth, indica el fallback de escritorio.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            spotify_search,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "spotify.queue",
            "Añade una pista a la cola de Spotify Web API.",
            {
                "type": "object",
                "properties": {
                    "uri": {"type": "string"},
                    "device_id": {"type": "string"},
                },
                "required": ["uri"],
            },
            spotify_queue,
            "L1",
        )
    )
