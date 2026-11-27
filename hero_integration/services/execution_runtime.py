"""Dispatch imports and sync runs and fence each native migration adapter.

Only the scheduler decides when to call the scheduled operations. Existing
queue paths remain callable for jobs created before an upgrade.
"""
import json
from contextvars import ContextVar
import frappe


from hero_integration.services import migration_archive
def execute(operation, *, run_name="", **context):
    if operation == "migration":
        from hero_integration.services import file_migration, migration

        action = frappe.db.get_value("HERO Integration Run", run_name, "action")
        if not action:
            raise frappe.DoesNotExistError("HERO migration run is missing.")
        if action in file_migration.RUN_ACTIONS:
            return file_migration._execute(run_name)
        return migration._execute_run(run_name)

    from hero_integration.services import sync_runtime_adapter as adapter

    if operation == "manual_sync":
        return adapter.run_pending_sync(context["expected_plan_generation"])
    if operation == "one_shot":
        return adapter.run_one_shot(**context)
    if operation == "scheduled_sync":
        return adapter.run_scheduled_auto_sync()
    if operation == "monitor":
        from hero_integration.services.polling import run_monitor

        # The scheduler already serializes its monitor ticks. A scheduled read
        # has no test cohort and revalidates its plan after the network call.
        return run_monitor()
    if operation == "manual_monitor":
        from hero_integration.services import polling, run_reservation

        # This named lock survives the monitor's DB rollbacks. Do not hold a
        # row lock across HTTP. Queued imports are already visible at admission.
        if not run_reservation.acquire(frappe):
            raise adapter.RuntimeBridgeError("Another HERO operation owns the execution slot.")
        try:
            active = run_reservation.locked_legacy_slot(frappe)
            if (run_reservation.legacy_slot_is_active(frappe, active)
                    or run_reservation.locked_pending_migrations(frappe)
                    or run_reservation.locked_b8_runs(frappe, frappe.local.site)):
                raise adapter.RuntimeBridgeError("Another HERO operation owns the execution slot.")
            frappe.db.rollback()
            return polling.run_manual_monitor(frappe, **context)
        finally:
            run_reservation.release(frappe)
    raise ValueError("Unknown HERO execution operation.")


# Native adapters may compute prices, create contacts or defer references. Only
# confirmed scalar fields participate in the generic polling writer.
_migration_decisions = ContextVar("hero_migration_field_decisions", default=None)


def field_decisions(doctype, name):
    context = _migration_decisions.get()
    return context[2] if context and context[:2] == (doctype, name) else {}


def adapter_baseline(doc, current, prior, *, verify=False):
    """Keep independent domain fields under their native checks and preserve local edits."""
    if _migration_decisions.get() is None:
        return current
    decisions = field_decisions(doc.doctype, doc.name)
    if not decisions:
        return current
    return {field: prior[field] if field in prior and field in decisions
            and (verify or decisions[field] in {None, "local"}) else value for field, value in current.items()}


def preserve_local_fields(doc, values):
    """Share the field decision with specialized native writers before their save."""
    if _migration_decisions.get() is None:
        return
    decisions = field_decisions(doc.doctype, doc.name)
    for field in values.keys() & decisions.keys():
        if decisions[field] in {None, "local"}:
            values[field] = doc.get(field)


def bound_outcome(outcome, doctype, name, tracked):
    """Use the canonical field baseline when legacy provenance predates a sync."""
    from dataclasses import replace

    decisions = {field: action for field, action in field_decisions(doctype, name).items() if field in tracked}
    if not decisions:
        return outcome
    groups = {key: set(getattr(outcome, key)) - decisions.keys() for key in outcome.__dataclass_fields__}
    for field, group in decisions.items():
        if group:
            groups[group].add(field)
    return replace(outcome, **{key: frozenset(fields) for key, fields in groups.items()})


def locked_provenance(instance, source_type, identity, doctype):
    from hero_integration.services.binding_registry import BindingConflict

    rows = frappe.db.get_values("HERO Integration Link", dict(source_system="HERO", source_instance=instance,
        source_type=source_type, source_id=identity), ["name", "target_doctype", "target_name", "sync_status", "creation", "source_id"],
        as_dict=True, limit=2, order_by=None, for_update=True)
    if len(rows) > 1 or rows and (rows[0].target_doctype != doctype or str(rows[0].source_id) != str(identity)):
        raise BindingConflict("Migration provenance does not identify one compatible target.")
    return rows[0] if rows else None


def link_source_type(operation):
    from hero_integration.services.projection_contracts import CONTRACTS

    if operation in CONTRACTS:
        return CONTRACTS[operation]["source_type"]
    return {"HeroCustomers": "customer", "HeroProjectMatches": "project", "HeroEmployees": "partner",
            "HeroProducts": "supply_product", "HeroServices": "supply_service", "HeroDocuments": "file_upload",
            "HeroPictures": "file_upload"}.get(operation)


