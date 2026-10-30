from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from hero_integration.client.graphql import HeroGraphQLClient
from hero_integration.client.settings import get_configuration
from hero_integration.integrations.erpnext.company import project_company
from hero_integration.integrations.erpnext.contact import (
    HomePhoneConflict,
    home_phone_for_customer,
    primary_contact_values,
    synchronize_primary_contact,
    synchronize_home_phone,
)
from hero_integration.sources.hero import GraphQLServiceSource, HeroSource, SourcePage, TestingPreviewSource
from hero_integration.services.change_detection import (
    changed_fields, decode as decode_hashes, encode as encode_hashes, field_hashes, managed_target_update, next_baseline, reconcile,
)
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
# A run is one queue job and RQ stops a job at its timeout.  A claim therefore
# cannot outlive timeout + grace; a Running run past that lease belongs to a
# stopped worker (killed process, lost host) and is released automatically.
RUN_JOB_TIMEOUT = 1500
RUN_LEASE_GRACE_SECONDS = 300
RUN_LEASE_SECONDS = RUN_JOB_TIMEOUT + RUN_LEASE_GRACE_SECONDS


@dataclass(frozen=True)
class Result:
    code: str
    detail: str
    target_doctype: str = ""
    target_name: str = ""


class CustomerAddressError(ValueError):
    pass


class RepeatedProjectionPageError(MappingPlanError):
    """The current projection attempt cannot continue without duplicating a page."""


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == []


class _Comparison:
    """One record's normalized HERO state and the target fields it tracks.

    ``values`` are the writable target values from the mapping plan.  Fields
    the plan reported as explicitly empty are compared as ``None`` but never
    written (no-clear policy); fields reported missing carry no evidence at
    all.  ``extra`` adds relation snapshots (for example the Customer home
    phone) that have no field on the target document itself.
    """

    def __init__(self, values, extra=None):
        extra = dict(extra or {})
        self.missing = set(getattr(values, "missing", ())) - set(values) - set(extra)
        empty = set(getattr(values, "empty", ())) - set(values)
        self.source_values = {**{field: None for field in empty}, **values, **extra}
        self.unwritable = empty | {field for field, value in extra.items() if _is_empty(value)}
        self.tracked = set(self.source_values) | self.missing
        self.source_hashes = field_hashes(self.source_values)

    def omit(self, fields) -> None:
        """Treat fields without a current HERO value as missing evidence."""
        omitted = set(fields) - set(self.source_values)
        self.missing |= omitted
        self.tracked |= omitted

    def target_values(self, target, snapshot) -> dict[str, object]:
        current = dict(snapshot(target))
        for field in self.tracked - set(current):
            current[field] = target.get(field)
        return current

    def reconcile(self, prior_source, prior_target, target_values):
        return reconcile(
            prior_source, prior_target, self.source_hashes, field_hashes(target_values),
            tracked=self.tracked, missing=self.missing,
            source_empty={field for field, value in self.source_values.items() if _is_empty(value)},
            target_empty={field for field, value in target_values.items() if _is_empty(value)},
            unwritable=self.unwritable,
        )


def execute_run(run_name: str) -> None:
    run = frappe.get_doc("HERO Integration Run", run_name)
    frappe.set_user(run.owner)
    if run.state in TERMINAL_STATES:
        return
    if not _claim_run(run):
        return
    testing_source = None
    claim_started_on = run.started_on
    failed = False
    try:
        validate_run_mappings(run)
        config = get_configuration()
        source = _source_for_run(run, config)
        if _testing_preview_enabled(run):
            testing_source = TestingPreviewSource(source, int(run.testing_max_actions or 0))
            source = testing_source
        _process_requested_phases(run, source)
        run.reload()
        if run.state == "Running" and run.started_on == claim_started_on:
            run.state = "Completed With Conflicts" if run.conflict_records else "Completed"
            run.finished_on = now_datetime()
            run.save()
            _mark_sync_success(run)
    except RepeatedProjectionPageError as error:
        failed = True
        frappe.db.rollback()
        _finalize_blocked_run(run.name, claim_started_on, str(error))
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
    except Exception as error:
        failed = True
        frappe.db.rollback()
        _finalize_failed_run(run.name, claim_started_on)
        # Frappe's worker retries these exceptions. A finalized run must not
        # be retried as a terminal no-op and reported as a successful job.
        if isinstance(error, (frappe.db.InternalError, frappe.RetryBackgroundJobError)):
            raise RuntimeError("HERO Integration run failed; see the original exception.") from error
        raise
    finally:
        if not failed:
            if testing_source is not None:
                _persist_testing_result(run, testing_source)
            _release_run(run.name)


def _validate_run_source_binding(run, config, snapshot=None):
    """Reject source changes before any provider request or record processing."""
    from hero_integration.services.introspection import endpoint_fingerprint

    instance = getattr(config, "source_instance", "")
    recorded = getattr(run, "source_instance", "") or ""
    if not instance or (recorded and recorded != instance):
        raise MappingPlanError("Migration run does not match the configured HERO source instance.")
    # A schema fingerprint or a matching URL is no proof of the HERO account
    # behind the token: require the administrator-confirmed binding that was
    # in force when this run was queued.
    if not getattr(config, "source_binding_confirmed", False):
        raise MappingPlanError("The HERO endpoint, token or source instance changed; an administrator must confirm the source binding.")
    if int(getattr(run, "source_binding_revision", 0) or 0) != int(getattr(config, "source_binding_revision", 0) or 0):
        raise MappingPlanError("The run was queued under an earlier HERO source binding; queue a new run.")
    if snapshot is None:
        # Historical runs lack an immutable endpoint binding; do not replay them
        # against whatever endpoint happens to be configured today.
        raise MappingPlanError("Historical run has no approved endpoint-bound read snapshot.")
    if (getattr(snapshot, "source_instance", "") != instance or
        getattr(snapshot, "endpoint_fingerprint", "") != endpoint_fingerprint(config.endpoint.url, instance)):
        raise MappingPlanError("Approved schema snapshot does not match the current HERO endpoint and source instance.")


