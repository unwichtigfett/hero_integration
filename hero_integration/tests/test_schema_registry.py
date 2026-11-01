from __future__ import annotations

import copy

import pytest

from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import StoredSnapshot
from hero_integration.services.schema_registry import SchemaRegistryError, build_schema_registry


def _named(kind: str, name: str) -> dict[str, object]:
    return {"kind": kind, "name": name}


def _non_null(value: dict[str, object]) -> dict[str, object]:
    return {"kind": "NON_NULL", "ofType": value}


def _list(value: dict[str, object]) -> dict[str, object]:
    return {"kind": "LIST", "ofType": value}


def _schema() -> dict[str, object]:
    return {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "case", "args": [{"name": "id", "type": _non_null(_named("SCALAR", "ID"))}, {"name": "includeArchived", "type": _named("SCALAR", "Boolean")}], "type": _non_null(_named("OBJECT", "Case"))},
            {"name": "search", "args": [{"name": "filter", "type": _non_null(_named("INPUT_OBJECT", "SearchInput"))}], "type": _list(_named("UNION", "SearchResult"))},
        ]},
        {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "updateCase", "args": [{"name": "input", "type": _non_null(_named("INPUT_OBJECT", "UpdateCaseInput"))}, {"name": "dryRun", "type": _named("SCALAR", "Boolean")}], "type": _named("OBJECT", "Case")} ]},
        {"kind": "OBJECT", "name": "Case", "interfaces": [_named("INTERFACE", "Node")], "fields": [{"name": "id", "type": _non_null(_named("SCALAR", "ID"))}, {"name": "title", "type": _named("SCALAR", "String"), "isDeprecated": True, "deprecationReason": "Use subject."}, {"name": "children", "type": _list(_non_null(_named("OBJECT", "Case")))}]},
        {"kind": "OBJECT", "name": "Project", "fields": [{"name": "id", "type": _non_null(_named("SCALAR", "ID"))}]},
        {"kind": "INTERFACE", "name": "Node", "possibleTypes": [_named("OBJECT", "Case"), _named("OBJECT", "Project")], "fields": [{"name": "id", "type": _non_null(_named("SCALAR", "ID"))}]},
        {"kind": "UNION", "name": "SearchResult", "possibleTypes": [_named("OBJECT", "Case"), _named("OBJECT", "Project")]},
        {"kind": "INPUT_OBJECT", "name": "SearchInput", "inputFields": [{"name": "term", "type": _named("SCALAR", "String")}, {"name": "options", "type": _named("INPUT_OBJECT", "SearchOptions")}]},
        {"kind": "INPUT_OBJECT", "name": "SearchOptions", "inputFields": [{"name": "status", "type": _named("ENUM", "Status")}]},
        {"kind": "INPUT_OBJECT", "name": "UpdateCaseInput", "inputFields": [{"name": "status", "type": _non_null(_named("ENUM", "Status"))}, {"name": "child", "type": _named("INPUT_OBJECT", "UpdateCaseInput")}, {"name": "options", "type": _named("INPUT_OBJECT", "SearchOptions")}]},
        {"kind": "ENUM", "name": "Status", "enumValues": [{"name": "OPEN"}, {"name": "CLOSED", "isDeprecated": True, "deprecationReason": "Legacy."}]},
        *({"kind": "SCALAR", "name": name} for name in ("ID", "String", "Boolean")),
    ]}}


def _stored(schema: dict[str, object] | None = None, *, fingerprint: str | None = None) -> StoredSnapshot:
    raw = schema or _schema()
    return StoredSnapshot("snapshot-1", raw, fingerprint or analyze_snapshot(raw).fingerprint, "hero-primary")


def test_registry_discovers_query_mutation_roots_and_authority_is_discovery_only():
    registry = build_schema_registry(_stored())
    assert tuple(item.name for item in registry.query_operations) == ("case", "search")
    assert tuple(item.name for item in registry.mutation_operations) == ("updateCase",)
    assert registry.execution_authority == "DISCOVERED_ONLY"
    assert not any(item.output_paths_truncated or item.input_paths_truncated for item in (*registry.query_operations, *registry.mutation_operations))
    query_arguments = {item.name: item for item in registry.query_operations[0].arguments}
    assert query_arguments["id"].reference_id == "hero.query.case.argument.id"
    assert query_arguments["id"].type_ref.required
    assert not query_arguments["includeArchived"].type_ref.required
    mutation_arguments = {item.name: item for item in registry.mutation_operations[0].arguments}
    assert mutation_arguments["input"].reference_id == "hero.mutation.updateCase.input"


