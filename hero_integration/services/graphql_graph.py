"""Read-only GraphQL snapshot analysis for HERO adapter tools."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


class GraphSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class GraphView:
    fingerprint: str
    nodes: tuple[dict[str, str], ...]
    edges: tuple[dict[str, str], ...]


def analyze_snapshot(snapshot: object) -> GraphView:
    """Validate a saved introspection payload and make a deterministic graph."""
    schema = _schema(snapshot)
    types = schema.get("types")
    if not isinstance(types, list):
        raise GraphSnapshotError("GraphQL schema types must be a list.")

    nodes: dict[str, dict[str, str]] = {}
    edges: set[tuple[str, str, str]] = set()
    for item in types:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        type_id = f"type:{name}"
        nodes[type_id] = {"id": type_id, "kind": str(item.get("kind") or "UNKNOWN"), "label": name}
        for field in item.get("fields") or []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            field_id = f"field:{name}.{field['name']}"
            nodes[field_id] = {"id": field_id, "kind": "FIELD", "label": field["name"]}
            edges.add((type_id, field_id, "HAS_FIELD"))
            target = _named_type(field.get("type"))
            if target:
                edges.add((field_id, f"type:{target}", "RETURNS"))

    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return GraphView(
        fingerprint=hashlib.sha256(canonical).hexdigest(),
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(
            {"source": source, "target": target, "kind": kind}
            for source, target, kind in sorted(edges)
            if target in nodes
        ),
    )


def _schema(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, dict):
        raise GraphSnapshotError("GraphQL snapshot must be an object.")
    schema = snapshot.get("__schema", snapshot.get("schema", snapshot))
    if not isinstance(schema, dict):
        raise GraphSnapshotError("GraphQL snapshot schema must be an object.")
    return schema


def _named_type(value: object) -> str | None:
    current = value
    while isinstance(current, dict):
        name = current.get("name")
        if isinstance(name, str) and name:
            return name
        current = current.get("ofType")
    return None
