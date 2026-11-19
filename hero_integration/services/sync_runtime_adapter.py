"""Resolve and revalidate the approved source and reader around B8 execution."""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
from uuid import uuid4

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
    executable = service.resolve_executable_plan(
        site_scope, source_instance, plan.generation, True, actor
    )
    if executable.selection.execution_mode != RUNTIME_EXECUTION_MODE:
        raise RuntimeBridgeError("Runtime plan execution mode is not AUTO_SYNC.")
    _require_latest_validated_snapshot(frappe, settings, source_instance, executable.selection)
    _require_area_enabled(frappe, settings, executable.selection)
    return _run(executable, frappe, automatic=True)


def run_materialized_monitor(*, frappe_module=None, client=None):
    """Observe one approved B2 query page without processing or checkpointing data."""
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
    actor = _scheduler_actor(frappe)
    plans = _runtime_plan_service(frappe)
    approved = plans.current_approved(site, source)
    if approved is None:
        raise RuntimeBridgeError("No approved canonical runtime plan exists for this site/source instance.")
    executable = plans.resolve_executable_plan(site, source, approved.generation, True, actor)
    _require_latest_validated_snapshot(frappe, settings, source, executable.selection)
    _require_area_enabled(frappe, settings, executable.selection)
    materialized = reconstruct_materialized_read_plan(executable.selection, frappe_module=frappe)
    from hero_integration.services.offset_pagination import validate_overlap
    validate_overlap(polling.get("page_overlap", 0), materialized.sealed.contract.page_size)
    if materialized.sealed.contract.page_size > polling["page_size"]:
        raise RuntimeBridgeError("The approved read page size exceeds the monitoring limit.")
    configuration = get_configuration()
    if configuration.source_instance != source:
        raise RuntimeBridgeError("Configured HERO source changed before monitoring.")
    transport = client or HeroGraphQLClient(configuration.endpoint, lambda: configuration.api_token,
                                            timeout=configuration.timeout_seconds, max_retries=0,
                                            requests_per_minute=int(polling["rate_limit"]))
    # Resolve the same generation and seal again immediately before transport.
    current = plans.resolve_executable_plan(site, source, approved.generation, True, actor)
    _require_latest_validated_snapshot(frappe, settings, source, current.selection)
    if current.selection != executable.selection:
        raise RuntimeBridgeError("Approved monitoring plan changed before the read.")
    rule_hash = effective_rule_fingerprint(materialized.sealed.contract.definition_ids,
        executable.selection.graphql_mapping, frappe_module=frappe)
    # No row/advisory lock or stale read snapshot may survive into HTTP.
    frappe.db.rollback()
    request = materialized.request
    data = transport.query(request.document, dict(request.variables))
    decode_graphql_result(request, Response(True, json.dumps({"data": data}), True))
    frappe.db.rollback()
    _validate_execution_evidence(frappe, plans, executable, materialized, polling, binding_revision,
                                 rule_hash, site, source, actor, automatic=True)
    contract = materialized.sealed.contract
    records = value_at(data, contract.records_path)
    if not isinstance(records, list) or len(records) > contract.page_size or not all(isinstance(row, dict) for row in records):
        raise RuntimeBridgeError("Materialized monitoring response violates its bounded read contract.")
    for row in records:
        identity = value_at(row, contract.identity_path)
        if isinstance(identity, bool) or not isinstance(identity, (str, int)) or not str(identity):
            raise RuntimeBridgeError("Materialized monitoring response has no valid record identity.")
    operation = contract.target_operation
    return MonitorResult(len(records) if operation == "HeroCustomers" else 0,
                         len(records) if operation == "HeroProjectMatches" else 0,
                         source, (operation,), rows_read=len(records))


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
    current = plans.resolve_executable_plan(site, source, executable.generation, automatic, actor)
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

    if all(area_enabled(settings, item) for item in AREAS):
        return
    try:
        target = json.loads(frappe.db.get_value("HERO GraphQL Mapping", selection.graphql_mapping, "mapping_json") or "{}").get("target_operation")
    except (TypeError, ValueError, AttributeError):
        target = None
    item = BY_OPERATION.get(target)
    if item is None or not area_enabled(settings, item):
        raise RuntimeBridgeError('Automatic sync for {0} is switched off on the Synchronization page.', item.label if item else 'this area')


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
                 confirmed, scope="BOUND_OBJECTS", frappe_module=None):
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
    source = str(settings.hero_source_instance or "default").strip()
    fingerprint = sha256(json.dumps(
        [site, actor, source, revision, plan_key, generation, scope, 50]
    ).encode()).hexdigest()
    budget = ExecutionBudget("sync", run_id, source)
    def receipt():
        row = frappe.db.get_value(FrappeSyncLedger.RUN, run_id,
            ["request_fingerprint", "actor", "site_scope", "state", "processed_count", "skipped_count"], as_dict=True)
        if not row:
            return None
        if row.actor != actor or row.site_scope != site or row.request_fingerprint != fingerprint:
            raise RuntimeBridgeError("This synchronization approval belongs to a different request.")
        return {"run": run_id, "status": row.state, "scope": scope, "plan_key": plan_key,
                "budget_id": budget.budget_id, "reserved": budget.used(), "limit": 50,
                "processed": row.processed_count, "skipped": row.skipped_count}

    prior = receipt()
    if prior:
        return prior
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


