"""Move HERO values that duplicate a native ERPNext field into that field (one-time, idempotent).

- Customer: HERO ``type`` (private/commercial) sets the native Customer Type; the
  extra Customer Type and Is Contact Person fields go.
- Project: HERO partner notes live in the native Notes field.
- Event: HERO colour is the native Color; "done" is the native status Completed.

Rules are revised through the normal draft and publish path, so each change is a
new mapping revision. The read service approval then needs a new approval in
the UI. A redundant field is hidden once no active mapping writes it; its values
were copied into the native field first.
"""
from __future__ import annotations

import json
from uuid import uuid4

import frappe

# operation -> [(match, change)]; change None removes the rule.
RULE_CHANGES = {
    "HeroCustomers": [
        ({"target_doctype": "Customer", "target_field": "custom_hero_customer_type"}, None),
        ({"target_doctype": "Customer", "target_field": "custom_hero_is_contact_person"}, None),
        ({"purpose": "Customer Type", "source_path": "company_name"},
         {"source_path": "type", "source_type": "string|null", "transformation": "Customer Type From HERO Type"}),
    ],
    "HeroProjectMatches": [
        ({"source_path": "partner_notes", "target_field": "custom_hero_partner_notes"},
         {"target_field": "notes", "transformation": "Plain Text To HTML"}),
    ],
    "HeroCalendarEvents": [
        ({"source_path": "color", "target_field": "custom_hero_color"}, {"target_field": "color"}),
        ({"source_path": "is_done", "target_field": "custom_hero_is_done"},
         {"target_field": "status", "purpose": "Native Binding", "transformation": "Native Adapter"}),
    ],
}
# Fields that duplicate a native field (or nothing writes any more); hidden after the move.
REDUNDANT_FIELDS = (
    ("Customer", "custom_hero_customer_type"),
    ("Customer", "custom_hero_is_contact_person"),
    ("Project", "custom_hero_partner_notes"),
    ("Project", "custom_hero_project_type_copy"),
    ("Event", "custom_hero_color"),
    ("Event", "custom_hero_is_done"),
)


def revise_content(content: dict, operation: str) -> list[str]:
    """Apply ``RULE_CHANGES`` to draft content in place; returns what changed."""
    changes, rules = [], content.get("field_rules") or {}
    linked = set(content.get("mapping_definition_ids") or ())
    for match, change in RULE_CHANGES.get(operation, ()):
        for rule_id, rule in list(rules.items()):
            if rule_id not in linked:
                continue
            if not all(rule.get(key) == value for key, value in match.items()):
                continue
            if change is None:
                # Unlink only: the draft keeps the rule snapshot and its server-managed provenance.
                ids = content.get("mapping_definition_ids")
                if isinstance(ids, list) and rule_id in ids:
                    ids.remove(rule_id)
                    changes.append(f"{rule_id}: removed")
            else:
                rule.update(change)
                changes.append(f"{rule_id}: {match} -> {change}")
    return changes


def _needs_revision(config: dict) -> bool:
    """Whether a rule this mapping selects still has an old binding (checked without opening a draft)."""
    changes = RULE_CHANGES.get(config.get("target_operation"))
    ids = config.get("mapping_definition_ids") or []
    if not changes or not ids:
        return False
    rows = frappe.get_all("HERO Frappe Mapping", filters={"status": "Active", "definition_id": ["in", ids]},
                          fields=["source_path", "target_doctype", "target_field", "purpose"], limit_page_length=0)
    return any(all(row.get(key) == value for key, value in match.items()) for match, _ in changes for row in rows)


def _revise(definition_id: str, operation: str) -> list[str]:
    from hero_integration import api

    draft = api.mapping_draft_create(definition_id)["draft"]
    if int(draft.get("content_revision") or 0):
        raise RuntimeError("an unpublished personal draft exists; publish or discard it first")
    content = draft.get("content") or {}
    probe = json.loads(json.dumps(content))
    if not revise_content(probe, operation):
        return []
    # A changed rule version needs a portable identity first (the editor does the same).
    bases = content.get("field_rule_bases") or {}
    for rule_id in sorted(set(probe.get("field_rules") or {}) & set(bases)):
        if probe["field_rules"][rule_id] == content["field_rules"][rule_id]:
            continue
        base = bases[rule_id]
        if not frappe.db.get_value("HERO Frappe Mapping", base["name"], "portable_definition_id"):
            api.mapping_draft_enroll_portable_rule(definition_id, rule_id, str(uuid4()), draft["base_active_revision"],
                                                   int(base["revision"]))
    draft = api.mapping_draft_create(definition_id)["draft"]
    content = draft.get("content") or {}
    changes = revise_content(content, operation)
    saved = api.mapping_draft_save(definition_id, draft["generation"], draft["content_revision"],
                                   draft["base_active_revision"], json.dumps(content))["draft"]
    api.mapping_draft_publish(definition_id, saved["generation"], saved["content_revision"],
                              saved["base_active_revision"], saved.get("reconciliation_revision") or 0,
                              confirmed=True, warning_fingerprint=saved.get("warning_fingerprint") or "")
    return changes


