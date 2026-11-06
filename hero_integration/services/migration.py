from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

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
from hero_integration.sources.hero import GraphQLServiceSource, SourcePage, TestingPreviewSource
from hero_integration.services.change_detection import (
    changed_fields, decode as decode_hashes, encode as encode_hashes, field_hashes, managed_target_update, new_mapped_field_conflicts, next_baseline, reconcile,
)
from hero_integration.services.conflict_notifications import notify_conflict_once, notify_review, notify_run_summary
from hero_integration.services.integration_events import record_output
from hero_integration.services import task_lifecycle
from hero_integration.services.performance import measured, profiled
from hero_integration.services.mapping_runtime import MappingPlanError, active_mapping_plan
from hero_integration.services.business_numbers import BusinessNumberError, customer_document_name, project_reference
from hero_integration.services.project_naming import ProjectAddressError, ProjectTitleValidationError, build_project_title
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
    pending_relation: bool = False


class CustomerAddressError(ValueError):
    pass


class RepeatedProjectionPageError(MappingPlanError):
    """The current projection attempt cannot continue without duplicating a page."""


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == []


class _Comparison:
    """Normalize writable values and relation snapshots for field comparison.

    Explicitly empty fields compare as None but are not written; missing fields
    carry no evidence. Relation snapshots cover values absent from the target,
    such as the Customer home phone.
    """

    def __init__(self, values, extra=None):
        extra = dict(extra or {})
        self.missing = set(getattr(values, "missing", ())) - set(values) - set(extra)
        empty = set(getattr(values, "empty", ())) - set(values)
        self.source_values = {**{field: None for field in empty}, **values, **extra}
        # Compare a float 0.0 as empty on the HERO side too, like Frappe's stored
        # 0.0 on the target side; writes still use the original values.
        self.source_values = {field: (None if type(value) is float and value == 0.0 else value)
                              for field, value in self.source_values.items()}
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
        # Frappe stores an empty Float/Currency as 0.0; compare it as empty.
        # Integers (Check, Int) keep 0, which is a real value there.
        return {field: (None if type(value) is float and value == 0.0 else value) for field, value in current.items()}

    def reconcile(self, prior_source, prior_target, target_values):
        return reconcile(
            prior_source, prior_target, self.source_hashes, field_hashes(target_values),
            tracked=self.tracked, missing=self.missing,
            source_empty={field for field, value in self.source_values.items() if _is_empty(value)},
            # Only consulted for fields without a baseline: a numeric field that
            # was just added holds Frappe's default 0, which is no local change.
            target_empty={field for field, value in target_values.items()
                          if _is_empty(value) or (type(value) in (int, float) and value == 0)},
            unwritable=self.unwritable,
        )


def _same_project_notes_after_mapping_change(record, incoming, current):
    """Recognize the two supported renderings of the same plain HERO text.

    Preserve the existing HTML and acknowledge each side's own representation.
    Markup and entities remain conflicts: interpreting them as plain text would
    not prove that the rendered content is the same.
    """
    from hero_integration.services.direct_mapping import plain_text_html
    from frappe.utils.html_utils import sanitize_html

    raw = record.get('partner_notes')
    return (isinstance(raw, str) and bool(raw.strip())
            and not any(character in raw for character in '<>')
            and not re.search(r'&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]{1,31});', raw, re.IGNORECASE)
            and incoming == sanitize_html(raw)
            and current == plain_text_html(raw))


def _reconcile_project_notes_representation(outcome, record, values, target_values):
    if ('notes' in outcome.conflicts
            and _same_project_notes_after_mapping_change(record, values.get('notes'), target_values.get('notes'))):
        from dataclasses import replace
        return replace(outcome, conflicts=outcome.conflicts - {'notes'},
                       converged=outcome.converged | {'notes'})
    return outcome


def _task_midnight_baseline(prior_source, prior_target, current):
    """Recognize a proven legacy Date stored by native Datetime as midnight.

    Both old acknowledgements must prove the same day. Any changed day, time
    component or timezone keeps the original conflict baseline intact.
    """
    from datetime import datetime, time
    result = dict(prior_target)
    for field in ('exp_start_date', 'exp_end_date'):
        value = current.get(field)
        if not isinstance(value, datetime) or value.tzinfo is not None or value.time() != time.min:
            continue
        day_hash = field_hashes({field: value.date().isoformat()})[field]
        if prior_source.get(field) == prior_target.get(field) == day_hash:
            result[field] = field_hashes({field: value})[field]
    return result


def _run_checkpoint_contract(run, *, lock=False):
    """The effective reader/rule contract on the existing Run, without credentials."""
    from hero_integration.services.mapping_runtime import effective_rule_fingerprint
    from hero_integration.services.read_contract import from_documents
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context

    if not getattr(run, "graphql_service", "") or not getattr(run, "graphql_mapping", ""):
        raise MappingPlanError("Legacy run has no explicit checkpoint reader/mapping evidence; queue a new run.")
    if lock:
        frappe.db.sql("SELECT field FROM `tabSingles` WHERE doctype=%s FOR UPDATE", ("HERO Integration Settings",))
    config = get_configuration()
    signatures = []
    readers = [(run.graphql_service, run.graphql_mapping, run.service_revision, run.mapping_revision)]
    if getattr(run, 'customer_service', ''):
        readers.append((run.customer_service, run.customer_mapping,
                        run.customer_service_revision, run.customer_mapping_revision))
    for service_name, mapping_name, service_revision, mapping_revision in sorted(readers):
        observed = frappe.get_doc("HERO GraphQL Mapping", mapping_name)
        scope = (observed.source_instance or '', observed.schema_snapshot, observed.direction)
        if lock:
            lock_exchange_context(*scope)
        mapping = frappe.get_doc("HERO GraphQL Mapping", mapping_name, for_update=lock)
        if (mapping.source_instance or '', mapping.schema_snapshot, mapping.direction) != scope:
            raise MappingPlanError("Checkpoint mapping context changed concurrently.")
        service = frappe.get_doc("HERO GraphQL Service", service_name, for_update=lock)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot, for_update=lock)
        _validate_run_source_binding(run, config, snapshot)
        contract = from_documents(service, mapping, snapshot)
        if contract.service_revision != int(service_revision) or contract.mapping_revision != int(mapping_revision):
            raise MappingPlanError("Checkpoint reader/mapping revision changed.")
        signatures.append({
            "service": service_name, "service_revision": contract.service_revision,
            "mapping": mapping_name, "mapping_revision": contract.mapping_revision,
            "reader": _digest({"query": service.query_document, "mapping": json.loads(mapping.mapping_json)}),
            "schema": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
            "schema_content": _digest(json.loads(snapshot.snapshot_json)),
            "field_rules": effective_rule_fingerprint(contract.definition_ids, mapping_name, lock=lock),
        })
    return {"version": 1, "site": frappe.local.site, "source_instance": config.source_instance,
            "actor": getattr(run, 'owner', '') or frappe.session.user,
            "action": getattr(run, 'action', ''), "mode": getattr(run, 'mode', ''),
            "source_binding_revision": int(config.source_binding_revision), "readers": signatures}


def bind_run_checkpoint(run):
    """Called only at acceptance of a new run; never upgrade a legacy checkpoint."""
    config = get_configuration()
    run.source_instance = config.source_instance
    run.owner = getattr(run, 'owner', '') or frappe.session.user
    run.checkpoint_contract_json = json.dumps(_run_checkpoint_contract(run, lock=True), sort_keys=True)


def _assert_checkpoint_contract(run, *, lock=False):
    try:
        expected = json.loads(getattr(run, "checkpoint_contract_json", "") or "")
    except (ValueError, TypeError):
        raise MappingPlanError("Legacy checkpoint has no bound contract; queue a newly approved run.") from None
    if not isinstance(expected, dict) or expected.get("version") != 1:
        raise MappingPlanError("Unsupported or legacy checkpoint contract; queue a new run.")
    if _run_checkpoint_contract(run, lock=lock) != expected:
        raise MappingPlanError("Source, schema, reader or effective field rules changed; checkpoint was not advanced.")


@contextmanager
def _checkpoint_transaction(run, claim_started_on):
    """Fence the existing lease/owner and atomically acknowledge one unit of work."""
    frappe.db.rollback()  # End the previous read snapshot; no unacknowledged writes cross this boundary.
    try:
        if _locked_active_run() != run.name:
            raise MappingPlanError("Run reservation was replaced; checkpoint was not advanced.")
        current = frappe.get_doc("HERO Integration Run", run.name, for_update=True)
        if (current.state != "Running" or current.started_on != claim_started_on
                or current.owner != frappe.session.user or not current.lease_expires_on
                or get_datetime(current.lease_expires_on) <= now_datetime()
                or current.checkpoint_contract_json != run.checkpoint_contract_json):
            raise MappingPlanError("Run ownership, generation, lease or bound contract changed.")
        _assert_checkpoint_contract(run, lock=True)
        yield
        _assert_checkpoint_contract(run, lock=True)
        run.lease_expires_on = now_datetime() + timedelta(seconds=RUN_LEASE_SECONDS)
        run.save()
        frappe.db.commit()
    except BaseException:
        frappe.db.rollback()
        raise


def _verified_historical_users(run, source):
    """Classify deleted employees from the bound HERO source without creating users."""
    action = getattr(run, 'action', '')
    if action not in PROJECT_ACTIONS and action != 'Projection Migration':
        return ()
    from hero_integration.client.graphql import HeroClientError
    from hero_integration.services import direct_runtime
    from hero_integration.services.employee_migration import EmployeeMigrationBlocked, _historical_identities
    from hero_integration.services.migration_testing import project_reader_fingerprint
    read = direct_runtime.approved_read('HeroEmployees', source.source_instance)
    if read is None:
        return ()
    fingerprint = project_reader_fingerprint(read)
    if source.revalidate and source.revalidate() != source.contract:
        raise MappingPlanError('Read approval changed before historical identity verification.')
    try:
        identities, _ = _historical_identities(source.client, read.records_path, None, None,
                                               read=read, source_instance=source.source_instance)
    except (HeroClientError, EmployeeMigrationBlocked):
        return ()  # Unverified references keep their ordinary pending status.
    if source.revalidate and source.revalidate() != source.contract:
        raise MappingPlanError('Read approval changed during historical identity verification.')
    current = direct_runtime.approved_read('HeroEmployees', source.source_instance)
    if current is None or project_reader_fingerprint(current) != fingerprint:
        raise MappingPlanError('Employee reader changed during historical identity verification.')
    return identities


@profiled('migration.run')
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
        _assert_checkpoint_contract(run)
        validate_run_mappings(run)
        config = get_configuration()
        source = _source_for_run(run, config)
        if bool(int(getattr(run, 'testing_enabled', 0) or 0)):
            from hero_integration.services.file_migration import related_test_for_run
            from hero_integration.services.migration_testing import RecordLimitedSource, RelatedProjectSource, record_limit
            cohort = related_test_for_run(run)
            testing_source = (RelatedProjectSource(source, record_limit(run), cohort) if cohort is not None
                              else RecordLimitedSource(source, record_limit(run)))
            source = testing_source
        from hero_integration.services.employee_migration import historical_user_scope
        historical = _verified_historical_users(run, source)
        with historical_user_scope(getattr(source, 'source_instance', ''), historical):
            _process_requested_phases(run, source, checkpoint=lambda: _checkpoint_transaction(run, claim_started_on))
        run.reload()
        if run.state == "Running":
            with _checkpoint_transaction(run, claim_started_on):
                run.reload()
                incomplete_sample = bool(testing_source and getattr(testing_source, 'status', '') == 'BLOCKED')
                run.state = "Completed With Conflicts" if run.conflict_records or incomplete_sample else "Completed"
                run.finished_on = now_datetime()
                run.save()
                _mark_sync_success(run)
            _notify_not_imported(run)
    except RepeatedProjectionPageError as error:
        failed = True
        frappe.db.rollback()
        _finalize_blocked_run(run.name, claim_started_on, str(error))
    except MappingPlanError as error:
        failed = True
        frappe.db.rollback()
        _finalize_blocked_run(run.name, claim_started_on, str(error))
        _notify_not_imported(run)
    except _Cancelled:
        frappe.db.rollback()
        run.reload()  # The cancelling request owns its persisted state; do not overwrite a newer claim.
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
            if _locked_active_run() == run.name:
                current = frappe.get_doc("HERO Integration Run", run.name, for_update=True)
                if current.started_on == claim_started_on:
                    _release_run(run.name)
        _advance_full_migration(run.name)