def _run(executable, frappe, *, automatic: bool, provider=None, run_id=None, fingerprint="", budget=None, bounded=False) -> RuntimePreflight:
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
    run_id = run_id or uuid4().hex
    revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    provider = provider or _UnprovenProvider()
    if bounded:
        raw = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot).snapshot_json
        provider = ApprovedBoundReader(materialized, raw, revision, polling["rate_limit"])
    object_type = None
    if bounded or provider.capabilities.complete_change_discovery:
        from graphql import get_named_type
        from hero_integration.services.graphql_schema_cache import compiled_schema
        raw = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot).snapshot_json
        schema = compiled_schema(raw).core
        object_type = get_named_type(schema.query_type.fields[materialized.request.protocol_field_name].type).name
        if getattr(provider, "object_type", object_type) != object_type:
            raise RuntimeBridgeError("Provider object type differs from the approved reader.")

    rules = effective_rule_fingerprint(contract.definition_ids, selection.graphql_mapping, frappe_module=frappe)
    configuration = replace(configuration, contract_fingerprint=sha256(json.dumps([
        executable.plan_key, executable.generation, revision, materialized.request.request_fingerprint,
        rules, polling["page_overlap"], polling["read_mode"],
    ]).encode()).hexdigest())
    plans = _runtime_plan_service(frappe)

    def validate():
        _validate_execution_evidence(frappe, plans, executable, materialized, polling, revision, rules,
                                     selection.site_scope, selection.source_instance, executable.actor,
                                     automatic=automatic)

    def reserve(kind, identity):
        try:
            budget.reserve(kind, identity)
        except BudgetReached:
            return False
        return True

    engine = SyncEngine(
        FrappeSyncLedger(frappe), materialized, provider,
        FrappeBoundIndex(frappe, object_type=object_type, definition_ids=contract.definition_ids),
        FrappeBoundProcessor(contract.mapping_revision, contract.definition_ids, frappe, run_id=run_id,
            object_type=object_type, mapping_name=selection.graphql_mapping, identity_path=contract.identity_path,
            contract_fingerprint=configuration.contract_fingerprint),
        reserve_identity=reserve if budget else None, revalidate=validate, object_type=object_type,
    )
    state = engine.configure(configuration)
    run = SyncRun(run_id, selection.site_scope, executable.actor, state.generation, automatic, fingerprint,
                  selection.source_instance, executable.plan_key, executable.generation, revision,
                  ("BOUND_OBJECTS" if bounded else "DELTA") if budget else "")
    if bounded:
        result = engine.compare_bound(run)
    else:
        result = engine.poll(run) if automatic else engine.pending_sync(run)
    return RuntimePreflight(result.status.value, executable.plan_key, executable.generation, executable.selection.source_instance, run_id)


class _UnprovenProvider:
    """No generic/offset reader can claim a complete B8 delta cursor."""
    capabilities = None

    def __init__(self):
        from hero_integration.services.sync_engine import ChangeCapabilities
        self.capabilities = ChangeCapabilities(False, False, False)

    def changes(self, cursor):
        raise AssertionError("An unproven provider must not be read.")
