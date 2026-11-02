"""Remembered GraphQL nodes ("Remember for Mapping") - pure helpers.

A bookmark stores the technical identity of one saved-schema graph node plus a
user-chosen alias.  The alias is a label only; identity always comes from the
validated snapshot.  Mapping context is derived exclusively from schema edges,
never from similar-sounding names, and is only a preselection hint.
"""
from __future__ import annotations

import hashlib
import json

from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.localized_error import LocalizedError

MAX_ALIAS_LENGTH = 80


class NodeBookmarkError(LocalizedError):
    pass


def normalize_alias(value: object) -> tuple[str, str]:
    """Return (alias, case-insensitive key); reject empty, overlong or control characters."""
    if not isinstance(value, str):
        raise NodeBookmarkError("Alias is required.")
    alias = " ".join(value.split())
    if not alias:
        raise NodeBookmarkError("Alias is required.")
    if len(alias) > MAX_ALIAS_LENGTH:
        raise NodeBookmarkError("Alias must not exceed {0} characters.", MAX_ALIAS_LENGTH)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise NodeBookmarkError("Alias must not contain control characters.")
    return alias, alias.casefold()


def bookmark_key(owner: str, snapshot_name: str, node_id: str) -> str:
    raw = json.dumps([owner, snapshot_name, node_id], ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def graph_index(schema: object) -> tuple[dict[str, dict], list[dict]]:
    graph = analyze_snapshot(schema)
    return {node["id"]: node for node in graph.nodes}, list(graph.edges)


def resolve_node(schema: object, node_id: str) -> dict[str, object]:
    """Return the server-side identity of a real graph node; aggregates do not exist here."""
    nodes, _ = graph_index(schema)
    node = nodes.get(node_id or "")
    if not node:
        raise NodeBookmarkError("The selected node does not exist in this validated snapshot.")
    return {"node_id": node["id"], "node_kind": node["kind"], "node_name": _display_name(node)}


def _display_name(node: dict) -> str:
    if node["kind"] in {"FIELD", "INPUT_FIELD"}:
        return f"{node.get('type_name')}.{node['label']}"
    if node["kind"] == "ARGUMENT":
        return f"{node.get('type_name')}.{node.get('field_name')}({node['label']})"
    if node["kind"] == "ENUM_VALUE":
        return f"{node.get('type_name')}.{node['label']}"
    return str(node["label"])


def mapping_context(schema: object, node_id: str) -> dict[str, object]:
    """Derive an unambiguous operation/path preselection from schema edges only."""
    nodes, edges = graph_index(schema)
    node = nodes.get(node_id)
    if not node:
        raise NodeBookmarkError("The remembered node does not exist in its snapshot.")
    roots = {kind: nodes[f"root:{kind}"].get("type_name") for kind in ("query", "mutation") if f"root:{kind}" in nodes}
    kind = node["kind"]
    navigation = {"mappable": False, "operation_kind": "", "operation": "", "path": "", "candidates": []}
    if kind in {"QUERY_OPERATION", "MUTATION_OPERATION"}:
        return {**navigation, "mappable": True, "operation_kind": kind.split("_")[0], "operation": node["label"],
                "reason": "The remembered node is an operation; the operation is preselected."}
    if kind == "FIELD":
        owner = node.get("type_name")
        for root_kind, root_name in roots.items():
            if owner == root_name:
                return {**navigation, "mappable": True, "operation_kind": root_kind.upper(), "operation": node["label"],
                        "reason": "The field is a root operation field; the operation is preselected."}
        returning = {edge["source"] for edge in edges if edge["kind"] == "RETURNS" and edge["target"] == f"type:{owner}"}
        candidates = [{"operation_kind": "QUERY", "operation": nodes[source]["label"], "path": node["label"]}
                      for source in sorted(returning)
                      if source in nodes and nodes[source].get("type_name") == roots.get("query")]
        return _choose(navigation, candidates, f"The field belongs to {owner}; query operations returning {owner} directly")
    if kind == "ARGUMENT":
        owner = node.get("type_name")
        for root_kind, root_name in roots.items():
            if owner == root_name:
                mutation = root_kind == "mutation"
                return {**navigation, "mappable": mutation, "operation_kind": root_kind.upper(), "operation": node.get("field_name"),
                        "path": node["label"] if mutation else "",
                        "reason": "Mutation argument path is preselected." if mutation
                        else "Query arguments are request inputs, not source fields; only the operation is offered."}
        return {**navigation, "reason": "The argument does not belong to a root operation; only navigation is offered."}
    if kind == "INPUT_FIELD":
        owner = node.get("type_name")
        arguments = {edge["source"] for edge in edges if edge["kind"] == "ARGUMENT_TYPE" and edge["target"] == f"type:{owner}"}
        candidates = [{"operation_kind": "MUTATION", "operation": nodes[argument].get("field_name"),
                       "path": f"{nodes[argument]['label']}.{node['label']}"}
                      for argument in sorted(arguments)
                      if argument in nodes and nodes[argument].get("type_name") == roots.get("mutation")]
        return _choose(navigation, candidates, f"The input field belongs to {owner}; mutation arguments of type {owner}")
    return {**navigation, "reason": f"A {kind} node is not a mappable source or target field; only navigation is offered."}


def _choose(navigation: dict, candidates: list[dict], subject: str) -> dict[str, object]:
    if len(candidates) == 1:
        return {**navigation, "mappable": True, **candidates[0], "candidates": candidates,
                "reason": f"{subject}: exactly one match, preselected."}
    if candidates:
        return {**navigation, "candidates": candidates, "reason": f"{subject}: {len(candidates)} matches; choose one explicitly."}
    return {**navigation, "reason": f"{subject}: none found; no path is inferred."}
