from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from websockets.asyncio.server import serve

from aether_core.events import EventBus

logger = logging.getLogger("aether.hud")


class HudServer:
    """Local WebSocket bridge for the Electron/Tauri HUD overlay."""

    def __init__(self, bus: EventBus, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.bus = bus
        self.host = host
        self.port = port
        self._clients: set[Any] = set()
        self._server = None

    async def start(self) -> None:
        self.bus.on("*", self._fanout)
        self._server = await serve(self._handler, self.host, self.port)
        logger.info("HUD websocket on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, websocket: Any) -> None:
        self._clients.add(websocket)
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "payload": {"state": self.bus.state.value, "name": "ADAM"},
                    }
                )
            )
            await self.bus.publish("hud_connected")
            async for message in websocket:
                try:
                    await self._from_hud(message)
                except Exception:  # noqa: BLE001
                    logger.exception("HUD command failed")
                    try:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "payload": {"message": "falló el comando"},
                                }
                            )
                        )
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            self._clients.discard(websocket)

    async def _from_hud(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        event_type = data.get("type")
        payload = data.get("payload") or {}
        if event_type == "ptt_down":
            await self.bus.publish("ptt", active=True)
        elif event_type == "ptt_up":
            await self.bus.publish("ptt", active=False)
        elif event_type == "cancel":
            await self.bus.publish("cancel")
        elif event_type == "confirm":
            await self.bus.publish("utterance", text=payload.get("text", "sí"))
        elif event_type == "text_command":
            await self.bus.publish("utterance", text=str(payload.get("text", "")))
        elif event_type == "cancel_job":
            await self.bus.publish("cancel_job", job_id=str(payload.get("job_id", "")))
        elif event_type == "cancel_mission":
            await self.bus.publish(
                "cancel_mission", mission_id=str(payload.get("mission_id", ""))
            )
        elif event_type == "list_jobs":
            await self.bus.publish("list_jobs")

    async def _fanout(self, event: Any) -> None:
        if not self._clients:
            return
        packet = json.dumps({"type": event.type, "payload": event.payload, "id": event.id})
        dead: list[Any] = []
        for client in self._clients:
            try:
                await client.send(packet)
            except Exception:  # noqa: BLE001
                dead.append(client)
        for client in dead:
            self._clients.discard(client)
