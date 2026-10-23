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
    primary_contact_values,
    synchronize_primary_contact,
    synchronize_home_phone,
)
from hero_integration.sources.hero import GraphQLServiceSource, HeroSource, SourcePage
from hero_integration.services.change_detection import changed_fields, decode as decode_hashes, encode as encode_hashes, field_hashes
from hero_integration.services.conflict_notifications import notify_conflict_once
from hero_integration.services.integration_events import record_output
from hero_integration.services.mapping_runtime import MappingPlanError, active_mapping_plan
from hero_integration.services.project_naming import ProjectTitleValidationError, build_project_title
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS, validate_projection_record


CUSTOMER_ACTIONS = {"Customer Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
PROJECT_ACTIONS = {"Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
TERMINAL_STATES = {"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked"}
PHASES = ("customer", "project")
PROJECTION_ACTIONS = {"Projection Migration", "Schedule Migration"}
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
        validate_run_mappings(run)
        config = get_configuration()
        source = _source_for_run(run, config)
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


def _source_for_run(run, config):
    """Bind every newly queued run to its approved immutable read revisions."""
    if not getattr(run, "graphql_service", ""):
        # Historical runs keep their recorded legacy behavior; new API calls
        # never create this form.
        return HeroSource(HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds), config.source_instance)
    from hero_integration.services.read_contract import ReadContractError, from_documents

    service = frappe.get_doc("HERO GraphQL Service", run.graphql_service)
    mapping = frappe.get_doc("HERO GraphQL Mapping", run.graphql_mapping)
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    contract = from_documents(service, mapping, snapshot)
    if int(run.service_revision or 0) != contract.service_revision or int(run.mapping_revision or 0) != contract.mapping_revision:
        raise MappingPlanError("The queued run no longer matches its approved service or mapping revision.")
    source = GraphQLServiceSource(HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds), service, contract, getattr(snapshot, "source_instance", "") or config.source_instance)
    if contract.target_operation == 'HeroProjectMatches':
        customer_service, customer_mapping, customer_snapshot = _customer_read_documents(run)
        customer_contract = from_documents(customer_service, customer_mapping, customer_snapshot)
        source.customer_source = GraphQLServiceSource(source.client, customer_service, customer_contract, source.source_instance)
    return source


def customer_dependency(service, mapping):
    """Resolve an explicitly configured same-snapshot dependency before queueing."""
    from hero_integration.services.read_contract import from_documents
    config = json.loads(mapping.mapping_json)
    if config.get('target_operation') != 'HeroProjectMatches':
        return {}
    name = config.get('customer_service')
    if not isinstance(name, str) or not name:
        raise MappingPlanError('Project read mapping requires an approved customer_service dependency.')
    customer_service = frappe.get_doc('HERO GraphQL Service', name)
    customer_mapping = frappe.get_doc('HERO GraphQL Mapping', customer_service.read_mapping)
    snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', service.schema_snapshot)
    contract = from_documents(customer_service, customer_mapping, snapshot)
    if contract.target_operation != 'HeroCustomers':
        raise MappingPlanError('Customer dependency must use the Customer adapter on the same snapshot.')
    return {'customer_service': customer_service.name, 'customer_service_revision': contract.service_revision,
            'customer_mapping': customer_mapping.name, 'customer_mapping_revision': contract.mapping_revision}


def _customer_read_documents(run):
    from hero_integration.services.read_contract import from_documents
    if not getattr(run, 'customer_service', '') or not getattr(run, 'customer_mapping', ''):
        raise MappingPlanError('This Project run has no frozen Customer dependency.')
    service = frappe.get_doc('HERO GraphQL Service', run.customer_service)
    mapping = frappe.get_doc('HERO GraphQL Mapping', run.customer_mapping)
    primary = frappe.get_doc('HERO GraphQL Service', run.graphql_service)
    snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', primary.schema_snapshot)
    contract = from_documents(service, mapping, snapshot)
    if contract.target_operation != 'HeroCustomers' or contract.service_revision != int(run.customer_service_revision) or contract.mapping_revision != int(run.customer_mapping_revision):
        raise MappingPlanError('The frozen Customer dependency changed; create a newly approved run.')
    return service, mapping, snapshot


