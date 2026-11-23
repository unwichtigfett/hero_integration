"""Synchronization conflict reporting and permission-checked single-field decisions.

A conflict link stores an English diagnostic and hash baselines, never the
synchronized values. The report therefore names what the diagnostic proves:
the HERO record, the affected Doctype fields with the HERO variables that
feed them (from the rules of the run that processed the record; unknown when
that run cannot be reconstructed), the current ERPNext value and, where
the diagnostic carries them, the old and new HERO values. Texts stay English;
the Synchronization page translates them at display time.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from frappe.model.document import Document
    from hero_integration.services.binding_registry import FieldBinding

import frappe

RECHECK_PREFIX = "Recheck requested: "
MAX_VALUE = 300
MAX_FIELDS = 20

_FIELD_LIST = re.compile(r"^(?:Field conflict requires review|Task fields require review): (?P<fields>[\w, ]+?)\.?$")
_NUMBER_DRIFT = re.compile(
    r"^HERO (?P<label>.+?) changed from (?P<old>'[^']*'|\"[^\"]*\") to (?P<new>'[^']*'|\"[^\"]*\")\. "
    r".* set (?P<field>\w+) to ")
_PRICE = re.compile(r"^The (?:imported )?(?P<list>Standard (?:Buying|Selling)) price (?P<name>\S+) ")

# Review reasons without a field list: the field they concern.
_REASON_FIELDS = (
    ("the Project has no customer", "customer"),
    ("The project address is missing or incomplete", "project_name"),
)

# What the two decisions do, per kind of report (English, translated in the page).
_EXPLANATIONS = {
    "field": "The field was changed in HERO and in ERPNext since the last synchronization, "
             "so neither value was overwritten.",
    "number": "HERO changed the business number. The ERPNext record keeps its name and number until you decide.",
    "review": "HERO delivered this record with a value ERPNext cannot use as it is. "
              "It was imported and waits for your confirmation.",
    "product": "The ERPNext Item or price was changed after the last import, so the newer HERO article was not applied.",
    "record": "HERO delivered this record, but it could not be imported. It has no ERPNext record yet.",
    "other": "The synchronization stopped for this record and needs a decision.",
}
_DECISIONS = {
    "Conflict": ("The next synchronization checks the record again and applies HERO changes that no longer conflict.",
                 "ERPNext keeps its current values; later HERO changes to this record are not imported."),
    "Pending Review": ("The record is released as it was imported.",
                       "ERPNext keeps the record, but later HERO changes to it are not imported."),
}


def report(link_name: str, source_instance: str) -> dict:
    """Report for one open conflict link of ``source_instance``."""
    link = frappe.get_doc("HERO Integration Link", link_name)
    instance = link.source_instance or "default"
    if instance != source_instance:
        raise frappe.PermissionError("The conflict belongs to a different HERO source.")
    if link.sync_status not in ("Conflict", "Pending Review"):
        raise frappe.ValidationError("The record has no open conflict.")
    detail = link.conflict_detail or ""
    recheck = detail.startswith(RECHECK_PREFIX)
    problem = detail[len(RECHECK_PREFIX):] if recheck else detail
    rules = (_product_rules(link, instance) if link.source_type == "supply_product"
             else _run_rules(link.source_type, link.source_id, instance))
    kind, fields = _fields(link, problem, rules)
    approve, decline = _DECISIONS[link.sync_status]
    return {
        "name": link.name, "kind": kind, "hero_id": link.source_id, "source_type": link.source_type,
        "area": _area_label(link.source_type), "doctype": link.target_doctype or "", "record": link.target_name or "",
        "record_exists": bool(link.target_doctype and link.target_name
                              and frappe.db.exists(link.target_doctype, link.target_name)),
        "status": "Recheck requested" if recheck else
                  ("Confirmation pending" if link.sync_status == "Pending Review" else "Conflict"),
        "problem": problem or "Conflict requires review.", "explanation": _EXPLANATIONS[kind],
        "fields": fields, "decidable": True, "approve_effect": approve, "decline_effect": decline,
    }


def record_report(record_name: str, source_instance: str) -> dict:
    """Read-only report of a record-level import finding."""
    record = frappe.get_doc("HERO Integration Record", record_name)
    run_instance = frappe.db.get_value("HERO Integration Run", record.run, "source_instance") if record.run else None
    if (run_instance or "default") != source_instance:
        raise frappe.PermissionError("The conflict belongs to a different HERO source.")
    detail = record.detail or record.result or ""
    names = _FIELD_LIST.match(detail) or re.match(r"^HERO response omits the source of: (?P<fields>[\w, ]+)\.$", detail)
    links = frappe.get_all("HERO Integration Link", filters={
        "source_system": "HERO", "source_instance": source_instance,
        "source_type": record.source_type, "source_id": record.source_id,
    }, fields=["target_doctype", "target_name"], limit_page_length=2)
    link = links[0] if len(links) == 1 else None
    doctype = (link.target_doctype if link else None) or _target_doctype(record.source_type)
    target = (link.target_name if link else None) or ""
    rules = _run_rules(record.source_type, record.source_id, source_instance, run=record.run)
    fields = [_field(record.source_type, doctype, target, name, rules) for name in _split(names)] if names else []
    return {
        "name": record.name, "kind": "record", "hero_id": record.source_id, "source_type": record.source_type,
        "area": _area_label(record.source_type), "doctype": doctype, "record": target,
        "record_exists": bool(doctype and target and frappe.db.exists(doctype, target)),
        "status": "Import conflict" if link else "Not imported", "problem": detail or "Conflict requires review.",
        "explanation": "The import reported a conflict for a record with an ERPNext link." if link else _EXPLANATIONS["record"],
        "fields": fields, "decidable": False, "run": record.run, "approve_effect": "",
        "decline_effect": "" if link else "Fix the record in HERO; the next run imports it.",
    }


# ---------------------------------------------------------------- field resolution

def _fields(link, problem: str, rules) -> tuple[str, list[dict]]:
    doctype, name = link.target_doctype, link.target_name
    if match := _FIELD_LIST.match(problem):
        return "field", [_field(link.source_type, doctype, name, field, rules) for field in _split(match)]
    if match := _NUMBER_DRIFT.match(problem):
        entry = _field(link.source_type, doctype, name, match["field"], rules)
        entry["hero_value"] = _literal(match["new"])
        entry["previous_hero_value"] = _literal(match["old"])
        return "number", [entry]
    if link.source_type == "supply_product":
        if problem.startswith("Item code ") and "is already used by an ERPNext Item" in problem:
            entry = _field(link.source_type, doctype, name, "item_code", rules)
            entry["hero_value"] = name
            return "product", [entry]
        if match := _PRICE.match(problem):
            entry = _field(link.source_type, "Item Price", match["name"], "price_list_rate", rules)
            entry["hero_variable"] = dict(rules or ()).get(("Item Price", match["list"]))
            return "product", [entry]
        return "product", [_field(link.source_type, doctype, name, field, rules) for field in _changed_item_fields(link)]
    for reason, field in _REASON_FIELDS:
        if reason in problem:
            return "review", [_field(link.source_type, doctype, name, field, rules)]
    return ("review" if link.sync_status == "Pending Review" else "other"), []


def _split(match) -> list[str]:
    return [item.strip() for item in match["fields"].split(",") if item.strip()][:MAX_FIELDS]


def _field(source_type: str, doctype: str, name: str | None, field: str, rules) -> dict:
    entry = {"doctype": doctype or "", "field": field, "label": field,
             "hero_variable": _hero_variable(source_type, doctype, field, rules), "value": None}
    if doctype and frappe.db.exists("DocType", doctype):
        meta = frappe.get_meta(doctype)
        docfield = meta.get_field(field)
        if docfield is not None:
            entry["label"] = docfield.label or field
        if name and docfield is not None and frappe.db.exists(doctype, name):
            entry["value"] = _display(frappe.db.get_value(doctype, name, field))
    return entry


def _hero_variable(source_type: str, doctype: str, field: str, rules) -> str | None:
    """HERO path of the rule that writes ``doctype.field``; None when it cannot be proven."""
    if not rules:
        return None
    exact = [path for (rule_doctype, rule_field), path in rules if rule_field == field and rule_doctype == doctype]
    # Customer contact fields are written to the Contact of the Customer.
    paths = exact or [path for (rule_doctype, rule_field), path in rules if rule_field == field]
    return ", ".join(dict.fromkeys(paths)) or None


def _product_rules(link, source_instance: str) -> tuple | None:
    """(doctype, field) → HERO path of the HeroProducts mapping the article was last imported under.

    Item Price rules are keyed by their price list. None when the mapping in
    force today is not the one recorded on the link (rules changed since)."""
    from hero_integration.services import direct_runtime

    try:
        read = direct_runtime.resolve_read("HeroProducts", source_instance)
    except Exception:  # an unreadable mapping names no variable
        return None
    recorded = (getattr(link, "mapping_version", "") or "").split(":", 1)
    if len(recorded) != 2 or recorded[1] != read.mapping_version[:len(recorded[1])]:
        return None
    return tuple((((spec.target_doctype, spec.relation) if spec.target_doctype == "Item Price"
                   else (spec.target_doctype, spec.target_field)), spec.source_path)
                 for spec in read.plan.definitions if spec.purpose != "Source Identity")


def _run_rules(source_type: str, source_id: str, source_instance: str, run: str | None = None) -> tuple | None:
    """(doctype, field) → HERO path of exactly the rules frozen onto the run of
    ``source_instance`` that processed this record. HERO ids repeat across
    sources, so a run of another source never counts. None when no such run
    exists or its rules changed."""
    operation = _operation(source_type)
    if not operation or source_type == "supply_product":
        return None
    if not run:
        rows = frappe.db.sql("""SELECT x.run FROM `tabHERO Integration Record` x
            JOIN `tabHERO Integration Run` r ON r.name = x.run
            WHERE x.source_type=%s AND x.source_id=%s AND r.mode='Execute' AND IFNULL(r.graphql_mapping, '') != ''
              AND COALESCE(NULLIF(r.source_instance, ''), 'default')=%s
            ORDER BY x.creation DESC LIMIT 1""", (source_type, str(source_id), source_instance))
        run = rows[0][0] if rows else None
    if not run or not frappe.db.exists("HERO Integration Run", run):
        return None
    document = frappe.get_doc("HERO Integration Run", run)
    if (getattr(document, "source_instance", "") or "default") != source_instance:
        return None
    if not getattr(document, "graphql_mapping", ""):
        return None  # without a frozen mapping every active rule would qualify
    from hero_integration.services.migration import _mapping_plan_for_run

    try:
        plan = _mapping_plan_for_run(document, operation)
    except Exception:  # the run's service, mapping or rule revisions are no longer reconstructable
        return None
    return tuple(((spec.target_doctype, spec.target_field), spec.source_path)
                 for spec in plan.definitions if spec.source_path and spec.target_field)


def _changed_item_fields(link) -> list[str]:
    """Item fields whose current value differs from the per-field import baseline."""
    import json

    from hero_integration.services.product_migration import owned_field_digests

    try:
        baseline = (json.loads(link.target_field_hashes or "{}") or {}).get("fields") or {}
    except (TypeError, ValueError, AttributeError):
        baseline = {}
    if not isinstance(baseline, dict) or not baseline or not frappe.db.exists("Item", link.target_name):
        return []  # imported before per-field baselines existed
    fields = tuple(baseline)  # the fields mapped when the baseline was taken
    current = owned_field_digests(frappe.get_doc("Item", link.target_name), fields)
    return [field for field in fields if baseline[field] != current[field]]


def _display(value) -> str | None:
    if value is None or value == "":
        return None
    text = frappe.utils.strip_html_tags(str(value)).strip() if isinstance(value, str) else str(value)
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE - 1] + "…"


def _literal(text: str) -> str:
    try:
        return str(ast.literal_eval(text))
    except (ValueError, SyntaxError):
        return text.strip("'\"")


def _areas():
    from hero_integration.services.sync_page import AREAS

    return {item.source_type: item for item in AREAS}


def _area_label(source_type: str) -> str:
    item = _areas().get(source_type)
    if item:
        return item.label
    return {"supply_product": "Products", "supply_service": "Services", "WageGroup": "Services", "file_upload": "Documents", "customer_address": "Contacts"}.get(source_type, source_type)


def _operation(source_type: str) -> str:
    item = _areas().get(source_type)
    return item.operation if item else ("HeroCustomers" if source_type == "customer_address" else "")


def _target_doctype(source_type: str) -> str:
    return {"customer": "Customer", "project": "Project", "Task": "Task", "CalendarEvent": "Event",
            "supply_product": "Item", "supply_service": "Item", "WageGroup": "Activity Type", "customer_address": "Address"}.get(source_type, "")


def _read_reviewed_record(event, proof):
    """Read one identity with the current approved reader, releasing DB locks before HTTP."""
    from hero_integration.services import sync_runtime_adapter as bridge
    from hero_integration.services.b8_runtime import ApprovedBoundReader, reconstruct_materialized_read_plan
    from hero_integration.services.mapping_runtime import effective_rule_fingerprint
    from hero_integration.services.polling import runtime_plan
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    site, source, actor = frappe.local.site, event.source_instance, frappe.session.user
    settings = frappe.get_single("HERO Integration Settings")
    bridge._require_confirmed_source_binding(settings)
    if settings.hero_source_instance != source or int(settings.source_binding_revision) != proof["source_revision"]:
        raise frappe.ValidationError(frappe._("Source binding changed during conflict review."))
    plans = bridge._runtime_plan_service(frappe)
    current = plans.current_approved(site, source)
    if current is None:
        raise frappe.ValidationError(frappe._("Conflict review requires the current approved runtime plan."))
    executable = plans.resolve_executable_plan(site, source, current.generation, False, actor)
    selection = executable.selection
    bridge._require_latest_validated_snapshot(frappe, settings, source, selection)
    bridge._require_area_enabled(frappe, settings, selection)
    materialized = reconstruct_materialized_read_plan(selection, frappe_module=frappe)
    contract = materialized.sealed.contract
    if contract.mapping_revision != event.mapping_revision or proof["mapping"] not in contract.definition_ids:
        raise frappe.ValidationError(frappe._("Field mapping changed during conflict review."))
    polling = runtime_plan(settings)
    rules = effective_rule_fingerprint(contract.definition_ids, selection.graphql_mapping, frappe_module=frappe)
    raw = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot).snapshot_json
    reader = ApprovedBoundReader(materialized, raw, proof["source_revision"], polling["rate_limit"])
    from hero_integration.services.read_contract import record_object_type
    owner = proof.get("reader") or {"external_id": event.external_id, "object_type": event.object_type}
    if record_object_type(reader.schema.core, materialized.request.document, contract.records_path) != owner["object_type"]:
        raise frappe.ValidationError(frappe._("Approved reader addresses another HERO object type."))
    frappe.db.rollback()
    row = reader.read_bound(owner["external_id"])
    frappe.db.rollback()
    bridge._validate_execution_evidence(frappe, plans, executable, materialized, polling, proof["source_revision"],
        rules, site, source, actor, automatic=False)
    processor = FrappeBoundProcessor(contract.mapping_revision, contract.definition_ids, frappe,
        object_type=owner["object_type"], mapping_name=selection.graphql_mapping)
    plan = processor._approved_plan(frappe, source)
    return row, plan, executable.generation, contract.identity_path


class ReviewedField(NamedTuple):
    event: Document
    proof: dict
    binding: FieldBinding
    target: Document
    link: Document | None
    remote: object
    fieldtype: str
    generation: int
    row: dict


def _review_field(event_name, expected_modified):
    import json
    from hero_integration.services.binding_registry import FrappeBindingStorage, native_fingerprint, verify_incarnation
    from hero_integration.services.execution_runtime import locked_provenance, link_source_type, migration_field_supported
    from hero_integration.services.mapping_runtime import source_lifecycle
    from hero_integration.services.writeback_readback import active_bound_project
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    frappe.only_for("System Manager")
    event = frappe.get_doc("HERO Sync Change Event", event_name)
    event.check_permission("read")
    if event.site_scope != frappe.local.site or event.outcome != "FIELD_CONFLICT" or str(event.modified) != expected_modified:
        raise frappe.ValidationError(frappe._("This field decision is stale."))
    proof = json.loads(event.field_changes)
    from hero_integration.services.read_contract import value_at
    row, plan, generation, identity_path = _read_reviewed_record(event, proof)
    storage = FrappeBindingStorage()
    parent = storage.object(frappe.local.site, event.object_binding, for_update=True)
    binding = storage.field(frappe.local.site, proof["binding"], for_update=True)
    current = frappe.get_doc(event.doctype, event.name, for_update=True)
    owner = proof.get("reader")
    reader_id = owner["external_id"] if owner else current.external_id
    if (current.open_field != proof["binding"] or current.outcome != "FIELD_CONFLICT"
            or str(current.modified) != expected_modified or current.field_changes != event.field_changes
            or not parent or not binding or binding.object_binding_key != parent.identity_key or parent.reconciliation
            or parent.target_incarnation != proof["incarnation"] or binding.direction.value != "HERO_TO_FRAPPE"
            or binding.revision != proof["binding_revision"] or parent.revision != proof["object_revision"]
            or binding.mapping_revision != current.mapping_revision or parent.mapping_revision != current.mapping_revision
            or parent.mapping_definition_id not in {plan.binding_contract.definition_id, binding.mapping_definition_id}
            or (parent.external_object_id, parent.object_type, parent.source_instance) !=
               (current.external_id, current.object_type, current.source_instance)
            or str(value_at(row, identity_path)) != reader_id or plan.source_deleted(row)
            or binding.target_doctype == "Project" and not active_bound_project(row, parent.external_object_id)
            or source_lifecycle(plan.target_operation or plan.operation, row)):
        raise frappe.ValidationError(frappe._("This field decision is stale."))
    target = frappe.get_doc(binding.target_doctype, binding.target_name, for_update=True)
    verify_incarnation(parent, target)
    target.check_permission("write")
    source_type = link_source_type(plan.target_operation or plan.operation)
    if owner:
        from hero_integration.services.migration import _require_exclusive_customer_address
        address = row.get("address")
        if (source_type != "customer" or parent.object_type != "customer_address" or target.doctype != "Address"
                or owner["object_type"] != plan.binding_contract.object_type
                or not isinstance(address, dict) or type(address.get("id")) is not int
                or str(address["id"]) != parent.external_object_id):
            raise frappe.ValidationError(frappe._("The reviewed address relationship has changed."))
        customer = storage.lock_source_object(frappe.local.site, parent.source_instance, owner["object_type"],
                                              "Customer", reader_id)
        owner_link = locked_provenance(parent.source_instance, "customer", reader_id, "Customer")
        if (not customer or customer.reconciliation or not owner_link
                or owner_link.sync_status != "Synchronized" or owner_link.target_name != customer.target_name
                or customer.mapping_revision != current.mapping_revision
                or customer.mapping_definition_id != plan.binding_contract.definition_id):
            raise frappe.ValidationError(frappe._("The reviewed address relationship has changed."))
        verify_incarnation(customer, frappe.get_doc("Customer", customer.target_name, for_update=True))
        _require_exclusive_customer_address(target, customer.target_name)
        source_type = "customer_address"
    link = locked_provenance(parent.source_instance, source_type, parent.external_object_id, target.doctype)
    if owner and not link:
        raise frappe.ValidationError(frappe._("The reviewed address relationship has changed."))
    if link and (link.target_name != target.name or link.sync_status != "Synchronized"):
        raise frappe.ValidationError(frappe._("Native link changed during conflict review."))
    rule = next((r for r in plan.definitions if r.definition_id == binding.mapping_definition_id), None)
    if (not rule or (rule.source_path, rule.target_field) != (binding.source_field, binding.target_field)
            or not migration_field_supported(rule, target, source_type)):
        raise frappe.ValidationError(frappe._("Field mapping changed during conflict review."))
    remote = FrappeBoundProcessor.project_scalar(plan, rule, row, parent.source_instance)
    fieldtype = target.meta.get_field(binding.target_field).fieldtype
    local_changed = native_fingerprint(target.get(binding.target_field), fieldtype) != native_fingerprint(proof["local"], fieldtype)
    reservation = json.loads(current.resolution_json or "{}")
    # Once dispatched, a newer local edit is evidence for another review. It
    # cannot authorize a second send or an overwrite by the old decision.
    if remote is None or (local_changed and (not reservation or reservation.get("phase") == "PREPARED")):
        raise frappe.ValidationError(frappe._("The reviewed field has changed."))
    return ReviewedField(current, proof, binding, target, link, remote, fieldtype, generation, row)


def _signed_review(action, evidence):
    """Bind a preview to its purpose, site, reviewer and the exact current evidence."""
    import hashlib
    import hmac
    import json

    from frappe.utils.verified_command import get_secret

    signed = [action, frappe.local.site, frappe.session.user, evidence]
    payload = json.dumps(signed, default=str, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(get_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def _has_new_local_change(reviewed, chosen_fp):
    from hero_integration.services.binding_registry import native_fingerprint

    current = native_fingerprint(reviewed.target.get(reviewed.binding.target_field), reviewed.fieldtype)
    return current not in (chosen_fp, native_fingerprint(reviewed.proof["local"], reviewed.fieldtype))


def _field_decision_preview(reviewed, choice, value, mutation_mapping):
    import json

    from hero_integration import api
    from hero_integration.services.binding_registry import native_fingerprint

    event, proof, binding, target, _, remote, fieldtype, generation, _ = reviewed
    if choice not in {"HERO", "ERPNEXT", "CUSTOM"}:
        raise frappe.ValidationError(frappe._("Select a field conflict decision."))
    chosen = proof["source"] if choice == "HERO" else proof["local"] if choice == "ERPNEXT" else value
    if chosen is None or isinstance(chosen, (list, dict)):
        raise frappe.ValidationError(frappe._("The mapping requires a scalar decision value."))
    reservation = json.loads(event.resolution_json or "{}")
    local = target.get(binding.target_field)
    observed = native_fingerprint(remote, fieldtype)
    if reservation:
        if (choice != reservation.get("choice") or chosen != reservation.get("value")
                or (reservation.get("mapping") and reservation["mapping"] != mutation_mapping)):
            raise frappe.ValidationError(frappe._("Resume the earlier field decision with its original value and mapping."))
    elif observed != native_fingerprint(proof["source"], fieldtype):
        raise frappe.ValidationError(frappe._("The reviewed HERO field has changed."))
    selected = native_fingerprint(chosen, fieldtype)
    cancel_unsent = reservation.get("phase") == "PREPARED"
    new_review = bool(reservation and observed == selected and _has_new_local_change(reviewed, selected))
    can_apply_locally = not reservation or (not new_review and not cancel_unsent and observed == selected)
    context = None
    if not reservation and observed != selected:
        if not mutation_mapping:
            raise frappe.ValidationError(frappe._("An approved single-field mutation mapping is required."))
        context = api._sync_hero_context(binding.identity_key, mutation_mapping, generation,
                                         review_event=event.name, reviewed_value=chosen)
    preview = {
        "target_doctype": target.doctype, "target_name": target.name, "field": binding.target_field,
        "hero_value": remote, "erpnext_value": local, "chosen_value": chosen,
        "operation": context["mapping"]["target"]["operation"] if context else None,
        "recovery": bool(reservation), "cancel_unsent": cancel_unsent, "requires_new_review": new_review,
        "updates_local": can_apply_locally and native_fingerprint(local, fieldtype) != selected,
    }
    if context:
        mutation = context["mapping"]["target"]
        arguments = context["intent"].variables[mutation["argument"]]
        preview["remote_change"] = {"object_type": event.object_type, "id": arguments["id"],
                                    "field": mutation["field"], "value": arguments[mutation["field"]]}
    # A token belongs to the administering user and to both observed field states.
    # The outbound fingerprint covers the materialized mutation, contract and schema.
    signed = [event.name, str(event.modified),
              event.field_changes, event.resolution_json, event.mapping_revision, binding.identity_key,
              binding.revision, generation, choice, mutation_mapping or "", preview,
              context["fingerprint"] if context else None]
    preview["confirmation_fingerprint"] = _signed_review("field-decision", signed)
    return preview, context


def preview_field(event_name, expected_modified, choice, *, value=None, mutation_mapping=None):
    """Show one authorized field decision and its effects without reserving a mutation."""
    frappe.only_for("System Manager")
    if choice == "RELEASE_RESERVATION":
        return _reserved_dispatch_preview(event_name, expected_modified)
    reviewed = _review_field(event_name, expected_modified)
    preview, _ = _field_decision_preview(reviewed, choice, value, mutation_mapping)
    return preview


def _require_confirmation(preview, confirmation_fingerprint, confirmed):
    import hmac

    if (confirmed not in (True, 1, "1", "true", "True") or not isinstance(confirmation_fingerprint, str)
            or not confirmation_fingerprint.isascii()
            or not hmac.compare_digest(confirmation_fingerprint, preview["confirmation_fingerprint"])):
        raise frappe.ValidationError(frappe._("This field preview has changed or was not confirmed. Preview the decision again."))


def _confirm_field_decision(reviewed, choice, value, mutation_mapping, confirmation_fingerprint, confirmed):
    preview, context = _field_decision_preview(reviewed, choice, value, mutation_mapping)
    _require_confirmation(preview, confirmation_fingerprint, confirmed)
    return preview["chosen_value"], context


def _acknowledge_reviewed_field(reviewed, chosen, chosen_fp):
    import json
    from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage, native_fingerprint
    from hero_integration.services.change_detection import managed_target_update

    event, _, binding, target, link, _, fieldtype, _, _ = reviewed
    storage = FrappeBindingStorage()
    siblings = {other.target_field: native_fingerprint(target.get(other.target_field),
                    target.meta.get_field(other.target_field).fieldtype)
                for other in storage.fields_for_object(frappe.local.site, binding.object_binding_key, for_update=True)
                if other.target_field != binding.target_field}
    if native_fingerprint(target.get(binding.target_field), fieldtype) != chosen_fp:
        target.set(binding.target_field, chosen)
        with managed_target_update(target.doctype, target.name, binding.object_binding_key), managed_target_update(
                target.doctype, target.name, link.name if link else binding.object_binding_key):
            target.save()
        target.reload()
    if native_fingerprint(target.get(binding.target_field), fieldtype) != chosen_fp:
        raise frappe.ValidationError(frappe._("Native validation changed the selected value."))
    if any(native_fingerprint(target.get(field), target.meta.get_field(field).fieldtype) != original
           for field, original in siblings.items()):
        raise frappe.ValidationError(frappe._("Native validation changed an independent bound field."))
    BindingRegistryService(storage).record_verified_acknowledgment(binding,
        expected_revision=binding.revision, mapping_definition_id=binding.mapping_definition_id,
        mapping_revision=binding.mapping_revision, source_fingerprint=chosen_fp, target_fingerprint=chosen_fp,
        evidence_id=event.name, version=binding.acknowledged_version+1, value_json=json.dumps(chosen, default=str))


def _review_audit(decision, *, for_update=True):
    filters = {"site": frappe.local.site, "request_fingerprint": decision["audit_fingerprint"],
               "user": decision["actor"]}
    if decision.get("correlation_id"):
        filters["correlation_id"] = decision["correlation_id"]
    names = frappe.get_all("HERO Mutation Audit", filters=filters, pluck="name", limit_page_length=2)
    if len(names) != 1:
        raise frappe.ValidationError(frappe._("The reviewed mutation reservation is ambiguous."))
    return frappe.get_doc("HERO Mutation Audit", names[0], for_update=for_update)


def _reserved_dispatch(event_name, expected_modified, *, for_update):
    import json
    from hero_integration import api

    event = frappe.get_doc("HERO Sync Change Event", event_name, for_update=for_update)
    event.check_permission("read")
    if (event.site_scope != frappe.local.site or event.outcome != "FIELD_CONFLICT"
            or str(event.modified) != expected_modified or not event.resolution_json):
        return None
    decision = json.loads(event.resolution_json)
    if decision.get("phase") not in {"PREPARED", "DISPATCHING"} or not decision.get("correlation_id"):
        return None
    audit = _review_audit(decision, for_update=for_update)
    audit.check_permission("read")
    if audit.status != "Approved" or audit.reservation_key != api.ACTIVE_MUTATION_RESERVATION:
        return None
    return event, audit, decision["phase"]


def _reserved_dispatch_preview(event_name, expected_modified, *, for_update=False):
    reservation = _reserved_dispatch(event_name, expected_modified, for_update=for_update)
    if reservation is None:
        raise frappe.ValidationError(frappe._("This field review has no recoverable active dispatch slot."))
    event, audit, phase = reservation
    preview = {"audit": audit.name, "phase": phase, "operation": audit.operation_name}
    preview["confirmation_fingerprint"] = _signed_review("release-dispatch-slot", [
        event.name, expected_modified, event.resolution_json, event.mapping_revision, event.object_binding,
        audit.name, str(audit.modified), audit.request_fingerprint, audit.correlation_id, phase])
    return preview


def _release_interrupted_review(event_name, expected_modified, lease):
    """Release only the abandoned audit owned by the leased event."""
    from hero_integration import api

    reservation = _reserved_dispatch(event_name, expected_modified, for_update=True)
    if reservation is None:
        return
    event, audit, phase = reservation
    if phase == "PREPARED":
        frappe.db.set_value(event.doctype, event.name, "resolution_json", None)
        api._finalize_mutation(audit, "Failed", "Review expired before mutation dispatch.", commit=lease.commit)
        return "NOT_SENT"
    api._finalize_mutation(audit, "Timeout", "Dispatch outcome requires verified readback.", commit=lease.commit)
    return "READBACK_REQUIRED"


def resolve_field(event_name, expected_modified, choice, *, value=None, mutation_mapping=None,
                  confirmation_fingerprint="", confirmed=False):
    """Serialize one field decision through preflight, external dispatch and native acknowledgment."""
    from hero_integration.services.sync_engine import FrappeSyncLedger

    frappe.only_for("System Manager")
    lease = FrappeSyncLedger()
    with lease.execution_lease("field-review:" + event_name):
        if choice == "RELEASE_RESERVATION":
            preview = _reserved_dispatch_preview(event_name, expected_modified, for_update=True)
            _require_confirmation(preview, confirmation_fingerprint, confirmed)
            return {"status": _release_interrupted_review(event_name, expected_modified, lease)}
        reviewed = _review_field(event_name, expected_modified)
        chosen, context = _confirm_field_decision(reviewed, choice, value, mutation_mapping,
                                                  confirmation_fingerprint, confirmed)
        try:
            return _apply_field_decision(reviewed, choice, chosen, mutation_mapping, context, lease)
        except Exception:
            frappe.db.rollback()
            _release_interrupted_review(event_name, expected_modified, lease)
            raise


def _apply_field_decision(reviewed, choice, chosen, mutation_mapping, context, lease):
    import json
    from hashlib import sha256
    from uuid import uuid4
    from hero_integration import api
    from hero_integration.services.binding_registry import native_fingerprint, state_fingerprint
    from hero_integration.services.execution_runtime import finish_field_review
    from hero_integration.services.read_contract import value_at

    event, proof, binding, _, _, remote, fieldtype, generation, row = reviewed
    event_name, expected_modified = event.name, str(event.modified)
    chosen_fp = native_fingerprint(chosen, fieldtype)
    observed = native_fingerprint(remote, fieldtype)
    decision = {"actor": frappe.session.user, "choice": choice, "value": chosen, "event_version": expected_modified}
    pending = json.loads(event.resolution_json or "{}")
    audit = None
    if pending:
        if pending.get("phase") == "PREPARED":
            # The session lease proves that the original worker has left. No
            # dispatch marker exists. Cancel that intent and require a new preview.
            _release_interrupted_review(event_name, expected_modified, lease)
            return {"status": "NOT_SENT", "field": binding.target_field}
        if (pending["choice"] != choice or pending["value"] != chosen or observed != chosen_fp
                or pending.get("mapping") != mutation_mapping):
            raise frappe.ValidationError(frappe._("The earlier mutation requires verified readback before another decision."))
        audit = _review_audit(pending)
        decision = dict(pending, confirmed_by=frappe.session.user)
    elif observed != chosen_fp:
        api._assert_mutation_execution_released()
        original_wire = value_at(row, binding.source_field)
        decision.update(mapping=mutation_mapping, phase="PREPARED", correlation_id=uuid4().hex,
                        audit_fingerprint=sha256((event.name + context["fingerprint"]).encode()).hexdigest())
        frappe.db.set_value(event.doctype, event.name, "resolution_json", json.dumps(decision, default=str), update_modified=False)
        audit = api._reserve_mutation(operation_name=context["mapping"]["target"]["operation"],
            schema_fingerprint=context["executable"].selection.schema_fingerprint, mapping_revision=binding.mapping_revision,
            request_fingerprint=decision["audit_fingerprint"], correlation_id=decision["correlation_id"], commit=lease.commit)
        latest = _review_field(event_name, expected_modified).remote
        fresh = api._sync_hero_context(binding.identity_key, mutation_mapping, generation,
            review_event=event.name, reviewed_value=chosen)
        if fresh["fingerprint"] != context["fingerprint"] or native_fingerprint(latest, fieldtype) != observed:
            raise frappe.ValidationError(frappe._("This field decision is stale."))
        fresh["review_source_fingerprint"] = state_fingerprint(original_wire, present=True)

        def before_mutation():
            # All validation and request materialization finished. A crash beyond this commit
            # can only be recovered via readback. Never re-enter the external transport.
            current = frappe.get_doc(event.doctype, event.name, for_update=True)
            if (current.outcome != "FIELD_CONFLICT" or current.open_field != binding.identity_key
                    or str(current.modified) != expected_modified or current.field_changes != event.field_changes
                    or json.loads(current.resolution_json or "{}") != decision):
                raise frappe.ValidationError(frappe._("This field decision is stale."))
            api._assert_mutation_execution_released()
            decision["phase"] = "DISPATCHING"
            frappe.db.set_value(event.doctype, event.name, "resolution_json", json.dumps(decision, default=str), update_modified=False)
            lease.commit()

        fresh["before_mutation"] = before_mutation
        lease.commit()
        api._assert_mutation_execution_released()
        api._SyncHeroTransport(fresh, audit).execute_once(fresh["intent"].document, fresh["intent"].variables)
        reviewed = _review_field(event_name, expected_modified)
        if native_fingerprint(reviewed.remote, fieldtype) != chosen_fp:
            raise frappe.ValidationError(frappe._("HERO did not confirm the selected value."))
    if audit:
        decision["correlation_id"] = audit.correlation_id
        if _has_new_local_change(reviewed, chosen_fp):
            from hero_integration.services.binding_registry import FrappeBindingStorage
            from hero_integration.services.execution_runtime import record_field_conflict

            parent = FrappeBindingStorage().object(frappe.local.site, binding.object_binding_key, for_update=True)
            finish_field_review(event.name, "SUPERSEDED", decision)
            next_review = record_field_conflict(parent, binding, reviewed.target, reviewed.remote, reviewed.row,
                run=event.run_id, mapping=event.mapping_name, reader=proof.get("reader"))
            api._finalize_mutation(audit, "Succeeded", "Verified remote decision; a later local edit requires review.",
                                   commit=lease.commit)
            return {"status": "FIELD_CONFLICT", "field": binding.target_field, "review": next_review}
    _acknowledge_reviewed_field(reviewed, chosen, chosen_fp)
    finish_field_review(event.name, "RESOLVED", decision)
    if audit:
        api._finalize_mutation(audit, "Succeeded", "Single field decision confirmed by approved readback.", commit=lease.commit)
    return {"status": "RESOLVED", "field": binding.target_field}
