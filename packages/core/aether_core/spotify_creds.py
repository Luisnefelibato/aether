from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from aether_core.config import Settings

TOKEN_URI = "https://accounts.spotify.com/api/token"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def token_file(settings: Settings) -> Path:
    return settings.resolve_path(settings.spotify.token_path)


def _looks_like_access_token(value: str) -> bool:
    token = value.strip()
    if len(token) < 80:
        return False
    if _HEX32.match(token):
        return False
    return True


def load_token_payload(settings: Settings) -> dict[str, Any]:
    path = token_file(settings)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_token_payload(settings: Settings, payload: dict[str, Any]) -> None:
    path = token_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_spotify_access_token(settings: Settings) -> str:
    payload = load_token_payload(settings)
    token = str(payload.get("access_token") or "").strip()
    refresh = str(payload.get("refresh_token") or "").strip()
    client_id = (
        os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        or str(payload.get("client_id") or "").strip()
        or settings.spotify.client_id
    )
    expiry = float(payload.get("expires_at") or 0)
    if token and refresh and client_id and expiry and expiry < time.time() + 60:
        refreshed = refresh_access_token(client_id, refresh)
        if refreshed:
            payload.update(refreshed)
            save_token_payload(settings, payload)
            return str(payload.get("access_token") or "").strip()
    if token:
        return token
    env_token = os.getenv("SPOTIFY_ACCESS_TOKEN", "").strip()
    if _looks_like_access_token(env_token):
        return env_token
    return ""


def refresh_access_token(client_id: str, refresh_token: str) -> dict[str, Any] | None:
    try:
        response = httpx.post(
            TOKEN_URI,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=20.0,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    token = str(body.get("access_token") or "").strip()
    if not token:
        return None
    expires_in = int(body.get("expires_in") or 3600)
    updated = {
        "access_token": token,
        "expires_at": time.time() + expires_in,
        "token_type": body.get("token_type") or "Bearer",
        "scope": body.get("scope") or "",
    }
    if body.get("refresh_token"):
        updated["refresh_token"] = body["refresh_token"]
    return updated
