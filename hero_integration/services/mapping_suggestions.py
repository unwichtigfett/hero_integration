"""Offline-safe source/target pickers and non-binding mapping suggestions."""
from __future__ import annotations

from dataclasses import dataclass

from hero_integration.services.direct_mapping import (
    ALLOWED_TARGET_TYPES,
    PROTECTED_FIELDS,
    RESERVED_TARGET_FIELDS,
    SOURCE_CONTRACTS,
    SYSTEM_FIELDS,
)


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


# These roots are part of the versioned queries in sources/hero.py.  The
# response object type is deliberately read from the saved introspection
# snapshot instead of being guessed here.
OPERATION_ROOTS = {
    "HeroCustomers": "contacts",
    "HeroProjectMatches": "project_matches",
}


def source_fields(operation: str, schema: dict[str, object], search: str = "") -> list[PickerField]:
    contract = SOURCE_CONTRACTS.get(operation)
    if not contract:
        return []
    needle = search.strip().lower()
    result = []
    root = OPERATION_ROOTS[operation]
    return_type, graphql_fields = _graphql_return_fields(schema, root)
    for path, source_type in contract["paths"].items():
        label = path.replace("_", " ").title()
        if needle and needle not in path.lower() and needle not in label.lower():
            continue
        graphql_type, nullable, relation = graphql_fields.get(path, (source_type, "null" in source_type, ""))
        result.append(PickerField(
            path, label, graphql_type, nullable, relation, True,
            "Selected by the versioned read-only import operation.",
            f"{root}.{path}", operation, return_type, source_type,
        ))
    for field in _schema_only_fields(schema, contract["paths"], return_type, root):
        if not needle or needle in field.path.lower() or needle in field.label.lower():
            result.append(field)
    return result


def target_fields(operation: str, metadata, search: str = "") -> list[PickerField]:
    contract = SOURCE_CONTRACTS.get(operation)
    if not contract:
        return []
    doctype = contract["target_doctype"]
    needle = search.strip().lower()
    result = []
    for field in metadata.fields(doctype):
        label = str(field.label or field.fieldname)
        if needle and needle not in field.fieldname.lower() and needle not in label.lower():
            continue
        protected = field.fieldname in SYSTEM_FIELDS | PROTECTED_FIELDS[doctype] | RESERVED_TARGET_FIELDS[doctype]
        is_child_relation = field.fieldtype == "Table"
        is_link = field.fieldtype == "Link"
        is_custom = bool(getattr(field, "is_custom_field", False))
        selectable = bool(field.fieldtype in ALLOWED_TARGET_TYPES and not getattr(field, "read_only", 0) and not protected and not is_link and is_custom)
        if selectable:
            reason = "Writable target field."
        elif not is_custom:
            reason = "Standard target field: it requires a reviewed app mapping, not a site-specific direct mapping."
        elif is_link:
            reason = "Link target: an approved entity resolver is required before it can be mapped."
        elif is_child_relation:
            reason = "Child-DocType relationship: a reviewed resolver is required before it can be mapped."
        else:
            reason = "Protected, technical, or unsupported target field."
        relation = str(field.options or "") if field.fieldtype in {"Link", "Table"} else ""
        result.append(PickerField(field.fieldname, label, field.fieldtype, False, relation, selectable, reason, mapping_type=field.fieldtype))
    return result


def suggest_from_source(operation: str, source_path: str, schema: dict[str, object], metadata) -> list[dict[str, object]]:
    source = next((item for item in source_fields(operation, schema) if item.path == source_path), None)
    if not source or not source.selectable:
        return []
    return _rank(source, target_fields(operation, metadata))


def suggest_from_target(operation: str, target_path: str, schema: dict[str, object], metadata) -> list[dict[str, object]]:
    target = next((item for item in target_fields(operation, metadata) if item.path == target_path), None)
    if not target or not target.selectable:
        return []
    candidates = []
    for source in source_fields(operation, schema):
        if source.selectable:
            candidates.extend(_rank(source, [target]))
    return sorted(candidates, key=lambda item: (-item["score"], item["source_path"]))


def _rank(source: PickerField, targets: list[PickerField]) -> list[dict[str, object]]:
    result = []
    source_words = set(source.path.replace("_", " ").split())
    for target in targets:
        if not target.selectable or not _types_compatible(source.mapping_type, target.fieldtype):
            continue
        target_words = set((target.path + " " + target.label).lower().replace("_", " ").split())
        overlap = len(source_words & target_words)
        score = 100 if source.path == target.path else 20 + overlap * 10
        result.append({
            "source_path": source.path,
            "source_type": source.mapping_type,
            "target_field": target.path,
            "score": score,
            "reason": "Type-compatible name and label match.",
            "requires_approval": True,
        })
    return sorted(result, key=lambda item: (-item["score"], item["target_field"]))


def _types_compatible(source_type: str, target_type: str) -> bool:
    if source_type.startswith("string"):
        return target_type in {"Data", "Small Text", "Date", "Datetime"}
    if source_type.startswith("int"):
        return target_type in {"Int", "Float", "Data"}
    return source_type.startswith("boolean") and target_type == "Check"


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
            type_name, nullable, relation = _type_details(field.get("type"))
            result.append(PickerField(
                field["name"], field["name"].replace("_", " ").title(), type_name, nullable,
                relation, False,
                "Schema-only: not selected by an approved read-only import operation.",
                f"{root}.{field['name']}" if root else field["name"], return_type=str(graphql_type.get("name", "")), mapping_type="",
            ))
    return result


def _graphql_return_fields(schema: dict[str, object], root: str) -> tuple[str, dict[str, tuple[str, bool, str]]]:
    """Read the current operation return type and scalar details from introspection."""
    schema = _schema_body(schema)
    types = schema.get("types", []) if isinstance(schema.get("types"), list) else []
    by_name = {item.get("name"): item for item in types if isinstance(item, dict) and isinstance(item.get("name"), str)}
    query = by_name.get("Query")
    if not isinstance(query, dict):
        return "", {}
    root_field = next((field for field in query.get("fields", []) if isinstance(field, dict) and field.get("name") == root), None)
    if not isinstance(root_field, dict):
        return "", {}
    return_type, _, _ = _type_details(root_field.get("type"))
    returned = by_name.get(return_type)
    if not isinstance(returned, dict):
        return return_type, {}
    details = {}
    for field in returned.get("fields", []):
        if isinstance(field, dict) and isinstance(field.get("name"), str):
            details[field["name"]] = _type_details(field.get("type"))
    return return_type, details


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
    relation = name if isinstance(current, dict) and current.get("kind") in {"OBJECT", "INTERFACE", "UNION"} else ""
    return name, not non_null, relation
