"""Resolve and revalidate the approved source and reader around B8 execution."""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from uuid import uuid4

from hero_integration.services.performance import profiled
from hero_integration.services.localized_error import LocalizedError


class RuntimeBridgeError(LocalizedError):
    """The scheduler cannot safely enter the B8 runtime."""


@dataclass(frozen=True, slots=True)
class RuntimePreflight:
    status: str
    plan_key: str
    generation: int
    source_instance: str
    run_id: str = ""
    inventory_progress: bool = False


def _frappe(frappe_module=None):
    if frappe_module is not None:
        return frappe_module
    import frappe
    return frappe


def _runtime_plan_service(frappe_module):
    from hero_integration.services.runtime_plan import (
        FrappeRuntimePlanAudit,
        FrappeRuntimePlanAuthority,
        FrappeRuntimePlanReferences,
        FrappeRuntimePlanStorage,
        RuntimePlanService,
    )
    return RuntimePlanService(
        FrappeRuntimePlanStorage(frappe_module),
        FrappeRuntimePlanReferences(frappe_module),
        FrappeRuntimePlanAudit(frappe_module),
        FrappeRuntimePlanAuthority(frappe_module),
    )


def _scheduler_actor(frappe_module) -> str:
    actor = getattr(getattr(frappe_module, "session", None), "user", "")
    if actor != "Administrator":
        raise PermissionError("B8 scheduler execution requires the Administrator service identity.")
    return actor


def _require_latest_validated_snapshot(frappe, settings, source_instance, selection) -> None:
    """Do not silently transfer approval when the source schema changes."""
    from hero_integration.client.graphql import HeroClientError, validate_endpoint
    from hero_integration.services.introspection import endpoint_fingerprint

    hosts = {line.strip().lower() for line in str(getattr(settings, "allowed_hero_hosts", "") or "").splitlines() if line.strip()}
    try:
        endpoint = validate_endpoint(getattr(settings, "hero_api_url", ""), hosts)
    except (HeroClientError, ValueError) as error:
        raise RuntimeBridgeError("Automatic sync requires a valid configured HERO endpoint.") from error
    rows = frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        filters={"source_instance": source_instance,
                 "endpoint_fingerprint": endpoint_fingerprint(endpoint.url, source_instance),
                 "analysis_status": "Validated"},
        fields=["name", "schema_fingerprint"],
        order_by="creation desc, name desc", limit_page_length=1,
    )
    if not rows:
        raise RuntimeBridgeError("No validated snapshot exists for the current HERO source and endpoint.")
    newest = rows[0]
    if (newest.get("name") != selection.schema_snapshot
            or newest.get("schema_fingerprint") != selection.schema_fingerprint):
        from hero_integration.services.schema_adoption import is_confirmed
        if is_confirmed(frappe, newest, selection):
            return
        raise RuntimeBridgeError("The approved plan does not use the newest validated snapshot; review and approve an updated plan.")


def _require_confirmed_source_binding(settings) -> None:
    """Refuse HERO reads after an unconfirmed endpoint, token or source change."""
    from hero_integration.client.settings import HeroConfigurationError, require_confirmed_source_binding

    revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    confirmed = int(getattr(settings, "source_binding_confirmed_revision", 0) or 0)
    try:
        require_confirmed_source_binding(revision, confirmed)
    except HeroConfigurationError as error:
        raise RuntimeBridgeError.from_error(error) from error


class ReaderDisabled(RuntimeBridgeError):
    """This approved reader was switched off for the site."""


def reader_allocations(readers, limit, last_reader):
    """Split one positive cycle budget in rotation order."""
    if not readers:
        return []
    names = [reader.graphql_service for reader in readers]
    start = names.index(last_reader) + 1 if last_reader in names else 0
    ordered = readers[start:] + readers[:start]
    share, extra = divmod(limit, len(ordered))
    return [(reader, share + int(index < extra)) for index, reader in enumerate(ordered)
            if share or index < extra]


@profiled("sync.scheduled_cycle")
def run_scheduled_auto_sync(*, frappe_module=None) -> RuntimePreflight:
    """Resolve the active source, schema, area and plan before constructing a change provider."""
    frappe = _frappe(frappe_module)
    settings = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.polling import effective_mode
    from hero_integration.services.runtime_plan import RUNTIME_EXECUTION_MODE

    if effective_mode(settings) != "AUTO_SYNC":
        raise RuntimeBridgeError("Canonical scheduled sync requires AUTO_SYNC mode.")
    _require_confirmed_source_binding(settings)
    site_scope = getattr(getattr(frappe, "local", None), "site", "")
    source_instance = str(getattr(settings, "hero_source_instance", "default") or "default").strip()
    if not site_scope or not source_instance:
        raise RuntimeBridgeError("Canonical scheduled sync requires current site and source identities.")
    actor = _scheduler_actor(frappe)
    service = _runtime_plan_service(frappe)
    plan = service.current_approved(site_scope, source_instance)
    if plan is None:
        raise RuntimeBridgeError("No approved canonical runtime plan exists for this site/source instance.")
    if plan.additional_readers:
        return _run_scheduled_readers(frappe, settings, service, plan, actor)
    executable = service.resolve_executable_plan(
        site_scope, source_instance, plan.generation, True, actor
    )
    if executable.selection.execution_mode != RUNTIME_EXECUTION_MODE:
        raise RuntimeBridgeError("Runtime plan execution mode is not AUTO_SYNC.")
    _require_latest_validated_snapshot(frappe, settings, source_instance, executable.selection)
    _require_area_enabled(frappe, settings, executable.selection)
    from hero_integration.services.schema_adoption import read_scope
    with read_scope(frappe, executable.selection):
        result = _run(executable, frappe, automatic=True)
    from hero_integration.services.automatic_writeback import enqueue
    enqueue(frappe, settings, executable)
    return result


