"""Server side of the simple Mapping editor: list, create, remove and re-activate field rules.

A rule maps one field the area's approved HERO query reads to one ERPNext field.
Every change is applied immediately (owner decision 2026-11-04): the area's read
mapping is saved as a new revision and its read service is approved again in the
same transaction. The transformation is chosen automatically from the field
types; the server validates every rule like any other mapping rule.
Only ordinary field rules can be added or removed; identity, name, relation and
fixed adapter rules stay as they are (the advanced editor shows them).
"""
from __future__ import annotations

import hashlib
import json

import frappe

# Areas whose field rules are configurable here, and their ERPNext target.
EDITABLE = {"HeroCustomers": "Customer", "HeroProjectMatches": "Project"}
LABELS = {"HeroCustomers": "Customers", "HeroProjectMatches": "Projects", "HeroCalendarEvents": "Events",
          "HeroTasks": "Tasks", "HeroProducts": "Products", "HeroDocuments": "Documents", "HeroPictures": "Pictures"}
REMOVABLE_PURPOSE = "Field"
RULE_FIELDS = ["name", "definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field",
               "purpose", "transformation", "relation", "mapping_revision", "status", "schema_snapshot",
               "personal_graphql_mapping"]


class SimpleMappingError(frappe.ValidationError):
    pass


# ---------------------------------------------------------------- reading

def _read_mappings() -> dict[str, dict]:
    """Active read mapping per area: {operation: {name, config, snapshot, revision, operation_name}}."""
    result = {}
    for row in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE"},
                              fields=["name", "mapping_json", "schema_snapshot", "mapping_revision", "operation_name"],
                              order_by="modified desc", limit_page_length=0):
        try:
            config = json.loads(row.mapping_json or "{}")
        except (TypeError, ValueError):
            continue
        operation = config.get("target_operation") if isinstance(config, dict) else None
        if operation and operation not in result:
            result[operation] = {"name": row.name, "config": config, "snapshot": row.schema_snapshot,
                                 "revision": int(row.mapping_revision or 0), "operation_name": row.operation_name}
    return result


def _services(mapping_name: str) -> list:
    return frappe.get_all("HERO GraphQL Service", filters={"read_mapping": mapping_name, "status": "Validated"},
                          fields=["name", "query_document", "service_revision", "execution_status"], limit_page_length=0)


def query_paths(document: str) -> list[str]:
    """Leaf paths under the root field of a read query (``a { b }`` -> ``a.b``)."""
    from graphql import parse

    try:
        operation = parse(document).definitions[0]
        root = operation.selection_set.selections[0]
    except Exception:
        return []
    paths = []

    def walk(selection_set, prefix):
        for item in selection_set.selections:
            name = getattr(getattr(item, "name", None), "value", None)
            if not name:
                continue
            path = f"{prefix}.{name}" if prefix else name
            if item.selection_set:
                walk(item.selection_set, path)
            else:
                paths.append(path)

    if root.selection_set:
        walk(root.selection_set, "")
    return paths


def _sources(operation: str, mapping: dict) -> list[dict]:
    from hero_integration.services.direct_mapping import SOURCE_CONTRACTS

    contract = (SOURCE_CONTRACTS.get(operation) or {}).get("paths") or {}
    services = _services(mapping["name"])
    read = set(query_paths(services[0].query_document)) if services else set()
    result = []
    for path in sorted(read):
        if path not in contract:
            continue
        head, _, tail = path.rpartition(".")
        result.append({"path": path, "object": head or "", "field": tail or path, "type": contract[path]})
    return result


def _rule_rows(operation: str, mapping: dict) -> list[dict]:
    ids = list(mapping["config"].get("mapping_definition_ids") or [])
    rows = frappe.get_all("HERO Frappe Mapping", filters={"operation": operation, "status": "Active"},
                          fields=RULE_FIELDS, limit_page_length=0)
    by_id = {row.definition_id: row for row in rows}
    result = []
    for definition_id in ids:
        row = by_id.get(definition_id)
        if row:
            result.append(_row(operation, row, "Active", mapping))
    if operation in EDITABLE:
        # Removed ordinary field rules of this area stay listed as Inactive and can be activated again.
        for row in rows:
            if (row.definition_id not in ids and row.purpose == REMOVABLE_PURPOSE and not row.get("personal_graphql_mapping")
                    and row.schema_snapshot == mapping["snapshot"]):
                result.append(_row(operation, row, "Inactive", mapping))
    return result


def _row(operation: str, row, status: str, mapping: dict) -> dict:
    return {"status": status, "operation": operation, "operation_label": LABELS.get(operation, operation),
            "hero": row.source_path, "source_type": row.source_type, "app": _app(row.target_doctype),
            "target": f"{row.target_doctype}.{row.target_field}", "target_doctype": row.target_doctype,
            "target_field": row.target_field, "transformation": row.transformation, "purpose": row.purpose,
            "revision": int(row.mapping_revision or 0), "definition_id": row.definition_id,
            "removable": operation in EDITABLE and row.purpose == REMOVABLE_PURPOSE, "mapping": mapping["name"]}