def record_field_conflict(parent, binding, target, incoming, row, *, run, mapping, reader=None, projection_issue=None):
    import json
    from hashlib import sha256

    from hero_integration.services.task_hierarchy import baseline_value
    source_revision = frappe.db.get_single_value("HERO Integration Settings", "source_binding_revision") or 0
    proof = {"field": binding.target_field, "binding": binding.identity_key, "object": parent.identity_key,
             "incarnation": parent.target_incarnation, "source_field": binding.source_field,
             "source_instance": parent.source_instance, "source_type": parent.object_type,
             "external_id": parent.external_object_id, "doctype": target.doctype, "name": target.name,
             "source": incoming, "local": target.get(binding.target_field),
             "baseline": baseline_value(binding),
             "baseline_known": bool(binding.acknowledged_value_json),
             "source_baseline_hash": binding.acknowledged_source_fingerprint,
             "target_baseline_hash": binding.acknowledged_target_fingerprint,
             "mapping": binding.mapping_definition_id, "mapping_revision": binding.mapping_revision,
             "object_revision": parent.revision, "binding_revision": binding.revision,
             "ack_version": binding.acknowledged_version, "source_revision": int(source_revision),
             "remote_revision": row.get("modified"), "target_revision": str(target.modified)}
    if projection_issue:
        proof["projection_issue"] = projection_issue
    if reader is not None:
        proof["reader"] = reader
    encoded = json.dumps(proof, default=str, sort_keys=True, ensure_ascii=False)
    existing = frappe.db.get_value("HERO Sync Change Event", {"open_field": binding.identity_key},
                                   ["name","field_changes","resolution_json"], as_dict=True, for_update=True)
    if existing:
        if existing.resolution_json:
            return existing.name  # Preserve the single dispatch reservation until readback.
        previous = json.loads(existing.field_changes)
        # Unrelated native saves or provider timestamps do not create a new field decision.
        metadata = {"target_revision", "remote_revision"}
        if all(previous.get(k) == v for k, v in json.loads(encoded).items() if k not in metadata):
            return existing.name
    if existing:
        frappe.db.set_value("HERO Sync Change Event", existing.name, {"outcome": "SUPERSEDED", "open_field": None})
    from uuid import uuid4
    event_id = sha256((encoded + uuid4().hex).encode()).hexdigest()
    frappe.get_doc({"doctype": "HERO Sync Change Event", "event_id": event_id, "object_binding": parent.identity_key,
                    "open_field": binding.identity_key, "site_scope": parent.site_scope,
                    "external_id": parent.external_object_id, "object_type": parent.object_type,
                    "source_instance": parent.source_instance, "source_version": str(row.get("modified") or ""),
                    "mapping_name": mapping, "mapping_revision": binding.mapping_revision, "run_id": run,
                    "outcome": "FIELD_CONFLICT", "field_changes": encoded}).insert(ignore_permissions=True)
    return event_id


def open_field_reviews(parent):
    rows = frappe.db.get_values("HERO Sync Change Event", {"object_binding": parent.identity_key,
        "outcome": "FIELD_CONFLICT"}, ["open_field", "name", "resolution_json", "field_changes"], as_dict=True, for_update=True)
    return {row.open_field: row for row in rows}


def finish_field_review(event, outcome, decision=None):
    import json
    values = {"outcome": outcome, "open_field": None}
    if decision is not None:
        values["resolution_json"] = json.dumps(decision, default=str, sort_keys=True)
    frappe.db.set_value("HERO Sync Change Event", event, values)


def retire_source(parent, link, reason):
    from hero_integration.services.binding_registry import hold_object

    if parent:
        hold_object(parent, reason, deleted=reason == "DELETED")
    if link:
        frappe.db.set_value("HERO Integration Link", link, {
            "sync_status": "Source Deleted" if reason == "DELETED" else "Conflict",
            "conflict_detail": "HERO source lifecycle: " + reason})


def migration_field_supported(rule, target, source_type):
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    if source_type != "customer_address":
        return FrappeBoundProcessor.supports_scalar(rule, target)
    # Only the Customer adapter may interpret an approved Customer.address relation.
    from hero_integration.services.query_mapping import FIELDS
    field = target.meta.get_field(rule.target_field)
    return (target.doctype == rule.target_doctype == "Address" and rule.operation == "HeroCustomers"
            and rule.relation == "Customer Primary Address" and rule.target_field in FIELDS["Address"]
            and (rule.source_path.startswith("address.") or (rule.source_path, rule.target_field) == ("phone_fax", "fax"))
            and field and not field.read_only and not field.fetch_from and not field.is_virtual
            and int(field.permlevel or 0) in target.get_permlevel_access("write"))


def locked_delivery_receipt(frappe_module, name):
    return frappe_module.get_doc("HERO Mutation Audit", name, for_update=True)


def deliver_bound_intent(frappe_module, proof, *, operation, fingerprint, initial_check, perform, event_name="", error_type=None):
    """Use one session lease and persist the dispatch boundary before any mutation request."""
    from hero_integration import api
    from hero_integration.services.sync_engine import FrappeSyncLedger

    ledger = FrappeSyncLedger(frappe_module)
    with ledger.execution_lease("outbound:" + proof["object"]):
        ledger.commit()
        initial_check()
        audit = api._reserve_mutation(operation_name=operation, schema_fingerprint=proof["snapshot"],
            mapping_revision=proof["mapping_revision"], request_fingerprint=fingerprint, bound_intent=proof,
            object_scope=proof["object"], event_name=event_name, commit=ledger.commit)

        def dispatch(recheck):
            ledger.commit()
            recheck()
            from hero_integration.services.automatic_writeback import validate_dispatch
            validate_dispatch(frappe_module)
            api._assert_mutation_execution_released()
            audit.delivery_stage = "Dispatching"
            audit.save(ignore_permissions=True)
            ledger.commit()

        try:
            result = perform(audit, dispatch, ledger)
            api._finalize_mutation(audit, "Succeeded", "Bound write confirmed by exact HERO readback.", commit=ledger.commit)
        except Exception as error:
            ledger.rollback()
            stored = locked_delivery_receipt(frappe_module, audit.name)
            if stored.delivery_stage != "Confirmed":
                status = "Timeout" if isinstance(error, TimeoutError) else "Failed"
                api._finalize_mutation(stored, status, "No verified acknowledgment. Inspect the delivery stage.",
                                       commit=ledger.commit)
            if error_type:
                raise error_type("Sending to HERO could not be confirmed; no automatic retry was performed.") from error
            frappe_module.throw(frappe_module._("Sync Hero is blocked or uncertain; no automatic retry was performed."))
        return audit, result