def _run_scheduled_readers(frappe, settings, service, plan, actor):
    from hero_integration.services.polling import runtime_plan
    from hero_integration.services.automatic_writeback import enqueue

    due = []
    for item in plan.selections:
        try:
            _require_area_enabled(frappe, settings, item)
        except ReaderDisabled:
            continue
        due.append(item)
    limit = runtime_plan(settings)["page_size"]
    if settings.testing_enabled:
        from hero_integration.api import _testing_max_actions
        limit = min(limit, _testing_max_actions(settings.testing_max_actions))
    previous = frappe.db.get_value("HERO Runtime Plan", plan.plan_key, "last_scheduled_reader")
    allocations = reader_allocations(due, limit, previous)
    if not allocations:
        raise RuntimeBridgeError("No approved reader is enabled.")
    cycle = uuid4().hex
    outcomes = []
    completed_slices = 0
    def remember(reader):
        # The regular scheduler owns the site polling lock. Never write a cursor into a newer plan.
        frappe.db.sql("UPDATE `tabHERO Runtime Plan` SET last_scheduled_reader=%s WHERE name=%s AND generation=%s",
                      (reader.graphql_service, plan.plan_key, plan.generation))
        frappe.db.commit()

    last_run = ""
    for item, allocation in allocations:
        remember(item)
        try:
            executable = service.resolve_executable_plan(item.site_scope, item.source_instance, plan.generation,
                                                         True, actor, reader=item.graphql_service)
            if getattr(settings, "source_binding_revision", None) != frappe.db.get_single_value(
                    "HERO Integration Settings", "source_binding_revision", cache=False):
                raise RuntimeBridgeError("Source revision changed before polling.")
            _require_latest_validated_snapshot(frappe, settings, item.source_instance, item)
            from hero_integration.services.schema_adoption import read_scope
            with read_scope(frappe, item):
                result = _run(executable, frappe, automatic=True, allocation=allocation, cycle=cycle)
            outcomes.append(result.status)
            if result.status in ("SUCCEEDED", "BOUNDED_COMPLETE") or result.inventory_progress:
                completed_slices += 1
            last_run = result.run_id
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="HERO scheduled reader failed: " + item.graphql_service)
            outcomes.append("FAILED")
    if len(allocations) == len(due):
        extra = limit % len(due)
        remember(allocations[max(0, extra - 1)][0])
    # The existing outbound selection remains the explicit primary reader.
    if settings.allow_graphql_mutation_execution:
        try:
            _require_area_enabled(frappe, settings, plan.selection)
        except ReaderDisabled:
            pass  # A disabled outbound area does not invalidate the inbound results.
        else:
            primary = service.resolve_executable_plan(plan.selection.site_scope, plan.selection.source_instance,
                                                      plan.generation, True, actor)
            enqueue(frappe, settings, primary)
    status = next((s for s in ("FAILED", "CONFLICT", "PARTIAL", "INCOMPLETE", "RECONCILIATION_REQUIRED", "BUDGET_REACHED", "DISABLED")
                   if s in outcomes), outcomes[-1])
    return RuntimePreflight(status, plan.plan_key, plan.generation, plan.selection.source_instance, last_run,
                            inventory_progress=status == "PARTIAL" and completed_slices == len(allocations))


@dataclass(frozen=True)
class ManualMonitorSample:
    plan_key: str
    generation: int
    source_revision: int
    operation: str
    limit: int
    offset: int | None = None  # None preserves the v2 single-page read.