def _source_for_run(run, config):
    """Bind every newly queued run to its approved immutable read revisions."""
    if not getattr(run, "graphql_service", ""):
        _validate_run_source_binding(run, config)
        raise MappingPlanError("Historical run requires a new approved endpoint-bound read service.")
    from hero_integration.services.read_contract import ReadContractError, from_documents

    service = frappe.get_doc("HERO GraphQL Service", run.graphql_service)
    mapping = frappe.get_doc("HERO GraphQL Mapping", run.graphql_mapping)
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    _validate_run_source_binding(run, config, snapshot)
    contract = from_documents(service, mapping, snapshot)
    if int(run.service_revision or 0) != contract.service_revision or int(run.mapping_revision or 0) != contract.mapping_revision:
        raise MappingPlanError("The queued run no longer matches its approved service or mapping revision.")
    def revalidate_generated():
        observed = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
        scope = (observed.source_instance or '', observed.schema_snapshot, observed.direction)
        from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
        lock_exchange_context(*scope)
        current_mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping, for_update=True)
        if (current_mapping.source_instance or '', current_mapping.schema_snapshot, current_mapping.direction) != scope:
            raise MappingPlanError('The generated mapping context changed before execution.')
        current_service = frappe.get_doc('HERO GraphQL Service', run.graphql_service, for_update=True)
        current_snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', current_service.schema_snapshot, for_update=True)
        current = from_documents(current_service, current_mapping, current_snapshot)
        if (current.service_revision != int(run.service_revision) or current.mapping_revision != int(run.mapping_revision)
                or current_snapshot.source_instance != getattr(run, 'source_instance', '')):
            raise MappingPlanError('The approved generated read context changed before execution.')
        return current
    source = GraphQLServiceSource(HeroGraphQLClient(
        config.endpoint, lambda: config.api_token, config.timeout_seconds,
        max_retries=0 if _testing_preview_enabled(run) else 2,
        requests_per_minute=_request_rate(),
    ), service, contract, getattr(snapshot, "source_instance", "") or config.source_instance,
       revalidate=revalidate_generated if getattr(contract, 'materialized_request', None) is not None else None)
    if contract.target_operation == 'HeroProjectMatches':
        customer_service, customer_mapping, customer_snapshot = _customer_read_documents(run)
        customer_contract = from_documents(customer_service, customer_mapping, customer_snapshot)
        source.customer_source = GraphQLServiceSource(source.client, customer_service, customer_contract, source.source_instance)
    return source


def _request_rate() -> int:
    """Runs draw from the same configured per-host HERO rate as polling."""
    from hero_integration.services.polling import runtime_plan

    return int(runtime_plan(frappe.get_single("HERO Integration Settings"))["rate_limit"])


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
        now = now_datetime()
        if active_run == run.name and frappe.db.get_value("HERO Integration Run", run.name, "state") == "Running":
            if run_lease_alive(run.name, now):
                return False
            # A duplicate or retried job of the same run after its worker died.
            _interrupt_run(run.name, now)
            run.reload()
        if active_run and active_run != run.name:
            active_state = frappe.db.get_value("HERO Integration Run", active_run, "state")
            if active_state not in TERMINAL_STATES:
                if run_lease_alive(active_run, now):
                    _block_run(run, f"Another HERO run is active: {active_run}.")
                    return False
                _interrupt_run(active_run, now)
        frappe.db.sql(
            "UPDATE `tabSingles` SET `value` = %s WHERE `doctype` = %s AND `field` = %s",
            (run.name, "HERO Integration Sync State", "active_run"),
        )
        run.state = "Running"
        started_on = now_datetime()
        if run.started_on:
            started_on = max(started_on, get_datetime(run.started_on) + timedelta(microseconds=1))
        run.started_on = started_on
        run.lease_expires_on = started_on + timedelta(seconds=RUN_LEASE_SECONDS)
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


def run_lease_alive(run_name: str, now=None) -> bool:
    """Whether a non-terminal run may still be executing in a live worker.

    Runs claimed before leases existed have no expiry; their ``started_on``
    plus the lease window is used instead.  A Queued run holds no claim.
    """
    row = frappe.db.get_value("HERO Integration Run", run_name, ["state", "lease_expires_on", "started_on"], as_dict=True)
    if not row or row.state in TERMINAL_STATES:
        return False
    if row.state != "Running":
        return True
    expires = row.lease_expires_on
    if not expires and row.started_on:
        expires = get_datetime(row.started_on) + timedelta(seconds=RUN_LEASE_SECONDS)
    return bool(expires) and get_datetime(expires) > (now or now_datetime())


def _interrupt_run(run_name: str, now) -> None:
    """Mark a stopped worker's run Failed so that its slot can be reused.

    Its uncommitted record work was already rolled back by the database; only
    the committed reservation remains.  Resume starts the run again.
    """
    fields = {
        "state": "Failed", "finished_on": now,
        "error_message": "The worker stopped before finishing (lease expired). Resume starts the run again.",
    }
    for phase in PHASES:
        if frappe.db.get_value("HERO Integration Run", run_name, f"{phase}_phase_state") == "Running":
            fields[f"{phase}_phase_state"] = "Failed"
    frappe.db.set_value("HERO Integration Run", run_name, fields, update_modified=False)


def _finalize_failed_run(run_name: str, claim_started_on) -> None:
    """Finalize only this still-current claim after its business rollback."""
    _finalize_rolled_back_run(run_name, claim_started_on, "Failed", "Migration failed. See Error Log for the redacted server exception.")


def _finalize_blocked_run(run_name: str, claim_started_on, detail: str) -> None:
    """Persist the repeated-page diagnosis after discarding the attempt's writes."""
    _finalize_rolled_back_run(run_name, claim_started_on, "Blocked", detail)


def _finalize_rolled_back_run(run_name: str, claim_started_on, state: str, detail: str) -> None:
    if _locked_active_run() != run_name:
        return
    run = frappe.get_doc("HERO Integration Run", run_name)
    if run.state != "Running" or run.started_on != claim_started_on:
        return
    _mark_running_phase_failed(run)
    run.state = state
    run.error_message = detail
    run.finished_on = now_datetime()
    run.save()
    _mark_sync_error(run)
    _release_run(run_name)
    frappe.db.commit()


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
        mapping_name=contract.mapping_name,
        native_task=json.loads(mapping.mapping_json).get('native_task_onboarding') is not None,
        lock_rules=getattr(contract, 'materialized_request', None) is not None,
    )


def _validate_enumeration_page(page: SourcePage, offset: int, seen_ids: set[str]) -> None:
    """Reject invalid pagination before applying any records from the page."""
    if page.next_offset is not None and (type(page.next_offset) is not int
            or page.next_offset <= offset or not page.records):
        raise MappingPlanError('Source pagination did not advance safely.')
    identities = []
    for row in page.records:
        identity = row.get('id') if isinstance(row, dict) else None
        if not isinstance(identity, (str, int)) or isinstance(identity, bool) or not str(identity).strip():
            raise MappingPlanError('Source identity is missing or invalid.')
        identities.append(str(identity))
    if len(set(identities)) != len(identities) or seen_ids.intersection(identities):
        raise MappingPlanError('Source identity repeated during enumeration.')
    seen_ids.update(identities)


def _process_phase(run, phase: str, reader) -> bool:
    if _phase_state(run, phase) == "Completed":
        return True
    _set_phase_state(run, phase, "Running")
    offset = int(run.resume_offset or 0) if run.resume_phase == phase else 0
    record_index = int(run.resume_record_index or 0) if run.resume_phase == phase else 0
    if offset or record_index:
        # Offset continuation requires a stable source snapshot; mid-page
        # continuation additionally requires persisted page identity evidence.
        _set_phase_state(run, phase, "Blocked")
        _block_run(run, "Cannot resume without a verified source snapshot and page identity evidence.")
        return False
    seen_ids: set[str] = set()
    while True:
        _cancel_if_requested(run)
        page: SourcePage = reader(offset)
        testing_source = getattr(reader, "__self__", None)
        if isinstance(testing_source, TestingPreviewSource) and testing_source.status in {"BLOCKED", "LIMIT_REACHED"}:
            _set_phase_state(run, phase, "Blocked")
            detail = (
                "Testing preview blocked because newest-first ordering is not verified for the selected HERO source."
                if testing_source.status == "BLOCKED"
                else "Testing preview reached its shared HERO request budget; no resume is permitted."
            )
            _block_run(run, detail)
            return False
        _validate_enumeration_page(page, offset, seen_ids)
        for index, record in enumerate(page.records):
            if index < record_index:
                continue
            _cancel_if_requested(run)
            migrate = _migrate_customer if phase == "customer" else _migrate_project
            result = _migrate_isolated(lambda: migrate(record, run))
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
            # Every Customer was processed; a blocked Customer only blocks its own Projects.
            if phase == "project" and _phase_has_blockers(run, phase):
                _set_phase_state(run, phase, "Blocked")
                _block_run(run, "Project phase contains unresolved records.")
                return False
            _set_phase_state(run, phase, "Completed")
            return True
        offset = page.next_offset
        record_index = 0


