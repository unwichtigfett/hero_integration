"""Local-only validation for HERO mutation drafts against a saved schema."""
from __future__ import annotations

import re


_MUTATION = re.compile(r"^\s*mutation(?:\s+[_A-Za-z][_0-9A-Za-z]*)?(?:\s*\([^)]*\))?\s*\{\s*([_A-Za-z][_0-9A-Za-z]*)")


def validate(schema: dict[str, object], document: str) -> dict[str, object]:
    match = _MUTATION.match(document)
    if not match:
        raise ValueError("A local mutation draft must start with a GraphQL mutation operation.")
    body = schema.get("__schema") or schema.get("schema") or schema
    if not isinstance(body, dict):
        raise ValueError("The saved schema is invalid.")
    mutation_type = body.get("mutationType") or body.get("mutation_type") or {}
    root_name = mutation_type.get("name") if isinstance(mutation_type, dict) else ""
    types = body.get("types") if isinstance(body.get("types"), list) else []
    root = next((item for item in types if isinstance(item, dict) and item.get("name") == root_name), None)
    if not isinstance(root, dict):
        raise ValueError("The saved schema has no mutation root.")
    operation = match.group(1)
    field = next((item for item in root.get("fields", []) if isinstance(item, dict) and item.get("name") == operation), None)
    if not isinstance(field, dict):
        raise ValueError("The mutation operation does not exist in the saved schema.")
    return {"operation": operation, "root": root_name, "return_type": _type_name(field.get("type"))}


def _type_name(type_ref: object) -> str:
    current = type_ref
    while isinstance(current, dict) and current.get("ofType"):
        current = current["ofType"]
    return str(current.get("name") or current.get("kind") or "unknown") if isinstance(current, dict) else "unknown"