def _require_bounded_monitor_query(materialized):
    """A declared page size alone cannot bound an unpaginated source list."""
    from graphql import FieldNode, IntValueNode, OperationDefinitionNode, VariableNode, parse

    contract = materialized.sealed.contract
    request = materialized.request
    definitions = parse(request.document).definitions
    if len(definitions) != 1 or not isinstance(definitions[0], OperationDefinitionNode):
        raise RuntimeBridgeError("Manual monitoring requires one bounded collection query.")
    fields = definitions[0].selection_set.selections
    if len(fields) != 1 or not isinstance(fields[0], FieldNode):
        raise RuntimeBridgeError("Manual monitoring requires one bounded collection root.")
    root = fields[0]
    path = (root.alias or root.name).value
    first = next((arg.value for arg in root.arguments if arg.name.value == "first"), None)
    count = request.variables.get(first.name.value) if isinstance(first, VariableNode) else (
        int(first.value) if isinstance(first, IntValueNode) else None)
    if contract.records_path != path or type(count) is not int or count != contract.page_size:
        raise RuntimeBridgeError("Manual monitoring needs a proven server-side limit on the selected collection.")
    return root


def _monitor_page_variables(materialized, root, sample):
    """Only the two native, identity-ordered readers authorize varying first and offset."""
    from graphql import VariableNode
    from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLDocumentRequest
    contract, request = materialized.sealed.contract, materialized.request
    args = {arg.name.value: arg.value.name.value for arg in root.arguments if isinstance(arg.value, VariableNode)}
    variables = request.variables
    expected_root = {"HeroCustomers": "contacts", "HeroProjectMatches": "project_matches"}.get(sample.operation)
    if (not isinstance(request, GraphQLDocumentRequest) or root.name.value != expected_root or contract.identity_path != "id"
            or type(sample.offset) is not int or sample.offset < 0
            or any(args.get(key) != key for key in ("first", "offset", "orderBy"))
            or contract.variables.get("first") != "$page_size" or contract.variables.get("offset") != "$offset"
            or contract.variables.get("orderBy") != "id" or variables.get("orderBy") != "id"
            or variables.get("offset") != 0):
        raise RuntimeBridgeError("Incremental MONITOR requires the approved identity-ordered first/offset reader.")
    size = min(sample.limit, contract.page_size)
    return {**variables, "first": size, "offset": sample.offset}, size