def revise_rules() -> dict:
    """Publish a revised mapping for every active read mapping that still has an old rule.

    Each mapping is revised on its own; a failure is logged, rolled back and does not stop the others.
    """
    report = {}
    for row in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE"},
                              fields=["name", "definition_id", "mapping_json"], limit_page_length=0):
        try:
            config = json.loads(row.mapping_json or "{}")
        except (ValueError, TypeError):
            continue
        if not row.definition_id or not isinstance(config, dict) or not _needs_revision(config):
            continue
        savepoint = "hero_native_move_" + uuid4().hex
        frappe.db.savepoint(savepoint)
        try:
            report[row.name] = _revise(row.definition_id, config["target_operation"])
        except Exception as error:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(title="HERO native field move: mapping not revised", message=f"{row.name}: {error!r}")
            report[row.name] = f"not revised: {error}"
    return report


def _written_targets() -> set[tuple[str, str]]:
    """Fields an active read mapping still writes (its selected rule versions)."""
    ids = set()
    for config in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE"},
                                 pluck="mapping_json", limit_page_length=0):
        try:
            ids.update(json.loads(config or "{}").get("mapping_definition_ids") or ())
        except (ValueError, TypeError, AttributeError):
            return {(doctype, fieldname) for doctype, fieldname in REDUNDANT_FIELDS}  # unreadable: keep everything
    rows = frappe.get_all("HERO Frappe Mapping", filters={"status": "Active", "definition_id": ["in", sorted(ids) or [""]]},
                          fields=["target_doctype", "target_field"], limit_page_length=0)
    return {(row.target_doctype, row.target_field) for row in rows}


def _hero_type_rule_active() -> bool:
    return bool(frappe.db.exists("HERO Frappe Mapping", {"status": "Active", "target_field": "customer_type",
                                                         "transformation": "Customer Type From HERO Type",
                                                         "personal_graphql_mapping": ["is", "set"]}))


def copy_values() -> dict:
    """Copy the HERO values into the native fields that an active mapping now writes, where still empty."""
    from hero_integration.services.direct_mapping import hero_customer_type, plain_text_html

    written = _written_targets()
    copied = {"notes": 0, "color": 0, "status": 0, "customer_type": 0}
    if ("Project", "notes") in written and frappe.db.has_column("Project", "custom_hero_partner_notes"):
        for row in frappe.get_all("Project", filters={"custom_hero_partner_notes": ["is", "set"]},
                                  fields=["name", "notes", "custom_hero_partner_notes"], limit_page_length=0):
            if not (row.notes or "").strip() and plain_text_html(row.custom_hero_partner_notes):
                frappe.db.set_value("Project", row.name, "notes", plain_text_html(row.custom_hero_partner_notes),
                                    update_modified=False)
                copied["notes"] += 1
    if ("Event", "color") in written and frappe.db.has_column("Event", "custom_hero_color"):
        for row in frappe.get_all("Event", filters={"custom_hero_color": ["is", "set"]},
                                  fields=["name", "color", "custom_hero_color"], limit_page_length=0):
            if not row.color:
                frappe.db.set_value("Event", row.name, "color", row.custom_hero_color, update_modified=False)
                copied["color"] += 1
    if ("Event", "status") in written and frappe.db.has_column("Event", "custom_hero_is_done"):
        for name in frappe.get_all("Event", filters={"custom_hero_is_done": "true", "status": "Open"}, pluck="name",
                                   limit_page_length=0):
            frappe.db.set_value("Event", name, "status", "Completed", update_modified=False)
            copied["status"] += 1
    if ("Customer", "customer_type") in written and _hero_type_rule_active() and frappe.db.has_column("Customer", "custom_hero_customer_type"):
        for row in frappe.get_all("Customer", filters={"custom_hero_customer_type": ["is", "set"]},
                                  fields=["name", "customer_type", "custom_hero_customer_type"], limit_page_length=0):
            wanted = hero_customer_type(row.custom_hero_customer_type)
            if row.customer_type != wanted:
                frappe.db.set_value("Customer", row.name, "customer_type", wanted, update_modified=False)
                copied["customer_type"] += 1
    return copied


def hide_redundant_fields() -> list[str]:
    """Hide the redundant fields no active mapping writes any more.

    They are hidden, not deleted: older mapping revisions still declare them in
    their schema provenance, and the column keeps the old values.
    """
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    written, hidden = _written_targets(), []
    for doctype, fieldname in REDUNDANT_FIELDS:
        if (doctype, fieldname) in written or not frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
            continue
        make_property_setter(doctype, fieldname, "hidden", "1", "Check", validate_fields_for_doctype=False)
        hidden.append(f"{doctype}.{fieldname}")
        frappe.clear_cache(doctype=doctype)
    return hidden


def run() -> dict:
    previous = frappe.session.user
    frappe.set_user("Administrator")
    try:
        rules = revise_rules()
        copied = copy_values()
        hidden = hide_redundant_fields()
    finally:
        frappe.set_user(previous)
    return {"rules": rules, "copied": copied, "hidden": hidden}