def _claim_run(run) -> bool:
    """Claim the Single-state slot under the database row lock.

    Queue job IDs are unique only per run, so they cannot serialize two
    independently requested runs.  The ``tabSingles`` row is the one durable
    shared slot and is locked before either worker observes or replaces it.
    """
    if not _acquire_active_run_lock():
        _block_run(run, "Could not acquire the HERO run reservation lock.")
        return False
    try:
        active_run = _locked_active_run()
        if active_run and active_run != run.name:
            active_state = frappe.db.get_value("HERO Integration Run", active_run, "state")
            if active_state not in TERMINAL_STATES:
                _block_run(run, f"Another HERO run is active: {active_run}.")
                return False
        frappe.db.sql(
            "UPDATE `tabSingles` SET `value` = %s WHERE `doctype` = %s AND `field` = %s",
            (run.name, "HERO Integration Sync State", "active_run"),
        )
        run.state = "Running"
        run.started_on = run.started_on or now_datetime()
        run.save()
        # Persist the reservation before another connection may inspect it or
        # this worker starts any source I/O.  Releasing the advisory lock
        # before that commit can otherwise expose an in-flight row version.
        frappe.db.commit()
        return True
    except frappe.QueryDeadlockError:
        # MariaDB can still surface a stale-row conflict to a connection that
        # lost the first-use race.  It has not obtained a reservation; roll
        # back that transaction and persist a normal blocked outcome rather
        # than letting a worker fail or retry against an unknown state.
        frappe.db.rollback()
        run.reload()
        _block_run(run, "Another connection is establishing the HERO run reservation; run was not started.")
        frappe.db.commit()
        return False
    finally:
        _release_active_run_lock()


def _release_run(run_name: str) -> None:
    # Do not clear a slot acquired by a later worker after this run finished.
    frappe.db.sql(
        "UPDATE `tabSingles` SET `value` = '' WHERE `doctype` = %s AND `field` = %s AND `value` = %s",
        ("HERO Integration Sync State", "active_run", run_name),
    )


def _locked_active_run() -> str:
    """Return the slot value while holding its transaction-local row lock."""
    doctype, field = "HERO Integration Sync State", "active_run"
    rows = frappe.db.sql(
        "SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s FOR UPDATE",
        (doctype, field),
        as_dict=True,
    )
    if not rows:
        # The connection-scoped lock in ``_claim_run`` serializes this
        # first-use insert.  Avoid INSERT IGNORE here: MariaDB can otherwise
        # report a stale-record error to the losing transaction even after the
        # winner committed its reservation.
        frappe.db.sql(
            "INSERT INTO `tabSingles` (`doctype`, `field`, `value`) VALUES (%s, %s, '')",
            (doctype, field),
        )
        return ""
    return str(rows[0].value or "") if rows else ""


def _acquire_active_run_lock() -> bool:
    """Serialize first-use upserts before taking the durable row lock.

    MariaDB can deadlock concurrent first-use creation of a missing
    ``tabSingles`` row.  The named lock is connection-scoped and only guards
    row creation/claim setup; the row lock remains the durable transaction
    boundary for the executing run.
    """
    rows = frappe.db.sql("SELECT GET_LOCK(%s, %s) AS acquired", ("hero_integration:active_run", 10), as_dict=True)
    return bool(rows and int(rows[0].acquired or 0) == 1)


def _release_active_run_lock() -> None:
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", ("hero_integration:active_run",))


def _process_requested_phases(run, source: HeroSource) -> None:
    # A project run is never allowed to bypass its own complete Customer pass.
    # This is intentionally stricter than a mere lookup during project creation:
    # the gate belongs to the same run and source instance, and survives retries.
    _bind_source_instance(run, source.source_instance)
    if run.action in PROJECTION_ACTIONS:
        _process_projection(run, source)
        return
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


def validate_run_mappings(run) -> None:
    """Validate every target phase against the contract frozen on ``run``."""
    operations = []
    if run.action in PROJECTION_ACTIONS:
        if not getattr(run, 'graphql_mapping', ''):
            raise MappingPlanError('A projection requires an explicitly approved saved read service.')
        mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
        operation = json.loads(mapping.mapping_json).get('target_operation')
        if operation not in PROJECTIONS:
            raise MappingPlanError('The selected target is not a registered read-only projection.')
        if run.action == 'Schedule Migration' and operation != 'HeroCalendarEvents':
            raise MappingPlanError('Schedule Migration requires the calendar contract.')
        _mapping_plan_for_run(run, operation)
        return
    # Project imports deliberately require the Customer plan as well, because
    # they first execute the complete Customer phase of this run.
    if run.action in CUSTOMER_ACTIONS or run.action in PROJECT_ACTIONS:
        operations.append("HeroCustomers")
    if run.action in PROJECT_ACTIONS:
        operations.append("HeroProjectMatches")
    for operation in operations:
        _mapping_plan_for_run(run, operation)