def run_materialized_monitor(*, frappe_module=None, client=None, sample=None):
    """Observe one approved page without writing business data or a scheduler checkpoint."""
    from hero_integration.services.b8_runtime import reconstruct_materialized_read_plan
    from hero_integration.services.polling import MonitorResult, effective_mode, runtime_plan
    from hero_integration.services.read_contract import value_at
    from hero_integration.graphql_protocol.graphql_result_decoder import decode_graphql_result
    from hero_integration.graphql_protocol.transport import Response
    from hero_integration.client.settings import get_configuration
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.services.mapping_runtime import effective_rule_fingerprint

    frappe = _frappe(frappe_module)
    frappe.db.rollback()  # MONITOR owns no business writes; start with current committed evidence.
    settings = frappe.get_single("HERO Integration Settings")
    if effective_mode(settings) != "MONITOR":
        raise RuntimeBridgeError("Materialized monitoring requires MONITOR mode.")
    _require_confirmed_source_binding(settings)
    polling = runtime_plan(settings)
    binding_revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    if polling["unsupported_sources"]:
        raise RuntimeBridgeError("Materialized monitoring configuration is not supported; review the approved query variables.")
    if polling["reconciliation"]:
        raise RuntimeBridgeError("Materialized monitoring does not yet support reconciliation.")
    site = getattr(getattr(frappe, "local", None), "site", "")
    source = str(getattr(settings, "hero_source_instance", "default") or "default").strip()
    if not site or not source:
        raise RuntimeBridgeError("Monitoring requires current site and source identities.")
    automatic = sample is None
    actor = _scheduler_actor(frappe) if automatic else frappe.session.user
    plans = _runtime_plan_service(frappe)
    approved = plans.current_approved(site, source)
    if approved is None:
        raise RuntimeBridgeError("No approved canonical runtime plan exists for this site/source instance.")
    if sample and (sample.plan_key != approved.plan_key or sample.generation != approved.generation
                   or sample.source_revision != binding_revision):
        raise RuntimeBridgeError("The reviewed MONITOR plan or source revision changed.")
    executable = plans.resolve_executable_plan(site, source, approved.generation, automatic, actor)
    _require_latest_validated_snapshot(frappe, settings, source, executable.selection)
    _require_area_enabled(frappe, settings, executable.selection)
    materialized = reconstruct_materialized_read_plan(executable.selection, frappe_module=frappe)
    request = materialized.request
    variables, page_size = request.variables, materialized.sealed.contract.page_size
    incremental = sample is not None and sample.offset is not None
    if sample:
        from hero_integration.services.migration_testing import validate_limit
        validate_limit(sample.limit)
        if sample.limit is None or sample.operation != materialized.sealed.contract.target_operation:
            raise RuntimeBridgeError("The reviewed MONITOR stream differs from the approved collection.")
        root = _require_bounded_monitor_query(materialized)
        if incremental:
            variables, page_size = _monitor_page_variables(materialized, root, sample)
    from hero_integration.services.offset_pagination import validate_overlap
    validate_overlap(polling.get("page_overlap", 0), materialized.sealed.contract.page_size)
    if incremental:
        page_size = min(page_size, polling["page_size"])
        variables["first"] = page_size
    elif materialized.sealed.contract.page_size > polling["page_size"]:
        raise RuntimeBridgeError("The approved read page size exceeds the monitoring limit.")
    configuration = get_configuration()
    if configuration.source_instance != source:
        raise RuntimeBridgeError("Configured HERO source changed before monitoring.")
    transport = client or HeroGraphQLClient(configuration.endpoint, lambda: configuration.api_token,
                                            timeout=polling["timeout_seconds"], max_retries=polling["retry_limit"],
                                            retry_backoff_seconds=polling["retry_backoff_seconds"],
                                            requests_per_minute=int(polling["rate_limit"]))
    # Resolve the same generation and seal again immediately before transport.
    current = plans.resolve_executable_plan(site, source, approved.generation, automatic, actor)
    _require_latest_validated_snapshot(frappe, settings, source, current.selection)
    if current.selection != executable.selection:
        raise RuntimeBridgeError("Approved monitoring plan changed before the read.")
    rule_hash = effective_rule_fingerprint(materialized.sealed.contract.definition_ids,
        executable.selection.graphql_mapping, frappe_module=frappe)
    # No row/advisory lock or stale read snapshot may survive into HTTP.
    frappe.db.rollback()
    data = transport.query(request.document, variables)
    decode_graphql_result(request, Response(True, json.dumps({"data": data}), True))
    frappe.db.rollback()
    _validate_execution_evidence(frappe, plans, executable, materialized, polling, binding_revision,
                                 rule_hash, site, source, actor, automatic=automatic)
    contract = materialized.sealed.contract
    records = value_at(data, contract.records_path)
    if not isinstance(records, list) or len(records) > page_size or not all(isinstance(row, dict) for row in records):
        raise RuntimeBridgeError("Materialized monitoring response violates its bounded read contract.")
    observed, conflicts = {}, set()
    last_number = -1
    for row in records:
        identity = value_at(row, contract.identity_path)
        if isinstance(identity, bool) or not isinstance(identity, (str, int)) or not str(identity).strip():
            raise RuntimeBridgeError("Materialized monitoring response has no valid record identity.")
        if sample:
            key = str(identity).strip()
            if incremental:
                if not key.isascii() or not key.isdigit() or len(key) > 140 or str(int(key)) != key or int(key) < last_number:
                    raise RuntimeBridgeError("MONITOR returned an invalid or out-of-order numeric identity.")
                last_number = int(key)
            if key in observed:
                if observed[key] != row:
                    conflicts.add(key)
            else:
                observed[key] = row
    if sample and not incremental:
        # The transport page is already bounded. Its ordering may vary across responses.
        def identity_order(key):
            return (0, len(key), key) if key.isascii() and key.isdigit() else (1, 0, key)
        keys = sorted(observed, key=identity_order)[:sample.limit]
        observed = {key: observed[key] for key in keys}
        conflicts.intersection_update(observed)
    fingerprints = {}
    if incremental:
        from hero_integration.services.offset_pagination import fingerprint
        fingerprints = {key: fingerprint(row) for key, row in observed.items()}
    operation = contract.target_operation
    count = len(observed) if sample else len(records)
    return MonitorResult(count if operation == "HeroCustomers" else 0,
                         count if operation == "HeroProjectMatches" else 0,
                         source, (operation,), rows_read=len(records), observed_ids=tuple(observed),
                         conflicting_ids=tuple(sorted(conflicts)), reader_digest=request.request_fingerprint,
                         page_size=page_size, fingerprints=fingerprints, minimum_interval=60 / polling["rate_limit"])


