"""Spotify OAuth PKCE for ADAM.

Luisfer completes login in the browser. This script never types passwords.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from base64 import urlsafe_b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "core"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(ROOT / ".env")

REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/")
SCOPES = "user-read-email user-read-playback-state user-modify-playback-state user-read-currently-playing"
TOKEN_JSON = ROOT / "data" / "spotify_tokens.json"
AUTHORIZE = "https://accounts.spotify.com/authorize"
TOKEN_URI = "https://accounts.spotify.com/api/token"


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode()


class _Callback(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if query.get("code"):
            self.server.auth_code = query["code"][0]  # type: ignore[attr-defined]
            body = b"ADAM ya recibio Spotify. Puedes cerrar esta pestana."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if query.get("error"):
            self.server.auth_error = query["error"][0]  # type: ignore[attr-defined]
            body = f"Spotify rechazo el permiso: {query['error'][0]}".encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def wait_for_code(auth_url: str, timeout_s: int = 600) -> str:
    host, port = "127.0.0.1", 8080
    parsed = urlparse(REDIRECT_URI)
    if parsed.hostname:
        host = parsed.hostname
    if parsed.port:
        port = parsed.port
    server = HTTPServer((host, port), _Callback)
    server.auth_code = ""  # type: ignore[attr-defined]
    server.auth_error = ""  # type: ignore[attr-defined]
    server.timeout = 2
    print("Servidor de callback listo. Abriendo Spotify...", flush=True)
    webbrowser.open(auth_url, new=1, autoraise=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        server.handle_request()
        error = getattr(server, "auth_error", "")
        if error:
            server.server_close()
            raise SystemExit(f"Spotify OAuth error: {error}")
        code = getattr(server, "auth_code", "")
        if code:
            server.server_close()
            return str(code)
    server.server_close()
    raise SystemExit("No llego el permiso de Spotify. Abre la URL, entra y pulsa Aceptar.")


def main() -> None:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        raise SystemExit("Falta SPOTIFY_CLIENT_ID en .env")
    verifier = _b64(secrets.token_bytes(64))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    url = f"{AUTHORIZE}?{query}"
    print("Abriendo Spotify. Entra tu y pulsa Aceptar. ADAM no escribe la contrasena.", flush=True)
    print(f"Redirect URI que debe estar en el Dashboard: {REDIRECT_URI}", flush=True)
    print(f"Si no se abre: {url}", flush=True)
    code = wait_for_code(url)
    response = httpx.post(
        TOKEN_URI,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise SystemExit(f"Spotify token HTTP {response.status_code}: {response.text[:300]}")
    body = response.json()
    access = str(body.get("access_token") or "").strip()
    if not access:
        raise SystemExit("Spotify no devolvio access_token")
    me = httpx.get(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {access}"},
        timeout=20.0,
    )
    me.raise_for_status()
    profile = me.json()
    payload = {
        "access_token": access,
        "refresh_token": body.get("refresh_token") or "",
        "token_type": body.get("token_type") or "Bearer",
        "scope": body.get("scope") or SCOPES,
        "expires_at": time.time() + int(body.get("expires_in") or 3600),
        "client_id": client_id,
        "display_name": profile.get("display_name") or "",
        "id": profile.get("id") or "",
    }
    TOKEN_JSON.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    name = payload["display_name"] or payload["id"] or "usuario"
    print(f"Spotify OAuth OK para {name}. Tokens en data/spotify_tokens.json", flush=True)


if __name__ == "__main__":
    main()
