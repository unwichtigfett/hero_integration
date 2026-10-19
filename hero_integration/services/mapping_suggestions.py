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
from hero_integration.services.frappe_mapping_catalog import fields as catalog_fields
from hero_integration.services.frappe_mapping_catalog import types_compatible


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
        graphql_type, nullable, relation = graphql_fields.get(path, (source_type, "null" in source_type, ""))
        field = PickerField(
            path, label, graphql_type, nullable, relation, True,
            "Selected by the versioned read-only import operation.",
            f"{root}.{path}", operation, return_type, source_type,
        )
        if not needle or _matches(field, needle):
            result.append(field)
    for field in _schema_only_fields(schema, contract["paths"], return_type, root):
        if not needle or _matches(field, needle):
            result.append(field)
    return result


def target_fields(
    operation: str, metadata, search: str = "", app_name: str = "", target_doctype: str = "",
) -> list[PickerField]:
    contract = SOURCE_CONTRACTS.get(operation)
    if not contract:
        return []
    if hasattr(metadata, "doctypes"):
        return [
            PickerField(
                item.path, item.label, item.fieldtype, item.nullable, item.relation, item.selectable, item.reason,
                mapping_type=item.fieldtype, app_name=item.app_name, target_doctype=item.doctype,
                is_custom=item.is_custom, required=item.required, select_options=item.select_options,
                activation_allowed=item.activation_allowed, description=item.description,
            )
            for item in catalog_fields(metadata, app_name, target_doctype, search)
        ]
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
        result.append(PickerField(field.fieldname, label, field.fieldtype, False, relation, selectable, reason, mapping_type=field.fieldtype, target_doctype=doctype))
    return result


def picker_page(fields: list[PickerField], start: int = 0, page_size: int = 50) -> dict[str, object]:
    """Bound a picker response without discarding its searchable result set."""
    start = max(0, int(start))
    page_size = min(50, max(1, int(page_size)))
    items = fields[start:start + page_size]
    return {
        "items": [item.__dict__ for item in items],
        "start": start,
        "page_size": page_size,
        "total": len(fields),
        "has_more": start + len(items) < len(fields),
    }


def suggest_from_source(
    operation: str, source_path: str, schema: dict[str, object], metadata, target_doctype: str = "",
) -> list[dict[str, object]]:
    source = next((item for item in source_fields(operation, schema) if item.path == source_path), None)
    if not source or not source.selectable:
        return []
    return _rank(source, target_fields(operation, metadata, target_doctype=target_doctype))


def suggest_from_target(
    operation: str, target_path: str, schema: dict[str, object], metadata, target_doctype: str = "",
) -> list[dict[str, object]]:
    target = next((item for item in target_fields(operation, metadata, target_doctype=target_doctype) if item.path == target_path), None)
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
        if not target.selectable or not types_compatible(source.mapping_type, target.fieldtype):
            continue
        target_words = set((target.path + " " + target.label).lower().replace("_", " ").split())
        overlap = len(source_words & target_words)
        score = 100 if source.path == target.path else 20 + overlap * 10
        if target.is_custom:
            score += 5
        if target.required:
            score -= 10
        result.append({
            "source_path": source.path,
            "source_type": source.mapping_type,
            "target_field": target.path,
            "target_doctype": target.target_doctype,
            "target_app": target.app_name,
            "score": score,
            "confidence": "high" if score >= 70 else "medium" if score >= 40 else "low",
            "reason": _suggestion_reason(source, target),
            "requires_approval": True,
            "draft_only": not target.activation_allowed,
        })
    return sorted(result, key=lambda item: (-item["score"], item["target_field"]))


def _matches(field: PickerField, needle: str) -> bool:
    return needle in " ".join((
        field.path, field.label, field.fieldtype, field.relation, field.graphql_path,
        field.return_type, field.mapping_type, field.description,
    )).lower()


def _suggestion_reason(source: PickerField, target: PickerField) -> str:
    parts = ["Type-compatible name and label match."]
    if target.relation:
        parts.append(f"Relation: {target.relation}.")
    if target.select_options:
        parts.append("Select values require validation before activation.")
    if target.required:
        parts.append("Required target field; target creation needs an app-owned contract.")
    if not target.activation_allowed:
        parts.append("Draft only for this non-canonical app target.")
    return " ".join(parts)


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
