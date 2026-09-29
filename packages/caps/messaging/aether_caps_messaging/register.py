from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from aether_core.config import Settings
from aether_core.tools import ToolRegistry, ToolSpec


def _result(ok: bool, **payload: Any) -> dict[str, Any]:
    return {"ok": ok, "verified": ok, **payload}


class DraftStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_drafts (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL,
                provider_message_id TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        connection.commit()
        return connection

    def create(self, channel: str, recipient: str, body: str) -> dict[str, Any]:
        draft_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO message_drafts VALUES (?,?,?,?,?,?,?)",
                (draft_id, channel, recipient, body, "draft", None, time.time()),
            )
            connection.commit()
        return self.get(draft_id) or {}

    def get(self, draft_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM message_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM message_drafts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, draft_id: str, provider_message_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE message_drafts SET status='sent', provider_message_id=? WHERE id=?",
                (provider_message_id, draft_id),
            )
            connection.commit()

    def delete(self, draft_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM message_drafts WHERE id=? AND status='draft'", (draft_id,)
            )
            connection.commit()
        return cursor.rowcount == 1


class TelegramBotAdapter:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token.strip()
        self.client = client

    def status(self) -> dict[str, Any]:
        state = "connected" if self.token else "needs_login"
        return {
            "service": "telegram",
            "status": state,
            "mode": "bot_http",
            "connected": bool(self.token),
            "detail": (
                "Telegram Bot token configurado."
                if self.token
                else "Crea un bot con BotFather y configura TELEGRAM_BOT_TOKEN."
            ),
        }

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            return _result(
                False,
                status="needs_login",
                error="Telegram Bot no configurado; no se envió nada.",
            )
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            if self.client is not None:
                response = await self.client.post(url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
            if body.get("ok") is not True:
                return _result(False, status="error", error=body.get("description"))
            return _result(True, status="connected", response=body.get("result"))
        except (httpx.HTTPError, ValueError) as exc:
            return _result(False, status="error", error=str(exc))

    async def send(self, chat_id: str, text: str) -> dict[str, Any]:
        result = await self._post("sendMessage", {"chat_id": chat_id, "text": text})
        if result["ok"]:
            message = result.get("response") or {}
            result["message_id"] = str(message.get("message_id") or "")
        return result

    async def delete(self, chat_id: str, message_id: str) -> dict[str, Any]:
        return await self._post(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
        )


class WhatsAppCloudAdapter:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.access_token = access_token.strip()
        self.phone_number_id = phone_number_id.strip()
        self.client = client

    def status(self) -> dict[str, Any]:
        connected = bool(self.access_token and self.phone_number_id)
        return {
            "service": "whatsapp",
            "status": "connected" if connected else "needs_oauth",
            "mode": "cloud_business_api",
            "connected": connected,
            "detail": (
                "WhatsApp Cloud Business configurado."
                if connected
                else "Opcional: configura WHATSAPP_ACCESS_TOKEN y WHATSAPP_PHONE_NUMBER_ID."
            ),
        }

    async def send(self, recipient: str, text: str) -> dict[str, Any]:
        if not self.access_token or not self.phone_number_id:
            return _result(
                False,
                status="needs_oauth",
                error="WhatsApp Cloud Business no configurado; no se envió nada.",
            )
        url = f"https://graph.facebook.com/v20.0/{self.phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": text},
        }
        try:
            headers = {"Authorization": f"Bearer {self.access_token}"}
            if self.client is not None:
                response = await self.client.post(url, headers=headers, json=payload)
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
            message_id = str((body.get("messages") or [{}])[0].get("id") or "")
            if not message_id:
                return _result(
                    False, status="error", error="Meta no devolvió identificador."
                )
            return _result(
                True,
                status="connected",
                message_id=message_id,
                provider_response=body,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return _result(False, status="error", error=str(exc))


def register_messaging(tools: ToolRegistry, settings: Settings) -> None:
    store = DraftStore(settings.resolve_path(settings.memory.sqlite_path))
    telegram = TelegramBotAdapter(
        getattr(settings.messaging, "telegram_bot_token", "")
        or os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    whatsapp = WhatsAppCloudAdapter(
        getattr(settings.messaging, "whatsapp_cloud_token", "")
        or os.getenv("WHATSAPP_CLOUD_TOKEN", "")
        or os.getenv("WHATSAPP_ACCESS_TOKEN", ""),
        getattr(settings.messaging, "whatsapp_phone_number_id", "")
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
    )

    async def status(_args: dict[str, Any]) -> str:
        return json.dumps(
            {
                "ok": True,
                "verified": True,
                "statuses": [telegram.status(), whatsapp.status()],
            },
            ensure_ascii=False,
        )

    async def create_draft(args: dict[str, Any]) -> str:
        channel = str(args.get("channel") or "").lower()
        recipient = str(args.get("recipient") or "")
        body = str(args.get("body") or "")
        if channel not in {"telegram", "whatsapp"} or not recipient or not body:
            return json.dumps(
                _result(False, error="channel, recipient y body válidos son obligatorios")
            )
        return json.dumps(
            _result(True, draft=store.create(channel, recipient, body)),
            ensure_ascii=False,
        )

    async def list_drafts(args: dict[str, Any]) -> str:
        return json.dumps(
            _result(True, drafts=store.list(int(args.get("limit") or 50))),
            ensure_ascii=False,
        )

    async def delete_draft(args: dict[str, Any]) -> str:
        deleted = store.delete(str(args.get("draft_id") or ""))
        return json.dumps(
            _result(
                deleted,
                deleted=deleted,
                error=None if deleted else "Borrador no encontrado o ya enviado.",
            ),
            ensure_ascii=False,
        )

    async def send_telegram_draft(args: dict[str, Any]) -> str:
        draft = store.get(str(args.get("draft_id") or ""))
        if not draft or draft["channel"] != "telegram" or draft["status"] != "draft":
            return json.dumps(_result(False, error="Borrador Telegram no disponible"))
        result = await telegram.send(draft["recipient"], draft["body"])
        if result["ok"]:
            store.mark_sent(draft["id"], result["message_id"])
        return json.dumps(result, ensure_ascii=False)

    async def delete_telegram_message(args: dict[str, Any]) -> str:
        result = await telegram.delete(
            str(args.get("chat_id") or ""), str(args.get("message_id") or "0")
        )
        return json.dumps(result, ensure_ascii=False)

    async def send_whatsapp_draft(args: dict[str, Any]) -> str:
        draft = store.get(str(args.get("draft_id") or ""))
        if not draft or draft["channel"] != "whatsapp" or draft["status"] != "draft":
            return json.dumps(_result(False, error="Borrador WhatsApp no disponible"))
        result = await whatsapp.send(draft["recipient"], draft["body"])
        if result["ok"]:
            store.mark_sent(draft["id"], result["message_id"])
        return json.dumps(result, ensure_ascii=False)

    tools.register(
        ToolSpec(
            "messaging.status",
            "Muestra conexión/onboarding de Telegram Bot y WhatsApp Cloud Business.",
            {"type": "object", "properties": {}},
            status,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "messaging.draft_create",
            "CREA un borrador SQLite; no envía.",
            {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "enum": ["telegram", "whatsapp"],
                    },
                    "recipient": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["channel", "recipient", "body"],
            },
            create_draft,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "messaging.draft_list",
            "LISTA borradores y envíos guardados.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            list_drafts,
            "L0",
        )
    )
    tools.register(
        ToolSpec(
            "messaging.draft_delete",
            "BORRA únicamente un borrador local; no borra mensajes enviados.",
            {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            delete_draft,
            "L1",
        )
    )
    tools.register(
        ToolSpec(
            "messaging.telegram_send_draft",
            "ENVÍA realmente un borrador mediante Telegram Bot HTTP.",
            {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            send_telegram_draft,
            "L2",
        )
    )
    tools.register(
        ToolSpec(
            "messaging.telegram_delete_message",
            "BORRA realmente un mensaje ya enviado mediante Telegram Bot HTTP.",
            {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string"},
                    "message_id": {"type": "string"},
                },
                "required": ["chat_id", "message_id"],
            },
            delete_telegram_message,
            "L2",
        )
    )
    tools.register(
        ToolSpec(
            "messaging.whatsapp_send_draft",
            "ENVÍA realmente un borrador por WhatsApp Cloud Business.",
            {
                "type": "object",
                "properties": {"draft_id": {"type": "string"}},
                "required": ["draft_id"],
            },
            send_whatsapp_draft,
            "L2",
        )
    )
