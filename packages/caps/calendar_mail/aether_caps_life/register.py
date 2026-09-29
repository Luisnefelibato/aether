from __future__ import annotations

import json
import os
import sqlite3
import time
from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from aether_core.config import Settings
from aether_core.google_creds import load_google_access_token
from aether_core.tools import ToolRegistry, ToolSpec


def _result(ok: bool, **payload: Any) -> str:
    return json.dumps(
        {"ok": ok, "verified": ok, **payload},
        ensure_ascii=False,
        default=str,
    )


class GoogleCalendarMailAdapter:
    """Thin Google REST adapter; OAuth consent happens outside this process."""

    def __init__(
        self,
        access_token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token
        self.client = client

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.client is not None:
            response = await self.client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    async def create_event(self, event: dict[str, Any]) -> dict[str, Any]:
        start = float(event["start_ts"])
        end = float(event["end_ts"])
        payload = {
            "summary": event["title"],
            "description": event.get("notes", ""),
            "start": {"dateTime": datetime.fromtimestamp(start, UTC).isoformat()},
            "end": {"dateTime": datetime.fromtimestamp(end, UTC).isoformat()},
        }
        response = await self._request(
            "POST",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers=self.headers,
            json=payload,
        )
        return response.json()

    async def list_events(self, limit: int) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers=self.headers,
            params={"maxResults": limit, "singleEvents": "true", "orderBy": "startTime"},
        )
        return list(response.json().get("items", []))

    async def send_mail(self, draft: dict[str, str]) -> dict[str, Any]:
        message = EmailMessage()
        message["To"] = draft["to"]
        message["Subject"] = draft["subject"]
        message.set_content(draft["body"])
        raw = urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        response = await self._request(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers=self.headers,
            json={"raw": raw},
        )
        return response.json()


def register_life(tools: ToolRegistry, settings: Settings) -> None:
    db_path = settings.resolve_path(settings.memory.sqlite_path)
    provider = str(getattr(settings.calendar_mail, "provider", "local_stub")).lower()
    google_token = load_google_access_token(settings)
    google = GoogleCalendarMailAdapter(google_token) if google_token else None

    def _conn() -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS calendar_events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                start_ts REAL,
                end_ts REAL,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_drafts (
                id TEXT PRIMARY KEY,
                to_addr TEXT,
                subject TEXT,
                body TEXT,
                status TEXT,
                ts REAL
            )
            """
        )
        conn.commit()
        return conn

    async def create_event(args: dict[str, Any]) -> str:
        title = str(args.get("title") or "")
        if not title:
            return _result(False, error="title required")
        start = float(args.get("start_ts") or time.time())
        end = float(args.get("end_ts") or (start + 3600))
        notes = str(args.get("notes") or "")
        if google:
            try:
                event = await google.create_event(
                    {"title": title, "start_ts": start, "end_ts": end, "notes": notes}
                )
                return _result(
                    True,
                    id=event.get("id"),
                    mode="google_api",
                    message=f"Evento '{title}' creado en Google Calendar",
                )
            except (httpx.HTTPError, ValueError) as exc:
                return _result(False, status="error", mode="google_api", error=str(exc))
        eid = str(uuid4())
        with _conn() as conn:
            conn.execute(
                "INSERT INTO calendar_events VALUES (?,?,?,?,?)",
                (eid, title, start, end, notes),
            )
            conn.commit()
        return _result(
            True,
            id=eid,
            mode="local_stub",
            stub=True,
            message=f"Evento local de prueba '{title}' creado; no se sincronizó",
        )

    async def list_events(args: dict[str, Any]) -> str:
        limit = int(args.get("limit") or 10)
        if google:
            try:
                events = await google.list_events(limit)
                return _result(True, events=events, mode="google_api")
            except (httpx.HTTPError, ValueError) as exc:
                return _result(False, status="error", mode="google_api", error=str(exc))
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, title, start_ts, end_ts, notes FROM calendar_events "
                "ORDER BY start_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        events = [
            {
                "id": r[0],
                "title": r[1],
                "start_ts": r[2],
                "end_ts": r[3],
                "notes": r[4],
            }
            for r in rows
        ]
        return _result(True, events=events, mode="local_stub", stub=True)

    async def draft_mail(args: dict[str, Any]) -> str:
        to_addr = str(args.get("to") or "")
        subject = str(args.get("subject") or "")
        body = str(args.get("body") or "")
        mid = str(uuid4())
        with _conn() as conn:
            conn.execute(
                "INSERT INTO mail_drafts VALUES (?,?,?,?,?,?)",
                (mid, to_addr, subject, body, "draft", time.time()),
            )
            conn.commit()
        return _result(
            True,
            id=mid,
            mode="local_draft",
            message=f"Borrador guardado para {to_addr}: {subject}",
        )

    async def list_mail_drafts(args: dict[str, Any]) -> str:
        limit = int(args.get("limit") or 20)
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, to_addr, subject, status, ts FROM mail_drafts "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        drafts = [
            {
                "id": row[0],
                "to": row[1],
                "subject": row[2],
                "status": row[3],
                "ts": row[4],
            }
            for row in rows
        ]
        return _result(True, drafts=drafts, mode="local_draft")

    async def send_mail(args: dict[str, Any]) -> str:
        mid = str(args.get("id") or "")
        with _conn() as conn:
            row = conn.execute(
                "SELECT id, to_addr, subject, body FROM mail_drafts WHERE id=?", (mid,)
            ).fetchone()
            if not row:
                return _result(False, error="borrador no encontrado")
            if google is None:
                return _result(
                    False,
                    status="needs_oauth",
                    mode="local_stub",
                    stub=True,
                    error="El borrador es local y NO fue enviado. Configura Google OAuth.",
                )
            try:
                sent = await google.send_mail(
                    {"to": row[1], "subject": row[2], "body": row[3]}
                )
            except (httpx.HTTPError, ValueError) as exc:
                return _result(False, status="error", mode="google_api", error=str(exc))
            conn.execute(
                "UPDATE mail_drafts SET status='sent' WHERE id=?", (mid,)
            )
            conn.commit()
        return _result(
            True,
            id=sent.get("id"),
            mode="google_api",
            message=f"Correo enviado y aceptado por Gmail para {row[1]}",
        )

    async def delete_mail_draft(args: dict[str, Any]) -> str:
        mid = str(args.get("id") or "")
        with _conn() as conn:
            cursor = conn.execute(
                "DELETE FROM mail_drafts WHERE id=? AND status='draft'", (mid,)
            )
            conn.commit()
        deleted = cursor.rowcount == 1
        return _result(
            deleted,
            deleted=deleted,
            error=None if deleted else "borrador no encontrado o ya enviado",
        )

    tools.register(
        ToolSpec(
            "calendar.create_event",
            "Crea un evento por Google API si hay OAuth; si no, solo un stub local marcado.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_ts": {"type": "number"},
                    "end_ts": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": ["title"],
            },
            create_event,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "calendar.list_events",
            "Lista eventos recientes.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            list_events,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "mail.draft",
            "Crea un borrador de correo (no envía).",
            {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
            draft_mail,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "mail.list_drafts",
            "Lista borradores de correo locales.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            list_mail_drafts,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "mail.send",
            "ENVÍA realmente un borrador por Gmail API; nunca simula éxito.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            send_mail,
            "L2",
        )
    )
    tools.register(
        ToolSpec(
            "mail.delete_draft",
            "BORRA un borrador local sin enviarlo.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            delete_mail_draft,
            "L1",
        )
    )
