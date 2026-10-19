from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import frappe
from frappe.utils import now_datetime

from hero_integration.client.graphql import HeroGraphQLClient
from hero_integration.client.settings import get_configuration
from hero_integration.integrations.erpnext.company import confirmed_project_company
from hero_integration.integrations.erpnext.contact import (
    HomePhoneConflict,
    home_phone_for_customer,
    synchronize_home_phone,
)
from hero_integration.sources.hero import HeroSource, SourcePage
from hero_integration.services.mapping_runtime import MappingPlanError, active_mapping_plan
from hero_integration.services.project_naming import ProjectTitleValidationError, build_project_title


CUSTOMER_ACTIONS = {"Customer Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
PROJECT_ACTIONS = {"Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
TERMINAL_STATES = {"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked"}
PHASES = ("customer", "project")
PHASE_BLOCKING_RESULTS = {"BLOCKED_RECORD", "CONFLICT", "SKIPPED_AMBIGUOUS", "UNAVAILABLE"}


@dataclass(frozen=True)
class Result:
    code: str
    detail: str
    target_doctype: str = ""
    target_name: str = ""


class CustomerAddressError(ValueError):
    pass


def execute_run(run_name: str) -> None:
    run = frappe.get_doc("HERO Integration Run", run_name)
    frappe.set_user(run.owner)
    if run.state in TERMINAL_STATES:
        return
    if not _claim_run(run):
        return
    try:
        _require_active_mappings(run.action)
        config = get_configuration()
        source = HeroSource(
            HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds), config.source_instance,
        )
        _process_requested_phases(run, source)
        run.reload()
        if run.state == "Running":
            run.state = "Completed With Conflicts" if run.conflict_records else "Completed"
            run.finished_on = now_datetime()
            run.save()
            _mark_sync_success(run)
    except MappingPlanError as error:
        run.reload()
        _mark_running_phase_failed(run)
        run.state = "Blocked"
        run.error_message = str(error)
        run.finished_on = now_datetime()
        run.save()
        _mark_sync_error(run)
    except _Cancelled:
        run.reload()
        run.state = "Cancelled"
        run.finished_on = now_datetime()
        run.save()
    except Exception:
        run.reload()
        _mark_running_phase_failed(run)
        run.state = "Failed"
        run.error_message = "Migration failed. See Error Log for the redacted server exception."
        run.finished_on = now_datetime()
        run.save()
        _mark_sync_error(run)
        frappe.log_error(title="HERO Integration migration failed")
        raise
    finally:
        _release_run(run.name)


def _claim_run(run) -> bool:
    state = frappe.get_single("HERO Integration Sync State")
    if state.active_run and state.active_run != run.name:
        active = frappe.get_doc("HERO Integration Run", state.active_run)
        if active.state not in TERMINAL_STATES:
            run.state = "Blocked"
            run.error_message = f"Another HERO run is active: {active.name}."
            run.finished_on = now_datetime()
            run.save()
            return False
    state.active_run = run.name
    state.save()
    run.state = "Running"
    run.started_on = run.started_on or now_datetime()
    run.save()
    return True


def _release_run(run_name: str) -> None:
    state = frappe.get_single("HERO Integration Sync State")
    if state.active_run == run_name:
        state.active_run = ""
        state.save()


def _process_requested_phases(run, source: HeroSource) -> None:
    # A project run is never allowed to bypass its own complete Customer pass.
    # This is intentionally stricter than a mere lookup during project creation:
    # the gate belongs to the same run and source instance, and survives retries.
    _bind_source_instance(run, source.source_instance)
    needs_customer_phase = run.action in CUSTOMER_ACTIONS or run.action in PROJECT_ACTIONS
    if needs_customer_phase and not _process_phase(run, "customer", source.customers):
        return
    if run.action in PROJECT_ACTIONS:
        if not _customer_phase_completed(run, source.source_instance):
            _block_run(run, "Project phase requires a completed Customer phase for this source instance and run.")
            return
        if not _process_phase(run, "project", source.projects):
            return
    if run.action == "Full Data Migration":
        _record_result(
            run,
            "attachment",
            {"id": "hero-v1-contract"},
            Result("UNAVAILABLE", "No approved HERO attachment query exists in the V1 source contract."),
        )


def _require_active_mappings(action: str) -> None:
    operations = []
    # Project imports deliberately require the Customer plan as well, because
    # they first execute the complete Customer phase of this run.
    if action in CUSTOMER_ACTIONS or action in PROJECT_ACTIONS:
        operations.append("HeroCustomers")
    if action in PROJECT_ACTIONS:
        operations.append("HeroProjectMatches")
    for operation in operations:
        active_mapping_plan(operation)


def _process_phase(run, phase: str, reader) -> bool:
    if _phase_state(run, phase) == "Completed":
        return True
    _set_phase_state(run, phase, "Running")
    offset = int(run.resume_offset or 0) if run.resume_phase == phase else 0
    record_index = int(run.resume_record_index or 0) if run.resume_phase == phase else 0
    while True:
        _cancel_if_requested(run)
        page: SourcePage = reader(offset)
        for index, record in enumerate(page.records):
            if index < record_index:
                continue
            _cancel_if_requested(run)
            result = _migrate_customer(record, run) if phase == "customer" else _migrate_project(record, run)
            _record_result(run, phase, record, result)
            _record_phase_result(run, phase, result)
            run.resume_phase = phase
            run.resume_offset = offset
            run.resume_record_index = index + 1
            run.save()
        run.resume_phase = phase
        run.resume_offset = page.next_offset or 0
        run.resume_record_index = 0
        run.save()
        if page.next_offset is None:
            if _phase_has_blockers(run, phase):
                _set_phase_state(run, phase, "Blocked")
                _block_run(run, f"Customer phase contains unresolved records; Project phase was not started." if phase == "customer" else f"Project phase contains unresolved records.")
                return False
            _set_phase_state(run, phase, "Completed")
            return True
        offset = page.next_offset
        record_index = 0


def _cancel_if_requested(run) -> None:
    run.reload()
    if run.state == "Cancelled":
        raise _Cancelled()


def _migrate_customer(record: dict[str, object], run) -> Result:
    try:
        plan = active_mapping_plan("HeroCustomers")
        source_id = plan.source_identity(record)
        values = plan.values(record)
        address = _customer_primary_address(record)
    except MappingPlanError as error:
        return Result("BLOCKED_RECORD", str(error))
    if plan.source_deleted(record):
        _mark_source_deleted(_link("customer", source_id, _source_instance(record)))
        return Result("SKIPPED_SOURCE_DELETED", "Source customer is deleted. No target deletion occurs.")
    if _is_contact_person(record):
        return Result("SKIPPED_CONTACT_PERSON", "Source contact person is not created as an ERPNext Customer.")
    if not values.get("customer_name"):
        return Result("BLOCKED_RECORD", "Configured customer record name is empty.")
    return _upsert(
        "customer", source_id, record, values, "Customer", "customer_name", plan.mapping_version, run,
        after_insert=lambda customer: _synchronize_customer_relations(
            customer, plan.home_phone(record), address, _source_instance(record), plan.mapping_version,
        ),
        target_snapshot=lambda customer: _customer_target_values(customer, values, plan.home_phone(record)),
    )


def _migrate_project(record: dict[str, object], run) -> Result:
    if not _target_available("Project"):
        return Result("UNAVAILABLE", "Project is not installed on this site.")
    try:
        plan = active_mapping_plan("HeroProjectMatches")
        source_id = plan.source_identity(record)
        source_instance = _source_instance(record)
        values = plan.values(
            record, lambda value: _customer_name_for_source(value, source_instance), _confirmed_company_name,
            _project_title_for_record,
        )
        values["custom_hero_source_id"] = f"{source_instance}:{source_id}"
    except (MappingPlanError, ProjectTitleValidationError) as error:
        return Result("BLOCKED_RECORD", str(error))
    if not values.get("project_name"):
        return Result("BLOCKED_RECORD", "Configured project record name is empty.")
    return _upsert(
        "project", source_id, record, values, "Project", "project_name", plan.mapping_version, run,
        source_instance=source_instance,
    )


def _upsert(
    source_type,
    source_id,
    record,
    values,
    doctype,
    name_field,
    mapping_version,
    run,
    *,
    after_insert=None,
    target_snapshot=None,
    source_instance: str = "default",
) -> Result:
    if not _target_available(doctype):
        return Result("UNAVAILABLE", f"{doctype} is not installed on this site.")
    source_hash = _digest(record)
    existing = _link(source_type, source_id, source_instance)
    if existing:
        if not frappe.db.exists(existing.target_doctype, existing.target_name):
            _mark_link_conflict(existing, "Stored target reference no longer exists.")
            return Result("CONFLICT", "Stored target reference no longer exists.")
        target = frappe.get_doc(existing.target_doctype, existing.target_name)
        snapshot = target_snapshot or (lambda item: _target_values(item, values))
        if _digest(snapshot(target)) != existing.target_payload_hash:
            _mark_link_conflict(existing, "Target changed outside HERO Integration.")
            return Result("CONFLICT", "Target changed outside HERO Integration.", doctype, target.name)
        if existing.source_payload_hash != source_hash:
            _mark_link_conflict(existing, "HERO source changed. Review is required before an update.")
            return Result("CONFLICT", "HERO source changed. Review is required before an update.", doctype, target.name)
        return Result("SKIPPED", "Record is already synchronized.", doctype, target.name)
    duplicate = frappe.db.exists(doctype, {name_field: values[name_field]})
    if duplicate:
        return Result("SKIPPED_AMBIGUOUS", f"Existing {doctype} has no stable HERO link.", doctype, duplicate)
    if run.mode == "Preview":
        return Result("PREVIEW_OK", "A new target record would be created.", doctype)
    target = frappe.get_doc({"doctype": doctype, **values})
    target.insert()
    try:
        if after_insert:
            after_insert(target)
    except (HomePhoneConflict, CustomerAddressError) as error:
        return Result("CONFLICT", str(error), doctype, target.name)
    snapshot = target_snapshot or (lambda item: _target_values(item, values))
    target_hash = _digest(snapshot(target))
    frappe.get_doc({
        "doctype": "HERO Integration Link", "source_system": "HERO", "source_type": source_type,
        "source_id": source_id, "source_instance": source_instance, "sync_status": "Synchronized",
        "conflict_detail": "", "last_synced_on": now_datetime(),
        "target_doctype": doctype, "target_name": target.name, "source_payload_hash": source_hash,
        "target_payload_hash": target_hash, "mapping_version": mapping_version,
    }).insert()
    return Result("CREATED", "Target created through native Frappe validation.", doctype, target.name)


def _record_result(run, source_type: str, record: dict[str, object], result: Result) -> None:
    source_id = str(record.get("id")) if isinstance(record.get("id"), int) else "unknown"
    frappe.get_doc({
        "doctype": "HERO Integration Record", "run": run.name, "source_type": source_type,
        "source_id": source_id, "result": result.code, "detail": result.detail,
        "target_doctype": result.target_doctype, "target_name": result.target_name,
    }).insert()
    run.processed = int(run.processed or 0) + 1
    if result.code == "CREATED":
        run.created_records = int(run.created_records or 0) + 1
    elif result.code in {"CONFLICT", "SKIPPED_AMBIGUOUS", "BLOCKED_RECORD", "UNAVAILABLE"}:
        run.conflict_records = int(run.conflict_records or 0) + 1
    else:
        run.skipped_records = int(run.skipped_records or 0) + 1


def _target_available(doctype: str) -> bool:
    return bool(frappe.db.exists("DocType", doctype))


def _link(source_type: str, source_id: str, source_instance: str = "default"):
    name = frappe.db.exists(
        "HERO Integration Link",
        {"source_type": source_type, "source_id": source_id, "source_instance": source_instance},
    )
    return frappe.get_doc("HERO Integration Link", name) if name else None


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _target_values(target, values: dict[str, object]) -> dict[str, object]:
    return {field: target.get(field) for field in values}


def _customer_target_values(target, values: dict[str, object], home_phone: str | None) -> dict[str, object]:
    snapshot = _target_values(target, values)
    if home_phone:
        snapshot["hero_phone_home"] = home_phone_for_customer(target)
    return snapshot


def _mark_source_deleted(link) -> None:
    if not link:
        return
    link.sync_status = "Source Deleted"
    link.conflict_detail = "Source customer is deleted; no ERPNext target deletion was performed."
    _save_link(link)


def _mark_link_conflict(link, detail: str) -> None:
    link.sync_status = "Conflict"
    link.conflict_detail = detail
    _save_link(link)


def _save_link(link) -> None:
    if hasattr(link, "save"):
        link.save()


def _customer_name_for_source(value: object, source_instance: str = "default") -> str:
    source_id = str(value) if isinstance(value, int) and value > 0 else ""
    customer_link = _link("customer", source_id, source_instance)
    if not customer_link or customer_link.target_doctype != "Customer":
        raise MappingPlanError("The configured HERO customer relation has no unambiguous ERPNext link.")
    return customer_link.target_name


def _source_instance(record: dict[str, object]) -> str:
    value = record.get("_hero_source_instance")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


def _is_contact_person(record: dict[str, object]) -> bool:
    """Contact persons have a distinct HERO lifecycle and must not become Customers."""
    return record.get("is_contact_person") is True


def _customer_primary_address(record: dict[str, object]) -> dict[str, object] | None:
    """Validate only the explicitly selected primary Customer.address relation."""
    value = record.get("address")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MappingPlanError("Customer primary address does not match the approved source contract.")
    source_id = value.get("id")
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise MappingPlanError("Customer primary address identity is missing or invalid.")
    fields = {key: value.get(key) for key in ("street", "zipcode", "city")}
    if not all(isinstance(item, str) and item.strip() for item in fields.values()):
        raise MappingPlanError("Customer primary address is incomplete.")
    return {"id": str(source_id), **{key: item.strip() for key, item in fields.items()}}


def _synchronize_customer_relations(customer, home_phone, address, source_instance: str, mapping_version: str) -> None:
    synchronize_home_phone(customer, home_phone)
    if address is not None:
        _synchronize_customer_address(customer, address, source_instance, mapping_version)


def _synchronize_customer_address(customer, address: dict[str, object], source_instance: str, mapping_version: str) -> None:
    if not _target_available("Address"):
        raise CustomerAddressError("Address is not installed on this site.")
    source_id = str(address["id"])
    values = {
        "address_title": customer.name,
        "address_line1": address["street"],
        "pincode": address["zipcode"],
        "city": address["city"],
    }
    payload = {"customer": customer.name, **address}
    existing = _link("customer_address", source_id, source_instance)
    if existing:
        if not frappe.db.exists("Address", existing.target_name):
            _mark_link_conflict(existing, "Stored customer address reference no longer exists.")
            raise CustomerAddressError("Stored customer address reference no longer exists.")
        target = frappe.get_doc("Address", existing.target_name)
        if _digest(_target_values(target, values)) != existing.target_payload_hash:
            _mark_link_conflict(existing, "Customer address changed outside HERO Integration.")
            raise CustomerAddressError("Customer address changed outside HERO Integration.")
        if existing.source_payload_hash != _digest(payload):
            _mark_link_conflict(existing, "HERO customer address changed. Review is required before an update.")
            raise CustomerAddressError("HERO customer address changed. Review is required before an update.")
        return
    target = frappe.get_doc({
        "doctype": "Address", **values,
        "links": [{"link_doctype": "Customer", "link_name": customer.name}],
    })
    target.insert()
    frappe.get_doc({
        "doctype": "HERO Integration Link", "source_system": "HERO", "source_type": "customer_address",
        "source_id": source_id, "source_instance": source_instance, "sync_status": "Synchronized",
        "conflict_detail": "", "last_synced_on": now_datetime(), "target_doctype": "Address",
        "target_name": target.name, "source_payload_hash": _digest(payload),
        "target_payload_hash": _digest(_target_values(target, values)), "mapping_version": mapping_version,
    }).insert()


def _bind_source_instance(run, source_instance: str) -> None:
    instance = source_instance.strip() if isinstance(source_instance, str) and source_instance.strip() else "default"
    current = getattr(run, "source_instance", "") or ""
    if current and current != instance:
        _block_run(run, "A resumed migration run cannot change its HERO source instance.")
        raise MappingPlanError("Migration source instance does not match the persisted run.")
    if not current:
        run.source_instance = instance
        run.save()


def _phase_state(run, phase: str) -> str:
    return str(getattr(run, f"{phase}_phase_state", "") or "Queued")


def _set_phase_state(run, phase: str, state: str) -> None:
    setattr(run, f"{phase}_phase_state", state)
    run.save()


def _record_phase_result(run, phase: str, result: Result) -> None:
    prefix = f"{phase}_phase"
    setattr(run, f"{prefix}_processed", int(getattr(run, f"{prefix}_processed", 0) or 0) + 1)
    if result.code == "CREATED":
        field = f"{prefix}_created"
    elif result.code in PHASE_BLOCKING_RESULTS:
        field = f"{prefix}_blocked"
    else:
        field = f"{prefix}_skipped"
    setattr(run, field, int(getattr(run, field, 0) or 0) + 1)


def _phase_has_blockers(run, phase: str) -> bool:
    return bool(int(getattr(run, f"{phase}_phase_blocked", 0) or 0))


def _customer_phase_completed(run, source_instance: str) -> bool:
    return (
        _phase_state(run, "customer") == "Completed"
        and (getattr(run, "source_instance", "") or "default") == source_instance
    )


def _block_run(run, detail: str) -> None:
    run.state = "Blocked"
    run.error_message = detail
    run.finished_on = now_datetime()
    run.save()


def _mark_running_phase_failed(run) -> None:
    for phase in PHASES:
        if _phase_state(run, phase) == "Running":
            setattr(run, f"{phase}_phase_state", "Failed")


def _confirmed_company_name() -> str:
    company = confirmed_project_company()
    if not company.is_valid:
        raise MappingPlanError(company.reason)
    return company.name


def _project_title_for_record(record: dict[str, object]) -> str:
    """Resolve the ERPNext field limit before constructing a deterministic title."""
    field = frappe.get_meta("Project").get_field("project_name")
    limit = getattr(field, "length", None)
    # Frappe Data fields use 140 when no site-specific explicit length is set.
    max_length = int(limit) if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 140
    return build_project_title(record, max_length=max_length)


def _mark_sync_success(run) -> None:
    if run.action != "Sync":
        return
    settings = frappe.get_single("HERO Integration Settings")
    settings.last_successful_sync = now_datetime()
    settings.sync_status = "Completed"
    settings.sync_error = ""
    settings.save()


def _mark_sync_error(run) -> None:
    if run.action != "Sync":
        return
    settings = frappe.get_single("HERO Integration Settings")
    settings.sync_status = run.state
    settings.sync_error = run.error_message
    settings.save()


class _Cancelled(Exception):
    pass