def outbound_target_bindings(frappe_module, plan, document, source_id, *, lock=False):
    """Reconcile the native link's claims with B6 before a configured domain adapter may send."""
    from hero_integration.services.binding_registry import BindingConflict, FrappeBindingStorage
    from hero_integration.services.mapping_registry_model import MappingDirection

    contract = plan.binding_contract
    if contract is None:
        raise BindingConflict("A mapped write requires an approved source identity.")
    site = frappe_module.local.site
    storage = FrappeBindingStorage(frappe_module)
    scope = {"site_scope": site, "source_instance": contract.source_instance, "target_doctype": document.doctype}
    # Resolve both claims. A source that points elsewhere cannot fall back to a legacy link.
    keys = set()
    for filters in ({"target_name": document.name},
                    {"object_type": contract.object_type, "external_object_id": str(source_id)}):
        criteria = {**scope, **filters}
        if "target_name" in filters:
            del criteria["source_instance"]
        rows = frappe_module.db.get_values(storage.OBJECT, criteria, "identity_key", limit=2, for_update=lock)
        keys.update(row[0] for row in rows)
    if not keys:
        return None, {}
    if len(keys) != 1:
        raise BindingConflict("Multiple object bindings claim this identity or destination.")
    parent = storage.object(site, keys.pop(), for_update=lock)
    if (not parent or parent.source_instance != contract.source_instance or parent.target_name != document.name or parent.external_object_id != str(source_id)
            or parent.lifecycle != "VALID" or parent.reconciliation or parent.object_type != contract.object_type
            or parent.mapping_definition_id != contract.definition_id or parent.mapping_revision != contract.revision):
        raise BindingConflict("Existing object binding differs from migration provenance or approved revision.")
    rules = {rule.definition_id: rule for rule in plan.definitions}
    current = {}
    for field in storage.fields_for_object(site, parent.identity_key, for_update=lock):
        if field.direction != MappingDirection.HERO_TO_FRAPPE:
            continue
        rule = rules.get(field.mapping_definition_id)
        if (not rule or field.target_field in current or field.mapping_revision != contract.revision
                or (field.source_instance, field.target_app, field.target_doctype, field.target_name)
                != (parent.source_instance, parent.target_app, parent.target_doctype, parent.target_name)
                or (field.source_field, field.target_doctype, field.target_field)
                != (rule.source_path, rule.target_doctype, rule.target_field)):
            raise BindingConflict("Stored field binding requires reconciliation before migration.")
        current[field.target_field] = field
    return parent, current


_MISSING_PROJECTION = object()


def _outbound_projection(plan, rule, remote, source, null_policy):
    from hero_integration.services.sync_engine import FrappeBoundProcessor
    value = FrappeBoundProcessor.project_scalar(plan, rule, remote, source)
    if value is not None:
        return value
    # Only the selected, explicitly clearable scalar can establish an empty
    # baseline. A missing key or an omitted/failed transformation cannot.
    raw = remote
    for key in rule.source_path.split("."):
        if not isinstance(raw, dict) or key not in raw:
            return _MISSING_PROJECTION
        raw = raw[key]
    if rule.transformation in {"Identity", "Trim", "Scalar Text"} and (
            (null_policy == "EXPLICIT_NULL" and raw is None) or (null_policy == "EMPTY_STRING" and raw == "")):
        return raw
    return _MISSING_PROJECTION


def outbound_field_decision(plan, rule, doc, remote, source, field, *, has_parent, null_policy="OMIT"):
    """Apply the common three-way decision to the mapped mutation's exact native projection."""
    from hero_integration.services.binding_registry import BindingRegistryService, BindingState, native_fingerprint
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    from hero_integration.services.automatic_writeback import automatic
    authority = getattr(plan, "field_authority", {}).get(rule.definition_id)
    if authority in {"HERO", "READ_ONLY"} or (automatic() and authority == "MANUAL"):
        return "HELD_BY_AUTHORITY", field
    if not field:
        if (has_parent
                and FrappeBoundProcessor.supports_scalar(rule, doc)):
            return "UNACKNOWLEDGED", None
        return None, None
    if (field.mapping_definition_id != rule.definition_id or field.source_field != rule.source_path
            or field.state in {BindingState.MAPPING_CHANGED, BindingState.RECONCILIATION_REQUIRED}
            or not FrappeBoundProcessor.supports_scalar(rule, doc)):
        return "CONFLICT", field
    projection = _outbound_projection(plan, rule, remote, source, null_policy)
    if projection is _MISSING_PROJECTION:
        from hero_integration.services.task_hierarchy import is_description
        fieldtype = doc.meta.get_field(field.target_field).fieldtype
        if (is_description(field) and remote.get("comment", _MISSING_PROJECTION) in ("", None)
                and field.acknowledged_source_fingerprint == native_fingerprint(None, fieldtype)
                and field.acknowledged_target_fingerprint == native_fingerprint(doc.get(field.target_field), fieldtype)):
            return "UNCHANGED", field
        return "OMITTED", field
    fieldtype = doc.meta.get_field(field.target_field).fieldtype
    return BindingRegistryService.inbound_reconciliation(field, active_mapping_revision=field.mapping_revision,
        source_fingerprint=native_fingerprint(projection, fieldtype),
        target_fingerprint=native_fingerprint(doc.get(field.target_field), fieldtype)), field


def acknowledge_outbound_field(registry, plan, rule, doc, remote, source, binding, local_value, correlation, *, null_policy="OMIT", **projection_metadata):
    from hero_integration.services.binding_registry import BindingConflict, native_fingerprint

    projection = _outbound_projection(plan, rule, remote, source, null_policy)
    if "task_section" in projection_metadata and remote.get("comment") == "":
        projection = None
    elif projection is _MISSING_PROJECTION:
        raise BindingConflict("The approved readback did not establish a field baseline.")
    fieldtype = doc.meta.get_field(binding.target_field).fieldtype
    return registry.record_verified_acknowledgment(binding, expected_revision=binding.revision,
        mapping_definition_id=binding.mapping_definition_id, mapping_revision=binding.mapping_revision,
        source_fingerprint=native_fingerprint(projection, fieldtype), target_fingerprint=native_fingerprint(local_value, fieldtype),
        evidence_id=correlation, version=binding.acknowledged_version + 1,
        **({"value_json": json.dumps(local_value, default=str), **projection_metadata} if projection_metadata else {}))


def require_clear_outbound(frappe_module, site, source, doctype, name):
    """Hold inbound baselines while the outcome of a write for this object is unresolved."""
    from hero_integration.services.binding_registry import BindingConflict, FrappeBindingStorage, PendingOutbound
    from hero_integration.services.frappe_writeback import object_write_scope

    claim = FrappeBindingStorage(frappe_module).target_claim(site, doctype, name, for_update=True)
    if claim and claim.source_instance != source:
        raise BindingConflict("Multiple object bindings claim this identity or destination.")
    key = object_write_scope(site, source, doctype, name)
    if frappe_module.db.get_value("HERO Mutation Audit", {"pending_object_key": key}, "name", for_update=True):
        raise PendingOutbound("The bound object has an unresolved outbound mutation.")