def _advance_full_migration(run_name: str) -> None:
    """Notify successful standalone areas and continue any parent migration."""
    if not hasattr(getattr(frappe, "db", None), "get_default"):
        return  # no site database (offline run doubles)
    try:
        from hero_integration.services.file_migration import on_run_finished
        if frappe.db.get_value("HERO Integration Run", run_name, "state") not in TERMINAL_STATES:
            return
        on_run_finished(run_name)
    except Exception:
        frappe.log_error(title="HERO Full Migration could not continue")


def _notify_not_imported(run) -> None:
    """Notify administrators of every record the run could not import."""
    if getattr(run, "mode", "") == "Preview":
        return
    try:
        notify_run_summary(run.name)
    except Exception:
        # A notification failure never changes the outcome of a finished run.
        frappe.log_error(title="HERO run summary notification failed")


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
        # Every record/phase checkpoint has already committed. End any read
        # snapshot and release all transaction callbacks before external HTTP.
        frappe.db.rollback()
        _assert_checkpoint_contract(run)
        current = from_documents(
            frappe.get_doc('HERO GraphQL Service', run.graphql_service),
            frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping),
            frappe.get_doc('HERO GraphQL Schema Snapshot', service.schema_snapshot),
        )
        frappe.db.rollback()
        return current
    source = GraphQLServiceSource(HeroGraphQLClient(
        config.endpoint, lambda: config.api_token, config.timeout_seconds,
        max_retries=0 if bool(int(getattr(run, 'testing_enabled', 0) or 0)) else 2,
        requests_per_minute=_request_rate(),
    ), service, contract, getattr(snapshot, "source_instance", "") or config.source_instance,
       revalidate=revalidate_generated)
    if contract.target_operation == 'HeroProjectMatches':
        customer_service, customer_mapping, customer_snapshot = _customer_read_documents(run)
        customer_contract = from_documents(customer_service, customer_mapping, customer_snapshot)
        def revalidate_customer():
            frappe.db.rollback()
            _assert_checkpoint_contract(run)
            current_service, current_mapping, current_snapshot = _customer_read_documents(run)
            current = from_documents(current_service, current_mapping, current_snapshot)
            frappe.db.rollback()
            return current
        source.customer_source = GraphQLServiceSource(
            source.client, customer_service, customer_contract, source.source_instance, revalidate=revalidate_customer,
        )
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
    """Claim the shared Singles slot before either worker observes or replaces it.

    Queue IDs are unique per run and cannot serialize different requested runs;
    the database row lock provides that boundary.
    """
    if not _acquire_active_run_lock():
        _block_run(run, "Could not acquire the HERO run reservation lock.")
        return False
    try:
        active_run = _locked_active_run()
        from hero_integration.services.run_reservation import locked_b8_runs
        if locked_b8_runs(frappe, frappe.local.site):
            _block_run(run, "A bound synchronization run is active; migration was not started.")
            return False
        now = now_datetime()
        if active_run == run.name and frappe.db.get_value("HERO Integration Run", run.name, "state", for_update=True) == "Running":
            if run_lease_alive(run.name, now, lock=True):
                return False
            # A duplicate or retried job of the same run after its worker died.
            _interrupt_run(run.name, now)
            run.reload()
        if active_run and active_run != run.name:
            active_state = frappe.db.get_value("HERO Integration Run", active_run, "state", for_update=True)
            if active_state not in TERMINAL_STATES:
                if run_lease_alive(active_run, now, lock=True):
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
        # A first-use stale-row conflict grants no reservation. Roll back and
        # record Blocked instead of retrying an uncertain claim.
        frappe.db.rollback()
        run.reload()
        _block_run(run, "Another connection is establishing the HERO run reservation; run was not started.")
        frappe.db.commit()
        return False
    finally:
        _release_active_run_lock()


def run_lease_alive(run_name: str, now=None, *, lock=False) -> bool:
    """Check whether a nonterminal run may still have a live worker.

    Older runs use started_on plus the lease window. Nonterminal rows outside
    Running conservatively count as alive.
    """
    row = frappe.db.get_value("HERO Integration Run", run_name, ["state", "lease_expires_on", "started_on"],
                              as_dict=True, **({"for_update": True} if lock else {}))
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
    from hero_integration.services.run_reservation import locked_legacy_slot
    return locked_legacy_slot(frappe)


def _acquire_active_run_lock() -> bool:
    from hero_integration.services.run_reservation import acquire
    return acquire(frappe)


def _release_active_run_lock() -> None:
    from hero_integration.services.run_reservation import release
    release(frappe)


def _process_requested_phases(run, source: GraphQLServiceSource, *, checkpoint=None) -> None:
    # A project run is never allowed to bypass its own complete Customer pass.
    # This is intentionally stricter than a mere lookup during project creation:
    # the gate belongs to the same run and source instance, and survives retries.
    checkpoint = checkpoint or nullcontext
    _bind_source_instance(run, source.source_instance)
    if run.action in PROJECTION_ACTIONS:
        _process_projection(run, source, checkpoint=checkpoint)
        return
    needs_customer_phase = run.action in CUSTOMER_ACTIONS or run.action in PROJECT_ACTIONS
    if needs_customer_phase and not _process_phase(run, "customer", source.customers, checkpoint=checkpoint):
        return
    if run.action in PROJECT_ACTIONS:
        if not _customer_phase_completed(run, source.source_instance):
            with checkpoint():
                _block_run(run, "Project phase requires a completed Customer phase for this source instance and run.")
            return
        completed = _process_phase(run, "project", source.projects, checkpoint=checkpoint)
        _refresh_project_phases(run, source)
        _refresh_project_logbook(run, source)
        if not completed:
            return
    if run.action == "Full Data Migration":
        with checkpoint():
            _record_result(
                run,
                "attachment",
                {"id": "hero-v1-contract"},
                Result("UNAVAILABLE", "No approved HERO attachment query exists in the V1 source contract."),
            )


def _refresh_project_phases(run, source) -> None:
    """Refresh the displayed HERO Phase outside the run checkpoint.

    Phase comes from the imported step ID, not sync state. A blocked project
    phase has already left Running and still needs its display refreshed.
    """
    if getattr(source, "query_adapter", None):
        return  # v2 phase values come exclusively from selected Query rules
    reader = getattr(source, "project_status_steps", None)
    if getattr(run, "mode", "Preview") != "Execute" or reader is None:
        return  # Preview and test doubles without a mode never write
    from hero_integration.integrations.project_form import refresh_project_phases

    try:
        refresh_project_phases(reader())
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="HERO project phase refresh failed")


def _refresh_project_logbook(run, source) -> None:
    """Run the legacy global logbook pass only for unrestricted Execute imports."""
    if getattr(source, "query_adapter", None):
        return  # v2 collections are processed from the approved Project row
    if bool(int(getattr(run, "testing_enabled", 0) or 0)):
        return  # this legacy pass cannot restrict writes to the run's selected Projects
    client = getattr(source, "client", None)
    if getattr(run, "mode", "Preview") != "Execute" or client is None or not hasattr(source, "project_status_steps"):
        return  # Preview and test doubles never read or write the logbook
    from hero_integration.services.project_logbook import sync_logbook

    try:
        emails = source.user_emails() if hasattr(source, "user_emails") else {}
        sync_logbook(client, emails, source_instance=source.source_instance)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title="HERO project logbook refresh failed")


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
        raise MappingPlanError.from_error(error) from error
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
        lock_rules=False,
    )


def _validate_enumeration_page(page: SourcePage, offset: int, seen_ids: set[str], *, allow_filtered_empty=False) -> None:
    """Reject invalid pagination before applying any records from the page."""
    if page.next_offset is not None and (type(page.next_offset) is not int
            or page.next_offset <= offset or (not page.records and not allow_filtered_empty)):
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


def _process_phase(run, phase: str, reader, *, checkpoint=None) -> bool:
    checkpoint = checkpoint or nullcontext
    if _phase_state(run, phase) == "Completed":
        return True
    with checkpoint():
        _set_phase_state(run, phase, "Running")
    offset = int(run.resume_offset or 0) if run.resume_phase == phase else 0
    record_index = int(run.resume_record_index or 0) if run.resume_phase == phase else 0
    if offset or record_index:
        # Offset continuation requires a stable source snapshot; mid-page
        # continuation additionally requires persisted page identity evidence.
        with checkpoint():
            _set_phase_state(run, phase, "Blocked")
            _block_run(run, "Cannot resume without a verified source snapshot and page identity evidence.")
        return False
    seen_ids: set[str] = set()
    while True:
        _cancel_if_requested(run)
        testing_source = getattr(reader, "__self__", None)
        from hero_integration.services.migration_testing import RelatedProjectSource
        try:
            page: SourcePage = reader(offset)
        except ValueError as error:
            if isinstance(testing_source, RelatedProjectSource):
                raise MappingPlanError(str(error)) from error
            raise
        if isinstance(testing_source, TestingPreviewSource) and testing_source.status in {"BLOCKED", "LIMIT_REACHED"}:
            detail = (
                "Testing preview blocked because newest-first ordering is not verified for the selected HERO source."
                if testing_source.status == "BLOCKED"
                else "Testing preview reached its shared HERO request budget; no resume is permitted."
            )
            with checkpoint():
                _set_phase_state(run, phase, "Blocked")
                _block_run(run, detail)
            return False
        _validate_enumeration_page(page, offset, seen_ids, allow_filtered_empty=isinstance(testing_source, RelatedProjectSource))
        for index, record in enumerate(page.records):
            if index < record_index:
                continue
            _cancel_if_requested(run)
            with checkpoint():
                migrate = _migrate_customer if phase == "customer" else _migrate_project
                result = _migrate_isolated(lambda: migrate(record, run))
                _record_result(run, phase, record, result)
                _record_phase_result(run, phase, result)
                run.resume_phase = phase
                run.resume_offset = offset
                run.resume_record_index = index + 1
                run.save()
        with checkpoint():
            run.resume_phase = phase
            run.resume_offset = page.next_offset or 0
            run.resume_record_index = 0
            run.save()
            if page.next_offset is None:
                # Per-record failures produce Completed With Conflicts and an admin summary;
                # they do not block the entire Project run.
                _set_phase_state(run, phase, "Completed")
                return True
        offset = page.next_offset
        record_index = 0


def _cancel_if_requested(run) -> None:
    run.reload()
    if run.state == "Cancelled":
        raise _Cancelled()


def _persist_testing_result(run, source: TestingPreviewSource) -> None:
    """Store only technical counters and source outcomes for the finished test run."""
    try:
        run.reload()
        status = "FAILED" if run.state == "Failed" else source.status
        run.testing_executed_actions = source.executed_actions
        run.testing_records_received = source.records_received
        run.testing_status = status
        from hero_integration.services.migration_testing import RecordLimitedSource, RelatedProjectSource
        results = {'record_limit': source.maximum, 'areas': source.results} if isinstance(source, RecordLimitedSource) else source.results
        if isinstance(source, RelatedProjectSource):
            results['selection'] = 'related_projects'
        run.testing_results_json = json.dumps(results, ensure_ascii=False, sort_keys=True)
        run.save()
    except Exception:
        frappe.log_error(title="HERO testing preview metadata could not be recorded")


