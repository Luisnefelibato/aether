from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from aether_core.config import Settings

TOKEN_URI = "https://oauth2.googleapis.com/token"


def token_file(settings: Settings) -> Path:
    return settings.resolve_path(settings.google.token_path)


def load_token_payload(settings: Settings) -> dict[str, Any]:
    path = token_file(settings)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def access_token_from_payload(payload: dict[str, Any]) -> str:
    return str(payload.get("access_token") or payload.get("token") or "").strip()


def load_google_access_token(settings: Settings) -> str:
    token = os.getenv("GOOGLE_ACCESS_TOKEN", "").strip()
    if token:
        return token
    payload = load_token_payload(settings)
    token = access_token_from_payload(payload)
    if not token:
        return ""
    expiry = float(payload.get("expires_at") or 0)
    refresh = str(payload.get("refresh_token") or "").strip()
    client_id = (
        os.getenv("GOOGLE_CLIENT_ID", "").strip()
        or str(payload.get("client_id") or "").strip()
        or settings.google.client_id
    )
    client_secret = (
        os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        or str(payload.get("client_secret") or "").strip()
        or settings.google.client_secret
    )
    if refresh and client_id and expiry and expiry < time.time() + 60:
        refreshed = refresh_access_token(client_id, client_secret, refresh)
        if refreshed:
            payload.update(refreshed)
            save_token_payload(settings, payload)
            return access_token_from_payload(payload)
    return token


def save_token_payload(settings: Settings, payload: dict[str, Any]) -> None:
    path = token_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any] | None:
    try:
        response = httpx.post(
            TOKEN_URI,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
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
    return {
        "access_token": token,
        "token": token,
        "expires_at": time.time() + expires_in,
        "token_type": body.get("token_type") or "Bearer",
        "scope": body.get("scope") or "",
    }