def _cancel_if_requested(run) -> None:
    run.reload()
    if run.state == "Cancelled":
        raise _Cancelled()


def _testing_preview_enabled(run) -> bool:
    return getattr(run, "mode", "") == "Preview" and bool(int(getattr(run, "testing_enabled", 0) or 0))


def _persist_testing_result(run, source: TestingPreviewSource) -> None:
    """Store only technical counters and source outcomes for the finished test run."""
    try:
        run.reload()
        status = "FAILED" if run.state == "Failed" else source.status
        run.testing_executed_actions = source.executed_actions
        run.testing_records_received = source.records_received
        run.testing_status = status
        run.testing_results_json = json.dumps(source.results, ensure_ascii=False, sort_keys=True)
        run.save()
    except Exception:
        frappe.log_error(title="HERO testing preview metadata could not be recorded")


def _migrate_customer(record: dict[str, object], run) -> Result:
    try:
        plan = _mapping_plan_for_run(run, "HeroCustomers")
        source_id = plan.source_identity(record)
        values = plan.values(record)
        # ERPNext rejects such an email address; the Customer is imported without it.
        email_note = _drop_invalid_email(values)
        home_phone, phone_originals, phone_note = _clean_customer_phones(values, plan.home_phone(record))
        contact_values = plan.customer_contact_values(values)
        address_note = ""
        try:
            address = _customer_primary_address(record, plan.customer_primary_address_line2(record))
        except IncompleteAddress as error:
            # The Customer is still imported; an incomplete address is skipped, as if HERO had none.
            address, address_note = None, f" {error} It was not imported."
        if address is not None and not address.get("country_code"):
            # HERO customers are German unless HERO names another country.
            address["country_code"] = DEFAULT_ADDRESS_COUNTRY
            address_note = " The HERO address has no country; Germany was assumed."
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
    tracks_home_phone = plan.has_home_phone()

    def after_insert(customer):
        _synchronize_customer_relations(
            customer, contact_values, home_phone, address, _source_instance(record), plan.mapping_version,
        )
        _keep_phone_originals(customer, phone_originals)

    def after_update(customer, outcome):
        # Relations receive only the reconciled HERO changes: a locally edited
        # Contact value is never rewritten because another field changed.
        _synchronize_customer_relations(
            customer, {field: value for field, value in contact_values.items() if field in outcome.apply},
            home_phone if "hero_phone_home" in outcome.apply else None,
            address, _source_instance(record), plan.mapping_version,
        )
        _keep_phone_originals(customer, {field: text for field, text in phone_originals.items() if field in outcome.apply})

    result = _upsert(
        "customer", source_id, record, values, "Customer", "customer_name", plan.mapping_version, run,
        after_insert=after_insert,
        after_update=after_update,
        target_snapshot=lambda customer: _customer_target_values(
            customer, values, plan.customer_contact_fields(), home_phone, track_home_phone=tracks_home_phone,
        ),
        source_instance=_source_instance(record),
        comparison_extra={"hero_phone_home": home_phone} if tracks_home_phone else None,
        required_on_create=("customer_type",),
    )
    note = email_note + phone_note + address_note
    if note and result.code != "BLOCKED_RECORD":
        from dataclasses import replace
        result = replace(result, detail=(result.detail or "") + note)
    return result


def _drop_invalid_email(values) -> str:
    """Remove an email ERPNext would reject and say so; umlauts elsewhere are fine."""
    email = values.get("email_id")
    if not isinstance(email, str) or not email.strip():
        return ""
    from frappe.utils import split_emails, validate_email_address

    if all(validate_email_address(item, throw=False) for item in split_emails(email)):
        return ""
    del values["email_id"]
    missing = getattr(values, "missing", None)
    if missing is not None:
        # No usable evidence: the stored baseline for this field is kept as is.
        missing.add("email_id")
    return " The HERO email address is not valid in ERPNext; it was not imported."


_PHONE_NUMBER = re.compile(r"\+?\d[\d \-().]*\d")
_PHONE_LABELS = {"mobile_no": "mobile phone", "hero_phone_home": "home phone"}


def _phone_number(value):
    """Return (number, original) for HERO phone text; original is set only when ERPNext would reject it."""
    from frappe.utils import validate_phone_number

    if not isinstance(value, str) or not value.strip() or validate_phone_number(value):
        return value, ""
    # "07000/11111" is one number; a slash between words separates two numbers.
    text = re.sub(r"(?<=\d)/(?=\d)", " ", value)
    for candidate in _PHONE_NUMBER.findall(text):
        if sum(char.isdigit() for char in candidate) >= 6 and validate_phone_number(candidate):
            return candidate, value
    return None, value


def _clean_customer_phones(values, home_phone):
    """Keep the first valid number of a phone text; the full text is kept as a Customer comment."""
    originals = {}
    mobile, original = _phone_number(values.get("mobile_no"))
    if original:
        originals["mobile_no"] = original
        if mobile:
            values["mobile_no"] = mobile
        else:
            del values["mobile_no"]
            missing = getattr(values, "missing", None)
            if missing is not None:
                missing.add("mobile_no")
    home, original = _phone_number(home_phone)
    if original:
        originals["hero_phone_home"] = original
        home_phone = home
    if not originals:
        return home_phone, originals, ""
    return home_phone, originals, " A HERO phone text is not a valid ERPNext number; the first number was imported and the full text kept as a comment."


def _keep_phone_originals(customer, originals) -> None:
    for field, text in originals.items():
        customer.add_comment("Comment", f"HERO {_PHONE_LABELS[field]} as entered in HERO: {text}")


def _rejections() -> tuple[type, ...]:
    """Errors where ERPNext or its database refuses one record's data."""
    candidates = (getattr(frappe, "ValidationError", None), getattr(frappe.db, "DataError", None))
    return tuple(item for item in candidates if isinstance(item, type))


def _migrate_isolated(migrate) -> Result:
    """Run one record so that an ERPNext validation error blocks only this record."""
    savepoint = "hero_record"
    frappe.db.savepoint(savepoint)
    try:
        return migrate()
    except Exception as error:
        if not isinstance(error, _rejections()):
            raise
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            # An inner full rollback removed the savepoint; the run fails as before.
            raise error from None
        frappe.clear_messages()
        frappe.log_error(title="HERO record rejected by ERPNext")
        return Result("BLOCKED_RECORD", f"ERPNext rejected the record ({type(error).__name__}); see Error Log.")


