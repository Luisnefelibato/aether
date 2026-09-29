"""Google OAuth for ADAM.

Luisfer completes login/2FA in the browser. This script never types passwords.
Expected account: luisfgomezosp@gmail.com
"""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(ROOT / ".env")

EXPECTED_EMAIL = "luisfgomezosp@gmail.com"
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8766/")
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]
CLIENT_JSON = ROOT / "data" / "google_oauth_client.json"
TOKEN_JSON = ROOT / "data" / "google_tokens.json"
DOWNLOADS = Path.home() / "Downloads"
CONSOLE_URLS = [
    "https://console.cloud.google.com/projectcreate?hl=es",
    "https://console.cloud.google.com/apis/library/gmail.googleapis.com?hl=es",
    "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com?hl=es",
    "https://console.cloud.google.com/auth/clients/create?hl=es",
]


def _client_from_mapping(data: dict) -> dict | None:
    block = data.get("installed") or data.get("web") or data
    client_id = str(block.get("client_id") or "").strip()
    client_secret = str(block.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI, "http://127.0.0.1:8766/", "http://localhost"],
        }
    }


def find_client_secrets() -> dict | None:
    env_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    env_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if env_id and env_secret:
        return _client_from_mapping(
            {"client_id": env_id, "client_secret": env_secret}
        )
    search = [
        CLIENT_JSON,
        *sorted(ROOT.glob("client_secret*.json"), reverse=True),
        *sorted(DOWNLOADS.glob("client_secret*.json"), reverse=True),
    ]
    for path in search:
        if not path.exists():
            continue
        try:
            parsed = _client_from_mapping(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if parsed:
            CLIENT_JSON.parent.mkdir(parents=True, exist_ok=True)
            CLIENT_JSON.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            return parsed
    return None


def wait_for_client(timeout_s: int = 480) -> dict:
    found = find_client_secrets()
    if found:
        print("Credenciales OAuth encontradas.", flush=True)
        return found
    print("Faltan credenciales OAuth de escritorio de Google Cloud.", flush=True)
    print(
        f"Entra con {EXPECTED_EMAIL} si Google lo pide (contrasena y 2FA las pones tu).",
        flush=True,
    )
    print("1) Crea o elige un proyecto (nombre ADAM).", flush=True)
    print("2) Activa Gmail API y Google Calendar API.", flush=True)
    print(
        "3) Pantalla de consentimiento: tipo Externo, usuario de prueba = tu Gmail.",
        flush=True,
    )
    print(
        "4) Crear cliente OAuth -> Aplicacion de escritorio -> Descargar JSON.",
        flush=True,
    )
    print(f"Deja el JSON en Descargas o en {CLIENT_JSON}", flush=True)
    for url in CONSOLE_URLS:
        webbrowser.open(url)
        time.sleep(1.2)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found = find_client_secrets()
        if found:
            print("Credenciales OAuth encontradas.", flush=True)
            return found
        time.sleep(3)
    raise SystemExit(
        "No llego el JSON del cliente OAuth. Descargalo y vuelve a correr "
        "py -3 scripts/google_oauth.py"
    )


def run_consent(client: dict) -> dict:
    from google_auth_oauthlib.flow import InstalledAppFlow

    CLIENT_JSON.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_JSON.write_text(json.dumps(client, indent=2), encoding="utf-8")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_JSON), scopes=SCOPES)
    print(f"Abriendo consentimiento de Google para {EXPECTED_EMAIL}...", flush=True)
    print("ADAM no escribe la contrasena ni el 2FA.", flush=True)
    print("En el navegador: entra con ese Gmail, completa 2FA y pulsa Permitir.", flush=True)
    creds = flow.run_local_server(
        host="localhost",
        bind_addr="127.0.0.1",
        port=8080,
        open_browser=True,
        redirect_uri_trailing_slash=True,
        timeout_seconds=600,
        authorization_prompt_message="Si no se abre: {url}",
        success_message="ADAM ya recibio el permiso. Puedes cerrar esta pestana.",
        access_type="offline",
        include_granted_scopes="true",
        login_hint=EXPECTED_EMAIL,
        prompt="consent",
    )
    return creds


def verify_email(access_token: str) -> str:
    response = httpx.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20.0,
    )
    response.raise_for_status()
    email = str(response.json().get("email") or "").strip().lower()
    if email != EXPECTED_EMAIL.lower():
        raise SystemExit(
            f"El consentimiento fue con {email or '(sin email)'}, no {EXPECTED_EMAIL}."
        )
    return email


def main() -> None:
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    client = wait_for_client()
    creds = run_consent(client)
    email = verify_email(creds.token)
    installed = client["installed"]
    payload = {
        "access_token": creds.token,
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": installed["client_id"],
        "client_secret": installed["client_secret"],
        "scopes": list(creds.scopes or SCOPES),
        "expires_at": creds.expiry.timestamp() if creds.expiry else time.time() + 3500,
        "email": email,
    }
    TOKEN_JSON.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Google OAuth OK para {email}. Tokens en data/google_tokens.json")


if __name__ == "__main__":
    main()