def _migrate_customer(record: dict[str, object], run) -> Result:
    from hero_integration.services.query_mapping import IncompleteMappedAddress
    try:
        plan = _mapping_plan_for_run(run, "HeroCustomers")
        source_id = plan.source_identity(record)
        if getattr(plan, 'adapter_options', None) and any(type(record.get(flag)) is not bool for flag in ('is_deleted', 'is_contact_person')):
            raise MappingPlanError('Mapped Customer lifecycle flags must be explicitly returned as booleans.')
        values = plan.values(record)
        # ERPNext rejects such an email address; the Customer is imported without it.
        email_note = _drop_invalid_email(values)
        home_phone, phone_originals, phone_note = _clean_customer_phones(values, plan.home_phone(record))
        contact_values = plan.customer_contact_values(values)
        address_note = ""
        incomplete_address = False
        try:
            if getattr(plan, 'adapter_options', None):
                from hero_integration.services.query_mapping import address_values
                address = address_values(plan, record)
            else:
                address = _customer_primary_address(record, plan.customer_primary_address_line2(record))
        except (IncompleteAddress, IncompleteMappedAddress) as error:
            # The Customer is still imported; an incomplete address is skipped, as if HERO had none.
            address, address_note = None, f" {error} It was not imported."
            incomplete_address = True
        if not getattr(plan, 'adapter_options', None) and address is not None and not address.get("country_code"):
            # HERO customers are German unless HERO names another country.
            address["country_code"] = DEFAULT_ADDRESS_COUNTRY
            address_note = " The HERO address has no country; Germany was assumed."
        document_name, name_note = _document_name(
            customer_document_name, record, "customer number", internal_prefix="HERO-CUSTOMER",
        )
    except (MappingPlanError, ValueError) as error:
        return Result("BLOCKED_RECORD", str(error))
    # A v2 query plan needs no lifecycle rule; its validated canonical
    # ``is_deleted`` flag decides on its own.
    adapter_deleted = bool(getattr(plan, 'adapter_options', None)) and record.get('is_deleted') is True
    if adapter_deleted or plan.source_deleted(record):
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
    defer_relations = _defer_relationships(run)
    if defer_relations:
        # ERPNext's Customer.on_update creates a Contact from these proxies.
        # Do not let that hook realize a relationship during the scalar import.
        for field in plan.customer_contact_fields() | {'first_name', 'last_name', 'email_id', 'mobile_no'}:
            values.pop(field, None)
            values.empty.discard(field)
            values.missing.add(field)
        if tracks_home_phone:
            values.missing.add('hero_phone_home')
        tracks_home_phone = False
    drift = _business_number_drift(
        "customer", source_id, _source_instance(record), "Customer", "custom_hero_customer_number",
        record.get("nr"), "customer number", run, observed="nr" in record,
    )
    if drift:
        return drift

    def after_insert(customer):
        if defer_relations:
            return
        _synchronize_customer_relations(
            customer, contact_values, home_phone, address, _source_instance(record), plan.mapping_version,
        )
        _keep_phone_originals(customer, phone_originals)

    def after_update(customer, outcome):
        if defer_relations:
            return
        # Relations receive only the reconciled HERO changes: a locally edited
        # Contact value is never rewritten because another field changed.
        _synchronize_customer_relations(
            customer, {field: value for field, value in contact_values.items() if field in outcome.apply},
            home_phone if "hero_phone_home" in outcome.apply else None,
            address, _source_instance(record), plan.mapping_version,
        )
        _keep_phone_originals(customer, {field: text for field, text in phone_originals.items() if field in outcome.apply})

    from hero_integration.services.deferred_references import realizing
    bindings_only = realizing() and getattr(run,'state',None)=='Completed' and run.mode!='Preview'
    if bindings_only:
        from hero_integration.services.mapping_runtime import MappedValues
        allowed = plan.customer_contact_fields() | {'first_name','last_name','email_id','mobile_no'}
        values = MappedValues({key:value for key,value in values.items() if key in allowed},
            missing=set(values.missing) | ((set(values) | set(values.empty))-allowed),
            empty=set(values.empty) & allowed)
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
        document_name=document_name, preserve_unselected=bindings_only,
    )
    if (not defer_relations and run.mode != 'Preview' and address is not None and result.target_name
            and result.code in {'SKIPPED', 'CONVERGED', 'OBSERVED'}):
        # An address-only observation need not change any Customer scalar.
        # Its own writer still verifies exclusive ownership and field baselines.
        frappe.db.savepoint('hero_customer_primary_relations')
        try:
            customer = frappe.get_doc('Customer', result.target_name)
            _synchronize_customer_relations(customer, {}, None, address, _source_instance(record), plan.mapping_version)
        except (HomePhoneConflict, CustomerAddressError) as error:
            frappe.db.rollback(save_point='hero_customer_primary_relations')
            return Result('CONFLICT', str(error), 'Customer', result.target_name)
    result = _sync_mapped_relations(plan, record, result, run)
    if incomplete_address and result.target_name and result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED'}:
        if not result.pending_relation and run.mode != 'Preview':
            record_output(run=run.name, source_type='customer', source_id=str(source_id), result='PENDING_REVIEW',
                          action='customer_address_review', target_doctype='Customer', target_name=result.target_name,
                          detail=address_note.strip())
        from dataclasses import replace
        result = replace(result, pending_relation=True)
    note = email_note + phone_note + address_note + (name_note if _creates(result) else "")
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
_PHONE_LABELS = {"mobile_no": "mobile phone", "hero_phone_home": "home phone", "phone": "phone"}


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
    if "phone" in values:
        main_phone, original = _phone_number(values["phone"])
        if original:
            originals["phone"] = original
            if main_phone:
                values["phone"] = main_phone
            else:
                values.pop("phone", None)
                if hasattr(values, "missing"):
                    values.missing.add("phone")
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


def _projection_task_scope(run, plan, mapping, snapshot_source, source_instance):
    """One owning scope decision for initial intake and deferred reconciliation."""
    operation = plan.target_operation or plan.operation
    scope = json.loads(mapping.mapping_json).get('native_task_onboarding')
    if getattr(plan, 'adapter_options', None) and operation == 'HeroTasks':
        scope = {'confirmed': True, 'source_instance': snapshot_source, 'source_type': 'Task'}
    if scope is not None and (operation != 'HeroTasks' or source_instance != scope['source_instance']
                              or getattr(run, 'source_instance', '') != scope['source_instance']):
        raise MappingPlanError('Native Task onboarding source does not match the approved run.')
    return scope


def _defer_relationships(run):
    from hero_integration.services.deferred_references import realizing
    return run.mode != 'Preview' and not realizing()


def _reference_rule(rule):
    return (rule.purpose in {'Customer Relation','Project Relation'} or
            rule.transformation in {'HERO Customer Link','HERO Project Link','HERO User Link','User By Partner ID'})


def _defer_reference_values(plan, values, run):
    """Keep relationship fields out of writes and baseline acknowledgements.

    The original record and exact dependencies are saved by remember(); only
    the post-success service may turn those identities into target values.
    """
    deferred = set()
    if not _defer_relationships(run):
        return deferred
    for rule in plan.definitions:
        if rule.target_doctype != plan.target_doctype:
            continue
        if _reference_rule(rule):
            if rule.target_field not in values.missing:
                deferred.add(rule.target_field)
            values.pop(rule.target_field, None)
            values.empty.discard(rule.target_field)
            values.missing.add(rule.target_field)
    return deferred


@measured('migration.record')
def _migrate_projection_record(record, run, plan, task_scope):
    if task_scope is None:
        return _migrate_isolated(lambda: _migrate_projection(record, run, plan))
    task_record = record if any(item.source_path == 'comment' for item in plan.definitions) else {
        k: v for k, v in record.items() if k != 'comment'}
    result = _migrate_isolated(lambda: _migrate_native_task(task_record, run, plan, task_scope))
    if (result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'PREVIEW_OK'} and result.target_doctype == 'Task'
            and not getattr(plan, 'adapter_options', None)
            and not _defer_relationships(run)
            and any(item.source_path == 'target_user_id' for item in plan.definitions)):
        action = _record_task_assignment_pending(run, record, result.target_name, task_scope['source_instance'])
        if action in {'BLOCKED', 'PENDING_IDENTITY', 'CONFLICT'}:
            from dataclasses import replace
            result = replace(result, pending_relation=True)
    return result


def realize_mapped_bindings(record,run,plan):
    """Update only native references/assignments of an already imported target."""
    from dataclasses import replace
    from hero_integration.services.deferred_references import realizing
    from hero_integration.services.query_mapping import mapped_values
    operation = plan.target_operation or plan.operation
    if (not realizing() or run.mode=='Preview' or run.state!='Completed' or
            operation not in {'HeroTasks','HeroProjectMatches','HeroCalendarEvents'} or plan.adapter_options.get('version')!=2
            or run.source_instance!=_source_instance(record)):
        raise MappingPlanError('Mapped binding realization requires its successful originating area.')
    instance,identity=_source_instance(record),plan.source_identity(record)
    kind,doctype,name_field={'HeroTasks':('Task','Task','subject'),
        'HeroProjectMatches':('project','Project','project_name'),
        'HeroCalendarEvents':('CalendarEvent','Event','custom_hero_key')}[operation]
    link=_link(kind,identity,instance)
    if (not link or link.target_doctype!=doctype or link.mapping_version!=plan.mapping_version or
            not frappe.db.exists(doctype,link.target_name)):
        raise MappingPlanError('The imported binding target or mapping changed before realization.')
    target=frappe.get_doc(doctype,link.target_name,for_update=True)
    if operation=='HeroCalendarEvents' and (target.get('event_type')!='Private' or any(target.get(field) for field in (
            'event_participants','send_reminder','sync_with_google_calendar','add_video_conferencing'))):
        return Result('CONFLICT','Event sharing or outbound settings changed; native calendar update requires review.',doctype,target.name)
    subset=replace(plan,definitions=tuple(rule for rule in plan.definitions
        if rule.target_doctype==doctype and _reference_rule(rule)))
    values=mapped_values(subset,record,customer_link=lambda value:_customer_name_for_source(value,instance),
        project_link=lambda value:_project_name_or_deferred(value,instance,plan,record))
    result=_upsert(kind,identity,record,values,doctype,name_field,plan.mapping_version,run,
        source_instance=instance,preserve_unselected=True)
    if operation=='HeroCalendarEvents':
        return result
    if operation=='HeroTasks':
        result=_mapped_task_assignment(record,run,plan,result)
    else:
        result=_project_assignments_after_import(record,run,result,identity,instance)
    return _sync_mapped_relations(plan,record,result,run)


