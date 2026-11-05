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


def revise_rules(copy_scopes=None) -> dict:
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
            before = _copy_plan(row.name)
            report[row.name] = _revise(row.definition_id, config["target_operation"])
            if copy_scopes is not None and report[row.name] and before:
                after = _copy_plan(row.name)
                if after and before['source'] == after['source'] and before['snapshot'] == after['snapshot']:
                    copy_scopes.append({'before': before, 'after': after})
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


def _copy_plan(name):
    """Capture exact selected rules; unprovable legacy mappings grant no data writes."""
    from hero_integration.services.mapping_runtime import active_mapping_plan
    try:
        mapping = frappe.get_doc("HERO GraphQL Mapping", name, for_update=True)
        if mapping.status != "Active" or mapping.direction != "QUERY_TO_FRAPPE":
            return None
        config = json.loads(mapping.mapping_json)
        if not mapping.source_instance or not mapping.schema_snapshot:
            return None
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", mapping.schema_snapshot)
        if snapshot.source_instance != mapping.source_instance or snapshot.analysis_status != "Validated":
            return None
        plan = active_mapping_plan(mapping.operation_name,
            definition_ids=tuple(config['mapping_definition_ids']),
            target_operation=config['target_operation'], mapping_name=name)
        return {'name': name, 'source': mapping.source_instance, 'snapshot': mapping.schema_snapshot,
                'revision': int(mapping.mapping_revision), 'version': plan.mapping_version, 'plan': plan}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _copy_moves(before, after):
    """Select only the legacy-to-native moves proven by both exact rule sets."""
    from hero_integration.services.direct_mapping import hero_customer_type, plain_text_html
    definitions = (
        ('Customer', 'customer', 'custom_hero_customer_type', 'customer_type', 'type',
         'Customer Type From HERO Type', hero_customer_type),
        ('Project', 'project', 'custom_hero_partner_notes', 'notes', 'partner_notes',
         'Plain Text To HTML', plain_text_html),
        ('Event', 'CalendarEvent', 'custom_hero_color', 'color', 'color', 'Identity', lambda value: value),
        ('Event', 'CalendarEvent', 'custom_hero_is_done', 'status', 'is_done', 'Native Adapter',
         lambda value: 'Completed' if value == 'true' else None),
    )
    for doctype, kind, legacy, target, path, transform, convert in definitions:
        legacy_transforms = {'Scalar Text'} if target == 'status' else {'Identity', 'Trim'}
        old = [rule for rule in before['plan'].definitions
               if rule.target_doctype == doctype and rule.target_field == legacy and rule.source_path == path
               and rule.transformation in legacy_transforms]
        new = [rule for rule in after['plan'].definitions
               if rule.target_doctype == doctype and rule.target_field == target and rule.source_path == path
               and rule.transformation == transform]
        old_native = [rule for rule in before['plan'].definitions
                      if rule.target_doctype == doctype and rule.target_field == target]
        if len(old) == len(new) == 1 and (not old_native or old_native[0] != new[0]):
            yield doctype, kind, legacy, target, convert


def copy_values(scopes=()) -> dict:
    """Copy unchanged source-bound values only from mappings revised by this run.

    Both legacy value and native destination require old field baselines.
    Missing historical evidence is left for normal reconciliation.
    """
    from hero_integration.services.change_detection import decode, field_hashes

    copied = {"notes": 0, "color": 0, "status": 0, "customer_type": 0}
    for scope in scopes:
        before, after = scope['before'], scope['after']
        current = _copy_plan(after['name'])
        if not current or any(current[key] != after[key] for key in ('source', 'snapshot', 'revision', 'version')):
            continue
        for doctype, kind, legacy, target, convert in _copy_moves(before, after):
            if not frappe.db.has_column(doctype, legacy):
                continue
            filters = {'source_system': 'HERO', 'source_instance': before['source'], 'source_type': kind,
                       'target_doctype': doctype, 'sync_status': 'Synchronized', 'mapping_version': before['version']}
            for name in frappe.get_all('HERO Integration Link', filters=filters, pluck='name', limit_page_length=0):
                link = frappe.get_doc('HERO Integration Link', name, for_update=True)
                if any(link.get(key) != value for key, value in filters.items()):
                    continue
                owners = frappe.get_all('HERO Integration Link', filters={'target_doctype': doctype,
                    'target_name': link.target_name}, pluck='name', limit_page_length=2)
                if owners != [name] or not frappe.db.exists(doctype, link.target_name):
                    continue
                doc = frappe.get_doc(doctype, link.target_name, for_update=True)
                baseline = decode(link.target_field_hashes) or {}
                values = {legacy: doc.get(legacy), target: doc.get(target)}
                if any(baseline.get(field) != digest for field, digest in field_hashes(values).items()):
                    continue
                raw = values[legacy]
                if target == 'customer_type' and (not isinstance(raw, str) or raw.strip().lower() not in {'private', 'commercial'}):
                    continue
                wanted = convert(raw)
                if wanted in (None, '') or wanted == values[target]:
                    continue
                if target in {'notes', 'color'} and values[target] not in (None, ''):
                    continue
                if target == 'status' and values[target] != 'Open':
                    continue
                frappe.db.set_value(doctype, doc.name, target, wanted, update_modified=False)
                copied[target] += 1
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
        scopes = []
        rules = revise_rules(copy_scopes=scopes)
        copied = copy_values(scopes)
        hidden = hide_redundant_fields()
    finally:
        frappe.set_user(previous)
    return {"rules": rules, "copied": copied, "hidden": hidden}
