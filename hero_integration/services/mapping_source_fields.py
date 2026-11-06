"""Schema field discovery shared by mapping import and publication."""
from __future__ import annotations

from dataclasses import dataclass

from hero_integration.services.direct_mapping import SOURCE_CONTRACTS
from hero_integration.services.graphql_graph import normalize_schema
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS


@dataclass(frozen=True)
class PickerField:
    path: str
    label: str
    fieldtype: str
    nullable: bool
    relation: str
    selectable: bool
    reason: str
    graphql_path: str = ""
    operation_name: str = ""
    return_type: str = ""
    mapping_type: str = ""
    app_name: str = ""
    target_doctype: str = ""
    is_custom: bool = False
    required: bool = False
    select_options: tuple[str, ...] = ()
    activation_allowed: bool = True
    description: str = ""


# These roots are part of the versioned queries in sources/hero.py.  The
# response object type is deliberately read from the saved introspection
# snapshot instead of being guessed here.
OPERATION_ROOTS = {
    "HeroCustomers": "contacts",
    "HeroProjectMatches": "project_matches",
}
OPERATION_ROOTS.update({name: value['root'] for name, value in PROJECTIONS.items()})
MAX_SOURCE_RELATION_DEPTH = 4


def source_fields(operation: str, schema: dict[str, object], search: str = "") -> list[PickerField]:
    contract = SOURCE_CONTRACTS.get(operation)
    schema = normalize_schema(schema)
    needle = search.strip().lower()
    if not contract:
        return [field for field in _operation_schema_fields(schema, operation) if not needle or _matches(field, needle)]
    result = []
    root = OPERATION_ROOTS[operation]
    return_type, graphql_fields = _graphql_contract_fields(schema, root, contract["paths"])
    for path, source_type in contract["paths"].items():
        label = path.replace("_", " ").title()
        details = graphql_fields.get(path)
        if details is None:
            continue
        graphql_type, nullable, relation, graphql_path = details
        field = PickerField(
            path, label, graphql_type, nullable, relation, True,
            "Selected by the versioned read-only import operation.",
            graphql_path, operation, return_type, source_type,
        )
        if not needle or _matches(field, needle):
            result.append(field)
    for field in _schema_only_fields(schema, contract["paths"], return_type, root):
        if not needle or _matches(field, needle):
            result.append(field)
    return result












def _matches(field: PickerField, needle: str) -> bool:
    return needle in " ".join((
        field.path, field.label, field.fieldtype, field.relation, field.graphql_path,
        field.return_type, field.mapping_type, field.description,
    )).lower()




def _schema_only_fields(
    schema: dict[str, object], selected: dict[str, str], return_type: str = "", root: str = "",
) -> list[PickerField]:
    schema = _schema_body(schema)
    result = []
    types = schema.get("types", []) if isinstance(schema.get("types"), list) else []
    # An operation picker must not present unrelated schema types as importable
    # paths. The fallback supports deliberately minimal test snapshots.
    candidates = [item for item in types if isinstance(item, dict) and item.get("name") == return_type] or types
    for graphql_type in candidates:
        if not isinstance(graphql_type, dict):
            continue
        for field in graphql_type.get("fields", []) if isinstance(graphql_type.get("fields"), list) else []:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str) or field["name"] in selected:
                continue
            type_name, nullable, kind = _type_details(field.get("type"))
            relation = type_name if kind in {"OBJECT", "INTERFACE", "UNION"} else ""
            result.append(PickerField(
                field["name"], field["name"].replace("_", " ").title(), type_name, nullable,
                relation, False,
                "Schema-only: not selected by an approved read-only import operation.",
                f"{root}.{field['name']}" if root else field["name"], return_type=str(graphql_type.get("name", "")), mapping_type="",
            ))
    return result