def _app(doctype: str) -> str:
    from frappe.modules.utils import get_module_app

    try:
        return get_module_app(frappe.db.get_value("DocType", doctype, "module")) or ""
    except Exception:
        return ""


def state() -> dict:
    """Everything the simple editor shows: areas with sources and targets, and all rules."""
    mappings = _read_mappings()
    operations, rules = [], []
    for operation, mapping in sorted(mappings.items(), key=lambda item: LABELS.get(item[0], item[0])):
        rules.extend(_rule_rows(operation, mapping))
        editable = operation in EDITABLE
        operations.append({"operation": operation, "label": LABELS.get(operation, operation), "editable": editable,
                           "mapping": mapping["name"], "snapshot": mapping["snapshot"], "revision": mapping["revision"],
                           "target_doctype": EDITABLE.get(operation, ""),
                           "sources": _sources(operation, mapping) if editable else []})
    sends = []
    for row in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "FRAPPE_TO_MUTATION"},
                              fields=["name", "mapping_json", "mapping_revision", "operation_name"], limit_page_length=0):
        try:
            config = json.loads(row.mapping_json or "{}")
        except (TypeError, ValueError):
            continue
        items = [config, *(config.get("additional_mappings") or [])] if isinstance(config, dict) else []
        for item in items:
            source, target = item.get("source") or {}, item.get("target") or {}
            if source or target:
                sends.append({"status": "Active", "operation": row.operation_name or target.get("operation") or "",
                              "hero": f"{target.get('argument') or ''}.{target.get('field_path') or target.get('field') or ''}".strip("."),
                              "app": source.get("app") or _app(source.get("doctype") or ""),
                              "target": f"{source.get('doctype') or ''}.{source.get('field') or ''}",
                              "revision": int(row.mapping_revision or 0), "mapping": row.name})
    snapshots = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"analysis_status": "Validated"},
                               fields=["name", "creation"], order_by="creation desc", limit_page_length=20)
    return {"operations": operations, "rules": rules, "sends": sends, "snapshots": [row.name for row in snapshots]}


# ---------------------------------------------------------------- transformation

def transformation_for(source_type: str, target) -> str:
    """The transformation the editor uses for a source type and a target field."""
    kind = (source_type or "").split("|")[0]
    fieldtype = getattr(target, "fieldtype", "")
    if fieldtype == "Link":
        return "Existing Record"
    if fieldtype == "Text Editor":
        return "Plain Text To HTML"
    if fieldtype in {"Data", "Small Text", "Text", "Long Text", "Code", "Select"}:
        if kind == "string":
            return "Trim"
        if kind == "int":
            return "Positive Int To Text" if fieldtype == "Data" else "Scalar Text"
        return "Scalar Text"
    return "Identity"


# ---------------------------------------------------------------- changes

def publish_revision(operation: str, ids: list[str] | None = None, content_changes: dict | None = None,
                     query_document: str | None = None) -> dict:
    """Publish the area's read mapping as a new revision through the personal draft publisher and approve
    its read service again. ``ids`` replaces the rule list, ``content_changes`` sets contract keys and
    ``query_document`` replaces the service query (for a contract version change)."""
    from copy import deepcopy

    from hero_integration import api
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    from hero_integration.services.mapping_draft_service import draft_key

    mapping = _read_mappings().get(operation)
    if not mapping:
        raise SimpleMappingError("This area has no active read mapping.")
    parent = frappe.get_doc("HERO GraphQL Mapping", mapping["name"], for_update=True)
    actor = frappe.session.user
    storage = FrappeMappingDraftStorage()
    service = storage.service()
    key = draft_key(frappe.local.site, parent.definition_id, actor)
    if frappe.db.get_value("HERO Mapping Draft", key, "state") == "OPEN":
        old = frappe.get_doc("HERO Mapping Draft", key, for_update=True)
        if int(old.content_revision or 0):
            raise SimpleMappingError("You have an unpublished draft of this mapping; publish or discard it under Advanced first.")
        service.discard(parent.definition_id, actor, int(old.generation), int(old.content_revision), confirmed=True)
    readers = _services(parent.name)
    for reader in readers:  # publication needs a currently approved reader
        doc = frappe.get_doc("HERO GraphQL Service", reader.name)
        if doc.execution_status != "Approved" or int(doc.approved_mapping_revision or 0) != int(parent.mapping_revision or 0):
            api.approve_read_service(doc.name, parent.name, int(doc.service_revision or 0), int(parent.mapping_revision or 0))
    draft = service.open_draft(parent.definition_id, actor)
    content = deepcopy(draft.content)
    if ids is not None:
        content["mapping_definition_ids"] = list(ids)
    content.update(content_changes or {})
    saved = service.save(parent.definition_id, actor, draft.content_revision, int(parent.mapping_revision), content,
                         expected_generation=draft.generation,
                         expected_reconciliation_revision=draft.reconciliation_revision).draft
    if query_document is not None:
        for reader in readers:
            doc = frappe.get_doc("HERO GraphQL Service", reader.name)
            doc.query_document = query_document
            doc.save()
    published = storage.publish(parent.definition_id, actor, saved.generation, saved.content_revision,
                                int(parent.mapping_revision), saved.reconciliation_revision, confirmed=True)
    current = frappe.get_doc("HERO GraphQL Mapping", published["name"])
    # The helper draft is published; do not leave it open.
    if frappe.db.get_value("HERO Mapping Draft", key, "state") == "OPEN":
        left = frappe.get_doc("HERO Mapping Draft", key)
        service.discard(current.definition_id, actor, int(left.generation), int(left.content_revision), confirmed=True)
    approved = []
    for reader in readers:
        doc = frappe.get_doc("HERO GraphQL Service", reader.name)
        api.approve_read_service(doc.name, current.name, int(doc.service_revision or 0), int(current.mapping_revision or 0))
        approved.append(doc.name)
    return {"mapping": current.name, "revision": int(current.mapping_revision or 0), "approved": approved}