def _validate_execution_evidence(frappe, plans, executable, materialized, polling, binding_revision,
                                 rule_hash, site, source, actor, *, automatic):
    from hero_integration.services.b8_runtime import reconstruct_materialized_read_plan
    from hero_integration.services.polling import runtime_plan
    from hero_integration.services.mapping_runtime import effective_rule_fingerprint
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context

    # Reuse the existing runtime generation, mapping-root and field-rule locks.
    # These locks protect only acknowledgement, after transport has completed.
    rows = frappe.db.sql("SELECT generation FROM `tabHERO Runtime Plan` WHERE plan_key=%s FOR UPDATE",
                         (executable.plan_key,))
    if len(rows) != 1 or int(rows[0][0]) != executable.generation:
        raise RuntimeBridgeError("Monitoring runtime generation changed during transport.")
    frappe.db.sql("SELECT field FROM `tabSingles` WHERE doctype=%s FOR UPDATE", ("HERO Integration Settings",))
    latest_settings = frappe.get_single("HERO Integration Settings")
    _require_confirmed_source_binding(latest_settings)
    if (int(getattr(latest_settings, "source_binding_revision", 0) or 0) != binding_revision
            or runtime_plan(latest_settings) != polling
            or str(getattr(latest_settings, "hero_source_instance", "default") or "default").strip() != source):
        raise RuntimeBridgeError("Monitoring source/configuration changed during transport.")
    mapping = frappe.get_doc("HERO GraphQL Mapping", executable.selection.graphql_mapping)
    lock_exchange_context(mapping.source_instance or '', mapping.schema_snapshot, mapping.direction)
    for doctype, name in (("HERO GraphQL Mapping", mapping.name),
                          ("HERO GraphQL Service", executable.selection.graphql_service),
                          ("HERO GraphQL Schema Snapshot", executable.selection.schema_snapshot)):
        frappe.get_doc(doctype, name, for_update=True)
    current = plans.resolve_executable_plan(site, source, executable.generation, automatic, actor,
                                            reader=executable.selection.graphql_service)
    _require_latest_validated_snapshot(frappe, latest_settings, source, current.selection)
    _require_area_enabled(frappe, latest_settings, current.selection)
    if (current.selection != executable.selection
            or reconstruct_materialized_read_plan(current.selection, frappe_module=frappe) != materialized
            or effective_rule_fingerprint(materialized.sealed.contract.definition_ids,
                mapping.name, lock=True, frappe_module=frappe) != rule_hash):
        raise RuntimeBridgeError("Monitoring reader, schema or effective field rules changed during transport.")


def _require_area_enabled(frappe, settings, selection) -> None:
    """Respect the area switches of the Synchronization page before any HERO request."""
    import json

    from hero_integration.services.sync_page import AREAS, BY_OPERATION, area_enabled

    from hero_integration.services.file_migration import DIRECT_OPERATIONS, pictures_enabled
    all_enabled = all(area_enabled(settings, item) for item in AREAS)
    allow_pictures = pictures_enabled()
    if all_enabled and allow_pictures:
        return
    try:
        target = json.loads(frappe.db.get_value("HERO GraphQL Mapping", selection.graphql_mapping, "mapping_json") or "{}").get("target_operation")
    except (TypeError, ValueError, AttributeError):
        target = None
    if not allow_pictures and (not target or target == "HeroPictures"):
        raise ReaderDisabled("Picture migration is disabled on this site.")
    # The four Desk switches cover only their own core areas. Direct areas
    # are admitted by the selected runtime plan and their site-specific gates.
    if all_enabled or target in DIRECT_OPERATIONS.values():
        return
    item = BY_OPERATION.get(target)
    if item is None or not area_enabled(settings, item):
        raise ReaderDisabled('Automatic sync for {0} is switched off on the Synchronization page.', item.label if item else 'this area')


def run_pending_sync(expected_plan_generation: int, *, frappe_module=None, provider=None) -> RuntimePreflight:
    """Run explicitly requested bound-only reconciliation while polling is OFF.

    Require the reviewed plan generation. An unproven provider returns incomplete
    or reconciliation status before reading; this grants no global cursor claim.
    """
    frappe = _frappe(frappe_module)
    settings = frappe.get_single("HERO Integration Settings")
    site_scope = getattr(getattr(frappe, "local", None), "site", "")
    source_instance = str(getattr(settings, "hero_source_instance", "default") or "default").strip()
    if not site_scope or not source_instance:
        raise RuntimeBridgeError("Pending Sync requires current site and source identities.")
    _require_confirmed_source_binding(settings)
    actor = getattr(getattr(frappe, "session", None), "user", "")
    service = _runtime_plan_service(frappe)
    executable = service.resolve_executable_plan(site_scope, source_instance, expected_plan_generation, False, actor)
    _require_latest_validated_snapshot(frappe, settings, source_instance, executable.selection)
    _require_area_enabled(frappe, settings, executable.selection)
    return _run(executable, frappe, automatic=False, provider=provider)