def _mapping_plan_for_run(run, target_operation: str):
    """Resolve exactly the mapping definitions frozen onto this queued run."""
    if not getattr(run, "graphql_mapping", ""):
        return active_mapping_plan(target_operation)
    from hero_integration.services.read_contract import ReadContractError, from_documents

    dependency = target_operation == 'HeroCustomers' and bool(getattr(run, 'customer_service', ''))
    if dependency:
        service, mapping, snapshot = _customer_read_documents(run)
    else:
        service = frappe.get_doc("HERO GraphQL Service", run.graphql_service)
        mapping = frappe.get_doc("HERO GraphQL Mapping", run.graphql_mapping)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    try:
        contract = from_documents(service, mapping, snapshot)
    except ReadContractError as error:
        raise MappingPlanError(str(error)) from error
    if int(getattr(run, 'customer_service_revision' if dependency else "service_revision", 0) or 0) != contract.service_revision:
        raise MappingPlanError("The queued run no longer matches its approved service revision.")
    if int(getattr(run, 'customer_mapping_revision' if dependency else "mapping_revision", 0) or 0) != contract.mapping_revision:
        raise MappingPlanError("The queued run no longer matches its approved mapping revision.")
    if contract.target_operation != target_operation:
        raise MappingPlanError("The selected read mapping does not provide the required target operation.")
    return active_mapping_plan(
        service.operation_name,
        definition_ids=contract.definition_ids,
        target_operation=contract.target_operation,
    )


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
        plan = _mapping_plan_for_run(run, "HeroCustomers")
        source_id = plan.source_identity(record)
        values = plan.values(record)
        contact_values = plan.customer_contact_values(values)
        address = _customer_primary_address(record, plan.customer_primary_address_line2(record))
    except MappingPlanError as error:
        return Result("BLOCKED_RECORD", str(error))
    if plan.source_deleted(record):
        if run.mode != 'Preview':
            _mark_source_deleted(_link("customer", source_id, _source_instance(record)))
        return Result("SKIPPED_SOURCE_DELETED", "Source customer is deleted. No target deletion occurs.")
    if _is_contact_person(record):
        return Result("SKIPPED_CONTACT_PERSON", "Source contact person is not created as an ERPNext Customer.")
    if not values.get("customer_name"):
        return Result("BLOCKED_RECORD", "Configured customer record name is empty.")
    if _source_instance(record) != 'default':
        values['custom_hero_source_id'] = f'{_source_instance(record)}:{source_id}'
    return _upsert(
        "customer", source_id, record, values, "Customer", "customer_name", plan.mapping_version, run,
        after_insert=lambda customer: _synchronize_customer_relations(
            customer, contact_values, plan.home_phone(record), address, _source_instance(record), plan.mapping_version,
        ),
        after_update=lambda customer: _synchronize_customer_relations(
            customer, contact_values, plan.home_phone(record), address, _source_instance(record), plan.mapping_version,
        ),
        target_snapshot=lambda customer: _customer_target_values(customer, values, contact_values, plan.home_phone(record)),
        source_instance=_source_instance(record),
    )


def _process_projection(run, source) -> None:
    """Reuse the existing run, read service, mapping plan and conflict-safe writer."""
    operation = source.contract.target_operation
    plan = _mapping_plan_for_run(run, operation)
    offset = int(run.resume_offset or 0)
    index_start = int(run.resume_record_index or 0)
    seen_pages = set()
    while True:
        _cancel_if_requested(run)
        page = source.projection(offset)
        page_ids = tuple(record.get('id') for record in page.records)
        if page_ids and page_ids in seen_pages:
            raise MappingPlanError('Projection pagination returned a repeated page.')
        seen_pages.add(page_ids)
        for index, record in enumerate(page.records):
            if index < index_start:
                continue
            _cancel_if_requested(run)
            result = _migrate_projection(record, run, plan)
            _record_result(run, PROJECTIONS[operation]['source_type'], record, result)
            run.resume_phase = operation
            run.resume_offset, run.resume_record_index = offset, index + 1
            run.save()
        if page.next_offset is None:
            return
        offset, index_start = page.next_offset, 0
        run.resume_offset, run.resume_record_index = offset, 0
        run.save()