def _process_projection(run, source, *, checkpoint=None) -> None:
    """Reuse the existing run, read service, mapping plan and conflict-safe writer."""
    checkpoint = checkpoint or nullcontext
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
    offset = int(run.resume_offset or 0)
    index_start = int(run.resume_record_index or 0)
    if offset or index_start:
        with checkpoint():
            _block_run(run, 'Cannot resume without a verified source snapshot and page identity evidence.')
        return
    task_scope = _projection_task_scope(run, plan, mapping, snapshot.source_instance, source.source_instance)
    seen_ids: set[str] = set()
    _PROJECT_REFERENCE_STATES.clear()
    _load_task_user_directory(source, operation)
    while True:
        _cancel_if_requested(run)
        from hero_integration.services.migration_testing import RelatedProjectSource
        try:
            page = source.projection(offset)
        except ValueError as error:
            if isinstance(source, RelatedProjectSource):
                raise MappingPlanError(str(error)) from error
            raise
        if isinstance(source, TestingPreviewSource) and source.status in {"BLOCKED", "LIMIT_REACHED"}:
            detail = (
                "Testing preview blocked because newest-first ordering is not verified for the selected HERO source."
                if source.status == "BLOCKED"
                else "Testing preview reached its shared HERO request budget; no resume is permitted."
            )
            with checkpoint():
                _block_run(run, detail)
            return
        try:
            _validate_enumeration_page(page, offset, seen_ids, allow_filtered_empty=isinstance(source, RelatedProjectSource))
        except MappingPlanError as error:
            raise RepeatedProjectionPageError(str(error)) from error
        _prefetch_project_references(source, operation, page.records)
        for index, record in enumerate(page.records):
            if index < index_start:
                continue
            _cancel_if_requested(run)
            with checkpoint():
                result = _migrate_projection_record(record, run, plan, task_scope)
                _record_result(run, PROJECTIONS[operation]['source_type'], record, result)
                run.resume_phase = operation
                run.resume_offset, run.resume_record_index = offset, index + 1
                run.save()
        if page.next_offset is None:
            unmatched = _TASK_USER_DIRECTORY.get('unmatched')
            if unmatched and getattr(run, 'mode', '') == 'Execute':
                from hero_integration.services.conflict_notifications import notify_unmatched_assignees
                with checkpoint():
                    notify_unmatched_assignees(run.name, unmatched)
            return
        offset, index_start = page.next_offset, 0
        with checkpoint():
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
    # HERO permits any employee to assign a task. The source author therefore
    # does not determine authority; the recipient and local policy still do.
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
    policy_payload = json.loads(policy.policy_json)
    silent_initial = policy_payload.get('silent_initial') is True
    task = frappe.get_doc('Task', task_name, for_update=True)
    if not silent_initial and not task_has_policy_company(task, policy):
        return decision('BLOCKED', 'PENDING_AUTHORITY: Task has no verified project in the policy company.', **details)
    if task.status == 'Cancelled' or (task.status == 'Completed' and not silent_initial):
        return decision('BLOCKED', 'PENDING_AUTHORITY: Task lifecycle does not permit assignment.', **details)
    if not frappe.has_permission('Task', 'write', doc=task, user=frappe.session.user):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: actor cannot write the Task.', **details)
    if not frappe.has_permission('Task', 'read', doc=task, user=recipient):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: recipient has no Task read access; sharing is forbidden.', **details)
    if not frappe.has_permission('ToDo', 'create'):
        return decision('BLOCKED', 'BLOCKED_PERMISSION: integration actor cannot create ToDo.', **details)
    if not silent_initial and not frappe.get_system_settings('disable_document_sharing'):
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
    history_docs = [frappe.get_doc('ToDo', row.name, for_update=True) for row in history]
    active = [item for item in history_docs if item.status == 'Open']
    details['existing'] = tuple(item.name for item in active)
    if silent_initial and task.status == 'Completed' and not state:
        completed_link = _link('task_completed_assignment', str(record['id']), source_instance)
        if completed_link:
            if completed_link.target_doctype != 'ToDo' or not frappe.db.exists('ToDo', completed_link.target_name):
                return decision('CONFLICT', 'The completed assignment binding has a missing target.', **details)
            completed = frappe.get_doc('ToDo', completed_link.target_name, for_update=True)
            expected_source = _digest({'recipient_id': target})
            observed = _digest({'task': completed.reference_name, 'recipient': completed.allocated_to,
                                'status': completed.status, 'reference_type': completed.reference_type})
            if (completed_link.source_payload_hash != expected_source or completed_link.target_payload_hash != observed
                    or completed.reference_type != 'Task' or completed.reference_name != task_name
                    or completed.allocated_to != recipient or completed.status != 'Closed'):
                return decision('BLOCKED', 'PENDING_AUTHORITY: completed assignment changed and is preserved.', **details)
            return decision('KEEP', 'Completed HERO assignment is already recorded.', **details)
        if active:
            return decision('BLOCKED', 'PENDING_AUTHORITY: existing local assignments are preserved.', **details)
        if history:
            if any(item.status == 'Closed' and item.allocated_to == recipient for item in history_docs):
                return decision('KEEP', 'Existing completed assignment already includes the HERO recipient.', **details)
            return decision('BLOCKED', 'PENDING_AUTHORITY: historical assignments require local review.', **details)
    if silent_initial and not state and active:
        if any(item.allocated_to == recipient for item in active):
            return decision('KEEP', 'Existing local assignment already includes the HERO recipient.', **details)
        return decision('BLOCKED', 'PENDING_AUTHORITY: existing local assignments are preserved.', **details)
    if len(active) > 1:
        return decision('CONFLICT', 'Multiple open assignments require local review.', **details)
    if state:
        issuer = (getattr(state, 'owner', None) or frappe.session.user) if policy_payload.get('allow_all_system_managers') else frappe.session.user
        valid = (state.state == 'SYSTEM' and state.task_name == task_name
                 and state.policy == policy.name and int(state.policy_revision) == int(policy.policy_revision)
                 and int(state.mapping_revision) == int(run.mapping_revision) and len(active) == 1
                 and active[0].name == state.todo and str(active[0].modified) == str(state.todo_modified)
                 and active[0].assigned_by == issuer)
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
    effects = policy_payload.get('native_effects')
    if not effects:
        return decision('BLOCKED', 'POLICY_REQUIRED: approve local ToDo date and native notification/follow effects.',
                        proposed='CREATE', **details)
    if not silent_initial and recipient != frappe.session.user and not effects['allow_notifications']:
        return decision('BLOCKED', 'POLICY_REQUIRED: native assignment notification is not authorized.',
                        proposed='CREATE', **details)
    if not silent_initial and frappe.get_cached_value('User', recipient, 'follow_assigned_documents') and not effects['allow_follow']:
        return decision('BLOCKED', 'POLICY_REQUIRED: native document following is not authorized.',
                        proposed='CREATE', **details)
    action = 'CREATE_COMPLETED' if silent_initial and task.status == 'Completed' else 'CREATE'
    return decision(action, 'Authorized initial assignment; local date is today.',
                    proposed=action, date_rule=effects['date_rule'], **details)


def _execute_initial_task_assignment(run, record, task_name, source_instance, observed):
    """Revalidate under the same transaction immediately before native effects."""
    current = _plan_task_assignment(run, record, task_name, source_instance)
    if current != observed or current.action not in {'CREATE', 'CREATE_COMPLETED'}:
        raise RuntimeError('Task assignment admission changed before execution.')
    from frappe.desk.form import assign_to
    from frappe.utils import nowdate
    from hero_integration.services.task_assignment_state import managed_assignment
    date = nowdate()
    try:
        with managed_assignment():
            policy = frappe.get_doc('HERO Task Assignment Policy', current.policy, for_update=True)
            completed = current.action == 'CREATE_COMPLETED'
            if json.loads(policy.policy_json).get('silent_initial') is True:
                created_todo = frappe.get_doc({'doctype': 'ToDo', 'allocated_to': current.recipient, 'reference_type': 'Task',
                    'reference_name': task_name, 'description': frappe._('Assignment for Task {0}').format(task_name),
                    'priority': 'Medium', 'status': 'Closed' if completed else 'Open',
                    'date': date, 'assigned_by': frappe.session.user}).insert()
            else:
                assign_to.add({'doctype': 'Task', 'name': task_name, 'assign_to': [current.recipient], 'date': date})
            if completed:
                todo = frappe.get_doc('ToDo', created_todo.name, for_update=True)
                if (todo.reference_type != 'Task' or todo.reference_name != task_name or todo.status != 'Closed'
                        or todo.allocated_to != current.recipient or todo.assigned_by != frappe.session.user):
                    raise RuntimeError('Completed assignment does not match its approved task and recipient.')
                frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
                    'source_instance': source_instance, 'source_type': 'task_completed_assignment',
                    'source_id': str(record['id']), 'target_doctype': 'ToDo', 'target_name': todo.name,
                    'sync_status': 'Synchronized', 'mapping_version': str(run.mapping_revision),
                    'source_payload_hash': _digest({'recipient_id': record['target_user_id']}),
                    'target_payload_hash': _digest({'task': todo.reference_name, 'recipient': todo.allocated_to,
                        'status': todo.status, 'reference_type': todo.reference_type}),
                    'last_synced_on': frappe.utils.now_datetime()}).insert()
                return todo.name
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
    if plan.action in {'CREATE', 'CREATE_COMPLETED'} and getattr(run, 'mode', '') == 'Execute':
        todo_name = _execute_initial_task_assignment(run, record, task_name, source_instance, plan)
        record_output(run=run.name, source_type='Task', source_id=str(record['id']), result='CREATED',
                      action='task_completed_assignment_created' if plan.action == 'CREATE_COMPLETED' else 'task_assignment_created',
                      target_doctype='Task', target_name=task_name, correlation_id=todo_name,
                      detail='Completed assignment recorded.' if plan.action == 'CREATE_COMPLETED' else 'Approved native assignment created.')
        return plan.action
    detail = json.dumps({'decision': plan.action, 'proposed': plan.proposed, 'reason': plan.detail, 'recipient': plan.recipient,
                         'existing_todos': list(plan.existing), 'local_date': plan.date_rule or 'POLICY_REQUIRED'}, sort_keys=True)
    pending = (plan.action == 'PENDING_IDENTITY' or
               (plan.action == 'BLOCKED' and plan.detail.startswith(('PENDING_AUTHORITY:', 'PENDING_SOURCE_ORDER:',
                                                                       'PENDING_TASK:', 'POLICY_REQUIRED:'))))
    record_output(run=run.name, source_type='Task', source_id=str(record['id']),
                  result='PENDING_REVIEW' if pending else 'CONFLICT' if plan.action in {'BLOCKED', 'CONFLICT'} else 'SKIPPED',
                  action='task_assignment_review', detail=detail, target_doctype='Task', target_name=task_name)
    if not pending and plan.action in {'BLOCKED', 'CONFLICT'} and hasattr(run, 'conflict_records'):
        run.conflict_records = int(run.conflict_records or 0) + 1
    return plan.action