def run_one_shot(*, request_id, plan_key, expected_plan_generation, expected_source_revision,
                 confirmed, scope="BOUND_OBJECTS", expected_limit=None, frappe_module=None):
    """Consume one explicit approval. Replaying its UUID only reads its receipt."""
    from uuid import UUID
    from hero_integration.services.polling import effective_mode
    from hero_integration.services.execution_budget import ExecutionBudget
    from hero_integration.services.sync_engine import FrappeSyncLedger

    frappe = _frappe(frappe_module)
    frappe.only_for("System Manager")
    if str(confirmed) != "1" or scope not in ("BOUND_OBJECTS", "DELTA"):
        raise RuntimeBridgeError("Explicit bounded synchronization approval is required.")
    try:
        run_id = UUID(str(request_id)).hex
        generation = int(expected_plan_generation)
        revision = int(expected_source_revision)
    except (TypeError, ValueError):
        raise RuntimeBridgeError("A request UUID and approved plan/source revisions are required.") from None
    actor, site = frappe.session.user, frappe.local.site
    settings = frappe.get_single("HERO Integration Settings")
    if int(getattr(settings, "testing_enabled", 0) or 0):
        from hero_integration.services.test_environment import require_environment
        require_environment(frappe)
    source = str(settings.hero_source_instance or "default").strip()
    from hero_integration.services.execution_budget import LEGACY_IDENTITY_LIMIT
    from hero_integration.api import _testing_max_actions

    def signature(limit):
        return sha256(json.dumps([site, actor, source, revision, plan_key, generation, scope, limit]).encode()).hexdigest()

    def receipt():
        row = frappe.db.get_value(FrappeSyncLedger.RUN, run_id,
            ["request_fingerprint", "actor", "site_scope", "state", "processed_count", "skipped_count",
             "budget_contract_version", "testing_max_actions"], as_dict=True)
        if not row:
            return None
        version = int(row.budget_contract_version or 1)
        limit = LEGACY_IDENTITY_LIMIT if version == 1 else _testing_max_actions(row.testing_max_actions)
        if (row.actor != actor or row.site_scope != site or row.request_fingerprint != signature(limit)
                or expected_limit is not None and _testing_max_actions(expected_limit) != limit):
            raise RuntimeBridgeError("This synchronization approval belongs to a different request.")
        saved_budget = ExecutionBudget("sync", run_id, source, limit, version)
        return {"run": run_id, "status": row.state, "scope": scope, "plan_key": plan_key,
                "budget_id": saved_budget.budget_id, "reserved": saved_budget.used(), "limit": limit,
                "processed": row.processed_count, "skipped": row.skipped_count}

    prior = receipt()
    if prior:
        return prior
    limit = _testing_max_actions(settings.testing_max_actions)
    if expected_limit is not None and _testing_max_actions(expected_limit) != limit:
        raise RuntimeBridgeError("The test limit changed. Review the current value before approval.")
    fingerprint = signature(limit)
    budget = ExecutionBudget("sync", run_id, source, limit, version=2)
    if effective_mode(settings) != "OFF" or revision != int(settings.source_binding_revision or 0):
        raise RuntimeBridgeError("A one-shot requires polling OFF and the reviewed source revision.")
    _require_confirmed_source_binding(settings)
    plan = _runtime_plan_service(frappe).resolve_executable_plan(site, source, generation, False, actor)
    if plan.plan_key != plan_key:
        raise RuntimeBridgeError("The reviewed runtime plan changed.")
    _require_latest_validated_snapshot(frappe, settings, source, plan.selection)
    _require_area_enabled(frappe, settings, plan.selection)
    _run(plan, frappe, automatic=False, run_id=run_id, fingerprint=fingerprint,
         budget=budget, bounded=(scope == "BOUND_OBJECTS"))
    result = receipt()
    if result is None:
        raise RuntimeBridgeError("Synchronization finished without a durable execution receipt.")
    return result