def _operation_schema_fields(schema: dict[str, object], operation: str) -> list[PickerField]:
    """Show exact saved query fields when no import contract exists, fail-closed."""
    schema = _schema_body(schema)
    types = {
        item.get("name"): item for item in schema.get("types", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    root = types.get(_operation_root_name(schema, "queryType", "Query"))
    if not isinstance(root, dict):
        return []
    operation_field = next((item for item in root.get("fields", []) if isinstance(item, dict) and item.get("name") == operation), None)
    if not isinstance(operation_field, dict):
        return []
    return_type, nullable, kind = _type_details(operation_field.get("type"))
    is_collection = _is_list(operation_field.get("type"))
    result: list[PickerField] = []
    if kind not in {"OBJECT", "INTERFACE", "UNION"} or return_type not in types:
        return [PickerField(
            operation, operation, return_type, nullable, "", False,
            "Schema-only query return: an approved read-only import contract is required before activation.",
            operation, operation, return_type,
        )]
    _walk_operation_fields(types, types[return_type], operation, "", return_type, is_collection, set(), result)
    return result


def _walk_operation_fields(
    types: dict[object, dict[str, object]], current: dict[str, object], root: str, path: str,
    return_type: str, collection: bool, visited: set[str], result: list[PickerField], depth: int = 0,
) -> None:
    name = str(current.get("name") or "")
    if depth >= MAX_SOURCE_RELATION_DEPTH or name in visited:
        return
    for item in current.get("fields", []) if isinstance(current.get("fields"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        type_name, nullable, kind = _type_details(item.get("type"))
        item_path = f"{path}.{item['name']}" if path else item["name"]
        graphql_path = f"{root}{'[]' if collection else ''}.{item_path}"
        item_collection = collection or _is_list(item.get("type"))
        reason = (
            "Collection return: an approved collection identity and read-only import contract are required before activation."
            if item_collection else "Schema-only query field: an approved read-only import contract is required before activation."
        )
        relation = type_name if kind in {"OBJECT", "INTERFACE", "UNION"} else ""
        result.append(PickerField(item_path, item["name"].replace("_", " ").title(), type_name, nullable, relation, False, reason, graphql_path, root, return_type))
        nested = types.get(type_name)
        if isinstance(nested, dict) and kind in {"OBJECT", "INTERFACE", "UNION"} and not _is_list(item.get("type")):
            _walk_operation_fields(types, nested, root, item_path, return_type, item_collection, visited | {name}, result, depth + 1)


def _is_list(reference: object) -> bool:
    current = reference
    while isinstance(current, dict):
        if current.get("kind") == "LIST":
            return True
        current = current.get("ofType")
    return False


def _graphql_contract_fields(
    schema: dict[str, object], root: str, selected_paths: dict[str, str],
) -> tuple[str, dict[str, tuple[str, bool, str, str]]]:
    """Resolve bounded, query-only selected source paths from saved introspection."""
    schema = normalize_schema(schema)
    types = schema.get("types", []) if isinstance(schema.get("types"), list) else []
    by_name = {item.get("name"): item for item in types if isinstance(item, dict) and isinstance(item.get("name"), str)}
    query_name = _operation_root_name(schema, "queryType", "Query")
    query = by_name.get(query_name)
    if not isinstance(query, dict):
        return "", {}
    root_field = next((field for field in query.get("fields", []) if isinstance(field, dict) and field.get("name") == root), None)
    if not isinstance(root_field, dict):
        return "", {}
    return_type, _, return_kind = _type_details(root_field.get("type"))
    if return_kind not in {"OBJECT", "INTERFACE", "UNION"} or return_type not in by_name:
        return return_type, {}
    return return_type, {
        path: details
        for path in selected_paths
        if (details := _resolve_selected_path(by_name, return_type, root, path)) is not None
    }


def _operation_root_name(schema: dict[str, object], key: str, fallback: str) -> str:
    """Use the declared query root, while retaining standard minimal-fixture support."""
    root = schema.get(key)
    if isinstance(root, dict) and isinstance(root.get("name"), str) and root["name"]:
        return root["name"]
    return fallback


def _resolve_selected_path(
    types: dict[object, dict[str, object]], root_type: str, root: str, path: str,
) -> tuple[str, bool, str, str] | None:
    """Resolve one finite response path without entering mutation inputs or cycles."""
    segments = tuple(segment for segment in path.split(".") if segment)
    if not segments or len(segments) > MAX_SOURCE_RELATION_DEPTH:
        return None
    current_type = root_type
    visited = {root_type}
    relations = []
    for index, segment in enumerate(segments):
        current = types.get(current_type)
        if not isinstance(current, dict):
            return None
        field = next(
            (item for item in current.get("fields", []) if isinstance(item, dict) and item.get("name") == segment),
            None,
        )
        if not isinstance(field, dict):
            return None
        type_name, nullable, kind = _type_details(field.get("type"))
        if index == len(segments) - 1:
            if kind in {"OBJECT", "INTERFACE", "UNION", "INPUT_OBJECT"}:
                return None
            return type_name, nullable, " / ".join(relations), f"{root}.{path}"
        if kind not in {"OBJECT", "INTERFACE", "UNION"} or type_name in visited:
            return None
        relations.append(f"{current_type}.{segment} → {type_name}")
        visited.add(type_name)
        current_type = type_name
    return None


def _schema_body(schema: dict[str, object]) -> dict[str, object]:
    nested = schema.get("__schema") or schema.get("schema")
    return nested if isinstance(nested, dict) else schema


def _type_details(type_ref: object) -> tuple[str, bool, str]:
    if not isinstance(type_ref, dict):
        return "unverified", True, ""
    non_null = type_ref.get("kind") == "NON_NULL"
    current = type_ref
    while isinstance(current, dict) and current.get("ofType"):
        current = current["ofType"]
    name = str(current.get("name") or current.get("kind") or "unverified") if isinstance(current, dict) else "unverified"
    kind = str(current.get("kind") or "") if isinstance(current, dict) else ""
    return name, not non_null, kind
