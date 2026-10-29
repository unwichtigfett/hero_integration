"""A new schema snapshot inherits the active mappings of its parent as personal drafts.

Parent: the previous snapshot of the same HERO source instance and endpoint.
Every active parent definition becomes a new, parentless personal draft of the
acting administrator, bound to the child snapshot and validated against the
child schema by the existing draft service.  Nothing is activated, no service
or runtime approval is copied, and the parent stays unchanged.  Definitions
that no longer fit the child schema are recorded as Incompatible with a reason.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import frappe

SNAPSHOT_DOCTYPE = "HERO GraphQL Schema Snapshot"
INHERITANCE_DOCTYPE = "HERO Snapshot Mapping Inheritance"
DIRECTIONS = ("QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION")
MUTATION_CONTENT_KEYS = {"version", "source", "target", "transformation", "additional_mappings"}


class InheritanceIncompatible(ValueError):
    """The parent definition cannot be carried into the child snapshot as is."""


def parent_for(source_instance: str, endpoint_fingerprint: str | None, *, before: str | None = None,
               exclude: str | None = None) -> str | None:
    """The most recently created snapshot of the same source and endpoint (optionally created before a time)."""
    filters: dict[str, object] = {"source_instance": source_instance,
                                  "endpoint_fingerprint": endpoint_fingerprint if endpoint_fingerprint else ["is", "not set"]}
    if before:
        filters["creation"] = ["<", before]
    if exclude:
        filters["name"] = ["!=", exclude]
    rows = frappe.get_all(SNAPSHOT_DOCTYPE, filters=filters, fields=["name"], order_by="creation desc, name desc", limit_page_length=1)
    return rows[0]["name"] if rows else None


def inherit_parent_mappings(child_snapshot: str, actor: str) -> dict[str, object]:
    """Create personal drafts on the child for every active parent definition; idempotent per definition."""
    from hero_integration.api import _validated_snapshot

    child, child_schema = _validated_snapshot(child_snapshot)
    parent_name = getattr(child, "parent_snapshot", None)
    if not parent_name:
        return _summary(child.name, None, [], "NO_PARENT")
    parent, parent_schema = _validated_snapshot(parent_name)
    if parent.source_instance != child.source_instance:
        return _summary(child.name, parent.name, [], "PARENT_SOURCE_MISMATCH")
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage

    service = FrappeMappingDraftStorage().service()
    own = {(row["direction"], row["operation_name"]) for row in frappe.get_all(
        "HERO GraphQL Mapping", filters={"schema_snapshot": child.name, "status": "Active"}, fields=["direction", "operation_name"])}
    created = {row["parent_definition"] for row in frappe.get_all(
        INHERITANCE_DOCTYPE, filters={"child_snapshot": child.name, "status": "Draft Created"}, fields=["parent_definition"])}
    results = []
    for row in frappe.get_all("HERO GraphQL Mapping", filters={"schema_snapshot": parent.name, "status": "Active"},
                              fields=["name", "definition_id", "direction", "operation_kind", "operation_name", "mapping_json"],
                              order_by="operation_name asc, name asc"):
        if row["direction"] not in DIRECTIONS:
            continue
        if row["definition_id"] in created:
            results.append({"operation": row["operation_name"], "status": "Draft Created", "reason": "Already inherited earlier."})
            continue
        if (row["direction"], row["operation_name"]) in own:
            results.append(_record(child, parent, row, actor, "Skipped", "The child snapshot has its own active mapping for this operation."))
            continue
        results.append(_inherit_one(service, child, child_schema, parent, parent_schema, row, actor))
    return _summary(child.name, parent.name, results, "COMPLETED")


def _inherit_one(service, child, child_schema, parent, parent_schema, row, actor) -> dict[str, object]:
    from hero_integration.services.mapping_draft_service import MappingDraftError

    savepoint = "hero_inherit_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        content = child_content(row, parent.name, child_schema, parent_schema)
        definition = {
            "site_scope": frappe.local.site, "definition_id": str(uuid4()), "source_instance": child.source_instance,
            "snapshot_name": child.name, "schema_fingerprint": child.schema_fingerprint, "direction": row["direction"],
            "operation_kind": row["operation_kind"], "operation_name": row["operation_name"],
        }
        service.open_new_draft(definition, actor, content)
    except (InheritanceIncompatible, MappingDraftError, ValueError, TypeError, KeyError, PermissionError) as error:
        frappe.db.rollback(save_point=savepoint)
        return _record(child, parent, row, actor, "Incompatible", _reason(error))
    return _record(child, parent, row, actor, "Draft Created",
                   "Personal draft created for the child snapshot; review and activate it explicitly.", definition["definition_id"])


def child_content(row, parent_snapshot: str, child_schema, parent_schema) -> dict[str, object]:
    """Copy one active parent definition into new-draft content with fresh rule and assignment identities."""
    from hero_integration.services.personal_mapping_content import RULE_FIELDS, references, unique_json_object

    try:
        content = json.loads(row["mapping_json"], object_pairs_hook=unique_json_object)
    except (TypeError, ValueError) as error:
        raise InheritanceIncompatible("The parent mapping is not valid JSON.") from error
    if not isinstance(content, dict):
        raise InheritanceIncompatible("The parent mapping must be an object.")
    # Imported or inherited content never carries authority or local provenance.
    for key in ("native_task_onboarding", "field_rule_bases", "_portable_exchange_schema"):
        content.pop(key, None)
    if row["direction"] == "FRAPPE_TO_MUTATION":
        unknown = set(content) - MUTATION_CONTENT_KEYS
        if unknown:
            raise InheritanceIncompatible("The parent mutation mapping contains configuration a new draft cannot hold: "
                                          + ", ".join(sorted(unknown)) + ".")
        if content.get("additional_mappings"):
            content["additional_mappings"] = [{**item, "id": str(uuid4())} for item in content["additional_mappings"]]
        return content
    if content.get("additional_mappings"):
        raise InheritanceIncompatible("Additional query assignments need an active definition; recreate them in the child draft.")
    old_ids = references(content)
    if not old_ids:
        raise InheritanceIncompatible("The parent query mapping has no field rules.")
    renamed, rules = {}, {}
    for old in old_ids:
        found = frappe.get_all("HERO Frappe Mapping", filters={"definition_id": old, "status": "Active", "schema_snapshot": parent_snapshot},
                               fields=sorted(RULE_FIELDS), limit_page_length=2)
        if len(found) != 1:
            raise InheritanceIncompatible("A referenced field rule is not a unique active rule of the parent snapshot.")
        new = str(uuid4())
        renamed[old] = new
        rules[new] = {key: found[0][key] for key in sorted(RULE_FIELDS) if found[0].get(key) is not None}
    content["mapping_definition_ids"] = [renamed[old] for old in old_ids]
    content["field_rules"] = rules
    if isinstance(content.get("field_contexts"), dict):
        content["field_contexts"] = {renamed.get(key, key): value for key, value in content["field_contexts"].items()}
    # The parent definition was valid on the parent schema; it stays valid when every schema element it
    # uses (record path, identity path, each rule source path) is unchanged in the child schema.
    record_path = content.get("records_path", content.get("record_path"))
    paths = [("records", None, record_path)]
    paths += [("identity", record_path, content.get("identity_path"))]
    paths += [("field rule", record_path, rule.get("source_path")) for rule in rules.values()]
    _assert_unchanged(paths, parent_schema, child_schema)
    return content


def _assert_unchanged(paths, parent_schema, child_schema) -> None:
    parent_index, child_index = _type_index(parent_schema), _type_index(child_schema)
    for label, base, path in paths:
        if not isinstance(path, str) or not path:
            continue
        before = _signature(parent_index, base, path)
        after = _signature(child_index, base, path)
        if before != after:
            raise InheritanceIncompatible(f"The {label} path {path!r} changed or is absent in the child schema.")


def _type_index(schema) -> tuple[dict[str, dict], str | None]:
    from hero_integration.services.graphql_graph import normalize_schema

    normalized = normalize_schema(schema)
    return {item.get("name"): item for item in normalized.get("types", [])}, (normalized.get("queryType") or {}).get("name")


def _type_text(ref) -> str:
    kind = (ref or {}).get("kind")
    if kind in {"LIST", "NON_NULL"}:
        inner = _type_text((ref or {}).get("ofType"))
        return f"[{inner}]" if kind == "LIST" else f"{inner}!"
    return f"{kind}:{(ref or {}).get('name')}"


def _signature(index, base_path, path) -> tuple | None:
    """Field-by-field type signature of a path, starting at the root query type (or at the record type)."""
    types, root = index
    current = root
    if base_path:
        resolved = _walk(types, root, base_path)
        if resolved is None:
            return None
        current = resolved[1]
    resolved = _walk(types, current, path)
    return resolved[0] if resolved else None


def _walk(types, start, path):
    current, signature = start, []
    for part in path.split("."):
        fields = (types.get(current) or {}).get("fields") or []
        field = next((entry for entry in fields if entry.get("name") == part), None)
        if field is None:
            return None
        ref = field.get("type") or {}
        signature.append((part, _type_text(ref)))
        while ref.get("kind") in {"LIST", "NON_NULL"}:
            ref = ref.get("ofType") or {}
        current = ref.get("name")
    return tuple(signature), current


def _record(child, parent, row, actor, status, reason, child_definition=None) -> dict[str, object]:
    key = hashlib.sha256(f"{child.name}\0{row['definition_id']}".encode()).hexdigest()
    values = {"child_snapshot": child.name, "parent_snapshot": parent.name, "direction": row["direction"],
              "operation_name": row["operation_name"], "parent_mapping": row["name"], "parent_definition": row["definition_id"],
              "child_definition": child_definition or "", "status": status, "reason": reason[:500], "actor": actor}
    if frappe.db.exists(INHERITANCE_DOCTYPE, key):
        doc = frappe.get_doc(INHERITANCE_DOCTYPE, key)
        doc.update(values)
        doc.save(ignore_permissions=True)
    else:
        frappe.get_doc({"doctype": INHERITANCE_DOCTYPE, "inheritance_key": key, **values}).insert(ignore_permissions=True)
    return {"operation": row["operation_name"], "direction": row["direction"], "status": status, "reason": reason[:500],
            "child_definition": child_definition or ""}


def _reason(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text.splitlines()[0][:500]


def _summary(child, parent, results, status) -> dict[str, object]:
    counts = {label: sum(item["status"] == label for item in results) for label in ("Draft Created", "Incompatible", "Skipped")}
    return {"status": status, "child_snapshot": child, "parent_snapshot": parent, "counts": counts, "results": results,
            "activation": False}