def _process_projection(run, source) -> None:
    """Reuse the existing run, read service, mapping plan and conflict-safe writer."""
    operation = source.contract.target_operation
    plan = _mapping_plan_for_run(run, operation)
    from hero_integration.services.read_contract import from_documents
    service = frappe.get_doc('HERO GraphQL Service', run.graphql_service)
    mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
    snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', service.schema_snapshot)
    reviewed = from_documents(service, mapping, snapshot)
    if (reviewed.target_operation != operation or reviewed.service_revision != int(run.service_revision)
            or reviewed.mapping_revision != int(run.mapping_revision)):
        raise MappingPlanError('The projection contract changed before processing.')
    task_scope = json.loads(mapping.mapping_json).get('native_task_onboarding')
    if task_scope is not None and (operation != 'HeroTasks' or source.source_instance != task_scope['source_instance']
                                   or getattr(run, 'source_instance', '') != task_scope['source_instance']):
        raise MappingPlanError('Native Task onboarding source does not match the approved run.')
    offset = int(run.resume_offset or 0)
    index_start = int(run.resume_record_index or 0)
    if offset or index_start:
        _block_run(run, 'Cannot resume without a verified source snapshot and page identity evidence.')
        return
    seen_ids: set[str] = set()
    while True:
        _cancel_if_requested(run)
        page = source.projection(offset)
        if isinstance(source, TestingPreviewSource) and source.status in {"BLOCKED", "LIMIT_REACHED"}:
            detail = (
                "Testing preview blocked because newest-first ordering is not verified for the selected HERO source."
                if source.status == "BLOCKED"
                else "Testing preview reached its shared HERO request budget; no resume is permitted."
            )
            _block_run(run, detail)
            return
        try:
            _validate_enumeration_page(page, offset, seen_ids)
        except MappingPlanError as error:
            raise RepeatedProjectionPageError(str(error)) from error
        for index, record in enumerate(page.records):
            if index < index_start:
                continue
            _cancel_if_requested(run)
            if task_scope is not None:
                task_record = record if any(item.source_path == 'comment' for item in plan.definitions) else {k: v for k, v in record.items() if k != 'comment'}
                result = _migrate_isolated(lambda: _migrate_native_task(task_record, run, plan, task_scope))
                if (result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'PREVIEW_OK'}
                        and result.target_doctype == 'Task'
                        and any(item.source_path == 'target_user_id' for item in plan.definitions)):
                    _record_task_assignment_pending(run, record, result.target_name, task_scope['source_instance'])
            else:
                result = _migrate_isolated(lambda: _migrate_projection(record, run, plan))
            _record_result(run, PROJECTIONS[operation]['source_type'], record, result)
            run.resume_phase = operation
            run.resume_offset, run.resume_record_index = offset, index + 1
            run.save()
        if page.next_offset is None:
            return
        offset, index_start = page.next_offset, 0
        run.resume_offset, run.resume_record_index = offset, 0
        run.save()


@dataclass(frozen=True)
class TaskAssignmentDecision:
    action: str
    detail: str
    task_name: str = ''
    recipient: str = ''
    existing: tuple[str, ...] = ()
    binding: str = ''
    policy: str = ''
    proposed: str = ''
    date_rule: str = ''


def _plan_task_assignment(run, record, task_name: str, source_instance: str) -> TaskAssignmentDecision:
    """Read authoritative assignment state without writing or granting authority.

    The locks are held through execution by the caller's transaction. A plan is
    an observation, never a capability to execute later.
    """
    def decision(action, detail, **values):
        return TaskAssignmentDecision(action, detail, task_name or '', **values)

    target = record.get('target_user_id')
    if 'target_user_id' not in record:
        return decision('NO_OP', 'PENDING_SOURCE_SEMANTICS: assignee was omitted; existing assignments are preserved.')
    if target is None:
        return decision('NO_OP', 'PENDING_SOURCE_SEMANTICS: explicit NULL never removes a native assignment.')
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
        return decision('PENDING_IDENTITY', 'PENDING_IDENTITY: source User identity is invalid.')
    if 'author_user_id' not in record:
        return decision('BLOCKED', 'PENDING_AUTHORITY: source author was not included in the read.')
    if record.get('author_user_id') is not None:
        return decision('BLOCKED', 'PENDING_AUTHORITY: source author has no verified system, peer or supervisor authority.')
    candidates = frappe.db.sql('''SELECT name, target_name FROM `tabHERO Object Binding`
        WHERE site_scope=%s AND source_instance=%s AND object_type=%s AND external_object_id=%s
          AND target_doctype=%s AND lifecycle=%s ORDER BY name LIMIT 2 FOR UPDATE''',
        (frappe.local.site, source_instance, 'User', str(target), 'User', 'VALID'), as_dict=True)
    if len(candidates) != 1 or not frappe.db.exists('User', candidates[0].target_name):
        return decision('PENDING_IDENTITY', 'PENDING_IDENTITY: source User has no unique existing User binding.')
    recipient = candidates[0].target_name
    if not frappe.db.get_value('User', recipient, 'enabled'):
        return decision('PENDING_IDENTITY', 'PENDING_IDENTITY: bound User is inactive.')
    details = {'recipient': recipient, 'binding': candidates[0].name}
    if not task_name:
        return decision('BLOCKED', 'PENDING_TASK: a new Task has no persisted identity or verified local permissions yet.', **details)
    from hero_integration.services.task_assignment_policy import approved_initial_policy, task_has_policy_company, PolicyError
    try:
        policy = approved_initial_policy(source_instance, candidates[0].name) if frappe.db.exists('DocType', 'HERO Task Assignment Policy') else None
    except PolicyError:
        policy = None
    if not policy:
        return decision('BLOCKED', 'PENDING_AUTHORITY: no current matching system assignment policy.', **details)
    details['policy'] = policy.name
    task = frappe.get_doc('Task', task_name, for_update=True)
    if not task_has_policy_company(task, policy):
        return decision('BLOCKED', 'PENDING_AUTHORITY: Task has no verified project in the policy company.', **details)
    if task.status in {'Completed', 'Cancelled'}:
        return decision('BLOCKED', 'PENDING_AUTHORITY: Task lifecycle does not permit assignment.', **details)
    if not frappe.has_permission('Task', 'write', doc=task, user=frappe.session.user):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: actor cannot write the Task.', **details)
    if not frappe.has_permission('Task', 'read', doc=task, user=recipient):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: recipient has no Task read access; sharing is forbidden.', **details)
    if not frappe.has_permission('ToDo', 'create'):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: integration actor cannot create ToDo.', **details)
    if not frappe.get_system_settings('disable_document_sharing'):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: native assignment requires sharing disabled.', **details)
    from hero_integration.services.task_assignment_state import assignment_boundary_available
    if not assignment_boundary_available():
        return decision('BLOCKED', 'PENDING_AUTHORITY: native assignment boundary is unavailable.', **details)
    link = _link('Task', str(record['id']), source_instance)
    if not link or link.target_doctype != 'Task' or link.target_name != task_name:
        return decision('CONFLICT', 'Task assignment requires its unique source binding.', **details)
    if not frappe.db.exists('DocType', 'HERO Task Assignment State'):
        return decision('BLOCKED', 'PENDING_AUTHORITY: managed assignment state is unavailable.', **details)
    states = frappe.db.sql('SELECT name FROM `tabHERO Task Assignment State` WHERE task_link=%s FOR UPDATE',
                           (link.name,), as_dict=True)
    if len(states) > 1:
        return decision('CONFLICT', 'Multiple assignment provenance records require review.', **details)
    state = frappe.get_doc('HERO Task Assignment State', states[0].name, for_update=True) if states else None
    history = frappe.db.sql('SELECT name FROM `tabToDo` WHERE reference_type=%s AND reference_name=%s FOR UPDATE',
                            ('Task', task_name), as_dict=True)
    active = [frappe.get_doc('ToDo', row.name, for_update=True) for row in history]
    active = [item for item in active if item.status == 'Open']
    details['existing'] = tuple(item.name for item in active)
    if len(active) > 1:
        return decision('CONFLICT', 'Multiple open assignments require local review.', **details)
    if state:
        valid = (state.state == 'SYSTEM' and state.task_name == task_name
                 and state.policy == policy.name and int(state.policy_revision) == int(policy.policy_revision)
                 and int(state.mapping_revision) == int(run.mapping_revision) and len(active) == 1
                 and active[0].name == state.todo and str(active[0].modified) == str(state.todo_modified)
                 and active[0].assigned_by == frappe.session.user)
        if valid:
            old_binding = frappe.get_doc('HERO Object Binding', state.user_binding, for_update=True)
            valid = (active[0].allocated_to == old_binding.target_name
                     and int(state.user_binding_revision or 0) == int(old_binding.binding_revision or 0)
                     and int(state.user_binding_revision or 0) > 0 and old_binding.lifecycle == 'VALID')
        if not valid:
            return decision('CONFLICT', 'PENDING_AUTHORITY: local ownership or assignment baseline changed.', **details)
        if state.user_binding == candidates[0].name:
            return decision('KEEP', 'Current managed assignment is unchanged.', **details)
        return decision('BLOCKED', 'PENDING_SOURCE_ORDER: transfer requires verified source ordering and authority.', **details)
    if active:
        return decision('CONFLICT', 'PENDING_AUTHORITY: an existing local assignment is preserved.', **details)
    if history:
        return decision('BLOCKED', 'PENDING_AUTHORITY: historical assignments require local review.', **details)
    effects = json.loads(policy.policy_json).get('native_effects')
    if not effects:
        return decision('BLOCKED', 'POLICY_REQUIRED: approve local ToDo date and native notification/follow effects.',
                        proposed='CREATE', **details)
    if recipient != frappe.session.user and not effects['allow_notifications']:
        return decision('BLOCKED', 'POLICY_REQUIRED: native assignment notification is not authorized.',
                        proposed='CREATE', **details)
    if frappe.get_cached_value('User', recipient, 'follow_assigned_documents') and not effects['allow_follow']:
        return decision('BLOCKED', 'POLICY_REQUIRED: native document following is not authorized.',
                        proposed='CREATE', **details)
    return decision('CREATE', 'Authorized native initial assignment; local date is today.',
                    proposed='CREATE', date_rule=effects['date_rule'], **details)


