"""JSON Schema that OpenAI-compatible gateways accept.

Pydantic emits ``$defs``/``$ref`` plus validation keywords (``pattern`` for decimals,
``format``, ``minimum``...). OpenAI handles them, but many providers behind OpenRouter
reject such schemas with a bare "400 bad request". The portable form inlines every
reference and drops keywords that only constrain values - the same trade-off
``anthropic.transform_schema`` makes. Nothing is lost: every response is validated
against the full Pydantic model afterwards, and failures go back to the model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

VALUE_CONSTRAINTS = frozenset(
    {"pattern", "format", "default", "title", "minimum", "maximum", "exclusiveMinimum",
     "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems"}
)  # fmt: skip


def portable_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    definitions: dict[str, Any] = schema.pop("$defs", {})

    def walk(node: Any, resolving: tuple[str, ...], property_map: bool = False) -> Any:
        if isinstance(node, list):
            return [walk(item, resolving) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node and not property_map:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in resolving:
                raise ValueError(f"Recursive schema '{name}' cannot be inlined")
            resolved = walk(definitions[name], (*resolving, name))
            # Keep what sits next to the reference, e.g. the field's own description.
            siblings = walk({k: v for k, v in node.items() if k != "$ref"}, resolving)
            return {**resolved, **siblings}
        # Keys of a "properties" map are field names (a field may be called "title").
        return {
            key: walk(value, resolving, property_map=key == "properties" and not property_map)
            for key, value in node.items()
            if property_map or key not in VALUE_CONSTRAINTS
        }

    result: dict[str, Any] = walk(schema, ())
    return result