def _migrate_native_task(record, run, plan, scope) -> Result:
    """Apply the explicitly approved Task intake using the existing Link identity."""
    from types import SimpleNamespace
    from hero_integration.services.binding_registry import BindingRegistryService
    from hero_integration.services.mapping_registry_model import MappingDirection
    from hero_integration.services.native_projections import _html, _text

    frappe.only_for('System Manager')
    if getattr(plan, 'adapter_options', None):
        return _migrate_mapped_task(record, run, plan, scope)
    instance = _source_instance(record)
    if scope != {'confirmed': True, 'source_instance': instance, 'source_type': 'Task'}:
        raise MappingPlanError('Native Task intake source scope is not approved.')
    source_id = plan.source_identity(record)
    selected_paths = {definition.source_path for definition in plan.definitions}
    if record.get('is_deleted') is True:
        return Result('SKIPPED_SOURCE_DELETED', 'Source deletion does not delete or close a Task.')
    if _no_project_match(record.get('target_project_match_id')):
        # No project: an explicitly empty value that neither links, clears nor needs review.
        record = {**record, 'target_project_match_id': None}
    values = {}
    try:
        if ('title' in record and record['title'] is not None
                and (not isinstance(record['title'], str) or record['title'].strip())):
            values['subject'] = _text(record['title'], 'Task title', required=True, limit=140)
        if 'comment' in selected_paths and 'comment' in record and record['comment'] is not None and (not isinstance(record['comment'], str) or record['comment'].strip()):
            values['description'] = _html(_text(record['comment'], 'Task comment'))
        if 'target_project_match_id' in selected_paths and record.get('target_project_match_id') is not None:
            try:
                values['project'] = _project_name_for_source(record['target_project_match_id'], instance)
            except DeletedSourceProjectError as error:
                return Result('SKIPPED_SOURCE_DELETED', str(error))
        if 'due_date' in selected_paths and record.get('due_date') is not None:
            deadline = _external_task_deadline(record['due_date'])
            if deadline is not None:
                values['custom_hero_due_date'] = deadline
    except ValueError as error:
        return Result('BLOCKED_RECORD', str(error))
    deferred_project = _defer_relationships(run) and 'target_project_match_id' in selected_paths
    if deferred_project:
        values.pop('project', None)
    lifecycle = _task_lifecycle_source(record)
    if _defer_relationships(run):
        lifecycle.pop(task_lifecycle.ASSIGNEE, None)
        lifecycle.pop(task_lifecycle.AUTHOR, None)
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
                task = frappe.get_doc({'doctype': 'Task', **values, **task_lifecycle.insert_values(lifecycle)}).insert()
            source_fields = {**field_hashes(values), **field_hashes(lifecycle)}
            target_fields = {**field_hashes({field: task.get(field) for field in values}),
                             **field_hashes({field: value for field, value in task_lifecycle.target_values(task).items()
                                             if field in lifecycle})}
            # Review unwritable HERO values; empty optional values need no review.
            pending_comment = ('comment' in selected_paths and _task_value_present(record.get('comment'))
                               and 'description' not in values)
            pending_project = (not deferred_project and 'target_project_match_id' in selected_paths
                               and _task_value_present(record.get('target_project_match_id')) and 'project' not in values)
            pending_deadline = ('due_date' in selected_paths and _task_value_present(record.get('due_date'))
                                and 'custom_hero_due_date' not in values)
            frappe.get_doc({
                'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': 'Task',
                'source_id': source_id, 'source_instance': instance, 'sync_status': 'Conflict' if pending_comment or pending_project or pending_deadline else 'Synchronized',
                'conflict_detail': 'Task optional fields require review.' if pending_comment or pending_project or pending_deadline else '',
                'target_doctype': 'Task', 'target_name': task.name, 'source_payload_hash': _digest(record),
                'target_payload_hash': _digest({field: task.get(field) for field in values}),
                'source_field_hashes': encode_hashes(source_fields), 'target_field_hashes': encode_hashes(target_fields),
                'mapping_version': version, 'last_synced_on': now_datetime(),
            }).insert()
            note = task_lifecycle.sync_assignment(task, None) if lifecycle.get(task_lifecycle.ASSIGNEE) else ''
            _remember_unmatched_assignee(task, note)
        except Exception:
            frappe.db.rollback(save_point='hero_native_task_insert')
            raise
        return Result('CREATED', ' '.join(filter(None, ('Native Task and source binding created.', note))), 'Task', task.name)
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
        if source_path not in selected_paths or (field == 'project' and deferred_project):
            continue
        source_hash = field_hashes({field: values[field]})[field] if field in values else None
        target_hash = field_hashes({field: task.get(field)})[field]
        binding = SimpleNamespace(direction=MappingDirection.HERO_TO_FRAPPE, mapping_revision=int(run.mapping_revision),
                                  acknowledged_source_fingerprint=source_base.get(field),
                                  acknowledged_target_fingerprint=target_base.get(field))
        if (field == 'project' and not _defer_relationships(run) and run.mode != 'Preview'
                and field not in source_base and field not in target_base and field in values and not task.get(field)):
            # The initial import deliberately left this relationship unacknowledged.
            # Acquire it only while the target is still empty; local choices win.
            updates[field] = values[field]
            continue
        decision = BindingRegistryService.inbound_reconciliation(binding, active_mapping_revision=int(run.mapping_revision),
                                                                  source_fingerprint=source_hash, target_fingerprint=target_hash)
        if decision == 'APPLY_SOURCE': updates[field] = values[field]
        elif decision == 'CONFIRM_EQUAL':
            source_base[field], target_base[field] = source_hash, target_hash
        elif decision in {'CONFLICT', 'UNACKNOWLEDGED', 'MAPPING_CHANGED'}: conflicts.append(field)
        elif decision == 'OMITTED':
            if source_path not in record:
                omitted.append(field)  # not returned: no evidence either way
            elif field == 'subject' or _task_value_present(record[source_path]) or source_base.get(field) is not None:
                # A blank title, an unusable HERO value or a HERO clear of an
                # imported value requires review; Frappe keeps its value.
                omitted.append(field)
                pending.append(field)
            # An optional field that HERO never filled is neither a change nor a review case.
    current = task_lifecycle.target_values(task)
    lifecycle_updates = {}
    for field, value in lifecycle.items():
        source_hash = field_hashes({field: value})[field]
        target_hash = field_hashes({field: current[field]})[field]
        decision = task_lifecycle.decide(field, source_hash, target_hash, source_base.get(field),
                                         target_base.get(field), current[field])
        if decision == 'APPLY':
            lifecycle_updates[field] = value
        elif decision == 'CONFIRM':
            source_base[field], target_base[field] = source_hash, target_hash
        elif decision == 'CONFLICT':
            conflicts.append(field)
    if run.mode == 'Preview':
        return Result('CONFLICT' if conflicts or pending else 'PREVIEW_OK', 'Native Task field comparison completed.', 'Task', task.name)
    note = ''
    if updates or lifecycle_updates:
        for field, value in updates.items(): task.set(field, value)
        task_lifecycle.apply(task, lifecycle_updates)
        with managed_target_update('Task', task.name, link.name): task.save()
        for field, value in updates.items():
            source_base[field] = field_hashes({field: value})[field]
            target_base[field] = field_hashes({field: task.get(field)})[field]
        applied = task_lifecycle.target_values(task)
        for field, value in lifecycle_updates.items():
            source_base[field] = field_hashes({field: value})[field]
            target_base[field] = field_hashes({field: applied[field]})[field]
        if task_lifecycle.ASSIGNEE in lifecycle_updates:
            note = task_lifecycle.sync_assignment(task, current[task_lifecycle.ASSIGNEE])
            _remember_unmatched_assignee(task, note)
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
    return Result('CONFLICT' if conflicts or pending or link.sync_status == 'Conflict'
                  else ('UPDATED' if updates or lifecycle_updates else 'SKIPPED'),
                  link.conflict_detail or note or 'Native Task field comparison completed.', 'Task', task.name)


_TASK_USER_DIRECTORY: dict = {}


def _load_task_user_directory(source, operation) -> None:
    """Remember the run's source; the HERO user list is read once, on first need."""
    _TASK_USER_DIRECTORY.clear()
    if operation == 'HeroTasks':
        _TASK_USER_DIRECTORY['source'] = source


def _remember_unmatched_assignee(task, note: str) -> None:
    if note:
        unmatched = _TASK_USER_DIRECTORY.setdefault('unmatched', {})
        email = task.get(task_lifecycle.ASSIGNEE)
        unmatched[email] = unmatched.get(email, 0) + 1


def _task_user_emails() -> dict | None:
    """HERO user id → e-mail for this run; None when HERO cannot provide it."""
    if 'emails' not in _TASK_USER_DIRECTORY:
        reader = getattr(_TASK_USER_DIRECTORY.get('source'), 'user_emails', None)
        emails = None
        if callable(reader):
            try:
                emails = reader()
            except Exception:
                frappe.log_error(title='HERO user directory unavailable', message=frappe.get_traceback())
        _TASK_USER_DIRECTORY['emails'] = emails
    return _TASK_USER_DIRECTORY['emails']


def _task_lifecycle_source(record) -> dict:
    from datetime import date
    try:
        from frappe.utils import getdate
        today = getdate()
    except Exception:  # offline runner without System Settings
        today = date.today()
    # Never guess an assignee without the Task field or without the HERO user list.
    wants_assignee = 'target_user_id' in record and _task_has_assignee_field()
    wants_author = 'author_user_id' in record and _task_has_field(task_lifecycle.AUTHOR)
    emails = _task_user_emails() if wants_assignee or wants_author else None
    def user_for(hero_user_id, email):
        # The HERO user ID stored by the people sync identifies the person; the e-mail is the fallback.
        from hero_integration.integrations.hero_people import user_by_hero_user
        return user_by_hero_user(hero_user_id, source_instance=record.get('_hero_source_instance')) or (task_lifecycle._user_for(email, frappe) if email else None)
    user_for = user_for if wants_author and emails is not None else None
    values = task_lifecycle.source_values(record, _external_task_deadline, emails or {}, today, user_for=user_for)
    if emails is None or not wants_assignee:
        values.pop(task_lifecycle.ASSIGNEE, None)
    return values


def _task_has_field(fieldname) -> bool:
    try:
        return bool(frappe.get_meta('Task').has_field(fieldname))
    except Exception:
        return False


def _task_has_assignee_field() -> bool:
    try:
        return bool(frappe.get_meta('Task').has_field(task_lifecycle.ASSIGNEE))
    except Exception:
        return False


def _task_value_present(value) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


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
        if not getattr(plan, 'adapter_options', None):
            validate_projection_record(operation, record, allow_partial=bool(existing))
        if getattr(plan, 'adapter_options', None) and contract.get('native_kind') == 'event' and any(type(record.get(flag)) is not bool for flag in ('deleted', 'is_recurring')):
            raise MappingPlanError('Mapped Event lifecycle flags must be explicitly returned as booleans.')
        if record.get('is_deleted') is True or record.get('deleted') is True:
            if run.mode != 'Preview':
                _mark_source_deleted(_link(contract['source_type'], source_id, instance))
            return Result('SKIPPED_SOURCE_DELETED', 'Source deletion is observed; no target deletion occurs.')
        # Hold Events with changed privacy/outbound settings before resolving native
        # options, including Preview calls without a complete run context.
        held = _review_hold(existing)
        if held:
            return held
        target = None
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
            native_record = record
            if getattr(plan, 'adapter_options', None):
                from hero_integration.services.query_mapping import event_record
                native_record = event_record(plan, record)
            native_values = native_resolve(contract, native_record, source_id, instance, options, lookup=_native_lookup,
                                          allow_partial=bool(existing), review=review)
            if contract.get('native_kind') == 'event' and options['target_timezone'] != frappe.db.get_single_value('System Settings', 'time_zone'):
                raise MappingPlanError('Configured event target timezone differs from the native System Settings timezone.')
            defaults = creation_defaults(contract, options)
            if target is not None and target.get('custom_hero_is_recurring') and record.get('is_recurring') is True:
                # An approved recurring event keeps its reviewed times silently.
                review = [reason for reason in review if not reason.startswith('Recurring HERO event')]
        values = plan.values(record, project_link=lambda value: _project_name_or_deferred(value, instance, plan, record),
                             native_value=lambda target: native_values[target], allow_partial=bool(existing))
        _defer_reference_values(plan, values, run)
        values['custom_hero_key'] = _digest([instance, contract['source_type'], source_id])
        values['custom_hero_source_instance'] = instance
    except DeletedSourceProjectError as error:
        return Result('SKIPPED_SOURCE_DELETED', str(error))
    except (ValueError, MappingPlanError) as error:
        return Result('BLOCKED_RECORD', str(error))
    result = _upsert(contract['source_type'], source_id, record, values, contract['target_doctype'],
                   'item_code' if contract['target_doctype'] == 'Item' else 'custom_hero_key', plan.mapping_version, run, source_instance=instance,
                   target_snapshot=lambda target: _projection_target_values(target, values, contract), creation_values=defaults,
                   preserve_omitted=contract.get('native_kind') == 'event',
                   review_reason=' '.join(review) if review and not existing else None)
    if review and existing and result.code in {'UPDATED', 'SKIPPED', 'OBSERVED', 'PREVIEW_OK'}:
        detail = 'Event time or recurrence requires review; unrelated field changes were considered. ' + review[0]
        if run.mode != 'Preview':
            _mark_link_conflict(frappe.get_doc('HERO Integration Link', existing.name), detail)
        return Result('CONFLICT', detail, result.target_doctype, result.target_name)
    return result


def _native_lookup(doctype, name):
    if not isinstance(name, str) or not name or not frappe.db.exists(doctype, name):
        raise MappingPlanError('Configured native reference does not exist: {0}', doctype)
    document = frappe.get_doc(doctype, name)
    frappe.has_permission(doctype, 'read', doc=document, throw=True)
    return document


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


class DeletedSourceProjectError(MappingPlanError):
    """The referenced HERO project is deleted; the referencing record is not imported."""


# Run-scoped classification of ProjectMatch IDs the HERO project list omits:
# {(source_instance, id): "DELETED" | "LEAD" | "UNLISTED"}.
_PROJECT_REFERENCE_STATES: dict[tuple[str, int], str] = {}