def process_migration_record(plan, record, instance, source_type, write, *, evidence, successful, relationships_only=False, held=None):
    """Fence a domain adapter and publish its verified binding in the caller's transaction.

    The approved reader establishes the source type and definition. Technical links
    retain the adapter's relation evidence. An existing registry claim and the link
    must agree before the adapter sees the row. Nothing here commits the transaction.
    """
    import frappe
    from frappe.modules.utils import get_module_app
    from hero_integration.services.binding_registry import (
        BindingConflict, BindingRegistryService, BindingState, FrappeBindingStorage, native_fingerprint,
    )
    from hero_integration.services.change_detection import managed_target_update
    from hero_integration.services.mapping_registry_model import MappingDirection
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    operation = plan.target_operation or plan.operation
    if migration_archive.excluded(operation, record):
        return held("SKIPPED_SOURCE_DELETED", "Archived source record excluded by migration settings.") if held else "skipped_archived"
    contract = plan.binding_contract
    if contract is None or contract.source_instance != instance:
        raise BindingConflict("Migration record lacks its approved source and mapping.")
    identity = plan.source_identity(record)
    doctype = plan.target_doctype
    object_type = contract.object_type
    reader = None
    if source_type == "customer_address":
        address = record.get("address")
        if (not plan.adapter_options or (plan.target_operation or plan.operation) != "HeroCustomers"
                or not isinstance(address, dict) or type(address.get("id")) is not int or address["id"] <= 0):
            raise BindingConflict("The approved Customer primary address identity is missing.")
        reader = {"external_id": identity, "object_type": object_type}
        identity, doctype, object_type = str(address["id"]), "Address", source_type
    site = frappe.local.site
    storage = FrappeBindingStorage(frappe)
    registry = BindingRegistryService(storage)
    parent = storage.lock_source_object(site, instance, object_type, doctype, identity)
    link_filters = dict(source_system="HERO", source_instance=instance, source_type=source_type, source_id=identity)

    def provenance():
        link = locked_provenance(instance, source_type, identity, doctype)
        return (link.target_name, link.sync_status) if link else (None, None)

    link = locked_provenance(instance, source_type, identity, doctype)
    name, status = (link.target_name, link.sync_status) if link else (None, None)
    if record.get("_hero_source_instance", instance) != instance:
        raise BindingConflict("The record belongs to a different source instance.")
    if relationships_only and (not name or status != "Synchronized"):
        raise BindingConflict("Deferred work requires its synchronized native target.")
    from hero_integration.services.mapping_runtime import source_lifecycle
    lifecycle = source_lifecycle(operation, record)
    if (parent and parent.lifecycle == "LEGACY" and status == "Source Deleted" and parent.target_name == name
            and parent.reconciliation == "DELETED" and lifecycle == "DELETED"):
        if held:
            return held("SKIPPED_SOURCE_DELETED", "Historical source binding remains inactive.")
        return "unchanged"
    if parent and (parent.lifecycle != "VALID" or parent.reconciliation or parent.target_name != name
                   or parent.mapping_revision != contract.revision or parent.mapping_definition_id != contract.definition_id):
        raise BindingConflict("Existing object binding differs from migration provenance or approved revision.")
    if not relationships_only and lifecycle == "UNVERIFIED":
        raise BindingConflict("Customer eligibility was omitted from the approved response.")
    source_deleted = not lifecycle and (not relationships_only or source_type == "customer_address") and plan.source_deleted(record)
    if migration_archive.includes_archived() and record.get(migration_archive.FIELDS.get(operation)) is True:
        # Keep the real flag for field mapping (e.g. Item.disabled) and source evidence.
        source_deleted = False
        if lifecycle == "DELETED":
            lifecycle = "CONTACT_PERSON" if operation == "HeroCustomers" and record.get("is_contact_person") is True else ""
    if source_type == "customer_address" and (lifecycle or source_deleted):
        raise BindingConflict("The owning Customer is ineligible for primary address synchronization.")
    if not relationships_only and (lifecycle or source_deleted):
        reason = lifecycle or "DELETED"
        if operation == "HeroTasks" and doctype == "Task" and reason == "DELETED" and parent and name:
            from hero_integration.services.binding_registry import verify_incarnation
            from hero_integration.services.cockpit_task_source import store
            verify_incarnation(parent, frappe.get_doc("Task", name, for_update=True))
            store(record, instance, expected_name=name)
        retire_source(parent, link.name if link else None, reason)
        if held:
            return held("SKIPPED_SOURCE_DELETED" if reason == "DELETED" else "SKIPPED_CONTACT_PERSON", reason)
        return "unchanged"
    if status == "Source Deleted":
        raise BindingConflict("Historical source link requires explicit reconciliation.")
    try:
        before = frappe.get_doc(doctype, name, for_update=True) if name else None
    except frappe.DoesNotExistError as error:
        raise BindingConflict("The bound native target no longer exists.") from error
    if parent:
        from hero_integration.services.binding_registry import verify_incarnation
        verify_incarnation(parent, before)
    if before and link and not parent:
        from hero_integration.services.calendar_interval import original_pending_occurrence
        from hero_integration.services.product_migration import original_pending_item
        pending_item = operation == "HeroProducts" and source_type == "supply_product" and original_pending_item(link, before, plan)
        if not (pending_item or operation == 'HeroCalendarEvents' and doctype == 'Event' and source_type == 'CalendarEvent'
                and original_pending_occurrence(frappe, link, before, record, instance)):
            raise BindingConflict("The historical link has no verified native incarnation. Identity review is required.")
    if before:
        before.check_permission("write")
    else:
        frappe.has_permission(doctype, "create", throw=True)
    rules = {rule.definition_id: rule for rule in plan.definitions}
    fields = {}
    protected = {}
    decisions = {}
    deleted = (record.get("is_deleted") is True or record.get("deleted") is True) and not migration_archive.includes_archived()
    pending = open_field_reviews(parent) if parent else {}
    equal_reviews = []
    conflicting = []
    from hero_integration.services import task_hierarchy
    adapter_record = record
    if parent:
        for binding in storage.fields_for_object(site, parent.identity_key, for_update=True):
            if binding.direction != MappingDirection.HERO_TO_FRAPPE:
                continue
            rule = rules.get(binding.mapping_definition_id)
            field = binding.target_field
            if (not rule or binding.mapping_revision != contract.revision
                    or (binding.source_instance, binding.target_app, binding.target_doctype, binding.target_name)
                    != (instance, get_module_app(before.meta.module), doctype, name)
                    or (rule.source_path, rule.target_field) != (binding.source_field, field)
                    or field in fields or not migration_field_supported(rule, before, source_type)
                    or binding.state in {BindingState.MAPPING_CHANGED, BindingState.RECONCILIATION_REQUIRED}):
                raise BindingConflict("Stored field binding requires reconciliation before migration.")
            fieldtype = before.meta.get_field(field).fieldtype
            current = native_fingerprint(before.get(field), fieldtype)
            if relationships_only or deleted:
                protected[field] = current
                decisions[field] = None
                continue
            try:
                business_record = task_hierarchy.clean_record(record, binding)
            except task_hierarchy.ProjectionConflict as error:
                record_field_conflict(parent, binding, before, record["comment"], record, run=evidence,
                                      mapping=contract.definition_id, projection_issue=str(error))
                protected[field] = current
                decisions[field] = None
                fields[field] = binding
                conflicting.append(field)
                adapter_record = dict(record, comment=before.get(field))
                continue
            if business_record is not record:
                adapter_record = business_record
            incoming = FrappeBoundProcessor.project_scalar(plan, rule, business_record, instance)
            remote = native_fingerprint(incoming, fieldtype) if incoming is not None else None
            decision = registry.inbound_reconciliation(binding, active_mapping_revision=contract.revision,
                                                       source_fingerprint=remote, target_fingerprint=current)
            review = pending.get(binding.identity_key)
            if (review and not review.resolution_json and decision in {"UNCHANGED", "CONFIRM_EQUAL"}
                    and task_hierarchy.confirmed(binding) and json.loads(review.field_changes).get("projection_issue")):
                equal_reviews.append(pending.pop(binding.identity_key).name)
                review = None
            if review and review.resolution_json:
                protected[field] = current
                decisions[field] = None
                fields[field] = binding
                continue
            if decision == "CONFLICT" or review and decision not in {"CONFIRM_EQUAL", "OMITTED"}:
                record_field_conflict(parent, binding, before, incoming, record, run=evidence,
                                      mapping=contract.definition_id, reader=reader)
                conflicting.append(field)
            if decision == "CONFLICT" or (binding.identity_key in pending and decision != "CONFIRM_EQUAL"):
                protected[field] = current
                decisions[field] = None
                fields[field] = binding
                continue
            if binding.identity_key in pending and decision == "CONFIRM_EQUAL":
                equal_reviews.append(pending.pop(binding.identity_key).name)
            if decision in {"UNACKNOWLEDGED", "MAPPING_CHANGED"}:
                raise BindingConflict(f"Concurrent or unconfirmed mapped field requires review: {field}.")
            # A verified outbound write can acknowledge different native and wire
            # representations (for example an editor paragraph and its plain text).
            if (decision == "UNCHANGED" and remote == binding.acknowledged_source_fingerprint
                    and current == binding.acknowledged_target_fingerprint and remote != current):
                protected[field] = current
                decisions[field] = None
                fields[field] = binding
                continue
            if decision in {"KEEP_LOCAL", "OMITTED"}:
                protected[field] = current
                decisions[field] = "local" if decision == "KEEP_LOCAL" else None
            elif decision == "APPLY_SOURCE":
                decisions[field] = "apply"
            else:
                decisions[field] = "unchanged" if remote == binding.acknowledged_source_fingerprint else "converged"
            fields[field] = binding

    # The existing domain callbacks capture the reader's original dictionary.
    # Expose the confirmed business text only for the duration of that native write.
    if adapter_record is not record:
        wire_comment = record["comment"]
        record["comment"] = adapter_record["comment"]
    token = _migration_decisions.set((doctype, name, decisions))
    try:
        with managed_target_update(doctype, name, None):
            result = write(before) if reader is not None else write()
    finally:
        _migration_decisions.reset(token)
        if adapter_record is not record:
            record["comment"] = wire_comment
    target_name, status = provenance()
    if name and target_name != name:
        raise BindingConflict("Migration changed an existing target identity.")
    if not target_name:
        if successful(result):
            raise BindingConflict("Completed migration has no persistent target identity.")
        return result
    target = frappe.get_doc(doctype, target_name, for_update=True)
    for field, prior in protected.items():
        if native_fingerprint(target.get(field), target.meta.get_field(field).fieldtype) != prior:
            raise BindingConflict(f"Migration would overwrite an unconfirmed local field: {field}.")
    if relationships_only or deleted or not successful(result) or status != "Synchronized":
        return result
    if source_type == "supply_product":
        from hero_integration.services.product_migration import _link_baseline
        link = frappe.db.get_value("HERO Integration Link", link_filters, ["target_field_hashes"], as_dict=True,
                                   for_update=True)
        if _link_baseline(link).get("adopted_locally"):
            return result  # Native adopted Items retain their explicit review contract.
    parent = registry.create_or_resolve_object_binding(site_scope=site, source_instance=instance,
        object_type=object_type, external_object_id=identity, target_app=get_module_app(target.meta.module),
        target_doctype=doctype, target_name=target.name, mapping_definition_id=contract.definition_id,
        mapping_revision=contract.revision)
    for rule in plan.definitions:
        field = rule.target_field
        if field in protected or not migration_field_supported(rule, target, source_type):
            continue
        incoming = FrappeBoundProcessor.project_scalar(plan, rule, adapter_record, instance)
        if incoming is None:
            continue
        fieldtype = target.meta.get_field(field).fieldtype
        actual = native_fingerprint(target.get(field), fieldtype)
        if actual != native_fingerprint(incoming, fieldtype):
            if field in fields:
                raise BindingConflict(f"Native lifecycle did not confirm the mapped field: {field}.")
            continue  # A domain-specific projection has no generic write authority.
        binding = fields.get(field) or registry.create_or_resolve_field_binding(parent, source_field=rule.source_path,
            target_field=field, mapping_definition_id=rule.definition_id, mapping_revision=contract.revision,
            direction=MappingDirection.HERO_TO_FRAPPE)
        if (binding.acknowledged_source_fingerprint, binding.acknowledged_target_fingerprint) != (actual, actual):
            registry.record_verified_acknowledgment(binding, expected_revision=binding.revision,
                mapping_definition_id=rule.definition_id, mapping_revision=contract.revision,
                source_fingerprint=actual, target_fingerprint=actual, evidence_id=evidence,
                version=binding.acknowledged_version + 1, value_json=json.dumps(target.get(field), default=str))
    for event in equal_reviews:
        finish_field_review(event, "CONVERGED")
    if conflicting or pending:
        detail = "Bound fields awaiting review: " + ", ".join(sorted(set(conflicting) | {
            field for field, binding in fields.items() if binding.identity_key in pending}))
        return held("CONFLICT", detail, target_doctype=doctype, target_name=target.name,
                    verified_object=parent.identity_key) if held else "conflict"
    if held and source_type == "customer" and plan.adapter_options and isinstance(record.get("address"), dict):
        # Carry only the canonical claim verified in this completed record transaction.
        from dataclasses import replace
        return replace(result, verified_object=parent.identity_key)
    return result


