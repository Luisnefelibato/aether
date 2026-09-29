from __future__ import annotations

from typing import Any, Awaitable, Callable


ToolHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


def normalize_json_schema(schema: Any) -> Any:
    """Moonshot requires a type on every property (no enum-only fields)."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "oneOf" in out:
        types = [
            alt.get("type")
            for alt in out["oneOf"]
            if isinstance(alt, dict) and alt.get("type")
        ]
        if "type" not in out:
            out["type"] = types[0] if len(set(types)) == 1 else "string"
        out.pop("oneOf", None)
        out.pop("const", None)
    if "enum" in out and "type" not in out:
        out["type"] = "string"
    if "properties" in out and "type" not in out:
        out["type"] = "object"
    if "items" in out and "type" not in out:
        out["type"] = "array"
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    if isinstance(out.get("properties"), dict):
        out["properties"] = {
            key: normalize_json_schema(value) for key, value in out["properties"].items()
        }
    if "items" in out:
        out["items"] = normalize_json_schema(out["items"])
    return out


class ToolSpec:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
        level: str = "L0",
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        self.level = level


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(self.resolve_name(name))

    def resolve_name(self, name: str) -> str:
        if name in self._tools:
            return name
        for original in self._tools:
            if original.replace(".", "_") == name:
                return original
        return name

    def level_for(self, name: str) -> str:
        spec = self._tools.get(self.resolve_name(name))
        return spec.level if spec else "L2"

    def openai_tools(self) -> list[dict[str, Any]]:
        out = []
        for spec in self._tools.values():
            # Kimi rejects dotted function names.
            safe = spec.name.replace(".", "_")
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": safe,
                        "description": f"{spec.description} [internal={spec.name}]",
                        "parameters": normalize_json_schema(spec.parameters),
                    },
                }
            )
        return out

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        resolved = self.resolve_name(name)
        spec = self._tools[resolved]
        result = spec.handler(arguments)
        if hasattr(result, "__await__"):
            return await result
        return result

    def names(self) -> list[str]:
        return list(self._tools.keys())