def _prefetch_project_references(source, operation: str, records) -> None:
    """Classify unbound project references of one page with one bundled HERO read."""
    if getattr(source, "query_adapter", None):
        return  # mapped imports never issue supplemental literal project queries
    path = PROJECTIONS.get(operation, {}).get('project_path')
    probe = getattr(source, 'project_reference_states', None)
    if not path or probe is None:
        return
    instance = getattr(source, 'source_instance', '') or 'default'
    unbound = {
        record.get(path) for record in records
        if type(record.get(path)) is int and record.get(path) > 0
        and (instance, record.get(path)) not in _PROJECT_REFERENCE_STATES
        and not _link('project', str(record.get(path)), instance)
    }
    if unbound:
        for value, state in probe(unbound).items():
            _PROJECT_REFERENCE_STATES[(instance, value)] = state


def _no_project_match(value) -> bool:
    """HERO uses ProjectMatch ID 0 for records that belong to no project."""
    return type(value) is int and value == 0


def _project_name_for_source(value, source_instance):
    if _no_project_match(value):
        return None  # same as a NULL reference: no new link, an existing one stays
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MappingPlanError('ProjectMatch reference must be a positive integer.')
    link = _link('project', str(value), source_instance)
    if getattr(link, 'sync_status', '') == 'Source Deleted':
        raise DeletedSourceProjectError('The referenced HERO project is deleted; the record is not imported.')
    if getattr(link, 'sync_status', '') in {REVIEW_PENDING, REVIEW_REJECTED}:
        raise MappingPlanError('ProjectMatch reference awaits or failed administrator review; linked records wait.')
    if not link:
        # Skip records of deleted HERO projects; KONT- contact requests have no project.
        state = _PROJECT_REFERENCE_STATES.get((source_instance or 'default', value))
        if state == 'DELETED':
            raise DeletedSourceProjectError('The referenced HERO project is deleted; the record is not imported.')
        if state == 'LEAD':
            return None
    if not link or link.target_doctype != 'Project' or not frappe.db.exists('Project', link.target_name):
        raise MappingPlanError('ProjectMatch reference has no verified native Project binding.')
    frappe.has_permission('Project', 'read', doc=frappe.get_doc('Project', link.target_name), throw=True)
    return link.target_name


def _project_name_or_deferred(value, source_instance, plan, record):
    """Allow an optional absent Project while recording its exact dependency."""
    try:
        return _project_name_for_source(value, source_instance)
    except DeletedSourceProjectError:
        raise
    except MappingPlanError:
        from hero_integration.services.deferred_references import installed, references
        if (type(value) is int and value > 0 and not _link('project', str(value), source_instance)
                and installed(frappe) and ('project', str(value)) in references(plan, record)):
            return None
        raise


def _project_assignments_after_import(record,run,result,source_id,source_instance):
    field_conflict = result.code == 'CONFLICT' and (result.detail or '').startswith('Field conflict requires review:')
    from hero_integration.services.deferred_references import realizing
    if (realizing() and run.mode != 'Preview' and result.target_name and 'project_match_assignments' in record
            and (result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED'} or field_conflict)):
        from hero_integration.integrations.project_assignments import sync as sync_assignees
        from hero_integration.services.employee_migration import historical_users
        historical = historical_users(source_instance)
        kwargs = {'allow_removals': False} if field_conflict else {}
        if historical:
            kwargs['historical_user_ids'] = historical
        assignees = sync_assignees(result.target_name, record, **kwargs)
        if assignees.get('historical'):
            record_output(run=run.name, source_type='project', source_id=str(source_id), result='SKIPPED',
                          action='project_historical_reference', target_doctype='Project', target_name=result.target_name,
                          detail=frappe._('{0} deleted HERO users remain as historical references; existing assignments were preserved.').format(assignees['historical']))
        if assignees["unmatched"]:
            detail = frappe._(
                '{0} project assignee(s) could not be resolved to an active, source-bound ERPNext user; existing assignments were preserved and require review.'
            ).format(assignees['unmatched'])
            record_output(run=run.name, source_type='project', source_id=str(source_id), result='PENDING_REVIEW',
                          action='project_assignment_review', target_doctype='Project',
                          target_name=result.target_name, detail=detail)
            from dataclasses import replace
            result = replace(result, pending_relation=True)
    return result


def _migrate_project(record: dict[str, object], run) -> Result:
    if not _target_available("Project"):
        return Result("UNAVAILABLE", "Project is not installed on this site.")
    try:
        plan = _mapping_plan_for_run(run, "HeroProjectMatches")
        source_id = plan.source_identity(record)
        source_instance = _source_instance(record)
        existing = _link("project", source_id, source_instance)
        held = _review_hold(existing)
        if held:
            return held
        review, keep = [], set()

        def customer_for(value):
            try:
                return _customer_name_for_source(value, source_instance)
            except UnlinkedCustomerError:
                if existing:
                    keep.add("customer")
                    return None
                # A genuinely absent source identity is a deferred relationship,
                # not an administrator rejection. Only use this path when the
                # existing queue can capture this exact mapped dependency.
                from hero_integration.services.deferred_references import installed, references
                if (type(value) is int and value > 0 and not _link("customer", str(value), source_instance)
                        and installed(frappe) and ("customer", str(value)) in references(plan, record)):
                    return None
                review.append(_unlinked_customer_reason(value))
                return None

        def title_for(source):
            try:
                return _project_title_for_record(source)
            except ProjectAddressError as error:
                if existing:
                    keep.add("project_name")
                    return "-"
                review.append(f"The project address is missing or incomplete ({error}); the title omits it.")
                return _project_title_for_record(source, include_address=False)

        values = plan.values(record, customer_for, _confirmed_company_name, title_for)
        deferred_fields = _defer_reference_values(plan, values, run)
        # An existing Project keeps its current customer/title when HERO no
        # longer supplies a usable value; nothing is cleared or renamed.
        for field in keep:
            values.pop(field, None)
            values.missing.add(field)
        values["custom_hero_source_id"] = f"{source_instance}:{source_id}"
        document_name, name_note = _document_name(project_reference, record, "project display_id")
    except (MappingPlanError, ProjectTitleValidationError) as error:
        return Result("BLOCKED_RECORD", str(error))
    title_basis = _hold_customer_only_title_change(record, values, existing)
    if not values.get("project_name") and not ("project_name" in values.missing and existing):
        return Result("BLOCKED_RECORD", "Configured project record name is empty.")
    drift = _business_number_drift(
        "project", source_id, source_instance, "Project", "custom_hero_display_id",
        record.get("display_id"), "project display_id", run, observed="display_id" in record,
    )
    if drift:
        return drift
    result = _upsert(
        "project", source_id, record, values, "Project", "project_name", plan.mapping_version, run,
        source_instance=source_instance, required_on_create=tuple(field for field in ("customer", "project_name") if field not in deferred_fields),
        comparison_extra=title_basis, document_name=document_name,
        review_reason=" ".join(review) or None,
        creation_values=None if getattr(plan, 'adapter_options', None) else _project_creation_values(record, values.get("company")),
    )
    result = _project_assignments_after_import(record,run,result,source_id,source_instance)
    if (getattr(plan, 'adapter_options', None)
            and (result.target_name or run.mode == 'Preview')
            and result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'PREVIEW_OK'}):
        from hero_integration.services.project_logbook import sync_mapped_entries
        logbook_conflicts = []
        sync_mapped_entries(plan, record, result.target_name or '', preview=run.mode == 'Preview', conflicts=logbook_conflicts)
        if logbook_conflicts:
            from dataclasses import replace
            result = replace(result, code='CONFLICT', detail=(result.detail or '') + ' Project logbook changes require review.')
    result = _sync_mapped_relations(plan, record, result, run)
    if name_note and _creates(result):
        from dataclasses import replace
        result = replace(result, detail=(result.detail or "") + name_note)
    return result


def _sync_mapped_relations(plan, record, result, run):
    """Preview declared relations or realize them inside the post-success worker."""
    if not getattr(plan, 'adapter_options', None) or not plan.adapter_options.get('native_relations'):
        return result
    from hero_integration.services.deferred_references import realizing
    if run.mode != 'Preview' and not realizing():
        return result
    if result.code not in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'PREVIEW_OK'}:
        return result
    from dataclasses import replace
    from hero_integration.services.mapped_relations import IncompleteProjectAddress, sync
    from hero_integration.services.mapped_collection_relations import IncompleteCustomerAddress, UnboundCustomerAddress
    try:
        counts = sync(plan, record, plan.target_doctype, result.target_name or '', preview=run.mode == 'Preview')
    except (UnboundCustomerAddress, IncompleteCustomerAddress, IncompleteProjectAddress) as error:
        if run.mode != 'Preview' and result.target_name:
            project = isinstance(error, IncompleteProjectAddress)
            record_output(run=run.name, source_type='project' if project else 'customer', source_id=str(plan.source_identity(record)),
                result='PENDING_REVIEW', action='project_address_review' if project else 'customer_address_review', target_doctype='Project' if project else 'Customer',
                target_name=result.target_name, detail=str(error))
        return replace(result, pending_relation=True)
    except ValueError as error:
        return replace(result, code='CONFLICT', detail=(result.detail or '') + ' ' + str(error))
    if counts.get('conflicts'):
        return replace(result, code='CONFLICT', detail=(result.detail or '') + ' Native relationship changes require review.')
    return result


def _document_name(name_for, record, label: str, *, internal_prefix=None) -> tuple[str | None, str]:
    """Use the exact HERO number, or an explicitly reported technical fallback."""
    try:
        name = name_for(record)
        if name is not None:
            return name, ""
        reason = f"HERO has no {label}."
    except BusinessNumberError as error:
        reason = str(error)
    if internal_prefix:
        return f"{internal_prefix}-{uuid4().hex}", (
            f" {reason} A collision-safe internal ID names the document; no replacement business number "
            "was generated. The original HERO value stays in its field."
        )
    return None, f" {reason} ERPNext's naming series names the new document; the HERO value stays in its field."


def _creates(result: Result) -> bool:
    return result.code == "CREATED" or (result.code == "PREVIEW_OK" and not result.target_name)


def _business_number_drift(
    source_type, source_id, source_instance, doctype, field, incoming, label, run, *, observed=True,
) -> Result | None:
    """A linked record keeps its HERO number until someone reviews a change made in HERO."""
    if not observed:
        return None
    if isinstance(incoming, int) and not isinstance(incoming, bool):
        incoming = str(incoming)
    link = _link(source_type, source_id, source_instance)
    if not link or link.target_doctype != doctype or not frappe.db.exists(doctype, link.target_name):
        return None
    stored = frappe.db.get_value(doctype, link.target_name, field)
    if not stored or stored == incoming:
        return None
    detail = (
        f"HERO {label} changed from {stored!r} to {incoming!r}. {doctype} {link.target_name} keeps its name "
        f"and number until reviewed; set {field} to {incoming!r} to accept the new number."
    )
    if run.mode != "Preview":
        _mark_link_conflict(link, detail)
    return Result("CONFLICT", detail, doctype, link.target_name)


def _name_taken(doctype: str, name: str) -> str:
    return (
        f"{doctype} {name!r} already exists without this HERO record's link. The HERO number is not reassigned, "
        f"suffixed or merged; check whether {name!r} is the same {doctype.lower()} and link or rename it."
    )


PROJECT_TITLE_BASIS = "hero_project_title_basis"


def _project_creation_values(record, company) -> dict | None:
    """A new Project gets the department and the Project Type (Gewerk) of its HERO trade."""
    from hero_integration.integrations.company_structure import department_for_trade
    from hero_integration.services.project_naming import project_trade

    try:
        trade_code = project_trade(record)[0]
    except ProjectTitleValidationError:
        return None
    from hero_integration.integrations.project_form import project_type_for_trade

    values = {"department": department_for_trade(trade_code, company), "project_type": project_type_for_trade(trade_code)}
    values = {field: value for field, value in values.items() if value}
    return values or None


