"""Read-only projections for the preserved HERO workspace client.

The browser client is deliberately kept compatible with the historic workspace
shape, while this module only projects snapshots already stored in Frappe.  It
does not know an endpoint, credential, or transport implementation.
"""
from __future__ import annotations

from typing import Any

from hero_integration.services.graphql_graph import GraphView, analyze_snapshot, normalize_schema


BUILTIN_SCALARS = frozenset({"Int", "String", "ID", "Boolean", "Float"})


def schema_projection(snapshot: object, *, search: str = "", kind: str = "") -> dict[str, Any]:
    """Return complete, saved introspection data in the workspace wire shape."""
    schema = normalize_schema(snapshot)
    graph = analyze_snapshot(snapshot)
    needle = search.strip().lower()
    types = [item for item in schema.get("types", []) if isinstance(item, dict)]
    if kind:
        types = [item for item in types if item.get("kind") == kind]
    if needle:
        types = [item for item in types if needle in _searchable_type(item).lower()]
    roots = {
        name: _named_root(schema.get(key))
        for name, key in (("query", "queryType"), ("mutation", "mutationType"), ("subscription", "subscriptionType"))
    }
    return {
        "fingerprint": graph.fingerprint,
        "types": types,
        "roots": {name: value for name, value in roots.items() if value},
        "directives": [item for item in schema.get("directives", []) if isinstance(item, dict)],
        "counts": graph_counts(graph),
    }


def graph_projection(snapshot: object) -> dict[str, Any]:
    """Project every saved graph node and edge; pagination is applied by API only."""
    graph = analyze_snapshot(snapshot)
    nodes = [
        {**node, "node_id": node["id"], "name": node["label"]}
        for node in graph.nodes
    ]
    edges = [
        {**edge, "source_id": edge["source"], "target_id": edge["target"]}
        for edge in graph.edges
    ]
    return {"fingerprint": graph.fingerprint, "nodes": nodes, "edges": edges, "coverage": graph_counts(graph)}


def graph_counts(graph: GraphView) -> dict[str, int]:
    """Derive viewer statistics from the actual complete graph model."""
    return {
        "types": sum(node["id"].startswith("type:") for node in graph.nodes),
        "queries": sum(node["kind"] == "QUERY_OPERATION" for node in graph.nodes),
        "mutations": sum(node["kind"] == "MUTATION_OPERATION" for node in graph.nodes),
        "fields": sum(node["kind"] in {"FIELD", "INPUT_FIELD"} for node in graph.nodes),
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
    }


def node_projection(snapshot: object, node_id: str) -> dict[str, Any]:
    """Return a node and its direct navigable relations from saved schema data."""
    graph = graph_projection(snapshot)
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    node = nodes.get(node_id)
    if not node:
        raise ValueError("The requested graph element does not exist in this saved snapshot.")
    incoming = [edge for edge in graph["edges"] if edge["target_id"] == node_id]
    outgoing = [edge for edge in graph["edges"] if edge["source_id"] == node_id]
    return {"node": node, "incoming": incoming, "outgoing": outgoing, "coverage": graph["coverage"]}


def _named_root(value: object) -> str | None:
    return value.get("name") if isinstance(value, dict) and isinstance(value.get("name"), str) else None


def _searchable_type(item: dict[str, Any]) -> str:
    values = [str(item.get("name") or ""), str(item.get("description") or "")]
    for field in [*item.get("fields", []), *item.get("inputFields", []), *item.get("enumValues", [])]:
        if isinstance(field, dict):
            values.append(str(field.get("name") or ""))
    return " ".join(values)
