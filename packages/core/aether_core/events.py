from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import uuid4


class SessionState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    DELEGATING = "delegating"
    VERIFYING = "verifying"
    SUPERVISING = "supervising"
    SPEAKING = "speaking"
    AWAITING_CONFIRM = "awaiting_confirm"


@dataclass
class AetherEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))


Handler = Callable[[AetherEvent], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[AetherEvent] = asyncio.Queue()
        self._state = SessionState.IDLE

    @property
    def state(self) -> SessionState:
        return self._state

    def set_state(self, state: SessionState) -> None:
        self._state = state
        asyncio.create_task(self.emit(AetherEvent("state", {"state": state.value})))

    def on(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def emit(self, event: AetherEvent) -> None:
        await self._queue.put(event)
        for handler in list(self._handlers.get(event.type, [])):
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        for handler in list(self._handlers.get("*", [])):
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result

    async def publish(self, event_type: str, **payload: Any) -> None:
        await self.emit(AetherEvent(event_type, payload))
