"""Deliver committed native observations through their approved writeback mappings."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import timedelta
import json

from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError

_automatic_plan = ContextVar("hero_automatic_write_plan", default=None)
_dispatch_event = ContextVar("hero_automatic_dispatch_event", default=None)
RECORD = "HERO Integration Record"


def automatic():
    return _automatic_plan.get() is not None


def enqueue(frappe, settings, executable):
    if not int(getattr(settings, "allow_graphql_mutation_execution", 0) or 0):
        return
    frappe.enqueue(process_pending, queue="long", enqueue_after_commit=True, deduplicate=True,
                   job_id="hero-auto-write-" + executable.plan_key,
                   source=executable.selection.source_instance, generation=executable.generation)


def current_plan(frappe, source, generation):
    from frappe.utils.scheduler import is_scheduler_inactive
    from hero_integration.services.polling import effective_mode, scheduler_due
    from hero_integration.services.sync_runtime_adapter import (
        _require_area_enabled, _require_confirmed_source_binding, _require_latest_validated_snapshot,
        _runtime_plan_service, _scheduler_actor,
    )

    settings = frappe.get_single("HERO Integration Settings")
    if (effective_mode(settings) != "AUTO_SYNC" or not settings.allow_graphql_mutation_execution
            or settings.hero_source_instance != source or is_scheduler_inactive(verbose=False)
            or not scheduler_due(settings, frappe.utils.now_datetime(),
                                 system_timezone=frappe.utils.get_system_timezone())):
        return None
    _require_confirmed_source_binding(settings)
    executable = _runtime_plan_service(frappe).resolve_executable_plan(
        frappe.local.site, source, generation, True, _scheduler_actor(frappe))
    _require_latest_validated_snapshot(frappe, settings, source, executable.selection)
    _require_area_enabled(frappe, settings, executable.selection)
    return executable


def validate_dispatch(frappe):
    """Recheck the switches and plan after the remote preflight, before dispatch."""
    plan = _automatic_plan.get()
    if plan is not None:
        current = current_plan(frappe, plan.selection.source_instance, plan.generation)
        if current is None or current.selection != plan.selection:
            raise RuntimeBridgeError("Automatic writeback configuration changed before delivery.")


def dispatch_selection():
    """Only an event selected and leased by process_pending can enter worker dispatch."""
    return _automatic_plan.get(), _dispatch_event.get()


def _profiles(frappe, plan):
    from hero_integration.services.mapped_send import assignments
    from hero_integration.services.personal_mapping_content import unique_json_object

    selected = plan.selection
    profiles = {}
    rows = frappe.get_all("HERO GraphQL Mapping", filters={"source_instance": selected.source_instance,
        "schema_snapshot": selected.schema_snapshot, "direction": "FRAPPE_TO_MUTATION", "status": "Active"},
        fields=["name", "mapping_json"], order_by="name")
    for row in rows:
        try:
            content = json.loads(row.mapping_json, object_pairs_hook=unique_json_object)
            source = content["source"]
            doctype = source["doctype"]
            usage = content.get("usage")
            if usage == "MAPPED_UPDATE_V2":
                reader, items, creating = content["read_service"], content["assignments"], False
            elif usage == "SEND_TO_HERO":
                config = content["send_options"]
                reader, creating = config["read_service"], config["mode"] == "CREATE"
                items = [item for _, item in assignments(content)]
            elif usage is None:
                reader, items, creating = selected.graphql_service, [{"source": source}], False
            else:
                continue
            if reader == selected.graphql_service:
                fields = {item["source"]["field"] for item in items if item["source"]["doctype"] == doctype}
                if usage == "MAPPED_UPDATE_V2":
                    from hero_integration.services.change_detection import related_field_key
                    fields.update(related_field_key(item["source"]) for item in items)
                profiles.setdefault(doctype, []).append((row.name, fields, creating))
        except (ValueError, KeyError, TypeError):
            continue  # Other active profile formats have their own adapter.
    return profiles


def _due_events(frappe, plan, profiles, limit):
    from frappe.query_builder.functions import Coalesce
    record = frappe.qb.DocType(RECORD)
    parent = frappe.qb.DocType("HERO Object Binding")
    # Historical bound observations predate delivery state. Scope them by the active
    # binding, so a worker never consumes an observation from a different source.
    return (frappe.qb.from_(record).left_join(parent).on(parent.name == record.correlation_id)
        .select(record.name, record.target_doctype, record.technical_action, record.changed_fields,
                record.correlation_id, record.target_name)
        .where((record.origin == "frappe") & record.target_doctype.isin(tuple(profiles))
               & (((record.technical_action == "frappe_create_observed") & (record.source_instance == plan.selection.source_instance)) |
                  (record.technical_action.isin(("frappe_change_observed", "frappe_related_change_observed"))
                   & (parent.site_scope == frappe.local.site) & (parent.source_instance == plan.selection.source_instance)))
               & Coalesce(record.outbound_status, "").isin(("", "Deferred"))
               & (record.outbound_retry_on.isnull() | (record.outbound_retry_on <= frappe.utils.now_datetime())))
        .orderby(Coalesce(record.outbound_retry_on, record.creation)).orderby(record.name).limit(limit).run(as_dict=True))


def _remaining_delivery(frappe, event, mapping, fields, receipt):
    """Continue only the unconfirmed part under a distinct idempotent event key."""
    from hashlib import sha256
    from hero_integration.services.integration_events import record_output

    proof = json.loads(receipt.intent_json or "{}")
    pending = sorted(fields.intersection(proof.get("deferred_native_fields", ())))
    if not pending:
        return True
    child = sha256(json.dumps([event.name, mapping, pending]).encode()).hexdigest()
    status = frappe.db.get_value(RECORD, child, "outbound_status")
    if status in {"Current", "Delivered", "Superseded"}:
        return True
    original = frappe.get_doc(RECORD, event.name)
    observed = json.loads(original.native_state_json or "{}")
    if not all(field in observed for field in pending):
        return False
    record_output(source_type=original.source_type, source_id=original.source_id,
        source_instance=original.source_instance, result="OBSERVED", action=event.technical_action,
        origin="frappe", correlation_id=original.correlation_id, target_doctype=original.target_doctype,
        target_name=original.target_name, changed_fields=pending, event_id=child,
        native_state={field: observed[field] for field in (*pending, "_related_document") if field in observed})
    return False


def _deliver(frappe, plan, event, profiles):
    from hero_integration import api
    from hero_integration.services.change_detection import native_create_event_id
    from hero_integration.services.sync_engine import Eligibility

    changed = {field.strip() for field in (event.changed_fields or "").split(",")}
    creating = event.technical_action == "frappe_create_observed"
    actions = [(name, fields & changed) for name, fields, is_create in profiles
               if creating == is_create and (creating or fields & changed)]
    if creating:
        doc = frappe.get_doc(event.target_doctype, event.target_name)
        if doc.doctype == "Task" and doc.get("parent_task"):
            # Reconcile any uncertain previous dispatch before retiring its old CREATE event.
            keys = [api._event_action_key(event.name, name) for name, _ in actions]
            if not keys or not frappe.db.exists("HERO Mutation Audit", {"event_action_key": ["in", keys]}):
                return "Superseded", "The native task is now a subtask."
        actions = [(name, fields) for name, fields in actions
                   if native_create_event_id(frappe.local.site, plan.selection.source_instance,
                       doc, frappe.get_doc("HERO GraphQL Mapping", name))[0] == event.name]
    if not actions:
        return "Deferred", "Awaiting an applicable active mapping."
    covered = set()
    delivered = False
    deferred = None
    superseded = 0
    for name, fields in actions:
        if covered & fields:
            return "Deferred", "Multiple active mutation mappings select the same changed field."
        covered.update(fields)
    confirmed = set()
    # A pending receipt from a later profile can fence the entire object.
    # Reconcile existing deliveries before any profile attempts new work.
    for name, fields in actions:
        receipt = frappe.db.get_value("HERO Mutation Audit", {"event_action_key": api._event_action_key(event.name, name)},
                                      ["delivery_stage", "correlation_id", "intent_json"], as_dict=True)
        if not receipt:
            continue
        if receipt.delivery_stage != "Confirmed":
            result = api.reconcile_bound_writeback(receipt.correlation_id)
            if result["status"] != "Confirmed":
                return "Deferred", "Awaiting verification of the original delivery."
        confirmed.add(name)
        delivered = True
        if not _remaining_delivery(frappe, event, name, fields, receipt):
            deferred = "Independent changes delivered. A mapped field still requires review."
    for name, _ in actions:
        if name in confirmed:
            continue
        preview = api.sync_hero_event_preview(event.name, name, plan.generation)
        if preview.get("superseded"):
            superseded += 1
            continue
        if preview.get("conflicts") or preview.get("eligibility") in (
                Eligibility.RECONCILIATION_REQUIRED.value, Eligibility.MAPPING_CHANGED.value):
            if not preview.get("can_send"):
                deferred = deferred or "Waiting for the mapped field or its dependency."
                continue
        if preview.get("eligible") or preview.get("can_send"):
            token = _dispatch_event.set((event, name))
            try:
                result = api.sync_hero_event_execute(event.name, name,
                    preview.get("request_fingerprint") or preview["fingerprint"], confirmed=True,
                    expected_plan_generation=plan.generation)
            finally:
                _dispatch_event.reset(token)
            if result["status"] not in ("SENT", "SUCCEEDED", "Confirmed"):
                return "Deferred", "Awaiting verification of the original delivery."
            delivered = True
            if preview.get("conflicts"):
                deferred = "Independent changes delivered. A mapped field still requires review."
        elif preview.get("held") or creating or preview.get("changes"):
            deferred = deferred or "Waiting for the mapped field or its dependency."
    if deferred:
        return "Deferred", deferred
    if superseded == len(actions):
        return "Superseded", "A later native save replaced these values."
    return ("Delivered", "Verified HERO delivery.") if delivered else ("Current", "No eligible local difference.")


def process_pending(source, generation):
    import frappe
    from hero_integration.services.polling import runtime_plan
    from hero_integration.services.sync_engine import FrappeSyncLedger, SyncConflict

    plan = current_plan(frappe, source, generation)
    if plan is None:
        return
    settings = frappe.get_single("HERO Integration Settings")
    polling = runtime_plan(settings)
    limit = polling["page_size"]
    if settings.testing_enabled:
        from hero_integration.services.test_environment import require_environment
        from hero_integration.api import _testing_max_actions
        require_environment(frappe)
        limit = min(limit, _testing_max_actions(settings.testing_max_actions))
    profiles = _profiles(frappe, plan)
    if not profiles:
        return
    events = _due_events(frappe, plan, profiles, limit)
    frappe.db.commit()
    token = _automatic_plan.set(plan)
    try:
        for event in events:
            ledger = FrappeSyncLedger(frappe)
            try:
                with ledger.execution_lease("outbound-event:" + event.name):
                    current = frappe.db.get_value(RECORD, event.name,
                        ["outbound_status", "outbound_attempts", "outbound_retry_on"], as_dict=True, for_update=True)
                    ledger.commit()
                    # An earlier worker may have finished or deferred the selected event.
                    if current is None or (current.outbound_status or "") not in {"", "Deferred"}:
                        continue
                    if current.outbound_retry_on and current.outbound_retry_on > frappe.utils.now_datetime():
                        continue
                    if current_plan(frappe, source, generation) is None:
                        return
                    try:
                        state, detail = _deliver(frappe, plan, event, profiles[event.target_doctype])
                    except Exception as error:
                        # The audit distinguishes pre-dispatch errors from uncertain sends.
                        # Reopen the receipt in the next cycle before any new mutation.
                        ledger.rollback()
                        state, detail = "Deferred", "Writeback deferred (" + type(error).__name__ + ")."
                    attempts = int(current.outbound_attempts or 0) + 1
                    delay = min(3600, max(1, polling["retry_backoff_seconds"]) * 2 ** min(attempts - 1, 10))
                    next_try = frappe.utils.now_datetime() + timedelta(seconds=delay) if state == "Deferred" else None
                    frappe.db.set_value(RECORD, event.name, {"outbound_status": state, "outbound_detail": detail,
                        "outbound_attempts": attempts, "outbound_retry_on": next_try}, update_modified=False)
                    ledger.commit()
            except SyncConflict:
                ledger.rollback()  # A concurrent worker owns this observation.
    finally:
        _automatic_plan.reset(token)