def _migrate_projection(record, run, plan) -> Result:
    operation = plan.target_operation or plan.operation
    contract = PROJECTIONS[operation]
    instance = _source_instance(record)
    try:
        source_id = plan.source_identity(record)
        validate_projection_record(operation, record)
        if record.get('is_deleted') is True or record.get('deleted') is True:
            if run.mode != 'Preview':
                _mark_source_deleted(_link(contract['source_type'], source_id, instance))
            return Result('SKIPPED_SOURCE_DELETED', 'Source deletion is observed; no target deletion occurs.')
        # An existing Event whose privacy or outbound settings changed must be
        # held before resolving the saved native options.  This makes Preview
        # fail closed even when it is invoked with a minimal test/run context,
        # and guarantees the protected target is never updated.
        existing = _link(contract['source_type'], source_id, instance) if contract.get('native_kind') == 'event' else None
        if existing:
            if existing.target_doctype != 'Event':
                return Result('CONFLICT', 'Stored calendar binding is not a native Event.')
            target = _native_lookup('Event', existing.target_name)
            if target.get('event_type') != 'Private' or any(target.get(f) for f in ('event_participants', 'send_reminder', 'sync_with_google_calendar', 'add_video_conferencing')):
                return Result('CONFLICT', 'Event sharing or outbound settings changed; native calendar update requires review.')
        native_values, defaults = {}, {}
        if contract.get('native_kind') or contract.get('native_bindings'):
            from hero_integration.services.native_projections import values as native_resolve, creation_defaults
            config = json.loads(frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping).mapping_json)
            options = config.get('native_options')
            native_values = native_resolve(contract, record, source_id, instance, options, lookup=_native_lookup,
                                          item_link=lambda identity: _item_name_for_source(identity, instance))
            if contract.get('native_kind') == 'event' and options['target_timezone'] != frappe.db.get_single_value('System Settings', 'time_zone'):
                raise MappingPlanError('Configured event target timezone differs from the native System Settings timezone.')
            defaults = creation_defaults(contract, options)
        values = plan.values(record, project_link=lambda value: _project_name_for_source(value, instance), native_value=lambda target: native_values[target])
        values['custom_hero_key'] = _digest([instance, contract['source_type'], source_id])
        values['custom_hero_source_instance'] = instance
    except (ValueError, MappingPlanError) as error:
        return Result('BLOCKED_RECORD', str(error))
    return _upsert(contract['source_type'], source_id, record, values, contract['target_doctype'],
                   'item_code' if contract['target_doctype'] == 'Item' else 'custom_hero_key', plan.mapping_version, run, source_instance=instance,
                   target_snapshot=lambda target: _projection_target_values(target, values, contract), creation_values=defaults)


def _native_lookup(doctype, name):
    if not isinstance(name, str) or not name or not frappe.db.exists(doctype, name):
        raise MappingPlanError('Configured native reference does not exist: ' + doctype)
    document = frappe.get_doc(doctype, name)
    frappe.has_permission(doctype, 'read', doc=document, throw=True)
    return document


def _item_name_for_source(identity, instance):
    link = _link('Documents_SupplyProduct', identity, instance)
    if not link or link.target_doctype != 'Item':
        raise MappingPlanError('Product version has no verified native Item binding.')
    return _native_lookup('Item', link.target_name).name


def _projection_target_values(target, values, contract):
    snapshot = _target_values(target, values)
    for binding in contract['bindings'].values():
        if binding['fieldtype'] == 'Table':
            field = binding['field']
            keys = ('custom_hero_source_id', 'custom_hero_resource_name', 'custom_hero_resource_type', 'custom_hero_type_id') if field == 'custom_hero_resources' else ('custom_hero_source_id', 'custom_hero_partner_name')
            snapshot[field] = [{key: row.get(key) for key in keys} for row in snapshot.get(field) or []]
            continue
        if binding['fieldtype'] != 'JSON':
            continue
        field = binding['field']
        value = snapshot.get(field)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        snapshot[field] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return snapshot


def _project_name_for_source(value, source_instance):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MappingPlanError('ProjectMatch reference must be a positive integer.')
    link = _link('project', str(value), source_instance)
    if not link or link.target_doctype != 'Project' or not frappe.db.exists('Project', link.target_name):
        raise MappingPlanError('ProjectMatch reference has no verified native Project binding.')
    frappe.has_permission('Project', 'read', doc=frappe.get_doc('Project', link.target_name), throw=True)
    return link.target_name


