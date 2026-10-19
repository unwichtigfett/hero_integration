"""Deterministic, read-only GraphQL introspection graph analysis."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class GraphSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class GraphView:
    fingerprint: str
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, str], ...]


def analyze_snapshot(snapshot: object) -> GraphView:
    """Derive types, operations, arguments and relations only from saved schema JSON."""
    schema = _schema(snapshot)
    types = schema.get("types")
    if not isinstance(types, list):
        raise GraphSnapshotError("GraphQL schema types must be a list.")
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str, str]] = set()
    types_by_name = {
        item.get("name"): item for item in types
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    for name, item in sorted(types_by_name.items()):
        kind = str(item.get("kind") or "UNKNOWN")
        type_id = f"type:{name}"
        nodes[type_id] = _node(type_id, kind, name, description=item.get("description"), type_name=name)
        _type_members(item, name, nodes, edges)
    for root_kind, schema_key in (("QUERY", "queryType"), ("MUTATION", "mutationType"), ("SUBSCRIPTION", "subscriptionType")):
        root_name = _named_type(schema.get(schema_key))
        if not root_name or f"type:{root_name}" not in nodes:
            continue
        root_id = f"root:{root_kind.lower()}"
        nodes[root_id] = _node(root_id, f"{root_kind}_ROOT", root_kind.title(), type_name=root_name)
        edges.add((root_id, f"type:{root_name}", "ROOT_TYPE", root_name))
        for field in types_by_name[root_name].get("fields") or []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            operation_id = f"operation:{root_kind.lower()}:{field['name']}"
            nodes[operation_id] = _node(
                operation_id, f"{root_kind}_OPERATION", field["name"], type_name=root_name,
                type_ref=_type_reference(field.get("type")), description=field.get("description"),
            )
            edges.add((root_id, operation_id, "ROOT_HAS_OPERATION", field["name"]))
            edges.add((operation_id, f"field:{root_name}.{field['name']}", "OPERATION_FIELD", field["name"]))
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return GraphView(
        fingerprint=hashlib.sha256(canonical).hexdigest(),
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=tuple(
            {"source": source, "target": target, "kind": kind}
            for source, target, kind, _label in sorted(edges) if source in nodes and target in nodes
        ),
    )


def _type_members(item, type_name: str, nodes, edges) -> None:
    type_id = f"type:{type_name}"
    for field_group, node_kind, relation in (("fields", "FIELD", "HAS_FIELD"), ("inputFields", "INPUT_FIELD", "HAS_INPUT_FIELD")):
        for field in item.get(field_group) or []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            field_name = field["name"]
            field_id = f"{node_kind.lower()}:{type_name}.{field_name}"
            nodes[field_id] = _node(
                field_id, node_kind, field_name, type_name=type_name, type_ref=_type_reference(field.get("type")),
                description=field.get("description"), nullable=_nullable(field.get("type")),
            )
            edges.add((type_id, field_id, relation, field_name))
            target = _named_type(field.get("type"))
            if target:
                edges.add((field_id, f"type:{target}", "INPUT_TYPE" if node_kind == "INPUT_FIELD" else "RETURNS", target))
            if node_kind == "FIELD":
                for argument in field.get("args") or []:
                    if not isinstance(argument, dict) or not isinstance(argument.get("name"), str):
                        continue
                    argument_name = argument["name"]
                    argument_id = f"argument:{type_name}.{field_name}.{argument_name}"
                    nodes[argument_id] = _node(
                        argument_id, "ARGUMENT", argument_name, type_name=type_name, field_name=field_name,
                        type_ref=_type_reference(argument.get("type")), description=argument.get("description"),
                        nullable=_nullable(argument.get("type")),
                    )
                    edges.add((field_id, argument_id, "HAS_ARGUMENT", argument_name))
                    target = _named_type(argument.get("type"))
                    if target:
                        edges.add((argument_id, f"type:{target}", "ARGUMENT_TYPE", target))
    for value in item.get("enumValues") or []:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        value_id = f"enum_value:{type_name}.{value['name']}"
        nodes[value_id] = _node(value_id, "ENUM_VALUE", value["name"], type_name=type_name, description=value.get("description"))
        edges.add((type_id, value_id, "HAS_ENUM_VALUE", value["name"]))
    for possible in item.get("possibleTypes") or []:
        target = _named_type(possible)
        if target:
            edges.add((type_id, f"type:{target}", "POSSIBLE_TYPE", target))
    for interface in item.get("interfaces") or []:
        target = _named_type(interface)
        if target:
            edges.add((type_id, f"type:{target}", "IMPLEMENTS", target))


def _node(node_id: str, kind: str, label: str, **detail: object) -> dict[str, Any]:
    return {"id": node_id, "kind": kind, "label": label, **{key: value for key, value in detail.items() if value not in (None, "")}}


def _schema(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, dict):
        raise GraphSnapshotError("GraphQL snapshot must be an object.")
    schema = snapshot.get("__schema", snapshot.get("schema", snapshot))
    if not isinstance(schema, dict):
        raise GraphSnapshotError("GraphQL snapshot schema must be an object.")
    # Historic full snapshots encode the same introspection data with snake-case
    # member names. Normalize at the read boundary so a complete stored source
    # can be selected by the viewer without importing its original runtime.
    if "query_type_name" in schema or "mutation_type_name" in schema:
        return _normalize_full_snapshot(schema)
    return schema


def _normalize_full_snapshot(schema: dict[str, object]) -> dict[str, object]:
    def type_ref(value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            "kind": value.get("kind"), "name": value.get("name"),
            "ofType": type_ref(value.get("of_type")),
        }

    def field(value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        result["type"] = type_ref(result.pop("type_ref", None))
        result["args"] = [
            {**argument, "type": type_ref(argument.get("type_ref"))}
            for argument in result.pop("arguments", []) if isinstance(argument, dict)
        ]
        return result

    types = []
    for raw in schema.get("types", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["fields"] = [field(value) for value in item.get("fields", [])]
        item["inputFields"] = [field(value) for value in item.pop("input_fields", [])]
        item["enumValues"] = item.pop("enum_values", [])
        item["possibleTypes"] = [type_ref(value) for value in item.pop("possible_types", [])]
        item["interfaces"] = [type_ref(value) for value in item.pop("interfaces", [])]
        types.append(item)
    return {
        "types": types, "directives": schema.get("directives", []),
        "queryType": {"name": schema["query_type_name"]} if schema.get("query_type_name") else None,
        "mutationType": {"name": schema["mutation_type_name"]} if schema.get("mutation_type_name") else None,
        "subscriptionType": {"name": schema["subscription_type_name"]} if schema.get("subscription_type_name") else None,
    }


def _named_type(value: object) -> str | None:
    current = value
    while isinstance(current, dict):
        name = current.get("name")
        if isinstance(name, str) and name:
            return name
        current = current.get("ofType")
    return None


def _type_reference(value: object) -> str:
    if not isinstance(value, dict):
        return "Unknown"
    if value.get("kind") == "NON_NULL":
        return f"{_type_reference(value.get('ofType'))}!"
    if value.get("kind") == "LIST":
        return f"[{_type_reference(value.get('ofType'))}]"
    return str(value.get("name") or value.get("kind") or "Unknown")


def _nullable(value: object) -> bool:
    return not isinstance(value, dict) or value.get("kind") != "NON_NULL"