def test_registry_catalogs_nested_outputs_inputs_wrappers_deprecation_interfaces_and_unions():
    registry = build_schema_registry(_stored())
    query, mutation = registry.operation("QUERY", "case"), registry.operation("MUTATION", "updateCase")
    assert query.return_type.graphql_type == "Case!"
    assert query.return_type.wrappers == ("NON_NULL",)
    assert query.source_fingerprint == registry.fingerprint
    assert "hero.query.case.id" in {item.reference_id for item in query.output_fields}
    assert all(item.operation_kind == "QUERY" and item.source_fingerprint == registry.fingerprint for item in query.output_fields)
    assert "hero.query.case.children" in {item.reference_id for item in query.output_fields}
    assert not any(item.reference_id.endswith("children.children") for item in query.output_fields)
    title = next(item for item in query.output_fields if item.reference_id == "hero.query.case.title")
    assert title.deprecated and title.deprecation_reason == "Use subject."
    assert "hero.mutation.updateCase.input.status" in {item.reference_id for item in mutation.input_fields}
    assert all(item.operation_kind == "MUTATION" and item.source_fingerprint == registry.fingerprint for item in mutation.input_fields)
    assert "hero.mutation.updateCase.input.child" in {item.reference_id for item in mutation.input_fields}
    assert "hero.mutation.updateCase.input.options.status" in {item.reference_id for item in mutation.input_fields}
    assert not any(item.reference_id.endswith("child.child") for item in mutation.input_fields)
    assert "hero.mutation.updateCase.result.id" in {item.reference_id for item in mutation.output_fields}
    assert registry.type("Node").possible_types == ("Case", "Project")
    assert registry.type("SearchResult").possible_types == ("Case", "Project")
    assert registry.type("Status").enum_values == ("CLOSED", "OPEN")
    assert registry.operation("QUERY", "search").return_type.graphql_type == "[SearchResult]"
    assert registry.operation("QUERY", "search").return_type.list_depth == 1


def test_depth_limit_is_separate_from_path_count_truncation_flag():
    schema = _schema()
    types = schema["__schema"]["types"]
    case = next(item for item in types if item["name"] == "Case")
    case["fields"].append({"name": "deep", "type": _named("OBJECT", "Level1")})
    for index in range(1, 4):
        types.append({"kind": "OBJECT", "name": f"Level{index}", "fields": [
            {"name": "child", "type": _named("OBJECT", f"Level{index + 1}")}
        ]})
    types.append({"kind": "OBJECT", "name": "Level4", "fields": [
        {"name": "value", "type": _named("SCALAR", "String")}
    ]})
    registry = build_schema_registry(_stored(schema))
    operation = registry.operation("QUERY", "case")
    deep_paths = {field.reference_id for field in operation.output_fields if ".deep" in field.reference_id}
    assert "hero.query.case.deep.child.child" in deep_paths
    assert "hero.query.case.deep.child.child.child" not in deep_paths
    assert registry.max_reference_path_depth == 3
    assert not operation.output_paths_truncated


def test_registry_is_deterministic_and_rejects_snapshot_and_schema_mismatches():
    first, second = build_schema_registry(_stored()), build_schema_registry(_stored())
    assert first == second
    assert first.fingerprint == analyze_snapshot(_schema()).fingerprint
    with pytest.raises(SchemaRegistryError, match="fingerprint"):
        build_schema_registry(_stored(fingerprint="0" * 64))
    malformed = copy.deepcopy(_schema())
    malformed["__schema"]["types"][0]["fields"][0]["type"] = _named("OBJECT", "Missing")
    with pytest.raises(SchemaRegistryError, match="unknown type"):
        build_schema_registry(_stored(malformed))


def test_registry_rejects_duplicate_members_and_has_no_frappe_or_transport_dependency():
    duplicate = copy.deepcopy(_schema())
    duplicate["__schema"]["types"][0]["fields"].append(copy.deepcopy(duplicate["__schema"]["types"][0]["fields"][0]))
    with pytest.raises(SchemaRegistryError, match="duplicate names"):
        build_schema_registry(_stored(duplicate))

    import ast
    import inspect
    from hero_integration.services import schema_registry

    imported = {node.names[0].name.split(".")[0] for node in ast.walk(ast.parse(inspect.getsource(schema_registry))) if isinstance(node, (ast.Import, ast.ImportFrom)) and node.names}
    assert "frappe" not in imported
    assert "requests" not in imported
    assert "hero_integration.client" not in inspect.getsource(schema_registry)