def _execute_initial_task_assignment(run, record, task_name, source_instance, observed):
    """Revalidate under the same transaction immediately before native effects."""
    current = _plan_task_assignment(run, record, task_name, source_instance)
    if current != observed or current.action != 'CREATE':
        raise RuntimeError('Task assignment admission changed before execution.')
    from frappe.desk.form import assign_to
    from frappe.utils import nowdate
    from hero_integration.services.task_assignment_state import managed_assignment
    date = nowdate()
    try:
        with managed_assignment():
            assign_to.add({'doctype': 'Task', 'name': task_name, 'assign_to': [current.recipient], 'date': date})
            rows = frappe.get_all('ToDo', filters={'reference_type': 'Task', 'reference_name': task_name,
                                  'status': 'Open'}, fields=['name', 'allocated_to'], limit=2)
            if len(rows) != 1 or rows[0].allocated_to != current.recipient:
                raise RuntimeError('Native assignment did not produce one verified recipient.')
            todo = frappe.get_doc('ToDo', rows[0].name, for_update=True)
            if (todo.reference_type != 'Task' or todo.reference_name != task_name
                    or todo.allocated_to != current.recipient or todo.assigned_by != frappe.session.user
                    or str(todo.date) != str(date)):
                raise RuntimeError('Native assignment does not match the approved Task, actor, recipient and date.')
            binding = frappe.get_doc('HERO Object Binding', current.binding, for_update=True)
            policy = frappe.get_doc('HERO Task Assignment Policy', current.policy, for_update=True)
            link = _link('Task', str(record['id']), source_instance)
            frappe.get_doc({'doctype': 'HERO Task Assignment State', 'task_link': link.name,
                'task_name': task_name, 'todo': todo.name, 'user_binding': binding.name,
                'user_binding_revision': binding.binding_revision, 'policy': policy.name,
                'policy_revision': policy.policy_revision, 'mapping_revision': int(run.mapping_revision),
                'todo_modified': todo.modified, 'state': 'SYSTEM', 'state_revision': 1}).insert()
        return todo.name
    except Exception:
        # Savepoint rollback does not clear Frappe's after-commit callbacks.
        frappe.db.rollback()
        raise


def _record_task_assignment_pending(run, record, task_name: str, source_instance: str) -> None:
    """Use the same locked admission decision for Preview and execution."""
    plan = _plan_task_assignment(run, record, task_name, source_instance)
    if plan.action == 'CREATE' and getattr(run, 'mode', '') == 'Execute':
        todo_name = _execute_initial_task_assignment(run, record, task_name, source_instance, plan)
        record_output(run=run.name, source_type='Task', source_id=str(record['id']), result='CREATED',
                      action='task_assignment_created', target_doctype='Task', target_name=task_name,
                      correlation_id=todo_name, detail='Approved native assignment created.')
        return
    detail = json.dumps({'decision': plan.action, 'proposed': plan.proposed, 'reason': plan.detail, 'recipient': plan.recipient,
                         'existing_todos': list(plan.existing), 'local_date': plan.date_rule or 'POLICY_REQUIRED'}, sort_keys=True)
    record_output(run=run.name, source_type='Task', source_id=str(record['id']),
                  result='CONFLICT' if plan.action in {'BLOCKED', 'CONFLICT', 'PENDING_IDENTITY'} else 'SKIPPED',
                  action='task_assignment_review', detail=detail, target_doctype='Task', target_name=task_name)
    if plan.action in {'BLOCKED', 'CONFLICT', 'PENDING_IDENTITY'} and hasattr(run, 'conflict_records'):
        run.conflict_records = int(run.conflict_records or 0) + 1