class InventoryIntake:
    """Materialize unbound rows with the approved single-record migration and its deferred worker."""

    def __init__(self, selection, contract, source_revision, *, allow_new=False, reader_fingerprint="", sync_run="", direct_read=None, product_usage=None, file_configuration=None):
        self.selection = selection
        self.contract = contract
        self.source_revision = source_revision
        self.origin = None
        self.has_conflicts = False
        self.allow_new = allow_new
        self.reader_fingerprint = reader_fingerprint
        self.sync_run = sync_run
        self.direct_read = direct_read
        self.file_configuration = file_configuration
        self.blobs = {}
        self.product_usage = product_usage
        self.plan = None
        self.related_profiles = None

    def begin_page(self):
        self.origin = None
        self.has_conflicts = False
        self.blobs = {}

    def prepare_file_blobs(self, rows, cleanup):
        from hero_integration.services import file_transfer
        self.blobs = file_transfer.prepare_inventory_blobs(self.direct_read, rows, self.selection.source_instance,
            self.file_configuration, cleanup, allow_new=self.allow_new)

    def carry_employee_revision(self, row):
        read = self.direct_read
        if not read or read.operation != "HeroEmployees" or not read.validate_write:
            return False
        from hero_integration.services.employee_binding_revision import carry_employee_bindings
        read.validate_write()
        carry_employee_bindings(read, row)
        return True

    def _binding_evidence(self, identity):
        from hero_integration.services.binding_registry import FrappeBindingStorage, matches_incarnation
        selection, plan = self.selection, self.plan
        scope, source = selection.site_scope, selection.source_instance
        contract = plan.binding_contract
        storage = FrappeBindingStorage(frappe)
        parent = storage.lock_source_object(scope, source, contract.object_type, plan.target_doctype, identity)
        if not parent or parent.lifecycle != "VALID" or parent.reconciliation:
            return None
        if parent.mapping_definition_id != contract.definition_id or parent.mapping_revision != contract.revision:
            return None
        link = locked_provenance(source, link_source_type(self.contract.target_operation), identity, plan.target_doctype)
        if not link or link.target_name != parent.target_name:
            return None
        creation = frappe.db.get_value(plan.target_doctype, parent.target_name, "creation", for_update=True)
        if not matches_incarnation(parent.target_incarnation, creation):
            return None
        fields = storage.fields_for_object(scope, parent.identity_key, for_update=True)
        return [parent.identity_key, parent.target_incarnation, sorted(field.identity_key for field in fields)]

    def _observe_mapped_relations(self, row, link):
        from hero_integration.services import mapped_update_v2, mapped_send
        from hero_integration.services.change_detection import recover_related_changes
        if self.related_profiles is None:
            profiles = frappe.get_all("HERO GraphQL Mapping", filters={"source_instance":self.selection.source_instance,
                "schema_snapshot":self.selection.schema_snapshot, "direction":"FRAPPE_TO_MUTATION","status":"Active",
                "operation_kind":"MUTATION"}, fields=["name","mapping_json"], limit_page_length=0)
            self.related_profiles = []
            for profile in profiles:
                content = json.loads(profile.mapping_json)
                if (content.get("usage") == "MAPPED_UPDATE_V2" and content.get("read_service") == self.selection.graphql_service
                        and content.get("area") == self.contract.target_operation and content.get("assignments")):
                    self.related_profiles.append(profile.name)
        for mapping in self.related_profiles:
            try:
                ctx = mapped_update_v2._load_context(mapping, self.plan.target_doctype, link.target_name)
                if (ctx.read.mapping.name != self.selection.graphql_mapping or ctx.read.contract != self.contract
                        or str(ctx.hero_id) != str(self.plan.source_identity(row))):
                    continue
                prepared = mapped_update_v2._prepare(ctx, None, preview_only=True, observed_record=row)
            except (mapped_update_v2.MappedUpdateError, mapped_send.MappedSendError, frappe.DoesNotExistError, frappe.PermissionError):
                # Incomplete outbound authority cannot inhibit the independently approved inbound adapter.
                continue
            recover_related_changes(ctx, prepared, self.sync_run)

    def _receipt_id(self, identity):
        from hashlib import sha256
        selection = self.selection
        return sha256(json.dumps(["inventory-adapter", selection.site_scope, selection.source_instance,
            self.reader_fingerprint, selection.graphql_mapping, selection.mapping_revision, identity]).encode()).hexdigest()

    def defer_task_write(self, row, identity, error):
        """Persist the rejected projection in the existing review queue before the page commits."""
        from hero_integration.services import deferred_references as references, migration
        from hero_integration.services.integration_events import record_output
        from hero_integration.services.migration_replay import count_result
        from hero_integration.services.sync_engine import FrappeBoundProcessor, SyncConflict

        run, plan = self._origin_run()
        source = self.selection.source_instance
        proof = references._origin_binding(source, "HeroTasks", identity)
        if (self.contract.target_operation != "HeroTasks" or not proof or proof["target_doctype"] != "Task"
                or proof["target_name"] != error.target_name or str(plan.source_identity(row)) != identity):
            raise SyncConflict("Rejected Task write has no matching migration identity.")
        record = {**row, "_hero_source_instance": source, "__hero_origin_binding": proof}
        references.save(run.name, source, "HeroTasks", identity, plan.mapping_version, record,
                        references.references(plan, row), result_code="BLOCKED_RECORD", defer_empty=True,
                        native_task_denial=True)
        key = references.observation_key(source, "HeroTasks", identity, run.name)
        if frappe.db.get_value(references.MAIN, key, "status", for_update=True) != "Conflict":
            raise SyncConflict("Native write review was not retained.")
        result = migration.Result("BLOCKED_RECORD", str(error), "Task", error.target_name)
        record_output(run=run.name, source_type="Task", source_id=identity, result=result.code,
                      action="hero_to_frappe", detail=result.detail, target_doctype="Task", target_name=error.target_name)
        count_result(run, result)
        self.has_conflicts = True
        # A subsequent successful scalar retry must also re-run the adapter and supersede
        # this review, even if an earlier page already admitted the same domain snapshot.
        receipt = self._receipt_id(identity)
        if frappe.db.exists(FrappeBoundProcessor.EVENT, receipt):
            frappe.db.set_value(FrappeBoundProcessor.EVENT, receipt, "source_version", "")

    def reconcile(self, row, identity, fields, *, require_existing_owner=False):
        """Admit a changed collection snapshot once to the domain adapter and its worker."""
        from hero_integration.services.deferred_references import MAIN, observation_key
        from hero_integration.services.offset_pagination import fingerprint
        from hero_integration.services.sync_engine import FrappeBoundProcessor, SyncStatus

        selection = self.selection
        operation = self.contract.target_operation
        source_type = link_source_type(operation)
        if self.plan is None:
            from hero_integration.services import migration
            self.plan = self.direct_read.plan if self.direct_read else migration._mapping_plan_for_run(selection, operation)
        if operation == "HeroProducts":
            from hero_integration.services.product_migration import _was_used
            if self.product_usage:
                eligible = identity in self.product_usage[1]
            elif "Genutzt" in row:
                eligible = _was_used(row)
            else:
                return SyncStatus.PARTIAL
            if not eligible:
                return True
        owner = self._binding_evidence(identity)
        if not owner and not fields and operation == "HeroEmployees" and self.carry_employee_revision(row):
            owner = self._binding_evidence(identity)
        if require_existing_owner and not owner:
            return SyncStatus.CONFLICT
        if owner:
            link = locked_provenance(selection.source_instance, source_type, identity, self.plan.target_doctype)
            if require_existing_owner and link.sync_status != "Synchronized":
                return SyncStatus.CONFLICT
            self._observe_mapped_relations(row, link)
        if not fields and not owner and not self.allow_new:
            if operation != "HeroProducts":
                return SyncStatus.PARTIAL
            from hero_integration.services.product_migration import original_pending_item
            link = locked_provenance(selection.source_instance, source_type, identity, "Item")
            if not link or link.sync_status != "Pending Review" or not frappe.db.exists("Item", link.target_name):
                return SyncStatus.PARTIAL
            if not original_pending_item(link, frappe.get_doc("Item", link.target_name, for_update=True), self.plan):
                return SyncStatus.PARTIAL
        if fields:
            # A field-only installation has no native migration link. Its ordinary
            # scalar comparisons remain authoritative; never adopt an unrelated row.
            link = locked_provenance(selection.source_instance, source_type, identity, fields[0].binding.target_doctype)
            if not link or link.target_name != fields[0].binding.target_name:
                return SyncStatus.PARTIAL
        receipt_id = self._receipt_id(identity)
        table = FrappeBoundProcessor.EVENT
        stored = frappe.db.get_value(table, receipt_id, ["source_version", "run_id"], as_dict=True, for_update=True)
        from hero_integration.services.offset_pagination import inventory_evidence
        evidence = inventory_evidence(row)
        digest = fingerprint([evidence, owner])
        current = owner and stored and stored.source_version == digest
        if current and self.file_configuration:
            from hero_integration.services.file_transfer import inventory_blob_present
            current = inventory_blob_present(link.target_name)
        if current:
            origin = stored.run_id
        else:
            from hero_integration.services.binding_registry import PendingOutbound
            try:
                result = self(row)
            except PendingOutbound:
                return SyncStatus.PARTIAL
            if result == SyncStatus.CONFLICT:
                self.has_conflicts = True
            if result not in (True, SyncStatus.PARTIAL):
                return result
            origin = self.origin.name
            if result == SyncStatus.PARTIAL and not frappe.db.exists(MAIN,
                    observation_key(selection.source_instance, operation, identity, origin)):
                return result  # No durable admitted work. Re-evaluate the row on the next pass.
            # Store the accepted post-write binding membership. Removal of a field
            # or recreation of the native target invalidates the admission receipt.
            digest = fingerprint([evidence, self._binding_evidence(identity)])
            if stored:
                frappe.db.set_value(table, receipt_id, {"source_version": digest, "run_id": origin})
            else:
                frappe.get_doc({"doctype": table, "event_id": receipt_id, "site_scope": selection.site_scope,
                    "source_instance": selection.source_instance, "object_type": self.plan.binding_contract.object_type,
                    "external_id": identity, "mapping_name": selection.graphql_mapping, "mapping_revision": selection.mapping_revision,
                    "source_version": digest, "run_id": origin, "outcome": "INVENTORY_ADMITTED"}).insert(ignore_permissions=True)
            if result == SyncStatus.PARTIAL:
                return result
        key = observation_key(selection.source_instance, operation, identity, origin)
        pending = frappe.db.get_value(MAIN, key, "status")
        if pending == "Conflict":
            return SyncStatus.CONFLICT
        return SyncStatus.PARTIAL if pending == "Waiting" else True

    def _origin_run(self):
        from hero_integration.services import migration
        from hero_integration.services.sync_page import BY_OPERATION

        operation = self.contract.target_operation
        area = BY_OPERATION.get(operation)
        if self.origin is None:
            if area:
                action = area.run_action
            else:
                from hero_integration.services.file_migration import DIRECT_OPERATIONS
                action = next(name for name, target in DIRECT_OPERATIONS.items() if target == operation)
            run = frappe.get_doc({"doctype": "HERO Integration Run", "action": action, "inventory_sync_run": self.sync_run,
                "mode": "Execute", "state": "Running", "started_on": frappe.utils.now_datetime(),
                "source_instance": self.selection.source_instance, "source_binding_revision": self.source_revision,
                "graphql_service": self.selection.graphql_service, "service_revision": self.selection.service_revision,
                "graphql_mapping": self.selection.graphql_mapping, "mapping_revision": self.selection.mapping_revision})
            if operation == "HeroProjectMatches":
                service = frappe.get_doc("HERO GraphQL Service", self.selection.graphql_service)
                mapping = frappe.get_doc("HERO GraphQL Mapping", self.selection.graphql_mapping)
                run.update(migration.customer_dependency(service, mapping))
            migration.bind_run_checkpoint(run)
            self.origin = run.insert()
            if self.plan is None:
                self.plan = self.direct_read.plan if self.direct_read else migration._mapping_plan_for_run(run, operation)
        return self.origin, self.plan

    def __call__(self, row):
        from hero_integration.services import migration
        from hero_integration.services.sync_engine import SyncStatus
        from hero_integration.services.sync_page import BY_OPERATION

        operation = self.contract.target_operation
        area = BY_OPERATION.get(operation)
        source_type = link_source_type(operation)
        run, plan = self._origin_run()
        record = {**row, "_hero_source_instance": self.selection.source_instance}
        # A missing field binding can still have a native target with an outstanding send.
        link = locked_provenance(self.selection.source_instance, source_type, plan.source_identity(record), plan.target_doctype)
        if link:
            require_clear_outbound(frappe, self.selection.site_scope, self.selection.source_instance,
                                   plan.target_doctype, link.target_name)
        if self.direct_read:
            if self.file_configuration:
                from hero_integration.services import file_transfer
                return file_transfer.apply_inventory_file(self.direct_read, row, self.origin,
                    self.selection.source_instance, self.blobs.get(plan.source_identity(record)))
            return self._direct_record(row, source_type)
        if area.operation == "HeroCustomers":
            write = lambda: migration._migrate_customer(record, run, plan=plan)
        elif area.operation == "HeroProjectMatches":
            write = lambda: migration._migrate_project(record, run, plan=plan)
        else:
            mapping = frappe.get_doc("HERO GraphQL Mapping", self.selection.graphql_mapping)
            scope = migration._projection_task_scope(run, plan, mapping, run.source_instance, run.source_instance)
            write = lambda: migration._migrate_projection_record(record, run, plan, scope)
        result = migration._migrate_with_binding(record, run, plan, area.source_type, write)
        migration._record_result(run, area.source_type, record, result)
        if result.code in {"BLOCKED_RECORD", "CONFLICT", "PENDING_REVIEW", "SKIPPED_AMBIGUOUS", "UNAVAILABLE"} or result.related_field_conflict:
            self.has_conflicts = True
            return SyncStatus.CONFLICT
        if result.code == "DEFERRED" or result.pending_relation:
            return SyncStatus.PARTIAL
        return result.code in {"CREATED", "UPDATED", "CONVERGED", "OBSERVED", "SKIPPED",
                               "SKIPPED_CONTACT_PERSON", "SKIPPED_SOURCE_DELETED", "SKIPPED_REJECTED"}

    def _direct_record(self, row, source_type):
        from hero_integration.services import direct_runtime, employee_migration, migration, product_migration, service_migration
        from hero_integration.services.binding_registry import BindingConflict
        from hero_integration.services.integration_events import record_output
        from hero_integration.services.sync_engine import SyncStatus

        read, run, instance = self.direct_read, self.origin, self.selection.source_instance
        identity = read.plan.source_identity(row)
        warnings = []
        if read.operation == "HeroProducts":
            links = product_migration._links(instance, [identity])
            def write():
                return product_migration.upsert(row, links.get(identity), product_migration._Masters(), instance,
                                                read=read, relation_warnings=warnings)
        elif read.operation == "HeroEmployees":
            row = dict(row)
            absences = row.pop("_hero_absences", None)
            company, *_ = employee_migration.preflight(instance, read=read)
            languages = frappe.get_all("Language", pluck="name", limit_page_length=0)
            def write():
                return employee_migration.upsert(row, instance, company, languages, read=read, absences=absences)
        else:
            row = dict(row)
            detail = row.pop("_hero_composition")
            company, _ = service_migration.preflight(instance, read)
            prior = service_migration.scoped_link(instance, source_type, identity, "Item")
            historical = row.pop("_hero_historical_products", {"requested": [], "reader": None, "rows": []})
            required = sorted(service_migration._historical_groups(prior, detail))
            if required != historical.get("requested") or required and not historical.get("reader"):
                return SyncStatus.PARTIAL
            def write():
                return service_migration.upsert(row, detail, read, instance, company, None)
        def apply():
            try:
                status = direct_runtime.process_record(read, row, instance, source_type, run.name, write)
                if read.operation == "HeroServices" and status in {"added","updated","unchanged","skipped_rejected"} and not read.plan.source_deleted(row):
                    service_migration.remember_relationships(run.name, instance, company, read, row, detail, status,
                                                           inventory_evidence=historical)
                if read.operation == "HeroEmployees" and status in {"added","updated","unchanged"}:
                    employee_migration.remember_relationships(run.name, instance, company, read, row)
            except (service_migration.ServiceBlocked, product_migration.PriceConflict, employee_migration.EmployeeMigrationBlocked) as error:
                raise BindingConflict(str(error)) from error
            return migration.Result({"added":"CREATED", "updated":"UPDATED", "unchanged":"CONVERGED",
                "skipped_rejected":"SKIPPED_REJECTED", "conflict":"CONFLICT"}.get(status,"PENDING_REVIEW"), "")
        result = migration._migrate_isolated(apply)
        if warnings:
            result = migration.Result("PENDING_REVIEW", "A mapped product relation requires review.")
        record_output(run=run.name, source_type=source_type, source_id=identity, result=result.code,
                      action="hero_to_frappe", detail=result.detail)
        from hero_integration.services.migration_replay import count_result
        count_result(run, result)
        if result.code in {"CONFLICT", "PENDING_REVIEW", "BLOCKED_RECORD"}:
            self.has_conflicts = True
            return SyncStatus.CONFLICT
        return True

    def finish(self):
        if self.origin:
            from functools import partial
            from hero_integration.services.deferred_references import notify_completed_run
            self.origin.state = "Completed With Conflicts" if self.has_conflicts else "Completed"
            self.origin.finished_on = frappe.utils.now_datetime()
            self.origin.save()
            frappe.db.after_commit.add(partial(notify_completed_run, self.origin.name))