def _migrate_project(record: dict[str, object], run) -> Result:
    if not _target_available("Project"):
        return Result("UNAVAILABLE", "Project is not installed on this site.")
    try:
        plan = _mapping_plan_for_run(run, "HeroProjectMatches")
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


def observe_monitor_record(operation: str, record: dict[str, object]) -> Result | None:
    """Compare one configured source record without mutating an ERPNext target.

    MONITOR reuses the active mapping plan and Link field baselines. A source
    change alone is technical output; a same-field change on both sides uses
    the established durable conflict and administrator-notification path.
    """
    try:
        if operation == "HeroCustomers":
            plan = active_mapping_plan(operation)
            source_id = plan.source_identity(record)
            values = plan.values(record)
            source_type, doctype = "customer", "Customer"
            snapshot = lambda target: _customer_target_values(target, values, plan.home_phone(record))
        elif operation == "HeroProjectMatches":
            plan = active_mapping_plan(operation)
            source_id = plan.source_identity(record)
            values = plan.values(
                record,
                lambda value: _customer_name_for_source(value, _source_instance(record)),
                _confirmed_company_name,
                _project_title_for_record,
            )
            values["custom_hero_source_id"] = f"{_source_instance(record)}:{source_id}"
            source_type, doctype = "project", "Project"
            snapshot = lambda target: _target_values(target, values)
        else:
            return None
    except (MappingPlanError, ProjectTitleValidationError) as error:
        return Result("BLOCKED_RECORD", str(error))
    link = _link(source_type, source_id, _source_instance(record))
    if not link or not frappe.db.exists(link.target_doctype, link.target_name):
        return None
    target = frappe.get_doc(link.target_doctype, link.target_name)
    prior_source = decode_hashes(getattr(link, "source_field_hashes", ""))
    prior_target = decode_hashes(getattr(link, "target_field_hashes", ""))
    if prior_source is None or prior_target is None:
        return Result("OBSERVED", "Linked record has no field-level baseline; MONITOR made no write.", doctype, target.name)
    source_hashes = field_hashes(values)
    target_values = snapshot(target)
    target_hashes = field_hashes(target_values)
    source_changed = changed_fields(prior_source, source_hashes)
    target_changed = changed_fields(prior_target, target_hashes)
    same_fields = source_changed & target_changed
    if same_fields:
        detail = f"Field conflict requires review: {', '.join(sorted(same_fields))}."
        notify_conflict_once(link, same_fields, detail)
        _mark_link_conflict(link, detail)
        return Result("CONFLICT", detail, doctype, target.name)
    if source_changed:
        _observe_link(link, _digest(record), _digest(target_values))
        return Result("OBSERVED", "HERO field change observed by MONITOR; no ERPNext write was prepared.", doctype, target.name)
    if target_changed:
        _observe_link(link, _digest(record), _digest(target_values))
        return Result("OBSERVED", "Frappe field change observed by MONITOR; no HERO action was prepared.", doctype, target.name)
    return None


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
    after_update=None,
    target_snapshot=None,
    source_instance: str = "default",
    creation_values: dict | None = None,
) -> Result:
    if not _target_available(doctype):
        return Result("UNAVAILABLE", f"{doctype} is not installed on this site.")
    source_hash = _digest(record)
    existing = _link(source_type, source_id, source_instance)
    if existing:
        if existing.target_doctype != doctype:
            return Result('CONFLICT', 'Stored binding has an incompatible target DocType.')
        if not frappe.db.exists(existing.target_doctype, existing.target_name):
            if run.mode != "Preview":
                _mark_link_conflict(existing, "Stored target reference no longer exists.")
            return Result("CONFLICT", "Stored target reference no longer exists.")
        target = frappe.get_doc(existing.target_doctype, existing.target_name)
        snapshot = target_snapshot or (lambda item: _target_values(item, values))
        target_values = snapshot(target)
        source_field_hashes = field_hashes(values)
        target_field_hashes = field_hashes(target_values)
        prior_source = decode_hashes(getattr(existing, "source_field_hashes", ""))
        prior_target = decode_hashes(getattr(existing, "target_field_hashes", ""))
        # RC15 links have whole-payload hashes only.  They remain fail-closed
        # until an administrator establishes a fresh, field-level baseline.
        if prior_source is None or prior_target is None:
            if run.mode == "Preview":
                return Result("PREVIEW_OK", "Existing legacy link requires a reviewed field-level baseline.", doctype, target.name)
            _observe_link(existing, source_hash, _digest(target_values))
            if _digest(target_values) != existing.target_payload_hash:
                _mark_link_conflict(existing, "Target changed outside HERO Integration; legacy link has no field baseline.")
                return Result("CONFLICT", "Target changed outside HERO Integration; legacy link has no field baseline.", doctype, target.name)
            if existing.source_payload_hash != source_hash:
                _mark_link_conflict(existing, "HERO source changed; legacy link has no field baseline.")
                return Result("CONFLICT", "HERO source changed; legacy link has no field baseline.", doctype, target.name)
            return Result("SKIPPED", "Record is already synchronized.", doctype, target.name)
        changed_source = changed_fields(prior_source, source_field_hashes)
        changed_target = changed_fields(prior_target, target_field_hashes)
        same_field_changes = changed_source & changed_target
        if same_field_changes:
            detail = f"Field conflict requires review: {', '.join(sorted(same_field_changes))}."
            if run.mode != "Preview":
                notify_conflict_once(existing, same_field_changes, detail)
                _mark_link_conflict(existing, detail)
            return Result("CONFLICT", detail, doctype, target.name)
        if run.mode == "Preview":
            if changed_source:
                return Result("PREVIEW_OK", "Existing target would receive non-conflicting HERO field changes.", doctype, target.name)
            return Result("PREVIEW_OK", "Existing target would remain unchanged; local changes are observed only.", doctype, target.name)
        _observe_link(existing, source_hash, _digest(target_values))
        if changed_source:
            savepoint = "hero_upsert_update"
            frappe.db.savepoint(savepoint)
            try:
                for field in changed_source:
                    if field in values:
                        target.set(field, values[field])
                target.save()
                if after_update:
                    after_update(target)
            except (HomePhoneConflict, CustomerAddressError) as error:
                frappe.db.rollback(save_point=savepoint)
                return Result("CONFLICT", str(error), doctype, target.name)
            target_values = snapshot(target)
            target_field_hashes = field_hashes(target_values)
            _update_link_baseline(existing, source_hash, _digest(target_values), source_field_hashes, target_field_hashes, mapping_version)
            return Result("UPDATED", "Non-conflicting HERO field changes were applied through native Frappe validation.", doctype, target.name)
        # A local-only change is observed but never becomes an outbound HERO
        # mutation.  Keep it as the target baseline for a later comparison.
        if changed_target:
            _update_link_baseline(existing, source_hash, _digest(target_values), source_field_hashes, target_field_hashes, mapping_version)
            return Result("OBSERVED", "Local field change observed; no outbound HERO mutation was prepared.", doctype, target.name)
        return Result("SKIPPED", "Record is already synchronized.", doctype, target.name)
    duplicate = frappe.db.exists(doctype, {name_field: values[name_field]})
    if duplicate:
        return Result("SKIPPED_AMBIGUOUS", f"Existing {doctype} has no stable HERO link.", doctype, duplicate)
    if run.mode == "Preview":
        return Result("PREVIEW_OK", "A new target record would be created.", doctype)
    # A target, its required relation(s), and the provenance link are one
    # integration unit.  A handled relation conflict must not leave the newly
    # inserted ERPNext document behind for a later run to mistake as manual.
    savepoint = "hero_upsert_relation"
    frappe.db.savepoint(savepoint)
    try:
        target = frappe.get_doc({"doctype": doctype, **(creation_values or {}), **values})
        target.insert()
        if after_insert:
            after_insert(target)
        snapshot = target_snapshot or (lambda item: _target_values(item, values))
        target_hash = _digest(snapshot(target))
        frappe.get_doc({
            "doctype": "HERO Integration Link", "source_system": "HERO", "source_type": source_type,
            "source_id": source_id, "source_instance": source_instance, "sync_status": "Synchronized",
            "conflict_detail": "", "last_synced_on": now_datetime(),
            "target_doctype": doctype, "target_name": target.name, "source_payload_hash": source_hash,
            "target_payload_hash": target_hash, "source_field_hashes": encode_hashes(field_hashes(values)),
            "target_field_hashes": encode_hashes(field_hashes(snapshot(target))), "mapping_version": mapping_version,
        }).insert()
    except (HomePhoneConflict, CustomerAddressError) as error:
        frappe.db.rollback(save_point=savepoint)
        return Result("CONFLICT", str(error), doctype)
    return Result("CREATED", "Target created through native Frappe validation.", doctype, target.name)


