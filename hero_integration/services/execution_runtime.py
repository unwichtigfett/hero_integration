"""Dispatch imports and sync runs and fence each native migration adapter.

Only the scheduler decides when to call the scheduled operations. Existing
queue paths remain callable for jobs created before an upgrade.
"""
import json
from contextvars import ContextVar
import frappe


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
            return polling.run_monitor()
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
        as_dict=True, limit=2, for_update=True)
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


def record_field_conflict(parent, binding, target, incoming, row, *, run, mapping, reader=None):
    import json
    from hashlib import sha256

    source_revision = frappe.db.get_single_value("HERO Integration Settings", "source_binding_revision") or 0
    proof = {"field": binding.target_field, "binding": binding.identity_key, "object": parent.identity_key,
             "incarnation": parent.target_incarnation, "source_field": binding.source_field,
             "source_instance": parent.source_instance, "source_type": parent.object_type,
             "external_id": parent.external_object_id, "doctype": target.doctype, "name": target.name,
             "source": incoming, "local": target.get(binding.target_field),
             "baseline": json.loads(binding.acknowledged_value_json) if binding.acknowledged_value_json else None,
             "baseline_known": bool(binding.acknowledged_value_json),
             "source_baseline_hash": binding.acknowledged_source_fingerprint,
             "target_baseline_hash": binding.acknowledged_target_fingerprint,
             "mapping": binding.mapping_definition_id, "mapping_revision": binding.mapping_revision,
             "object_revision": parent.revision, "binding_revision": binding.revision,
             "ack_version": binding.acknowledged_version, "source_revision": int(source_revision),
             "remote_revision": row.get("modified"), "target_revision": str(target.modified)}
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
        "outcome": "FIELD_CONFLICT"}, ["open_field", "name", "resolution_json"], as_dict=True, for_update=True)
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
    operation = plan.target_operation or plan.operation
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
    if source_type == "customer_address" and (lifecycle or plan.source_deleted(record)):
        raise BindingConflict("The owning Customer is ineligible for primary address synchronization.")
    if not relationships_only and (lifecycle or plan.source_deleted(record)):
        reason = lifecycle or "DELETED"
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
        raise BindingConflict("The historical link has no verified native incarnation. Identity review is required.")
    if before:
        before.check_permission("write")
    else:
        frappe.has_permission(doctype, "create", throw=True)
    rules = {rule.definition_id: rule for rule in plan.definitions}
    fields = {}
    protected = {}
    decisions = {}
    deleted = record.get("is_deleted") is True or record.get("deleted") is True
    pending = open_field_reviews(parent) if parent else {}
    equal_reviews = []
    conflicting = []
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
            incoming = FrappeBoundProcessor.project_scalar(plan, rule, record, instance)
            remote = native_fingerprint(incoming, fieldtype) if incoming is not None else None
            decision = registry.inbound_reconciliation(binding, active_mapping_revision=contract.revision,
                                                       source_fingerprint=remote, target_fingerprint=current)
            review = pending.get(binding.identity_key)
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
            if decision in {"KEEP_LOCAL", "OMITTED"}:
                protected[field] = current
                decisions[field] = "local" if decision == "KEEP_LOCAL" else None
            elif decision == "APPLY_SOURCE":
                decisions[field] = "apply"
            else:
                decisions[field] = "unchanged" if remote == binding.acknowledged_source_fingerprint else "converged"
            fields[field] = binding

    token = _migration_decisions.set((doctype, name, decisions))
    try:
        with managed_target_update(doctype, name, None):
            result = write(before) if reader is not None else write()
    finally:
        _migration_decisions.reset(token)
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
        incoming = FrappeBoundProcessor.project_scalar(plan, rule, record, instance)
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