def _migrate_native_task(record, run, plan, scope) -> Result:
    """Apply the explicitly approved Task intake using the existing Link identity."""
    from types import SimpleNamespace
    from hero_integration.services.binding_registry import BindingRegistryService
    from hero_integration.services.mapping_registry_model import MappingDirection
    from hero_integration.services.native_projections import _html, _text

    frappe.only_for('System Manager')
    instance = _source_instance(record)
    if scope != {'confirmed': True, 'source_instance': instance, 'source_type': 'Task'}:
        raise MappingPlanError('Native Task intake source scope is not approved.')
    source_id = plan.source_identity(record)
    selected_paths = {definition.source_path for definition in plan.definitions}
    if record.get('is_deleted') is True:
        return Result('SKIPPED_SOURCE_DELETED', 'Source deletion does not delete or close a Task.')
    if _no_project_match(record.get('target_project_match_id')):
        # No project: treated as omitted, so it neither links nor clears nor needs review.
        record = {key: value for key, value in record.items() if key != 'target_project_match_id'}
    values = {}
    try:
        if ('title' in record and record['title'] is not None
                and (not isinstance(record['title'], str) or record['title'].strip())):
            values['subject'] = _text(record['title'], 'Task title', required=True, limit=140)
        if 'comment' in selected_paths and 'comment' in record and record['comment'] is not None and (not isinstance(record['comment'], str) or record['comment'].strip()):
            values['description'] = _html(_text(record['comment'], 'Task comment'))
        if 'target_project_match_id' in selected_paths and record.get('target_project_match_id') is not None:
            values['project'] = _project_name_for_source(record['target_project_match_id'], instance)
        if 'due_date' in selected_paths and record.get('due_date') is not None:
            deadline = _external_task_deadline(record['due_date'])
            if deadline is not None:
                values['custom_hero_due_date'] = deadline
    except ValueError as error:
        return Result('BLOCKED_RECORD', str(error))
    selected = getattr(plan, 'definitions', ())
    assignment_selected = any(item.source_path == 'target_user_id' for item in selected)
    if assignment_selected:
        from dataclasses import replace
        core_plan = replace(plan, definitions=tuple(item for item in selected if item.source_path != 'target_user_id'))
        plan_version = core_plan.mapping_version
    else:
        plan_version = plan.mapping_version
    version = f"native-task-v1:{int(run.mapping_revision)}:{plan_version}"
    key = ':'.join(('HERO', instance, 'Task', source_id))
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    database = getattr(frappe.conf, 'db_name', None)
    if not isinstance(database, str) or not database:
        raise MappingPlanError('Native Task intake requires the current database identity.')
    _lock_active_target('native-task-intake:' + database, 'Task', key)
    rows = frappe.db.sql('SELECT name FROM `tabHERO Integration Link` WHERE source_key=%s FOR UPDATE', (key,), as_dict=True)
    if len(rows) > 1:
        raise MappingPlanError('Task source identity is not unique.')
    if not rows:
        if not values.get('subject'):
            return Result('BLOCKED_RECORD', 'A nonempty Task title is required for intake.')
        if run.mode == 'Preview':
            return Result('PREVIEW_OK', 'A native Task would be created.', 'Task')
        frappe.db.savepoint('hero_native_task_insert')
        try:
            with managed_target_update('Task', None, None):
                task = frappe.get_doc({'doctype': 'Task', **values}).insert()
            source_fields = field_hashes(values)
            target_fields = field_hashes({field: task.get(field) for field in values})
            pending_comment = 'comment' in selected_paths and 'comment' in record and 'description' not in values
            pending_project = ('target_project_match_id' in selected_paths and 'target_project_match_id' in record
                               and 'project' not in values)
            pending_deadline = 'due_date' in selected_paths and 'due_date' in record and 'custom_hero_due_date' not in values
            frappe.get_doc({
                'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'Task',
                'source_id': source_id, 'source_instance': instance, 'sync_status': 'Conflict' if pending_comment or pending_project or pending_deadline else 'Synchronized',
                'conflict_detail': 'Task optional fields require review.' if pending_comment or pending_project or pending_deadline else '',
                'target_doctype': 'Task', 'target_name': task.name, 'source_payload_hash': _digest(record),
                'target_payload_hash': _digest({field: task.get(field) for field in values}),
                'source_field_hashes': encode_hashes(source_fields), 'target_field_hashes': encode_hashes(target_fields),
                'mapping_version': version, 'last_synced_on': now_datetime(),
            }).insert()
        except Exception:
            frappe.db.rollback(save_point='hero_native_task_insert')
            raise
        return Result('CREATED', 'Native Task and source binding created.', 'Task', task.name)
    link = frappe.get_doc('HERO Integration Link', rows[0].name, for_update=True)
    prior_version = str(link.mapping_version or '').split(':')
    same_core = (assignment_selected and len(prior_version) == 3 and prior_version[0] == 'native-task-v1'
                 and prior_version[1].isdigit() and prior_version[2] == plan_version)
    if link.target_doctype != 'Task' or (link.mapping_version != version and not same_core):
        return Result('CONFLICT', 'Task binding or approved mapping version requires review.')
    frappe.db.sql('SELECT name FROM `tabTask` WHERE name=%s FOR UPDATE', (link.target_name,))
    if not frappe.db.exists('Task', link.target_name):
        return Result('CONFLICT', 'Bound Task no longer exists.')
    task = frappe.get_doc('Task', link.target_name, for_update=True)
    source_base = decode_hashes(link.source_field_hashes)
    target_base = decode_hashes(link.target_field_hashes)
    if source_base is None or target_base is None:
        return Result('CONFLICT', 'Task binding lacks a confirmed field baseline.', 'Task', task.name)
    pending, conflicts, updates, omitted = [], [], {}, []
    for field, source_path in (('subject', 'title'), ('description', 'comment'), ('project', 'target_project_match_id'),
                               ('custom_hero_due_date', 'due_date')):
        if source_path not in selected_paths:
            continue
        source_hash = field_hashes({field: values[field]})[field] if field in values else None
        target_hash = field_hashes({field: task.get(field)})[field]
        binding = SimpleNamespace(direction=MappingDirection.HERO_TO_FRAPPE, mapping_revision=int(run.mapping_revision),
                                  acknowledged_source_fingerprint=source_base.get(field),
                                  acknowledged_target_fingerprint=target_base.get(field))
        decision = BindingRegistryService.inbound_reconciliation(binding, active_mapping_revision=int(run.mapping_revision),
                                                                  source_fingerprint=source_hash, target_fingerprint=target_hash)
        if decision == 'APPLY_SOURCE': updates[field] = values[field]
        elif decision == 'CONFIRM_EQUAL':
            source_base[field], target_base[field] = source_hash, target_hash
        elif decision in {'CONFLICT', 'UNACKNOWLEDGED', 'MAPPING_CHANGED'}: conflicts.append(field)
        elif decision == 'OMITTED':
            omitted.append(field)
            if source_path in record: pending.append(field)
    if run.mode == 'Preview':
        return Result('CONFLICT' if conflicts or pending else 'PREVIEW_OK', 'Native Task field comparison completed.', 'Task', task.name)
    if updates:
        for field, value in updates.items(): task.set(field, value)
        with managed_target_update('Task', task.name, link.name): task.save()
        for field, value in updates.items():
            source_base[field] = field_hashes({field: value})[field]
            target_base[field] = field_hashes({field: task.get(field)})[field]
    link.source_field_hashes = encode_hashes(source_base)
    link.target_field_hashes = encode_hashes(target_base)
    link.mapping_version = version
    if conflicts or pending:
        link.sync_status = 'Conflict'
        link.conflict_detail = 'Task fields require review: ' + ', '.join(sorted(conflicts + pending))
    elif not omitted:
        link.sync_status = 'Synchronized'
        link.conflict_detail = ''
    link.save()
    return Result('CONFLICT' if conflicts or pending or link.sync_status == 'Conflict' else ('UPDATED' if updates else 'SKIPPED'),
                  link.conflict_detail or 'Native Task field comparison completed.', 'Task', task.name)