def _record_result(run, source_type: str, record: dict[str, object], result: Result) -> None:
    source_id = str(record.get("id")) if isinstance(record.get("id"), int) else "unknown"
    record_output(
        run=run.name, source_type=source_type, source_id=source_id, result=result.code,
        action="preview" if run.mode == "Preview" else "hero_to_frappe",
        detail=result.detail, target_doctype=result.target_doctype, target_name=result.target_name,
    )
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


def _customer_target_values(target, values: dict[str, object], contact_values: dict[str, object], home_phone: str | None) -> dict[str, object]:
    snapshot = _target_values(target, values)
    # Existing rows created before Contact persistence (or a relationship that
    # cannot safely be read) retain the Customer-field snapshot for conflict
    # detection.  A missing native value is not evidence that a user changed
    # the visible Customer projection to ``None``.
    snapshot.update({
        field: value
        for field, value in primary_contact_values(target, set(contact_values)).items()
        if value is not None
    })
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


def _observe_link(link, source_hash: str, target_hash: str) -> None:
    """Keep observation separate from the last successfully compared baseline."""

    link.observed_source_hash = source_hash
    link.observed_target_hash = target_hash
    link.last_observed_on = now_datetime()
    _save_link(link)


def _update_link_baseline(link, source_hash, target_hash, source_fields, target_fields, mapping_version) -> None:
    link.source_payload_hash = source_hash
    link.target_payload_hash = target_hash
    link.source_field_hashes = encode_hashes(source_fields)
    link.target_field_hashes = encode_hashes(target_fields)
    link.mapping_version = mapping_version
    link.sync_status = "Synchronized"
    link.conflict_detail = ""
    link.last_synced_on = now_datetime()
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