def _apply(operation: str, ids: list[str]) -> dict:
    return publish_revision(operation, ids)


def create(operation: str, source_path: str, target_doctype: str, target_field: str) -> dict:
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _FrappeMetadata
    from hero_integration.services.direct_mapping import DirectMappingError, MappingSpec, validate_spec

    if operation not in EDITABLE:
        raise SimpleMappingError("Rules of this area are fixed; use the advanced editor.")
    mapping = _read_mappings().get(operation)
    if not mapping:
        raise SimpleMappingError("This area has no active read mapping.")
    source = next((item for item in _sources(operation, mapping) if item["path"] == source_path), None)
    if source is None:
        raise SimpleMappingError("The HERO field is not read by this area's approved query.")
    if target_doctype != EDITABLE[operation]:
        raise SimpleMappingError(f"This area writes {EDITABLE[operation]} records.")
    field = frappe.get_meta(target_doctype).get_field(target_field)
    if field is None:
        raise SimpleMappingError("The ERPNext field does not exist.")
    spec = MappingSpec(operation, source_path, target_doctype, target_field, source["type"], "Field",
                       transformation_for(source["type"], field))
    try:
        validate_spec(spec, _FrappeMetadata())
    except DirectMappingError as error:
        raise SimpleMappingError(str(error)) from None
    ids = list(mapping["config"].get("mapping_definition_ids") or [])
    taken = frappe.get_all("HERO Frappe Mapping", filters={"definition_id": ["in", ids or [""]], "status": "Active",
                                                           "target_doctype": target_doctype, "target_field": target_field},
                           pluck="definition_id")
    if taken:
        raise SimpleMappingError("An active rule already writes this ERPNext field; remove it first.")
    definition_id = "simple-" + hashlib.sha256(f"{operation}:{source_path}:{target_doctype}:{target_field}".encode()).hexdigest()[:16]
    existing = frappe.db.get_value("HERO Frappe Mapping", {"definition_id": definition_id}, ["name", "status"], as_dict=True)
    if existing and existing.status != "Active":
        frappe.db.set_value("HERO Frappe Mapping", existing.name, "status", "Active")
    elif not existing:
        frappe.get_doc({"doctype": "HERO Frappe Mapping", "definition_id": definition_id, "operation": operation,
                        "source_path": source_path, "source_type": source["type"], "target_doctype": target_doctype,
                        "target_field": target_field, "purpose": "Field", "transformation": spec.transformation,
                        "relation": "", "priority": 5, "status": "Active",
                        "schema_snapshot": mapping["snapshot"]}).insert()
    result = _apply(operation, [*ids, definition_id] if definition_id not in ids else ids)
    return {**result, "definition_id": definition_id, "transformation": spec.transformation}


def remove(operation: str, definition_ids: list[str]) -> dict:
    mapping = _read_mappings().get(operation)
    if not mapping or operation not in EDITABLE:
        raise SimpleMappingError("Rules of this area are fixed; use the advanced editor.")
    rows = {row.definition_id: row for row in frappe.get_all(
        "HERO Frappe Mapping", filters={"definition_id": ["in", definition_ids or [""]]}, fields=RULE_FIELDS)}
    fixed = [identity for identity in definition_ids if (rows.get(identity) or {}).get("purpose") != REMOVABLE_PURPOSE]
    if fixed:
        raise SimpleMappingError("Identity, name and relation rules cannot be removed here.")
    ids = [identity for identity in mapping["config"].get("mapping_definition_ids") or [] if identity not in definition_ids]
    return _apply(operation, ids)


def activate(operation: str, definition_ids: list[str]) -> dict:
    mapping = _read_mappings().get(operation)
    if not mapping or operation not in EDITABLE:
        raise SimpleMappingError("Rules of this area are fixed; use the advanced editor.")
    inactive = {row["definition_id"] for row in _rule_rows(operation, mapping) if row["status"] == "Inactive"}
    unknown = [identity for identity in definition_ids if identity not in inactive]
    if unknown:
        raise SimpleMappingError("Only inactive field rules of this area can be activated.")
    ids = list(mapping["config"].get("mapping_definition_ids") or [])
    return _apply(operation, [*ids, *(identity for identity in definition_ids if identity not in ids)])