def _hold_customer_only_title_change(record, values, link) -> dict | None:
    """Keep Project titles when only the HERO customer name or salutation changed.

    Trade, number and address changes still use field-wise conflict rules.
    Track the customer-free title as comparison evidence; older baselines without
    it preserve the title while recording that basis.
    """
    if not values.get("project_name"):
        return None
    # Preserve the previously persisted neutral basis when the visible title
    # format changes, so existing titles are not treated as changed trade data.
    neutral = {**record, "customer": {"title": "", "full_name": "- -"}}
    try:
        basis = {PROJECT_TITLE_BASIS: build_project_title(neutral, max_length=10_000)}
    except ProjectTitleValidationError:
        return None
    prior = decode_hashes(getattr(link, "source_field_hashes", "")) if link else None
    if not prior or prior.get("project_name") is None:
        return basis
    title_changed = prior["project_name"] != field_hashes({"project_name": values["project_name"]})["project_name"]
    prior_basis = prior.get(PROJECT_TITLE_BASIS)
    # Only regenerate an old-format title (with salutation) when the stored
    # source baseline exactly matches this record's previous title and the
    # title was not edited in ERPNext; a local title is kept without review.
    if title_changed and prior_basis == field_hashes(basis)[PROJECT_TITLE_BASIS]:
        try:
            legacy = build_project_title(record, max_length=10_000, _legacy_salutation=True)
            if (prior["project_name"] == field_hashes({"project_name": legacy})["project_name"]
                    and not _project_title_edited_locally(link)):
                return basis
        except ProjectTitleValidationError:
            pass
    if title_changed and (prior_basis is None or prior_basis == field_hashes(basis)[PROJECT_TITLE_BASIS]):
        del values["project_name"]
        values.missing.add("project_name")
    return basis


def _project_title_edited_locally(link) -> bool:
    prior = decode_hashes(getattr(link, "target_field_hashes", "")) or {}
    if prior.get("project_name") is None or not getattr(link, "target_name", ""):
        return True
    current = frappe.db.get_value("Project", link.target_name, "project_name")
    return prior["project_name"] != field_hashes({"project_name": current})["project_name"]