def _external_task_deadline(value):
    """Convert only offset-bearing instants to the configured site timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        instant = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        zone = frappe.db.get_single_value('System Settings', 'time_zone')
        if instant.tzinfo is None or instant.utcoffset() is None or not isinstance(zone, str) or not zone:
            return None
        return instant.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return None


def _migrate_projection(record, run, plan) -> Result:
    operation = plan.target_operation or plan.operation
    contract = PROJECTIONS[operation]
    instance = _source_instance(record)
    try:
        source_id = plan.source_identity(record)
        existing = _link(contract['source_type'], source_id, instance) if contract.get('native_kind') == 'event' else None
        validate_projection_record(operation, record, allow_partial=bool(existing))
        if record.get('is_deleted') is True or record.get('deleted') is True:
            if run.mode != 'Preview':
                _mark_source_deleted(_link(contract['source_type'], source_id, instance))
            return Result('SKIPPED_SOURCE_DELETED', 'Source deletion is observed; no target deletion occurs.')
        # An existing Event whose privacy or outbound settings changed must be
        # held before resolving the saved native options.  This makes Preview
        # fail closed even when it is invoked with a minimal test/run context,
        # and guarantees the protected target is never updated.
        if existing:
            if existing.target_doctype != 'Event':
                return Result('CONFLICT', 'Stored calendar binding is not a native Event.')
            target = _native_lookup('Event', existing.target_name)
            if target.get('event_type') != 'Private' or any(target.get(f) for f in ('event_participants', 'send_reminder', 'sync_with_google_calendar', 'add_video_conferencing')):
                return Result('CONFLICT', 'Event sharing or outbound settings changed; native calendar update requires review.')
        native_values, defaults, review = {}, {}, []
        if contract.get('native_kind') or contract.get('native_bindings'):
            from hero_integration.services.native_projections import values as native_resolve, creation_defaults
            config = json.loads(frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping).mapping_json)
            options = config.get('native_options')
            native_values = native_resolve(contract, record, source_id, instance, options, lookup=_native_lookup,
                                          item_link=lambda identity: _item_name_for_source(identity, instance),
                                          allow_partial=bool(existing), review=review)
            if contract.get('native_kind') == 'event' and options['target_timezone'] != frappe.db.get_single_value('System Settings', 'time_zone'):
                raise MappingPlanError('Configured event target timezone differs from the native System Settings timezone.')
            defaults = creation_defaults(contract, options)
        values = plan.values(record, project_link=lambda value: _project_name_for_source(value, instance),
                             native_value=lambda target: native_values[target], allow_partial=bool(existing))
        values['custom_hero_key'] = _digest([instance, contract['source_type'], source_id])
        values['custom_hero_source_instance'] = instance
    except (ValueError, MappingPlanError) as error:
        return Result('BLOCKED_RECORD', str(error))
    result = _upsert(contract['source_type'], source_id, record, values, contract['target_doctype'],
                   'item_code' if contract['target_doctype'] == 'Item' else 'custom_hero_key', plan.mapping_version, run, source_instance=instance,
                   target_snapshot=lambda target: _projection_target_values(target, values, contract), creation_values=defaults,
                   preserve_omitted=contract.get('native_kind') == 'event')
    if review and result.code in {'UPDATED', 'SKIPPED', 'OBSERVED', 'PREVIEW_OK'}:
        detail = 'Event time or recurrence requires review; unrelated field changes were considered. ' + review[0]
        if run.mode != 'Preview':
            _mark_link_conflict(frappe.get_doc('HERO Integration Link', existing.name), detail)
        return Result('CONFLICT', detail, result.target_doctype, result.target_name)
    return result


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
            if field not in values:
                continue
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


def _no_project_match(value) -> bool:
    """HERO uses ProjectMatch ID 0 for records that belong to no project."""
    return type(value) is int and value == 0


def _project_name_for_source(value, source_instance):
    if _no_project_match(value):
        return None  # same as a NULL reference: no new link, an existing one stays
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
    if not values.get("project_name") and not (
        "project_name" in values.missing and _link("project", source_id, source_instance)
    ):
        return Result("BLOCKED_RECORD", "Configured project record name is empty.")
    return _upsert(
        "project", source_id, record, values, "Project", "project_name", plan.mapping_version, run,
        source_instance=source_instance, required_on_create=("customer", "project_name"),
    )


def observe_monitor_record(operation: str, record: dict[str, object]) -> Result | None:
    """Compare one configured source record without mutating an ERPNext target.

    MONITOR reuses the active mapping plan and the same field-wise three-way
    comparison as the materializer.  It never advances a baseline: a source or
    local change is technical output only, and a same-field change on both
    sides uses the durable conflict and administrator-notification path.
    """
    extra = None
    try:
        if operation == "HeroCustomers":
            plan = active_mapping_plan(operation)
            source_id = plan.source_identity(record)
            values = plan.values(record)
            source_type, doctype = "customer", "Customer"
            home_phone = plan.home_phone(record)
            tracks_home_phone = plan.has_home_phone()
            extra = {"hero_phone_home": home_phone} if tracks_home_phone else None
            contact_fields = plan.customer_contact_fields()
            snapshot = lambda target: _customer_target_values(
                target, values, contact_fields, home_phone, track_home_phone=tracks_home_phone,
            )
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
    comparison = _Comparison(values, extra)
    target_values = comparison.target_values(target, snapshot)
    outcome = comparison.reconcile(prior_source, prior_target, target_values)
    if outcome.conflicts:
        detail = f"Field conflict requires review: {', '.join(sorted(outcome.conflicts))}."
        notify_conflict_once(link, set(outcome.conflicts), detail)
        _mark_link_conflict(link, detail)
        return Result("CONFLICT", detail, doctype, target.name)
    if outcome.apply or outcome.acknowledged:
        _observe_link(link, _digest(record), _digest(target_values))
        return Result("OBSERVED", "HERO field change observed by MONITOR; no ERPNext write was prepared.", doctype, target.name)
    if outcome.local:
        _observe_link(link, _digest(record), _digest(target_values))
        return Result("OBSERVED", "Frappe field change observed by MONITOR; no HERO action was prepared.", doctype, target.name)
    if outcome.converged:
        return Result("OBSERVED", "HERO and Frappe changed to the same values; MONITOR left the baseline unchanged.", doctype, target.name)
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
    preserve_omitted: bool = False,
    comparison_extra: dict | None = None,
    required_on_create: tuple[str, ...] = (),
) -> Result:
    if not _target_available(doctype):
        return Result("UNAVAILABLE", f"{doctype} is not installed on this site.")
    source_hash = _digest(record)
    existing = _link(source_type, source_id, source_instance)
    if existing:
        if existing.target_doctype != doctype:
            return Result('CONFLICT', 'Stored binding has an incompatible target DocType.')
        if preserve_omitted and existing.mapping_version != mapping_version:
            return Result('CONFLICT', 'Event binding or approved mapping version requires review.', doctype, existing.target_name)
        if not frappe.db.exists(existing.target_doctype, existing.target_name):
            if run.mode != "Preview":
                _mark_link_conflict(existing, "Stored target reference no longer exists.")
            return Result("CONFLICT", "Stored target reference no longer exists.")
        target = frappe.get_doc(existing.target_doctype, existing.target_name)
        snapshot = target_snapshot or (lambda item: _target_values(item, values))
        prior_source = decode_hashes(getattr(existing, "source_field_hashes", ""))
        prior_target = decode_hashes(getattr(existing, "target_field_hashes", ""))
        # RC15 links have whole-payload hashes only.  They remain fail-closed
        # until an administrator establishes a fresh, field-level baseline.
        if prior_source is None or prior_target is None:
            target_values = snapshot(target)
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
        comparison = _Comparison(values, comparison_extra)
        target_values = comparison.target_values(target, snapshot)
        target_field_hashes = field_hashes(target_values)
        if preserve_omitted:
            # Events omit unconfirmed values entirely: their acknowledged
            # baselines are carried forward instead of being compared.
            comparison.omit(set(prior_source) | set(prior_target))
        outcome = comparison.reconcile(prior_source, prior_target, target_values)
        if outcome.conflicts:
            detail = f"Field conflict requires review: {', '.join(sorted(outcome.conflicts))}."
            if run.mode != "Preview":
                notify_conflict_once(existing, set(outcome.conflicts), detail)
                _mark_link_conflict(existing, detail)
            return Result("CONFLICT", detail, doctype, target.name)
        if run.mode == "Preview":
            if outcome.apply:
                return Result("PREVIEW_OK", "Existing target would receive non-conflicting HERO field changes.", doctype, target.name)
            return Result("PREVIEW_OK", "Existing target would remain unchanged; local changes are observed only.", doctype, target.name)
        _observe_link(existing, source_hash, _digest(target_values))
        if outcome.apply:
            savepoint = "hero_upsert_update"
            frappe.db.savepoint(savepoint)
            try:
                # Write only fields HERO changed while Frappe kept the baseline;
                # locally changed fields and their relations are never touched.
                for field in sorted(outcome.apply):
                    if field in values:
                        target.set(field, values[field])
                with managed_target_update(doctype, target.name, existing.name):
                    target.save()
                if after_update:
                    after_update(target, outcome)
            except (HomePhoneConflict, CustomerAddressError) as error:
                frappe.db.rollback(save_point=savepoint)
                return Result("CONFLICT", str(error), doctype, target.name)
            target_values = comparison.target_values(target, snapshot)
            target_field_hashes = field_hashes(target_values)
        if outcome.advances_baseline:
            _update_link_baseline(
                existing, source_hash, _digest(target_values),
                next_baseline(prior_source, comparison.source_hashes, outcome, tracked=comparison.tracked),
                next_baseline(prior_target, target_field_hashes, outcome, tracked=comparison.tracked),
                mapping_version,
            )
        kept = f" Local changes were preserved: {', '.join(sorted(outcome.local))}." if outcome.local else ""
        if outcome.apply:
            return Result("UPDATED", "Non-conflicting HERO field changes were applied through native Frappe validation." + kept, doctype, target.name)
        if outcome.converged:
            return Result("CONVERGED", "HERO and Frappe changed to the same values; the baseline was advanced without a write." + kept, doctype, target.name)
        # Observations must not advance the last acknowledged baseline.
        if outcome.local:
            return Result("OBSERVED", "Local field change observed; no outbound HERO mutation was prepared." + kept, doctype, target.name)
        if outcome.acknowledged:
            return Result("SKIPPED", "HERO cleared values that the no-clear policy does not write: " + ", ".join(sorted(outcome.acknowledged)) + ".", doctype, target.name)
        return Result("SKIPPED", "Record is already synchronized.", doctype, target.name)
    unknown = sorted(set(getattr(values, "missing", ())) & set(required_on_create))
    if unknown:
        # A derived classification (e.g. Customer type from company_name) is
        # never defaulted for a new document when HERO omitted its source.
        return Result("BLOCKED_RECORD", "HERO response omits the source of: " + ", ".join(unknown) + ".")
    duplicate = frappe.db.exists(doctype, {name_field: values[name_field]})
    if duplicate:
        duplicate = _unlinked_namesake(doctype, name_field, values[name_field])
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
        comparison = _Comparison(values, comparison_extra)
        target_values = comparison.target_values(target, snapshot)
        target_hash = _digest(target_values)
        frappe.get_doc({
            "doctype": "HERO Integration Link", "source_system": "HERO", "source_type": source_type,
            "source_id": source_id, "source_instance": source_instance, "sync_status": "Synchronized",
            "conflict_detail": "", "last_synced_on": now_datetime(),
            "target_doctype": doctype, "target_name": target.name, "source_payload_hash": source_hash,
            "target_payload_hash": target_hash, "source_field_hashes": encode_hashes(comparison.source_hashes),
            "target_field_hashes": encode_hashes(field_hashes(target_values)), "mapping_version": mapping_version,
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


def _unlinked_namesake(doctype: str, name_field: str, value: object) -> str | None:
    """A same-named target without a HERO link may be a manual record; one linked to another HERO record is not."""
    names = frappe.get_all(doctype, filters={name_field: value}, pluck="name", limit_page_length=0)
    linked = set(frappe.get_all(
        "HERO Integration Link", filters={"target_doctype": doctype, "target_name": ["in", names]},
        pluck="target_name", limit_page_length=0,
    )) if names else set()
    return next((name for name in names if name not in linked), None)


def _target_values(target, values: dict[str, object]) -> dict[str, object]:
    return {field: target.get(field) for field in values}


def _customer_target_values(
    target, values: dict[str, object], contact_fields, home_phone: str | None, *, track_home_phone: bool = False,
) -> dict[str, object]:
    snapshot = _target_values(target, values)
    # Existing rows created before Contact persistence (or a relationship that
    # cannot safely be read) retain the Customer-field snapshot for conflict
    # detection.  A missing native value is not evidence that a user changed
    # the visible Customer projection to ``None``.
    snapshot.update({
        field: value
        for field, value in primary_contact_values(target, set(contact_fields)).items()
        if value is not None
    })
    if home_phone or track_home_phone:
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
    if type(value) is not int or value <= 0:
        raise MappingPlanError("The configured HERO customer relation requires a positive integer identity.")
    source_id = str(value)
    customer_link = _link("customer", source_id, source_instance)
    if not customer_link or customer_link.target_doctype != "Customer":
        raise MappingPlanError("The configured HERO customer relation has no unambiguous ERPNext link.")
    return customer_link.target_name


def _source_instance(record: dict[str, object]) -> str:
    value = record.get("_hero_source_instance")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


DEFAULT_ADDRESS_COUNTRY = "de"


class IncompleteAddress(MappingPlanError):
    """Street, zip code or city is missing; the Customer is imported without this address."""


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
        raise IncompleteAddress("Customer primary address is incomplete.")
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
    company = project_company()
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
