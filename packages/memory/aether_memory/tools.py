from __future__ import annotations

import json
from typing import Any

from aether_core.tools import ToolRegistry, ToolSpec
from aether_memory.store import MemoryStore


def register_memory_tools(tools: ToolRegistry, memory: MemoryStore) -> None:
    async def set_pref(args: dict[str, Any]) -> str:
        key = str(args.get("key") or "")
        value = str(args.get("value") or "")
        await memory.set_preference(key, value)
        return json.dumps({"ok": True, "message": f"Preferencia {key}={value}"})

    async def list_prefs(_args: dict[str, Any]) -> str:
        prefs = await memory.list_preferences()
        return json.dumps({"ok": True, "preferences": prefs})

    async def set_fact(args: dict[str, Any]) -> str:
        source = str(args.get("source") or "user")
        if source.lower() in {"unverified", "model", "kimi", "assistant"}:
            return json.dumps(
                {
                    "ok": False,
                    "verified": False,
                    "error": "No se guardan hechos no verificados",
                }
            )
        fact = await memory.set_fact(
            str(args["subject"]),
            str(args["predicate"]),
            args.get("value"),
            float(args.get("confidence", 1.0)),
            source,
        )
        return json.dumps({"ok": True, "verified": True, "fact": fact}, ensure_ascii=False)

    async def get_facts(args: dict[str, Any]) -> str:
        facts = await memory.get_facts(
            args.get("subject"),
            args.get("predicate"),
            bool(args.get("include_history", False)),
            int(args.get("limit", 100)),
        )
        return json.dumps({"ok": True, "facts": facts}, ensure_ascii=False)

    async def list_entities(args: dict[str, Any]) -> str:
        entities = await memory.list_entities(args.get("kind"))
        return json.dumps({"ok": True, "verified": True, "entities": entities}, ensure_ascii=False)

    async def put_entity(args: dict[str, Any]) -> str:
        entity = await memory.put_entity(
            str(args["name"]), str(args.get("kind", "")), args.get("metadata")
        )
        return json.dumps({"ok": True, "entity": entity}, ensure_ascii=False)

    async def add_link(args: dict[str, Any]) -> str:
        link = await memory.add_link(
            str(args["source_id"]),
            str(args["target_id"]),
            str(args["relation"]),
            args.get("metadata"),
        )
        return json.dumps({"ok": True, "link": link}, ensure_ascii=False)

    async def forget_fact(args: dict[str, Any]) -> str:
        result = await memory.forget_fact(
            str(args["subject"]),
            str(args["predicate"]) if args.get("predicate") else None,
        )
        return json.dumps({"ok": True, "verified": True, **result}, ensure_ascii=False)

    async def consolidate(args: dict[str, Any]) -> str:
        result = await memory.consolidate(int(args.get("limit") or 40))
        return json.dumps({"ok": True, "verified": True, **result}, ensure_ascii=False, default=str)

    async def add_summary(args: dict[str, Any]) -> str:
        summary = await memory.add_summary(
            str(args["scope"]),
            str(args["content"]),
            args.get("source_from"),
            args.get("source_to"),
        )
        return json.dumps({"ok": True, "summary": summary}, ensure_ascii=False)

    tools.register(
        ToolSpec(
            "memory.set_preference",
            "Guarda una preferencia del usuario (IDE, luces, etc.).",
            {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
            set_pref,
            "L0",
        )
    )
    schemas = [
        (
            "memory.set_fact",
            "Guarda una nueva versión de un hecho estructurado.",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "value": {},
                    "confidence": {"type": "number"},
                    "source": {"type": "string"},
                },
                "required": ["subject", "predicate", "value"],
            },
            set_fact,
        ),
        (
            "memory.get_facts",
            "Consulta hechos actuales o su historial.",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "include_history": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
            get_facts,
        ),
        (
            "memory.put_entity",
            "Crea o actualiza una entidad.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["name"],
            },
            put_entity,
        ),
        (
            "memory.list_entities",
            "Lista entidades (personas, proyectos, compromisos).",
            {
                "type": "object",
                "properties": {"kind": {"type": "string"}},
            },
            list_entities,
        ),
        (
            "memory.add_link",
            "Relaciona dos entidades.",
            {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": "string"},
                    "relation": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["source_id", "target_id", "relation"],
            },
            add_link,
        ),
        (
            "memory.remember_fact",
            "Alias de memory.set_fact para recordar un hecho.",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "value": {},
                    "confidence": {"type": "number"},
                    "source": {"type": "string"},
                },
                "required": ["subject", "predicate", "value"],
            },
            set_fact,
        ),
        (
            "memory.recall_facts",
            "Alias de memory.get_facts.",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "include_history": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
            get_facts,
        ),
        (
            "memory.link_entity",
            "Alias de memory.add_link.",
            {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": "string"},
                    "relation": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["source_id", "target_id", "relation"],
            },
            add_link,
        ),
        (
            "memory.forget_fact",
            "Desactiva un hecho (superseding, no borra historial).",
            {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                },
                "required": ["subject"],
            },
            forget_fact,
        ),
        (
            "memory.consolidate",
            "Resume episodios recientes en un summary persistente.",
            {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
            consolidate,
        ),
        (
            "memory.add_summary",
            "Guarda un resumen estructurado.",
            {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "content": {"type": "string"},
                    "source_from": {"type": "number"},
                    "source_to": {"type": "number"},
                },
                "required": ["scope", "content"],
            },
            add_summary,
        ),
    ]
    for name, description, schema, handler in schemas:
        tools.register(ToolSpec(name, description, schema, handler, "L0"))
    tools.register(
        ToolSpec(
            "memory.list_preferences",
            "Lista preferencias guardadas.",
            {"type": "object", "properties": {}},
            list_prefs,
            "L0",
        )
    )
