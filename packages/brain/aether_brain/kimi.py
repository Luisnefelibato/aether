from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

import httpx

from aether_core.config import Settings

logger = logging.getLogger("aether.brain")

MAX_TOOL_RESULT_CHARS = 6000


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if "b64" in str(key).lower() or key in {"image", "screenshot", "png"}:
                text = str(item)
                out[key] = text[:80] + "..." if len(text) > 80 else text
            else:
                out[key] = _compact_value(item)
        return out
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "...[truncated]"
    return value


def _compact_tool_result(result: str) -> str:
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    try:
        parsed = json.loads(text)
        text = json.dumps(_compact_value(parsed), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    if len(text) > MAX_TOOL_RESULT_CHARS:
        return text[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"
    return text


class KimiPlanner:
    """Moonshot Kimi planner — all reasoning / tool-calling / multimodal."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=120.0)

    @property
    def _api_key(self) -> str:
        return self.settings.moonshot_api_key

    async def close(self) -> None:
        await self._client.aclose()

    async def run_agent_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        execute_tool: ExecuteTool,
        require_tool: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self._api_key:
            return (
                "Falta MOONSHOT_API_KEY. Configura tu clave en .env para activar Kimi.",
                [],
            )

        trace: list[dict[str, Any]] = []
        working = list(messages)

        forced_retry = False
        for round_index in range(self.settings.brain.max_tool_rounds):
            force_this_round = require_tool and not trace and (round_index == 0 or forced_retry)
            data = await self._chat(working, tools, require_tool=force_this_round)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                if require_tool and not trace and not forced_retry:
                    working.append(
                        {
                            "role": "assistant",
                            "content": message.get("content") or "",
                        }
                    )
                    working.append(
                        {
                            "role": "system",
                            "content": (
                                "La petición es una ORDEN de acción. No afirmes que hiciste "
                                "nada sin ejecutar una herramienta. Llama ahora la herramienta "
                                "adecuada; si faltan datos, usa una herramienta de inspección "
                                "o explica que no pudiste ejecutar, pero nunca simules éxito."
                            ),
                        }
                    )
                    forced_retry = True
                    continue
                if require_tool and not trace:
                    return (
                        "No pude ejecutar la acción porque no se invocó ninguna herramienta.",
                        trace,
                    )
                return (message.get("content") or "").strip(), trace

            working.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
            )

            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    args = {}

                result = await execute_tool(name, args)
                trace.append({"name": name, "arguments": args, "result": result})
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", name),
                        "content": _compact_tool_result(result),
                    }
                )

                try:
                    parsed = json.loads(result)
                    if isinstance(parsed, dict) and parsed.get("pending_confirm"):
                        return "", trace
                except json.JSONDecodeError:
                    pass

        return "Alcancé el límite de pasos de la misión.", trace

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        require_tool: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.brain.model,
            "messages": messages,
            "max_tokens": self.settings.brain.max_tokens,
            "temperature": self.settings.brain.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "required" if require_tool else "auto"
        # kimi-k2.6 enables thinking by default. required + thinking = 400.
        # thinking.disabled only accepts temperature 0.6.
        if require_tool:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = 0.6
        elif getattr(self.settings.brain, "thinking", False):
            payload["thinking"] = {"type": "enabled"}

        resp = await self._post(payload)
        if resp.status_code >= 400:
            body = resp.text[:800]
            logger.error("Kimi error %s: %s", resp.status_code, body)
            if "incompatible with thinking" in body or (
                "tool_choice" in body and "thinking" in body
            ):
                payload["thinking"] = {"type": "disabled"}
                payload["temperature"] = 0.6
                resp = await self._post(payload)
                if resp.status_code >= 400:
                    payload["tool_choice"] = "auto"
                    payload.pop("thinking", None)
                    payload["temperature"] = 1
                    resp = await self._post(payload)
            elif "temperature" in body:
                payload["temperature"] = 0.6 if "0.6" in body else 1
                if payload["temperature"] == 0.6:
                    payload["thinking"] = {"type": "disabled"}
                else:
                    payload.pop("thinking", None)
                resp = await self._post(payload)
            elif payload.get("thinking"):
                payload.pop("thinking", None)
                payload["temperature"] = 1
                resp = await self._post(payload)
        if resp.status_code >= 400:
            logger.error("Kimi error %s: %s", resp.status_code, resp.text[:800])
            resp.raise_for_status()
        return resp.json()

    async def _post(self, payload: dict[str, Any]):
        return await self._client.post(
            f"{self.settings.brain.base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    async def describe_image(self, prompt: str, image_b64: str, mime: str = "image/png") -> str:
        """Multimodal helper for screen/camera capabilities via Kimi."""
        if not self._api_key:
            return "Falta MOONSHOT_API_KEY"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                    },
                ],
            }
        ]
        data = await self._chat(messages, tools=[])
        choice = (data.get("choices") or [{}])[0]
        return ((choice.get("message") or {}).get("content") or "").strip()


# Back-compat alias
GrokPlanner = KimiPlanner