def observe_monitor_record(operation: str, record: dict[str, object]) -> Result | None:
    """Compare a source record through the active mapping without target writes.

    MONITOR uses the materializer's three-way comparison but never advances
    baselines. Same-field changes on both sides use durable conflicts and
    administrator notifications; other changes remain technical output.
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
            extra = _hold_customer_only_title_change(record, values, _link("project", source_id, _source_instance(record)))
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
    if operation == 'HeroProjectMatches':
        outcome = _reconcile_project_notes_representation(outcome, record, values, target_values)
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


@measured('migration.target_upsert')
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
    preserve_unselected: bool = False,
    comparison_extra: dict | None = None,
    required_on_create: tuple[str, ...] = (),
    document_name: str | None = None,
    review_reason: str | None = None,
) -> Result:
    if not _target_available(doctype):
        return Result("UNAVAILABLE", f"{doctype} is not installed on this site.")
    source_hash = _digest(record)
    existing = _link(source_type, source_id, source_instance)
    if preserve_unselected:
        from hero_integration.services.deferred_references import realizing
        if not realizing() or run.mode=='Preview' or not existing:
            raise MappingPlanError('Relationship-only updates require their imported target and realization service.')
    held = _review_hold(existing)
    if held:
        return held
    if existing:
        if existing.target_doctype != doctype:
            return Result('CONFLICT', 'Stored binding has an incompatible target DocType.')
        if not frappe.db.exists(existing.target_doctype, existing.target_name):
            if run.mode != "Preview":
                _mark_link_conflict(existing, "Stored target reference no longer exists.")
            return Result("CONFLICT", "Stored target reference no longer exists.")
        target = frappe.get_doc(existing.target_doctype, existing.target_name)
        snapshot = target_snapshot or (lambda item: _target_values(item, values))
        prior_source = decode_hashes(getattr(existing, "source_field_hashes", ""))
        prior_target = decode_hashes(getattr(existing, "target_field_hashes", ""))
        rebind_event = preserve_omitted and existing.mapping_version != mapping_version
        if rebind_event:
            # An approved mapping may have been republished without changing
            # this Event. Rebind only when every previous target field and every
            # currently supplied previously-owned target field is unchanged.
            # New fields may be acquired only when empty or already equal.
            comparison = _Comparison(values, comparison_extra)
            if prior_source is not None and prior_target is not None:
                comparison.omit(set(prior_source) | set(prior_target))
                current_target = field_hashes(comparison.target_values(target, snapshot))
                common = set(comparison.source_hashes) & set(prior_target)
                safe = (bool(comparison.source_hashes) and set(prior_source) == set(prior_target)
                        and all(current_target.get(field) == prior_target[field] for field in common)
                        and not new_mapped_field_conflicts(target, values, set(prior_source) | set(prior_target)))
            else:
                safe = False
            if not safe:
                return Result('CONFLICT', 'Event binding or approved mapping version requires review.', doctype, existing.target_name)
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
        if doctype == 'Task':
            prior_target = _task_midnight_baseline(prior_source, prior_target, target_values)
        target_field_hashes = field_hashes(target_values)
        if preserve_omitted or preserve_unselected:
            # Events omit unconfirmed values entirely: their acknowledged
            # baselines are carried forward instead of being compared.
            comparison.omit(set(prior_source) | set(prior_target))
        outcome = comparison.reconcile(prior_source, prior_target, target_values)
        if source_type == 'project' and doctype == 'Project':
            outcome = _reconcile_project_notes_representation(outcome, record, values, target_values)
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
        if rebind_event:
            existing.mapping_version = mapping_version
            _save_link(existing)
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
    # A new document takes the HERO business number as its name; a name already
    # in use is never suffixed, reassigned or merged.
    if document_name and frappe.db.exists(doctype, document_name):
        return Result("CONFLICT", _name_taken(doctype, document_name), doctype)
    if run.mode == "Preview":
        named = f" named {document_name!r}" if document_name else ""
        return Result("PREVIEW_OK", f"A new target record would be created{named}.", doctype)
    # A target, its required relation(s), and the provenance link are one
    # integration unit.  A handled relation conflict must not leave the newly
    # inserted ERPNext document behind for a later run to mistake as manual.
    savepoint = "hero_upsert_relation"
    frappe.db.savepoint(savepoint)
    try:
        target = frappe.get_doc({"doctype": doctype, **(creation_values or {}), **values})
        from hero_integration.services.guards import hero_project_naming

        # The target has no name/link yet; the guarded insert still owns its
        # inbound fields and must not be mistaken for a local edit.
        with hero_project_naming(), managed_target_update(doctype, None, ''):
            if document_name:
                target.insert(set_name=document_name)
            else:
                target.insert()
        if after_insert:
            after_insert(target)
        snapshot = target_snapshot or (lambda item: _target_values(item, values))
        comparison = _Comparison(values, comparison_extra)
        target_values = comparison.target_values(target, snapshot)
        target_hash = _digest(target_values)
        frappe.get_doc({
            "doctype": "HERO Integration Link", "source_system": "HERO", "source_type": source_type,
            "source_id": source_id, "source_instance": source_instance,
            "sync_status": REVIEW_PENDING if review_reason else "Synchronized",
            "conflict_detail": review_reason or "", "last_synced_on": now_datetime(),
            "target_doctype": doctype, "target_name": target.name, "source_payload_hash": source_hash,
            "target_payload_hash": target_hash, "source_field_hashes": encode_hashes(comparison.source_hashes),
            "target_field_hashes": encode_hashes(field_hashes(target_values)), "mapping_version": mapping_version,
        }).insert()
    except (HomePhoneConflict, CustomerAddressError) as error:
        frappe.db.rollback(save_point=savepoint)
        return Result("CONFLICT", str(error), doctype)
    except getattr(frappe, "UniqueValidationError", ()) as error:
        # A competing worker may have claimed the unique source_key after our
        # initial lookup. Roll back the target and all relations with the claim.
        if not error.args or error.args[0] != "HERO Integration Link":
            raise
        frappe.db.rollback(save_point=savepoint)
        if hasattr(frappe, "clear_messages"):
            frappe.clear_messages()
        return Result(
            "CONFLICT", "HERO source identity was claimed concurrently; no new target or relations were kept. "
            "Retry the same source identity to reuse its established link.", doctype,
        )
    except frappe.DuplicateEntryError as error:
        # Another import committed this name after the existence check above.
        if not document_name or tuple(error.args[:2]) != (doctype, document_name):
            raise
        frappe.db.rollback(save_point=savepoint)
        if hasattr(frappe, "clear_messages"):
            frappe.clear_messages()
        return Result("CONFLICT", _name_taken(doctype, document_name), doctype)
    if review_reason:
        notify_review(_link(source_type, source_id, source_instance), review_reason)
        return Result("PENDING_REVIEW", review_reason + " Linked records wait for an administrator decision.",
                      doctype, target.name)
    return Result("CREATED", "Target created through native Frappe validation.", doctype, target.name)


REVIEW_PENDING, REVIEW_REJECTED = "Pending Review", "Rejected"


def _review_hold(link) -> Result | None:
    """A record awaiting or refused by an administrator is never written by a run."""
    status = getattr(link, "sync_status", "") if link else ""
    if status == REVIEW_PENDING:
        return Result("PENDING_REVIEW", "Awaits administrator approval: " + (link.conflict_detail or ""),
                      link.target_doctype, link.target_name)
    if status == REVIEW_REJECTED:
        return Result("SKIPPED_REJECTED", "An administrator rejected this HERO record; it is not imported.",
                      link.target_doctype)
    return None


def _references_besides_hero(doctype: str, name: str, ignore=()) -> str | None:
    """Name the first static or dynamic reference other than the HERO binding itself.

    ``ignore`` names further referencing DocTypes that go away with the target.
    """
    skipped = {"HERO Integration Link", *ignore}
    from frappe.model.delete_doc import get_dynamic_linked_docs
    from frappe.desk.form.linked_with import get_linked_docs, get_linked_doctypes

    document = frappe.get_doc(doctype, name)
    for reference in get_dynamic_linked_docs(document):
        if reference["reference_doctype"] not in skipped:
            return f"{reference['reference_doctype']} {reference['reference_docname']}"
    linked = get_linked_docs(doctype, name, linkinfo=get_linked_doctypes(doctype)) or {}
    for reference_doctype, found in linked.items():
        # Frappe v16: {"docs": [...permitted rows], "hidden_count": n}
        docs, hidden = found.get("docs") or [], int(found.get("hidden_count") or 0)
        if reference_doctype not in skipped and (docs or hidden):
            return f"{reference_doctype} {docs[0].get('name') if docs else '(not permitted)'}"
    return None


def decide_review(link, decision: str) -> None:
    """Approve releases a held record; reject retains it. Editing is not supported.
    """
    if getattr(link, "sync_status", "") == "Conflict":
        if decision == "approve":
            prefix = "Recheck requested: "
            if not (link.conflict_detail or "").startswith(prefix):
                link.conflict_detail = prefix + (link.conflict_detail or "Conflict requires review.")
        elif decision == "reject":
            link.sync_status = REVIEW_REJECTED
            link.conflict_detail = "Rejected by an administrator; the existing record is retained and HERO changes are not imported."
        else:
            raise MappingPlanError("Review decision must be approve or reject.")
        _save_link(link)
        return
    if getattr(link, "sync_status", "") != REVIEW_PENDING:
        raise MappingPlanError("Only an open conflict or a record that awaits review can be approved or rejected.")
    if decision == "approve":
        link.sync_status, link.conflict_detail = "Synchronized", ""
    elif decision == "reject":
        link.sync_status = REVIEW_REJECTED
        link.conflict_detail = "Rejected by an administrator; the existing record is retained and HERO changes are not imported."
    else:
        raise MappingPlanError("Review decision must be approve or reject.")
    _save_link(link)


def _record_result(run, source_type: str, record: dict[str, object], result: Result) -> None:
    from hero_integration.services.deferred_references import remember
    remember(run, source_type, record, result)
    source_id = str(record.get("id")) if isinstance(record.get("id"), int) else "unknown"
    record_output(
        run=run.name, source_type=source_type, source_id=source_id, result=result.code,
        action="preview" if run.mode == "Preview" else "hero_to_frappe",
        detail=result.detail, target_doctype=result.target_doctype, target_name=result.target_name,
    )
    run.processed = int(run.processed or 0) + 1
    if result.code == "CREATED":
        run.created_records = int(run.created_records or 0) + 1
    elif result.code in {"CONFLICT", "SKIPPED_AMBIGUOUS", "BLOCKED_RECORD", "UNAVAILABLE", "PENDING_REVIEW"}:
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
    # Keep Customer-field baselines for older rows or unreadable Contact links;
    # a missing native value does not prove a local edit to None.
    snapshot.update({
        field: value
        for field, value in primary_contact_values(target, set(contact_fields)).items()
        if value is not None
    })
    # ERPNext v16 fills these Customer fields from the primary Contact
    # (fetch_from) and stores an empty projection as ''; HERO's empty value
    # is null.  Both mean empty and are no local change.
    for field in set(contact_fields) | {"first_name", "last_name", "email_id", "mobile_no"}:
        if snapshot.get(field, target.get(field)) == "":
            snapshot[field] = None
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


def _unlinked_customer_reason(value) -> str:
    """Say why the Project has no customer, so the administrator can decide."""
    reason = "The HERO customer has no unambiguous ERPNext link; the Project has no customer."
    try:
        rows = frappe.db.sql("""SELECT result FROM `tabHERO Integration Record` WHERE source_type='customer'
                                AND source_id=%s ORDER BY creation DESC LIMIT 1""", (str(value),))
    except Exception:
        return reason
    hints = {"SKIPPED_SOURCE_DELETED": "The HERO customer is deleted in HERO",
             "SKIPPED_CONTACT_PERSON": "The HERO customer is a contact person, not a customer"}
    hint = hints.get(rows[0][0]) if rows else None
    return f"{hint}; the Project has no customer." if hint else reason


class UnlinkedCustomerError(MappingPlanError):
    """The HERO customer has no unambiguous ERPNext Customer link."""


def _customer_name_for_source(value: object, source_instance: str = "default") -> str:
    if type(value) is not int or value <= 0:
        raise MappingPlanError("The configured HERO customer relation requires a positive integer identity.")
    source_id = str(value)
    customer_link = _link("customer", source_id, source_instance)
    if (not customer_link or customer_link.target_doctype != "Customer"
            or getattr(customer_link, "sync_status", "") in {REVIEW_PENDING, REVIEW_REJECTED, "Source Deleted"}
            or not frappe.db.exists("Customer", customer_link.target_name)):
        raise UnlinkedCustomerError("The configured HERO customer relation has no unambiguous ERPNext link.")
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
        from hero_integration.services.primary_address import fill_primary_address

        fill_primary_address(customer.name, source_instance)


def _synchronize_customer_address(customer, address: dict[str, object], source_instance: str, mapping_version: str) -> None:
    if not _target_available("Address"):
        raise CustomerAddressError("Address is not installed on this site.")
    source_id = str(address["id"])
    code = address.get('country_code')
    configured_country = address.get('country_name')
    if configured_country:
        country = _native_lookup('Country', configured_country).name
    else:
        if code is not None and not isinstance(code, str):
            raise CustomerAddressError('HERO country code must be text when supplied.')
        code = code.strip().lower() if isinstance(code, str) and code.strip() else DEFAULT_ADDRESS_COUNTRY
        countries = frappe.get_list('Country', filters={'code': code}, pluck='name', limit_page_length=2)
        if len(countries) != 1:
            raise CustomerAddressError('HERO country code has no unique readable native Country binding.')
        country = countries[0]
    values = {
        'country': country, 'address_type': 'Billing',
        "address_title": customer.name,
        "address_line1": address["street"],
        "pincode": address["zipcode"],
        "city": address["city"],
    }
    if isinstance(address.get("line_2"), str) and address["line_2"].strip():
        values["address_line2"] = address["line_2"].strip()
    if "mapped_values" in address:
        values = {**address["mapped_values"], "country": country}
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
        if "mapped_values" in address:
            _require_exclusive_customer_address(target, customer.name)
            prior_source = decode_hashes(getattr(existing, "source_field_hashes", ""))
            prior_target = decode_hashes(getattr(existing, "target_field_hashes", ""))
            if prior_source is None or prior_target is None:
                raise CustomerAddressError("Mapped Address requires a verified field baseline.")
            comparison = _Comparison(values)
            current = comparison.target_values(target, lambda doc: _target_values(doc, values))
            outcome = comparison.reconcile(prior_source, prior_target, current)
            if outcome.conflicts:
                _mark_link_conflict(existing, "Mapped Address fields require reconciliation.")
                raise CustomerAddressError("Mapped Address fields require reconciliation.")
            if outcome.apply:
                for field in outcome.apply:
                    if field in values: target.set(field, values[field])
                with managed_target_update('Address', target.name, existing.name): target.save()
            if outcome.advances_baseline:
                current = comparison.target_values(target, lambda doc: _target_values(doc, values))
                _update_link_baseline(existing, _digest(payload), _digest(current),
                    next_baseline(prior_source, comparison.source_hashes, outcome, tracked=comparison.tracked),
                    next_baseline(prior_target, field_hashes(current), outcome, tracked=comparison.tracked), mapping_version)
            return
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


def _project_title_for_record(record: dict[str, object], include_address: bool = True) -> str:
    """Resolve the ERPNext field limit before constructing a deterministic title."""
    field = frappe.get_meta("Project").get_field("project_name")
    limit = getattr(field, "length", None)
    # Frappe Data fields use 140 when no site-specific explicit length is set.
    max_length = int(limit) if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else 140
    return build_project_title(record, max_length=max_length, include_address=include_address)


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


def _migrate_mapped_task(record, run, plan, scope) -> Result:
    """Reuse field-wise upsert and the existing guarded assignment boundary."""
    from hero_integration.services.query_mapping import mapped_values
    instance = _source_instance(record)
    if scope != {'confirmed': True, 'source_instance': instance, 'source_type': 'Task'}:
        raise MappingPlanError('Native Task intake source scope is not approved.')
    source_id = plan.source_identity(record)
    if type(record.get('is_deleted')) is not bool:
        return Result('BLOCKED_RECORD', 'Mapped Task deletion state must be explicitly returned as a boolean.')
    if record.get('is_deleted') is True:
        return Result('SKIPPED_SOURCE_DELETED', 'Source deletion does not delete or close a Task.')
    try:
        values = mapped_values(plan, record,
            project_link=lambda value: _project_name_or_deferred(value, instance, plan, record))
        _defer_reference_values(plan, values, run)
        if values.get('completed_on'):
            values['status'] = 'Completed'
    except (ValueError, MappingPlanError) as error:
        return Result('BLOCKED_RECORD', str(error))
    if not values.get('subject'):
        return Result('BLOCKED_RECORD', 'A mapped Task title is required for intake.')
    result = _upsert('Task', source_id, record, values, 'Task', 'subject', plan.mapping_version, run,
        source_instance=instance, required_on_create=('subject',))
    return _mapped_task_assignment(record,run,plan,result)


def _mapped_task_assignment(record,run,plan,result):
    from hero_integration.services.query_mapping import mapped_values
    instance = _source_instance(record)
    source_id = plan.source_identity(record)
    from hero_integration.services.direct_mapping import MISSING, lookup
    assignment_rule = next((spec for spec in plan.definitions if spec.target_doctype == 'ToDo'
                            and spec.target_field == 'allocated_to'), None)
    raw_assignee = lookup(record, assignment_rule.source_path) if assignment_rule else MISSING
    assignment_error = None
    try:
        assignment = {} if type(raw_assignee) is int and raw_assignee == 0 else mapped_values(plan, record, target_doctype='ToDo')
    except (ValueError, MappingPlanError) as error:
        # A bad or unresolved assignee is not evidence that the Task itself
        # is invalid. Keep any local assignment and report the relation apart.
        assignment, assignment_error = {}, str(error)
    if result.code not in {'CREATED','UPDATED','SKIPPED','CONVERGED','OBSERVED','PREVIEW_OK'}:
        return result
    from hero_integration.services.employee_migration import historical_users
    if (type(raw_assignee) is int and str(raw_assignee) in historical_users(instance)
            and result.target_name and result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED'}):
        if run.mode != 'Preview':
            record_output(run=run.name, source_type='Task', source_id=str(source_id), result='SKIPPED',
                action='task_historical_reference', target_doctype='Task', target_name=result.target_name,
                detail=frappe._('A deleted HERO user remains as a historical reference; existing assignments were preserved.'))
        return result
    if assignment_error and result.target_name and run.mode != 'Preview':
        record_output(run=run.name, source_type='Task', source_id=str(source_id), result='PENDING_REVIEW',
            action='task_assignment_review', target_doctype='Task', target_name=result.target_name,
            detail='Task was imported; its assignee requires review: ' + assignment_error)
        from dataclasses import replace
        result = replace(result, pending_relation=True)
    if _defer_relationships(run):
        return result
    recipient = assignment.get('allocated_to')
    if recipient and result.target_name and result.code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'PREVIEW_OK'}:
        assignment_rule = next(spec for spec in plan.definitions if spec.target_doctype == 'ToDo')
        from hero_integration.services.direct_mapping import lookup
        assignment_record = {**record, 'target_user_id': lookup(record, assignment_rule.source_path)}
        admitted = _plan_task_assignment(run, assignment_record, result.target_name, instance)
        if admitted.recipient and admitted.recipient != recipient:
            return Result('CONFLICT', 'Mapped assignee differs from its verified native User binding.', 'Task', result.target_name)
        assignment_action = _record_task_assignment_pending(run, assignment_record, result.target_name, instance)
        if assignment_action in {'BLOCKED', 'PENDING_IDENTITY', 'CONFLICT'}:
            from dataclasses import replace
            result = replace(result, pending_relation=True)
        _ack_mapped_assignment(run, assignment_record, result.target_name, instance, recipient)
    elif not assignment_error and result.target_name and run.mode != 'Preview':
        from hero_integration.services.direct_mapping import MISSING, lookup
        assignment_rule = next((spec for spec in plan.definitions if spec.target_doctype == 'ToDo'
                                and spec.target_field == 'allocated_to'), None)
        raw = lookup(record, assignment_rule.source_path) if assignment_rule else MISSING
        if type(raw) is int and raw > 0:
            _record_task_assignment_pending(run, {**record, 'target_user_id': raw}, result.target_name, instance)
            from dataclasses import replace
            result = replace(result, pending_relation=True)
    return result


def _ack_mapped_assignment(run, record, task_name, source_instance, recipient) -> bool:
    """Acknowledge only the currently verified system-owned native assignment."""
    if getattr(run, 'mode', '') != 'Execute':
        return False
    current = _plan_task_assignment(run, record, task_name, source_instance)
    if current.action != 'KEEP' or current.recipient != recipient:
        return False
    link = _link('Task', str(record['id']), source_instance)
    if not link:
        raise MappingPlanError('Mapped assignment has no verified parent Task binding.')
    locked = frappe.get_doc('HERO Integration Link', link.name, for_update=True)
    if (locked.source_system != 'HERO' or locked.source_type != 'Task'
            or str(locked.source_id) != str(record['id']) or (locked.source_instance or 'default') != source_instance
            or locked.target_doctype != 'Task' or locked.target_name != task_name):
        raise MappingPlanError('Mapped assignment parent Task binding changed.')
    source = decode_hashes(locked.source_field_hashes)
    target = decode_hashes(locked.target_field_hashes)
    if source is None or target is None:
        raise MappingPlanError('Mapped assignment requires a verified parent field baseline.')
    key = 'ToDo.allocated_to'
    acknowledged = field_hashes({key: recipient})[key]
    source[key], target[key] = acknowledged, acknowledged
    frappe.db.set_value('HERO Integration Link', locked.name,
        {'source_field_hashes': encode_hashes(source), 'target_field_hashes': encode_hashes(target)}, update_modified=False)
    return True
