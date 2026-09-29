"""Onboard a local Home Assistant Core and write URL+token into aether/.env."""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
CRED_PATH = ROOT / "data" / "homeassistant" / ".adam-credentials.json"
BASE = os.environ.get("HASS_BASE", "http://127.0.0.1:8123").rstrip("/")
CLIENT_ID = f"{BASE}/"


def _request(method: str, path: str, *, data=None, headers=None, form=False, timeout=30):
    body = None
    req_headers = dict(headers or {})
    if data is not None and form:
        body = urllib.parse.urlencode(data).encode()
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE}{path}", data=body, headers=req_headers, method=method
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}


def wait_ready(timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE}/api/onboarding", timeout=5)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2)
    raise RuntimeError(f"Home Assistant no abrió {BASE}: {last}")


def long_lived_token(access_token: str) -> str:
    import asyncio

    import websockets

    async def _run() -> str:
        uri = BASE.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        async with websockets.connect(uri, open_timeout=20) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(hello)
            await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(auth)
            await ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "auth/long_lived_access_token",
                        "client_name": "ADAM",
                        "lifespan": 3650,
                    }
                )
            )
            result = json.loads(await ws.recv())
            if not result.get("success"):
                raise RuntimeError(result)
            return str(result["result"])

    return asyncio.run(_run())


def upsert_env(url: str, token: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    mapping = {"HOME_ASSISTANT_URL": url, "HOME_ASSISTANT_TOKEN": token}
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.strip().startswith("#") else ""
        if key in mapping:
            out.append(f"{key}={mapping[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in mapping.items():
        if key not in seen:
            out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    wait_ready()
    status = _request("GET", "/api/onboarding")
    password = secrets.token_urlsafe(18)
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CRED_PATH.exists():
        password = json.loads(CRED_PATH.read_text(encoding="utf-8"))["password"]
    else:
        CRED_PATH.write_text(
            json.dumps(
                {"username": "luisfer", "password": password, "name": "Luisfer"},
                indent=2,
            ),
            encoding="utf-8",
        )

    done: list[str] = []
    if isinstance(status, list):
        done = [str(item.get("step")) for item in status if item.get("done")]
    elif isinstance(status, dict):
        done = [str(item) for item in (status.get("done") or [])]
    onboarded = "user" in done

    access = None
    if not onboarded:
        created = _request(
            "POST",
            "/api/onboarding/users",
            data={
                "client_id": CLIENT_ID,
                "name": "Luisfer",
                "username": "luisfer",
                "password": password,
                "language": "es",
            },
        )
        code = created["auth_code"]
        tokens = _request(
            "POST",
            "/auth/token",
            data={
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
            },
            form=True,
        )
        access = tokens["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        for path, payload in (
            ("/api/onboarding/core_config", {}),
            ("/api/onboarding/analytics", {}),
            ("/api/onboarding/integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
        ):
            try:
                _request("POST", path, data=payload, headers=headers)
            except urllib.error.HTTPError:
                pass
    if access is None:
        creds = json.loads(CRED_PATH.read_text(encoding="utf-8"))
        # Login flow is heavier; prefer stored token if present.
        stored = creds.get("access_token")
        if not stored:
            raise RuntimeError("Home Assistant ya tiene usuario; borra data/homeassistant y reinicia hass")
        access = stored

    token = long_lived_token(access)
    creds = json.loads(CRED_PATH.read_text(encoding="utf-8"))
    creds["access_token"] = access
    creds["long_lived_token"] = token
    CRED_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    upsert_env(BASE, token)
    ping = _request("GET", "/api/", headers={"Authorization": f"Bearer {token}"})
    print(json.dumps({"ok": ping.get("message") == "API running.", "url": BASE}, indent=2))


if __name__ == "__main__":
    main()
