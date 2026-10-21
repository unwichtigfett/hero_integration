"""Dynamic operation catalog and explainable name-only mapping suggestions."""
from __future__ import annotations

import re
from collections.abc import Mapping

from hero_integration.services.graphql_graph import GraphSnapshotError, normalize_schema


class SchemaOperationError(ValueError):
    pass


def catalog(snapshot: object, operation_kind: str) -> list[dict[str, object]]:
    kind = operation_kind.upper()
    root_key = {"QUERY": "queryType", "MUTATION": "mutationType"}.get(kind)
    if not root_key:
        raise SchemaOperationError("Operation kind must be QUERY or MUTATION.")
    try:
        schema = normalize_schema(snapshot)
    except GraphSnapshotError as error:
        raise SchemaOperationError("Invalid saved GraphQL schema.") from error
    root = schema.get(root_key)
    root_name = root.get("name") if isinstance(root, Mapping) else ""
    types = {item.get("name"): item for item in schema.get("types") or [] if isinstance(item, Mapping)}
    root_type = types.get(root_name)
    if not isinstance(root_type, Mapping):
        return []
    return [
        {
            "operation_kind": kind, "operation_name": field["name"], "root_type": root_name,
            "return_type": type_reference(field.get("type")),
            "arguments": [
                {"name": arg.get("name"), "type": type_reference(arg.get("type")), "required": is_required(arg.get("type"))}
                for arg in field.get("args") or [] if isinstance(arg, Mapping)
            ],
        }
        for field in root_type.get("fields") or [] if isinstance(field, Mapping) and isinstance(field.get("name"), str)
    ]


def suggest(source: str, candidates: list[str]) -> list[dict[str, object]]:
    """Explain name affinity only; callers must keep all results as drafts."""
    source_tokens = set(_tokens(source))
    result = []
    for candidate in candidates:
        overlap = source_tokens & set(_tokens(candidate))
        score = len(overlap) / max(len(source_tokens | set(_tokens(candidate))), 1)
        if score:
            result.append({"candidate": candidate, "confidence": round(score, 2), "reason": "shared normalized names: " + ", ".join(sorted(overlap))})
    return sorted(result, key=lambda item: (-item["confidence"], item["candidate"]))


def type_reference(ref: object) -> str:
    if not isinstance(ref, Mapping):
        return "Unknown"
    if ref.get("kind") == "NON_NULL": return f"{type_reference(ref.get('ofType'))}!"
    if ref.get("kind") == "LIST": return f"[{type_reference(ref.get('ofType'))}]"
    return str(ref.get("name") or ref.get("kind") or "Unknown")


def is_required(ref: object) -> bool:
    return isinstance(ref, Mapping) and ref.get("kind") == "NON_NULL"


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", value))
