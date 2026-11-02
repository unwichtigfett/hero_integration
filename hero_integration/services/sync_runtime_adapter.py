"""Canonical B8 runtime entrypoints with fail-closed preflight ownership.

This boundary resolves the persisted runtime plan before a provider exists.
It intentionally does not compile a second GraphQL request or treat an offset
reader as a change provider while B7 materialized runtime persistence is absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from hero_integration.services.localized_error import LocalizedError


BLOCKED_NO_B7_MATERIALIZED_RUNTIME = "BLOCKED_NO_B7_MATERIALIZED_RUNTIME"


class RuntimeBridgeError(LocalizedError):
    """The scheduler cannot safely enter the B8 runtime."""


@dataclass(frozen=True, slots=True)
class RuntimePreflight:
    status: str
    plan_key: str
    generation: int
    source_instance: str


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
    revision = int(getattr(settings, "source_binding_revision", 0) or 0)
    if int(getattr(settings, "source_binding_confirmed_revision", 0) or 0) != revision:
        raise RuntimeBridgeError("The HERO endpoint, token or source instance changed; an administrator must confirm the source binding.")


def run_scheduled_auto_sync(*, frappe_module=None) -> RuntimePreflight:
    """Revalidate the selected AUTO_SYNC plan before any provider construction.

    The blocked status is not an execution success. It says only that the
    canonical plan remains valid while its B7 materialized runtime is missing.
    """
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
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    import json

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
    # Reuse the existing runtime generation, mapping-root and field-rule locks.
    # These locks protect only acknowledgement, after transport has completed.
    rows = frappe.db.sql("SELECT generation FROM `tabHERO Runtime Plan` WHERE plan_key=%s FOR UPDATE",
                         (approved.plan_key,))
    if len(rows) != 1 or int(rows[0][0]) != approved.generation:
        raise RuntimeBridgeError("Monitoring runtime generation changed during transport.")
    frappe.db.sql("SELECT field FROM `tabSingles` WHERE doctype=%s FOR UPDATE", ("HERO Integration Settings",))
    latest_settings = frappe.get_single("HERO Integration Settings")
    _require_confirmed_source_binding(latest_settings)
    if (int(getattr(latest_settings, "source_binding_revision", 0) or 0) != binding_revision
            or effective_mode(latest_settings) != "MONITOR" or runtime_plan(latest_settings) != polling
            or str(getattr(latest_settings, "hero_source_instance", "default") or "default").strip() != source):
        raise RuntimeBridgeError("Monitoring source/configuration changed during transport.")
    mapping = frappe.get_doc("HERO GraphQL Mapping", executable.selection.graphql_mapping)
    lock_exchange_context(mapping.source_instance or '', mapping.schema_snapshot, mapping.direction)
    for doctype, name in (("HERO GraphQL Mapping", mapping.name),
                          ("HERO GraphQL Service", executable.selection.graphql_service),
                          ("HERO GraphQL Schema Snapshot", executable.selection.schema_snapshot)):
        frappe.get_doc(doctype, name, for_update=True)
    current = plans.resolve_executable_plan(site, source, approved.generation, True, actor)
    _require_latest_validated_snapshot(frappe, latest_settings, source, current.selection)
    if (current.selection != executable.selection
            or reconstruct_materialized_read_plan(current.selection, frappe_module=frappe) != materialized
            or effective_rule_fingerprint(materialized.sealed.contract.definition_ids,
                mapping.name, lock=True, frappe_module=frappe) != rule_hash):
        raise RuntimeBridgeError("Monitoring reader, schema or effective field rules changed during transport.")
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
                         source, (operation,))


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
    return _run(executable, frappe, automatic=False, provider=provider)


def _run(executable, frappe, *, automatic: bool, provider=None) -> RuntimePreflight:
    from hero_integration.services.b8_runtime import reconstruct_materialized_read_plan
    from hero_integration.services.polling import effective_mode
    from hero_integration.services.sync_engine import (
        ChangeCapabilities,
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
    engine = SyncEngine(
        FrappeSyncLedger(frappe), materialized, provider or _UnprovenProvider(),
        FrappeBoundIndex(frappe),
        FrappeBoundProcessor(materialized.sealed.contract.mapping_revision, materialized.sealed.contract.definition_ids, frappe),
    )
    state = engine.configure(configuration)
    run = SyncRun(uuid4().hex, executable.selection.site_scope, executable.actor, state.generation, automatic)
    result = engine.poll(run) if automatic else engine.pending_sync(run)
    return RuntimePreflight(result.status.value, executable.plan_key, result.generation, executable.selection.source_instance)


class _UnprovenProvider:
    """No generic/offset reader can claim a complete B8 delta cursor."""
    capabilities = None

    def __init__(self):
        from hero_integration.services.sync_engine import ChangeCapabilities
        self.capabilities = ChangeCapabilities(False, False, False)

    def changes(self, cursor):
        raise AssertionError("An unproven provider must not be read.")