def _customer_primary_address(record: dict[str, object], selected_line2: str | None = None) -> dict[str, object] | None:
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
    country = value.get('country')
    country_code = country.get('code') if isinstance(country, dict) else None
    address = {
        "id": str(source_id),
        **{key: item.strip() for key, item in fields.items()},
        **({'country_code': country_code} if country_code is not None else {}),
    }
    if isinstance(selected_line2, str) and selected_line2.strip():
        address["line_2"] = selected_line2.strip()
    return address


def _synchronize_customer_relations(customer, contact_values, home_phone, address, source_instance: str, mapping_version: str) -> None:
    synchronize_primary_contact(customer, contact_values)
    synchronize_home_phone(customer, home_phone)
    if address is not None:
        _synchronize_customer_address(customer, address, source_instance, mapping_version)


def _synchronize_customer_address(customer, address: dict[str, object], source_instance: str, mapping_version: str) -> None:
    if not _target_available("Address"):
        raise CustomerAddressError("Address is not installed on this site.")
    source_id = str(address["id"])
    code = address.get('country_code')
    if not isinstance(code, str) or not code.strip():
        raise CustomerAddressError('Primary address requires an explicit HERO country.code selection.')
    countries = frappe.get_list('Country', filters={'code': code.strip().lower()}, pluck='name', limit_page_length=2)
    if len(countries) != 1:
        raise CustomerAddressError('HERO country code has no unique readable native Country binding.')
    values = {
        'country': countries[0], 'address_type': 'Billing',
        "address_title": customer.name,
        "address_line1": address["street"],
        "pincode": address["zipcode"],
        "city": address["city"],
    }
    if isinstance(address.get("line_2"), str) and address["line_2"].strip():
        values["address_line2"] = address["line_2"].strip()
    payload = {"customer": customer.name, **address}
    existing = _link("customer_address", source_id, source_instance)
    if existing:
        if existing.target_doctype != "Address":
            _mark_link_conflict(existing, "Stored customer address reference has an incompatible target DocType.")
            raise CustomerAddressError("Stored customer address reference has an incompatible target DocType.")
        if not frappe.db.exists("Address", existing.target_name):
            _mark_link_conflict(existing, "Stored customer address reference no longer exists.")
            raise CustomerAddressError("Stored customer address reference no longer exists.")
        target = frappe.get_doc("Address", existing.target_name)
        if "address_line2" not in values:
            prior_source = decode_hashes(getattr(existing, "source_field_hashes", ""))
            prior_target = decode_hashes(getattr(existing, "target_field_hashes", ""))
            if prior_source and prior_target and "address_line2" in prior_source:
                legacy_source_fields = {field: values.get(field) for field in prior_source if field != "address_line2"}
                legacy_target_fields = {field: target.get(field) for field in prior_target if field != "address_line2"}
                if field_hashes(legacy_source_fields) != {field: value for field, value in prior_source.items() if field != "address_line2"}:
                    _mark_link_conflict(existing, "HERO customer address changed outside the selected line_2 binding.")
                    raise CustomerAddressError("HERO customer address changed outside the selected line_2 binding.")
                if field_hashes(legacy_target_fields) != {field: value for field, value in prior_target.items() if field != "address_line2"}:
                    _mark_link_conflict(existing, "Customer address changed outside HERO Integration.")
                    raise CustomerAddressError("Customer address changed outside HERO Integration.")
                return
            if _digest(_target_values(target, values)) != existing.target_payload_hash:
                _mark_link_conflict(existing, "Customer address changed outside HERO Integration.")
                raise CustomerAddressError("Customer address changed outside HERO Integration.")
            if existing.source_payload_hash != _digest(payload):
                _mark_link_conflict(existing, "HERO customer address changed. Review is required before an update.")
                raise CustomerAddressError("HERO customer address changed. Review is required before an update.")
            return
        _require_exclusive_customer_address(target, customer.name)
        prior_source = decode_hashes(getattr(existing, "source_field_hashes", ""))
        prior_target = decode_hashes(getattr(existing, "target_field_hashes", ""))
        if prior_source is None or prior_target is None:
            _mark_link_conflict(existing, "Customer address has no field-level baseline for the selected line_2 binding.")
            raise CustomerAddressError("Customer address has no field-level baseline for the selected line_2 binding.")
        legacy_source_fields = {field: values.get(field) for field in prior_source if field != "address_line2"}
        legacy_target_fields = {field: target.get(field) for field in prior_target if field != "address_line2"}
        if field_hashes(legacy_source_fields) != {field: value for field, value in prior_source.items() if field != "address_line2"}:
            _mark_link_conflict(existing, "HERO customer address changed outside the selected line_2 binding.")
            raise CustomerAddressError("HERO customer address changed outside the selected line_2 binding.")
        if field_hashes(legacy_target_fields) != {field: value for field, value in prior_target.items() if field != "address_line2"}:
            _mark_link_conflict(existing, "Customer address changed outside HERO Integration.")
            raise CustomerAddressError("Customer address changed outside HERO Integration.")
        source_hashes = field_hashes(values)
        target_values = _target_values(target, values)
        target_hashes = field_hashes(target_values)
        changed_source = changed_fields(prior_source, source_hashes)
        changed_target = changed_fields(prior_target, target_hashes)
        # V1 links predate address_line2.  Their field baseline therefore
        # omits it; an empty current target field is not an external mutation
        # and must not block the tightly-scoped V2 extension.
        allowed_target_changes = set()
        if "address_line2" not in prior_target and not target.get("address_line2"):
            allowed_target_changes.add("address_line2")
        if changed_target - allowed_target_changes:
            _mark_link_conflict(existing, "Customer address changed outside HERO Integration.")
            raise CustomerAddressError("Customer address changed outside HERO Integration.")
        if changed_source - {"address_line2"}:
            _mark_link_conflict(existing, "HERO customer address changed outside the selected line_2 binding.")
            raise CustomerAddressError("HERO customer address changed outside the selected line_2 binding.")
        if not changed_source:
            return
        if "address_line2" not in prior_source and target.get("address_line2"):
            _mark_link_conflict(existing, "Customer address line_2 has an untracked local value.")
            raise CustomerAddressError("Customer address line_2 has an untracked local value.")
        target.address_line2 = values["address_line2"]
        target.save()
        target_values = _target_values(target, values)
        _update_link_baseline(
            existing, _digest(payload), _digest(target_values), field_hashes(values),
            field_hashes(target_values), mapping_version,
        )
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
        "target_payload_hash": _digest(_target_values(target, values)),
        "source_field_hashes": encode_hashes(field_hashes(values)),
        "target_field_hashes": encode_hashes(field_hashes(_target_values(target, values))),
        "mapping_version": mapping_version,
    }).insert()


def _require_exclusive_customer_address(target, customer_name: str) -> None:
    links = list(target.get("links") or [])
    customer_links = [
        row for row in links
        if row.get("link_doctype") == "Customer" and row.get("link_name") == customer_name
    ]
    if len(links) != 1 or len(customer_links) != 1:
        raise CustomerAddressError("Customer address is shared or has an ambiguous Dynamic Link binding.")


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