def _run(executable, frappe, *, automatic: bool, provider=None, run_id=None, fingerprint="", budget=None, bounded=False, allocation=None, cycle="") -> RuntimePreflight:
    from hero_integration.services.b8_runtime import reconstruct_materialized_read_plan
    from hero_integration.services.polling import effective_mode
    from hero_integration.services.sync_engine import (
        FrappeBoundIndex,
        FrappeBoundProcessor,
        FrappeSyncLedger,
        PollingConfiguration,
        SyncEngine,
        SyncRun,
    )

    if executable.selection.operation_revision != executable.selection.service_revision:
        raise RuntimeBridgeError("Runtime plan operation revision is stale.")
    materialized = reconstruct_materialized_read_plan(executable.selection, frappe_module=frappe)
    settings = frappe.get_single("HERO Integration Settings")
    if automatic:
        if effective_mode(settings) != "AUTO_SYNC":
            raise RuntimeBridgeError("Automatic synchronization is switched off.")
        _require_area_enabled(frappe, settings, executable.selection)
    configuration = PollingConfiguration(
        executable.selection.site_scope,
        effective_mode(settings) == "AUTO_SYNC",
        max(60, int(getattr(settings, "polling_interval_minutes", 60) or 60) * 60),
        executable.selection.source_instance,
        executable.selection.mapping_revision,
        executable.selection.schema_fingerprint,
    )
    from hero_integration.services.polling import runtime_plan
    from hero_integration.services.execution_budget import BudgetReached
    from hero_integration.services.mapping_runtime import effective_rule_fingerprint
    from hero_integration.services.b8_runtime import ApprovedBoundReader

    polling = runtime_plan(settings)
    if polling["unsupported_sources"]:
        raise RuntimeBridgeError("The polling configuration is unsupported.")
    selection = executable.selection
    contract = materialized.sealed.contract
    if contract.target_operation == "HeroPictures":
        from hero_integration.services.file_migration import pictures_enabled
        if not pictures_enabled():
            raise RuntimeBridgeError("Picture migration is disabled on this site.")
    run_id = run_id or uuid4().hex
    revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    provider = provider or _UnprovenProvider()
    # Without a verified remote change cursor, revisit known identities in
    # bounded slices. Keep the global delta watermark exclusive to true deltas.
    if automatic and not provider.capabilities.complete_change_discovery:
        required_flags = {"HeroCustomers": ("is_deleted", "is_contact_person"), "HeroProjectMatches": ("is_deleted",),
                          "HeroTasks": ("is_deleted",), "HeroCalendarEvents": ("deleted",),
                          "HeroDocuments": ("is_deleted",), "HeroPictures": ("is_deleted",)}
        prefix = ("data", *contract.records_path.split("."))
        observed = {binding.response_path for binding in materialized.request.result_bindings}
        file_inventory = (polling["full_inventory"] and contract.target_operation in {"HeroDocuments", "HeroPictures"}
                          and contract.adapter_options and all((*prefix, key) in observed for key in ("id", "uuid", "category")))
        bounded = bool(file_inventory or all((*prefix, flag) in observed for flag in required_flags.get(contract.target_operation, ())))
    raw = None
    if bounded or provider.capabilities.complete_change_discovery:
        raw = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot).snapshot_json
    if bounded:
        provider = ApprovedBoundReader(materialized, raw, revision, polling["rate_limit"])
        provider.concurrency = polling["concurrency"]
        provider.read_options = {"timeout": polling["timeout_seconds"], "max_retries": polling["retry_limit"],
                                 "retry_backoff_seconds": polling["retry_backoff_seconds"]}
    object_type = None
    if bounded or provider.capabilities.complete_change_discovery:
        from hero_integration.services.read_contract import record_object_type
        from hero_integration.services.graphql_schema_cache import compiled_schema
        schema = compiled_schema(raw).core
        object_type = record_object_type(schema, materialized.request.document, contract.records_path)
        if getattr(provider, "object_type", object_type) != object_type:
            raise RuntimeBridgeError("Provider object type differs from the approved reader.")

    employee_directory = (automatic and bounded and polling["full_inventory"] and not getattr(settings, "testing_enabled", 0)
        and contract.target_operation == "HeroEmployees" and contract.records_path == "company.partners"
        and contract.identity_path == "id" and contract.adapter_options and not contract.variables)
    inventory = automatic and bounded and polling["full_inventory"] and (bool(provider.inventory_arguments()) or employee_directory)
    if inventory:
        provider.prepare_inventory(selection, frappe, complete_history=not getattr(settings, "testing_enabled", 0))
        provider.employee_directory = bool(employee_directory)
    rules = effective_rule_fingerprint(contract.definition_ids, selection.graphql_mapping, frappe_module=frappe)
    shared_contract = [revision, materialized.request.request_fingerprint, rules]
    policy = [bool(inventory), polling["allow_backfill"],
        provider.product_usage[0] if inventory and provider.product_usage else None,
        getattr(settings, "testing_max_actions", 0) if getattr(settings, "testing_enabled", 0) else None,
    ]
    def signature(dimensions):
        return sha256(json.dumps(dimensions).encode()).hexdigest()
    configuration = replace(configuration, contract_fingerprint=signature([
        executable.plan_key, replace(selection, validated_snapshot="").semantic_hash, *shared_contract, polling["read_mode"], *policy]))

    def legacy_compatible(previous):
        if (previous.source_instance, previous.mapping_revision, previous.schema_fingerprint) != (
                configuration.source_instance, configuration.mapping_revision, configuration.schema_fingerprint):
            return False
        if previous.contract_fingerprint == configuration.contract_fingerprint:
            return True
        generations = frappe.get_all("HERO Runtime Plan Audit", filters={"runtime_plan": executable.plan_key,
            "semantic_hash": selection.semantic_hash, "action": "APPROVED"}, pluck="generation", limit_page_length=0)
        return any(previous.contract_fingerprint == signature([executable.plan_key, generation, *shared_contract,
            polling["page_overlap"], polling["read_mode"], polling["page_size"], *policy]) for generation in generations)

    plans = _runtime_plan_service(frappe)

    def validate():
        if inventory and contract.target_operation == "HeroProducts":
            from hero_integration.services.product_usage import load
            current_usage = load(selection.source_instance)
            if (current_usage and current_usage[0]) != (provider.product_usage and provider.product_usage[0]):
                raise RuntimeBridgeError("The source-approved product usage file changed during inventory.")
        _validate_execution_evidence(frappe, plans, executable, materialized, polling, revision, rules,
                                     selection.site_scope, selection.source_instance, executable.actor,
                                     automatic=automatic)

    remaining = min(allocation, polling["page_size"]) if allocation is not None else polling["page_size"]
    if automatic and getattr(settings, "testing_enabled", 0):
        from hero_integration.api import _testing_max_actions
        from hero_integration.services.execution_budget import ExecutionBudget
        from hero_integration.services.test_environment import require_environment

        require_environment(frappe)
        limit = min(remaining, _testing_max_actions(settings.testing_max_actions))
        budget = ExecutionBudget("sync", run_id, selection.source_instance, limit, version=2)
        remaining = min(remaining, limit)

    def reserve(kind, identity):
        nonlocal remaining
        if automatic:
            if remaining == 0:
                return False
            remaining -= 1
        if not budget:
            return True
        try:
            budget.reserve_record(kind, identity)
        except BudgetReached:
            return False
        return True

    def select_bound(identities):
        from itertools import islice
        # One lookahead retains the continuation position when this slice is full.
        batch = list(islice(identities, budget.limit + 1))
        budget.freeze(object_type, batch[:budget.limit], evidence={
            "reader": configuration.contract_fingerprint, "source_revision": revision})
        return iter(batch)

    inventory_options = None
    page_limit = 1
    if inventory:
        from hero_integration.services.execution_runtime import InventoryIntake
        from hero_integration.services.sync_page import BY_OPERATION
        size = min(contract.page_size, remaining)
        files = contract.target_operation in {"HeroDocuments", "HeroPictures"}
        if files:
            # Each admitted File can occupy the migration downloader's full binary budget.
            size = min(size, 2)
        if provider.product_usage:
            size = min(size, 48)  # Same verified product_ids batch as the migration reader.
        if not employee_directory:
            page_limit = min(polling["max_pages"], max(1, remaining // size))
        # A test selection must not enlist unbudgeted dependent migration workers.
        # Domain adapters own relationships. Restricted scalar authorities still
        # use the field processor until the complete adapter obeys the same policy.
        domain_authorized = (not budget and (contract.target_operation in BY_OPERATION or contract.target_operation in {"HeroProducts", "HeroServices"} or employee_directory or files)
                             and contract.adapter_options
                             and all(role in {"HERO", "BIDIRECTIONAL"} for role in (contract.field_authority or {}).values()))
        if domain_authorized and provider.direct_read:
            provider.direct_read = replace(provider.direct_read, validate_write=validate)
        intake = (InventoryIntake(selection, contract, revision, allow_new=polling["allow_backfill"],
                  reader_fingerprint=configuration.contract_fingerprint, sync_run=run_id, direct_read=provider.direct_read,
                  product_usage=provider.product_usage, file_configuration=provider._configuration if files else None) if domain_authorized else None)
        inventory_options = (size, min(polling["page_overlap"], size - 1), intake)
    ledger = (FrappeSyncLedger(frappe, reader=selection.source_instance + "|" + selection.graphql_service,
                              cycle=cycle or run_id, allocation=remaining) if automatic else
              FrappeSyncLedger(frappe, testing=bool(getattr(settings, "testing_enabled", 0))))
    if automatic:
        ledger.recover_scheduled_run(selection.site_scope)
        ledger.adopt_single_reader_state(configuration, legacy_compatible)
    engine = SyncEngine(
        ledger, materialized, provider,
        FrappeBoundIndex(frappe, object_type=object_type, definition_ids=contract.definition_ids),
        FrappeBoundProcessor(contract.mapping_revision, contract.definition_ids, frappe, run_id=run_id,
            object_type=object_type, mapping_name=selection.graphql_mapping, identity_path=contract.identity_path,
            contract_fingerprint=configuration.contract_fingerprint),
        reserve_identity=reserve if automatic or budget else None,
        select_bound=select_bound if bounded and budget and not automatic else None,
        revalidate=validate, object_type=object_type, inventory=inventory_options, inventory_pages=page_limit, bound_workers=(min(polling["page_size"], provider.bound_batch_size) if bounded and provider.bound_batch_size > 1
            else polling["concurrency"] if bounded else 1),
    )
    state = engine.configure(configuration)
    run = SyncRun(run_id, selection.site_scope, executable.actor, state.generation, automatic, fingerprint,
                  selection.source_instance, executable.plan_key, executable.generation, revision,
                  ("INVENTORY" if inventory else "BOUND_OBJECTS" if bounded else "DELTA"),
                  budget.limit if budget else 0, budget.version if budget else 0)
    if bounded:
        from hero_integration.services.employee_migration import historical_user_scope
        with historical_user_scope(selection.source_instance, *getattr(provider, "historical_people", ((), ()))):
            result = engine.compare_bound(run)
    else:
        result = engine.poll(run) if automatic else engine.pending_sync(run)
    from hero_integration.services.sync_engine import inventory_slice_completed
    progress = (inventory and result.status.value == "PARTIAL"
                and inventory_slice_completed(result.diagnostic, result.inventory_checkpoint))
    return RuntimePreflight(result.status.value, executable.plan_key, executable.generation,
                            executable.selection.source_instance, run_id, inventory_progress=bool(progress))


class _UnprovenProvider:
    """No generic/offset reader can claim a complete B8 delta cursor."""
    capabilities = None

    def __init__(self):
        from hero_integration.services.sync_engine import ChangeCapabilities
        self.capabilities = ChangeCapabilities(False, False, False)

    def changes(self, cursor):
        raise AssertionError("An unproven provider must not be read.")
