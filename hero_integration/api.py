from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import frappe


ACTIONS = {
    "Customer Migration",
    "Project Migration",
    "Cheap Data Migration",
    "Full Data Migration",
    "Sync",
    "Projection Migration",
    "Schedule Migration",
}
TERMINAL_STATES = {"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked"}
MUTATION_EXECUTION_DISABLED = "HERO_MUTATION_EXECUTION_DISABLED"
ACTIVE_MUTATION_RESERVATION = "active"


@frappe.whitelist(methods=["POST"])
def start_migration(
    action: str, dry_run: int | bool = 1, graphql_service: str = ""
) -> dict[str, str]:
    """Queue a manually requested migration; this RPC is always administrative."""
    frappe.only_for("System Manager")
    return _start_migration(
        action=action, dry_run=dry_run, scheduled=False, graphql_service=graphql_service
    )


def start_scheduled_sync() -> dict[str, str]:
    """Internal scheduler entrypoint; automatic execution requires an explicit service."""
    return {"status": "SKIPPED_NO_APPROVED_SERVICE"}


@frappe.whitelist(methods=["POST"])
def pending_sync(expected_plan_generation: int | str) -> dict[str, object]:
    """Run a manually authorized, bound-only B8 pending-sync attempt."""
    frappe.only_for("System Manager")
    try:
        generation = int(expected_plan_generation)
    except (TypeError, ValueError):
        frappe.throw("Pending Sync requires a positive current runtime-plan generation.")
    if generation < 1:
        frappe.throw("Pending Sync requires a positive current runtime-plan generation.")
    from hero_integration.services.sync_runtime_adapter import RuntimeBridgeError, run_pending_sync
    from hero_integration.services.sync_engine import SyncError

    try:
        result = run_pending_sync(generation)
    except (RuntimeBridgeError, SyncError, PermissionError, ValueError) as error:
        frappe.throw(str(error))
    return {
        "status": result.status,
        "plan_key": result.plan_key,
        "generation": result.generation,
        "source_instance": result.source_instance,
    }


@frappe.whitelist()
def sync_hero_preview(
    binding_identity_key: str, mapping_name: str, expected_plan_generation: int | str
) -> dict[str, object]:
    """Build one current, bound-only outbound intent without contacting HERO."""
    frappe.only_for("System Manager")
    context = _sync_hero_context(binding_identity_key, mapping_name, expected_plan_generation)
    from hero_integration.services.sync_engine import Eligibility, sync_hero_eligibility

    eligibility = sync_hero_eligibility(context["intent"])
    return {
        "eligible": eligibility is not Eligibility.NOT_ELIGIBLE,
        "eligibility": eligibility.value,
        "binding_revision": context["binding"].revision,
        "plan_generation": context["executable"].generation,
        "request_fingerprint": context["fingerprint"],
        "effect_summary": "One explicitly confirmed bound-field HERO mutation will be sent once; no automatic retry is performed.",
    }


@frappe.whitelist(methods=["POST"])
def sync_hero_execute(
    binding_identity_key: str,
    mapping_name: str,
    expected_plan_generation: int | str,
    expected_binding_revision: int | str,
    confirmation_fingerprint: str = "",
    confirmed: int | bool = 0,
) -> dict[str, object]:
    """Execute one revalidated B6-bound, schema-approved HERO mutation."""
    frappe.only_for("System Manager")
    context = _sync_hero_context(binding_identity_key, mapping_name, expected_plan_generation)
    binding = context["binding"]
    try:
        binding_revision = int(expected_binding_revision)
    except (TypeError, ValueError):
        frappe.throw("Sync Hero binding revision is invalid.")
    if binding_revision != binding.revision:
        frappe.throw("Sync Hero binding changed; reload its preview.")
    from hero_integration.services.sync_engine import (
        Eligibility,
        OutboundApproval,
        execute_sync_hero,
        sync_hero_eligibility,
    )

    if sync_hero_eligibility(context["intent"]) is Eligibility.NOT_ELIGIBLE:
        frappe.throw("The synchronized binding has no local change to send.")
    if confirmed not in (True, 1, "1", "true", "True") or confirmation_fingerprint != context["fingerprint"]:
        frappe.throw("Explicit confirmation for this exact Sync Hero preview is required.")
    _assert_mutation_execution_released()
    audit = _reserve_mutation(
        operation_name=context["mapping"]["target"]["operation"],
        schema_fingerprint=context["snapshot"].schema_fingerprint,
        mapping_revision=binding.mapping_revision,
        request_fingerprint=context["fingerprint"],
    )
    try:
        approval = OutboundApproval(True, True, True, True, True, binding.acknowledged_version + 1)
        updated = execute_sync_hero(
            context["intent"], approval, _SyncHeroTransport(context, audit), context["bindings"]
        )
    except Exception as error:
        _finalize_mutation(
            audit, "Timeout" if error.__class__.__name__ in {"TimeoutError", "URLError"} else "Failed",
            "Sync Hero outcome is unconfirmed. No automatic retry was performed; inspect the remote effect before another explicit submission.",
        )
        frappe.throw("Sync Hero is blocked or uncertain; no automatic retry was performed.")
    _finalize_mutation(audit, "Succeeded", "Exact B6-bound Sync Hero mutation completed; values were not retained in the audit.")
    return {"status": "SUCCEEDED", "correlation_id": audit.correlation_id, "binding_revision": updated.revision}


def _sync_hero_context(binding_identity_key: str, mapping_name: str, expected_plan_generation: int | str) -> dict[str, object]:
    """Rebuild all outbound inputs from current B6, plan, schema, and document state."""
    from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage
    from hero_integration.services.frappe_writeback import (
        WritebackContractError, build_request_checked, fingerprint_payload, normalise_mapping,
    )
    from hero_integration.services.sync_engine import OutboundIntent
    from hero_integration.services.sync_runtime_adapter import _runtime_plan_service

    try:
        generation = int(expected_plan_generation)
    except (TypeError, ValueError):
        frappe.throw("Sync Hero requires a positive current runtime-plan generation.")
    if generation < 1:
        frappe.throw("Sync Hero requires a positive current runtime-plan generation.")
    site_scope, actor = frappe.local.site, frappe.session.user
    storage = FrappeBindingStorage(frappe)
    bindings = BindingRegistryService(storage)
    binding = storage.field(site_scope, binding_identity_key)
    if binding is None:
        frappe.throw("Sync Hero requires an existing field binding.")
    parent = storage.object(site_scope, binding.object_binding_key)
    if parent is None or parent.source_instance != binding.source_instance:
        frappe.throw("Sync Hero binding has no current source object.")
    executable = _runtime_plan_service(frappe).resolve_executable_plan(
        site_scope, binding.source_instance, generation, False, actor
    )
    if binding.mapping_revision != executable.selection.mapping_revision:
        frappe.throw("Sync Hero binding mapping revision is stale.")
    row = frappe.get_doc("HERO GraphQL Mapping", mapping_name)
    if row.status != "Active" or row.direction != "FRAPPE_TO_MUTATION" or row.source_instance != binding.source_instance:
        frappe.throw("Sync Hero mutation mapping is not active for this source instance.")
    snapshot, schema = _validated_snapshot(row.schema_snapshot)
    if (
        snapshot.name != executable.selection.schema_snapshot
        or snapshot.schema_fingerprint != executable.selection.schema_fingerprint
    ):
        frappe.throw("Sync Hero mutation snapshot does not match the approved runtime plan.")
    try:
        mapping = normalise_mapping(json.loads(row.mapping_json), schema)
    except (TypeError, ValueError, WritebackContractError):
        frappe.throw("Sync Hero mapping is no longer schema-safe.")
    if mapping["source"]["doctype"] != binding.target_doctype or mapping["source"]["field"] != binding.target_field:
        frappe.throw("Sync Hero mapping does not match the bound target field.")
    contracts = frappe.get_all(
        "HERO Mutation Contract",
        filters={"schema_snapshot": snapshot.name, "operation_name": mapping["target"]["operation"], "status": "Active"},
        fields=["name"],
    )
    if len(contracts) != 1:
        frappe.throw("Exactly one active schema-bound HERO Mutation Contract is required for Sync Hero.")
    document = frappe.get_doc(binding.target_doctype, binding.target_name)
    frappe.has_permission(binding.target_doctype, "write", document, throw=True)
    value = document.get(binding.target_field)
    outbound_document, variables = build_request_checked(mapping, schema, parent.external_object_id, value)
    local_fingerprint = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    canonical = fingerprint_payload(
        row.name, int(row.mapping_revision or 1), document.modified, binding.source_instance,
        parent.external_object_id, outbound_document, variables,
    )
    canonical += "|" + binding.identity_key + "|" + str(binding.revision)
    canonical += "|" + executable.plan_key + "|" + str(executable.generation)
    intent = OutboundIntent(
        binding, binding.mapping_revision, executable.selection.schema_fingerprint,
        local_fingerprint, value is not None, outbound_document, variables,
    )
    return {
        "bindings": bindings, "binding": binding, "parent": parent, "executable": executable,
        "snapshot": snapshot, "mapping": mapping, "intent": intent,
        "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(),
    }


class _SyncHeroTransport:
    """One-shot adapter over the existing mutation client; it never retries."""

    def __init__(self, context: dict[str, object], audit) -> None:
        self._context, self._audit = context, audit

    def execute_once(self, document: str, variables: dict[str, object]):
        from hero_integration.client.graphql_mutation import HeroMutationClient
        from hero_integration.client.settings import get_configuration
        from hero_integration.services.sync_engine import OutboundAck

        configuration = get_configuration()
        result = HeroMutationClient(
            configuration.endpoint, lambda: configuration.api_token, configuration.timeout_seconds
        ).execute_once(document, variables)
        mapping, parent, intent = self._context["mapping"], self._context["parent"], self._context["intent"]
        payload = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
        returned = payload.get(mapping["target"]["operation"]) if isinstance(payload, dict) else None
        if not isinstance(returned, dict) or str(returned.get("id")) != str(parent.external_object_id):
            raise RuntimeError("HERO mutation response could not confirm the bound remote identity.")
        source_fingerprint = hashlib.sha256(json.dumps(returned, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return OutboundAck(True, self._audit.correlation_id, source_fingerprint, intent.local_fingerprint)


def _start_migration(
    action: str, dry_run: int | bool, scheduled: bool, graphql_service: str
) -> dict[str, str]:
    if action not in ACTIONS:
        frappe.throw("Unsupported HERO migration action.")
    if not graphql_service:
        frappe.throw("An explicitly approved GraphQL service is required for a new migration run.")
    service, mapping = _approved_read_service(graphql_service)
    is_preview = bool(int(dry_run))
    testing = _testing_preview_configuration() if is_preview else {"enabled": False, "max_actions": 0}
    from hero_integration.services.migration import customer_dependency

    dependencies = (
        customer_dependency(service, mapping)
        if action in {"Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
        else {}
    )
    _require_selected_mappings(action, service, mapping, dependencies)
    if scheduled and _has_active_run():
        return {"status": "SKIPPED_ACTIVE_RUN"}
    run = frappe.get_doc(
        {
            "doctype": "HERO Integration Run",
            "action": action,
            "mode": "Preview" if is_preview else "Execute",
            "graphql_service": service.name,
            "service_revision": service.service_revision,
            "graphql_mapping": mapping.name,
            "mapping_revision": mapping.mapping_revision,
            "testing_enabled": int(testing["enabled"]),
            "testing_max_actions": testing["max_actions"],
            **dependencies,
        }
    )
    run.insert()
    frappe.enqueue(
        "hero_integration.services.migration.execute_run",
        queue="long",
        timeout=1500,
        enqueue_after_commit=True,
        job_id=f"hero-integration:{frappe.local.site}:{run.name}",
        deduplicate=True,
        run_name=run.name,
    )
    return {"status": "QUEUED", "run": run.name}


def _approved_read_service(service_name: str):
    from hero_integration.services.read_contract import ReadContractError, from_documents

    service = frappe.get_doc("HERO GraphQL Service", service_name)
    if not service.read_mapping:
        frappe.throw("The selected GraphQL service has no approved read mapping.")
    mapping = frappe.get_doc("HERO GraphQL Mapping", service.read_mapping)
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    try:
        from_documents(service, mapping, snapshot)
    except (ReadContractError, ValueError) as error:
        frappe.throw(str(error))
    return service, mapping


@frappe.whitelist(methods=["POST"])
def approve_read_service(
    query_id: str,
    mapping_name: str,
    expected_service_revision: int,
    expected_mapping_revision: int,
) -> dict[str, object]:
    """Approve one stored read contract; approval never queues a run."""
    frappe.only_for("System Manager")
    observed_mapping = frappe.get_doc("HERO GraphQL Mapping", mapping_name)
    scope = (observed_mapping.source_instance or '', observed_mapping.schema_snapshot, observed_mapping.direction)
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    lock_exchange_context(*scope)
    mapping = frappe.get_doc("HERO GraphQL Mapping", mapping_name, for_update=True)
    if (mapping.source_instance or '', mapping.schema_snapshot, mapping.direction) != scope:
        frappe.throw('The mapping context changed; reload it before approval.')
    service = frappe.get_doc("HERO GraphQL Service", query_id, for_update=True)
    if int(service.service_revision or 0) != int(expected_service_revision or 0) or int(
        mapping.mapping_revision or 0
    ) != int(expected_mapping_revision or 0):
        frappe.throw("The service or mapping changed; reload it before approval.")
    if (
        service.schema_snapshot != mapping.schema_snapshot
        or service.status != "Validated"
        or mapping.status != "Active"
    ):
        frappe.throw("Only a validated service and active mapping on the same snapshot can be approved.")
    # A changed read binding invalidates approval in the service controller.
    # Persist that binding first, then explicitly approve its resulting state
    # in the same request transaction.
    if service.read_mapping != mapping.name:
        service.read_mapping = mapping.name
        service.save()
    service.execution_status = "Approved"
    service.approved_service_revision = service.service_revision
    service.approved_mapping_revision = mapping.mapping_revision
    config = json.loads(mapping.mapping_json)
    if config.get('native_task_onboarding') is not None:
        from hero_integration.services.mapping_runtime import active_mapping_plan
        from hero_integration.services.read_contract import from_documents
        snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', service.schema_snapshot)
        contract = from_documents(service, mapping, snapshot)
        active_mapping_plan(service.operation_name, definition_ids=contract.definition_ids,
                            target_operation=contract.target_operation, mapping_name=mapping.name, native_task=True)
    service.save()
    return {
        "status": service.execution_status,
        "service_revision": service.service_revision,
        "mapping_revision": mapping.mapping_revision,
    }


@frappe.whitelist(methods=['POST'])
def task_assignment_policy_create(payload_json: str):
    from hero_integration.services.task_assignment_policy import create_policy
    return {'name': create_policy(json.loads(payload_json))}


@frappe.whitelist(methods=['POST'])
def task_assignment_policy_approve(name: str, expected_revision: int):
    from hero_integration.services.task_assignment_policy import approve_policy
    return {'name': approve_policy(name, expected_revision)}


@frappe.whitelist(methods=['POST'])
def task_assignment_policy_revoke(name: str, expected_revision: int):
    from hero_integration.services.task_assignment_policy import revoke_policy
    return {'name': revoke_policy(name, expected_revision)}


@frappe.whitelist(methods=["POST"])
def cancel_run(run_name: str) -> dict[str, str]:
    frappe.only_for("System Manager")
    run = frappe.get_doc("HERO Integration Run", run_name)
    if run.state not in TERMINAL_STATES:
        run.state = "Cancelled"
        run.save()
    return {"status": run.state, "run": run.name}


@frappe.whitelist(methods=["POST"])
def resume_run(run_name: str) -> dict[str, str]:
    frappe.only_for("System Manager")
    run = frappe.get_doc("HERO Integration Run", run_name)
    if run.state not in {"Failed", "Cancelled", "Blocked"}:
        frappe.throw("Only failed, cancelled, or blocked runs can resume.")
    if _as_bool(getattr(run, "testing_enabled", 0)):
        frappe.throw("Testing previews cannot resume because their shared HERO request budget is run-bound.")
    from hero_integration.services.migration import validate_run_mappings

    validate_run_mappings(run)
    run.state = "Queued"
    run.error_message = ""
    run.save()
    frappe.enqueue(
        "hero_integration.services.migration.execute_run",
        queue="long",
        timeout=1500,
        enqueue_after_commit=True,
        job_id=f"hero-integration:{frappe.local.site}:{run.name}",
        deduplicate=True,
        run_name=run.name,
    )
    return {"status": "QUEUED", "run": run.name}


@frappe.whitelist()
def integration_profiles() -> dict[str, str]:
    frappe.only_for("System Manager")
    from hero_integration.integrations.helpdesk.profile import profile_status, ticket_migration_status

    ready, detail = profile_status()
    return {"erpnext": "REQUIRED_INSTALLED", "helpdesk": detail, "tickets": ticket_migration_status()}


@frappe.whitelist()
def integration_overview() -> dict[str, object]:
    """Return a redacted, stored-state-only summary for the HERO Desk overview."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    mapping_counts = {
        state: frappe.db.count("HERO Frappe Mapping", {"status": state})
        for state in ("Draft", "Active", "Disabled", "Rejected")
    }
    return {
        "connection": {
            "endpoint_configured": bool(settings.hero_api_url),
            "allowed_host_count": len([host for host in (settings.allowed_hero_hosts or "").splitlines() if host.strip()]),
            "scheduled_sync_enabled": bool(settings.scheduled_sync_enabled),
            "sync_status": settings.sync_status or "Not started",
            "last_successful_sync": settings.last_successful_sync,
        },
        "snapshot_count": frappe.db.count("HERO GraphQL Schema Snapshot"),
        "service_count": frappe.db.count("HERO GraphQL Service"),
        "mapping_counts": mapping_counts,
        "latest_run": frappe.get_all(
            "HERO Integration Run",
            fields=["name", "action", "state", "modified"],
            order_by="modified desc",
            limit_page_length=1,
        ),
    }


@frappe.whitelist()
def workspace_sync_status() -> dict[str, object]:
    """Return stored run and polling state; this endpoint never contacts HERO."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    state = frappe.get_single("HERO Integration Sync State")
    from hero_integration.services.polling import runtime_plan

    return {
        "polling": runtime_plan(settings),
        "last_successful_sync": getattr(settings, "last_successful_sync", None),
        "sync_status": getattr(settings, "sync_status", "") or "Not started",
        "sync_error": getattr(settings, "sync_error", "") or "",
        "watermarks": {
            "customer": getattr(state, "customer_watermark", "") or "",
            "project": getattr(state, "project_watermark", "") or "",
            "status": getattr(state, "watermark_status", "") or "Not established",
        },
        "runs": frappe.get_all(
            "HERO Integration Run", fields=[
                "name", "action", "mode", "state", "processed", "created_records", "conflict_records", "error_message", "modified",
                "testing_enabled", "testing_max_actions", "testing_executed_actions", "testing_records_received", "testing_status", "testing_results_json",
            ],
            order_by="modified desc", limit_page_length=10,
        ),
        "events": frappe.get_all(
            "HERO Polling Event", fields=["name", "mode", "status", "customer_records", "project_records", "detail", "modified"],
            order_by="modified desc", limit_page_length=10,
        ),
    }
@frappe.whitelist()
def schema_snapshot_list(start: int | str = 0, page_size: int | str = 100) -> list[dict[str, object]]:
    """List saved snapshots; request successive pages until a page is empty."""
    frappe.only_for("System Manager")
    start = _mapping_int(start, "start", 0, 2147483647)
    page_size = _mapping_int(page_size, "page_size", 1, 100)
    return _schema_snapshot_rows(start, page_size)


@frappe.whitelist(methods=["POST"])
def workspace_schema_refresh(connection_id: str = "", binding_generation: int | str = 0) -> dict[str, object]:
    """Capture a new complete, validated live HERO schema snapshot.

    This is intentionally an explicit System Manager action.  Opening a page,
    loading settings, or generating mapping suggestions never invokes it.
    """
    frappe.only_for("System Manager")
    # Refresh always uses persisted server configuration, never a selected snapshot.
    del connection_id, binding_generation
    from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient
    from hero_integration.client.settings import HeroConfigurationError, get_configuration
    from hero_integration.services.introspection import (
        INTROSPECTION_QUERY,
        COMPATIBILITY_INTROSPECTION_QUERY,
        IntrospectionError,
        canonical_schema,
        captured_at_utc,
        endpoint_fingerprint,
        structural_diff,
    )

    try:
        configuration = get_configuration()
        endpoint_hash = endpoint_fingerprint(configuration.endpoint.url, configuration.source_instance)
        client = HeroGraphQLClient(
            configuration.endpoint,
            token_provider=lambda: configuration.api_token,
            timeout=configuration.timeout_seconds,
            max_retries=1,
        )
        try:
            response = client.introspect(INTROSPECTION_QUERY)
        except HeroClientError as error:
            if error.code != "GRAPHQL_ERRORS":
                raise
            response = client.introspect(COMPATIBILITY_INTROSPECTION_QUERY)
        schema = canonical_schema(response)
    except (HeroClientError, HeroConfigurationError, IntrospectionError, ValueError) as error:
        frappe.throw(f"HERO schema refresh failed safely: {error}")

    schema_json = {"__schema": schema}
    from hero_integration.services.graphql_graph import analyze_snapshot

    fingerprint = analyze_snapshot(schema_json).fingerprint
    type_count = len(schema["types"])
    existing = frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        filters={
            "schema_fingerprint": fingerprint,
            "source_instance": configuration.source_instance,
            "endpoint_fingerprint": endpoint_hash,
        },
        fields=["name", "schema_fingerprint", "execution_contract_status"],
        limit_page_length=1,
    )
    if existing:
        row = existing[0]
        return {
            "status": "UNCHANGED",
            "snapshot_name": row["name"],
            "schema_fingerprint": row["schema_fingerprint"],
            "execution_contract_status": row.get("execution_contract_status") or "Compatible",
            "type_count": type_count,
        }

    previous_rows = frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        filters={"source_instance": configuration.source_instance, "endpoint_fingerprint": endpoint_hash},
        fields=["name"], order_by="modified desc", limit_page_length=1,
    )
    previous = frappe.get_doc("HERO GraphQL Schema Snapshot", previous_rows[0]["name"]) if previous_rows else None
    diff = structural_diff(json.loads(previous.snapshot_json) if previous else None, schema)
    captured_at = captured_at_utc()
    snapshot = frappe.get_doc({
        "doctype": "HERO GraphQL Schema Snapshot",
        "snapshot_json": json.dumps(schema_json, ensure_ascii=False, sort_keys=True),
        "source_instance": configuration.source_instance,
        "endpoint_fingerprint": endpoint_hash,
        "schema_version": 1,
        "captured_at": captured_at,
        "structural_diff_json": json.dumps(diff, ensure_ascii=False, sort_keys=True),
        "execution_contract_status": "Blocked" if diff["breaking"] else "Compatible",
    })
    snapshot.insert()
    return {
        "status": "CAPTURED",
        "snapshot_name": snapshot.name,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "execution_contract_status": snapshot.execution_contract_status,
        "captured_at": captured_at,
        "type_count": type_count,
        "structural_diff": diff,
    }


def _schema_snapshot_rows(start: int = 0, page_size: int = 100) -> list[dict[str, object]]:
    return frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        fields=[
            "name", "schema_fingerprint", "analysis_status", "analysis_detail", "modified",
            "source_instance", "endpoint_fingerprint", "captured_at", "execution_contract_status",
        ],
        order_by="modified desc, name asc",
        limit_start=start,
        limit_page_length=page_size,
    )


@frappe.whitelist()
def graphql_service_list() -> list[dict[str, object]]:
    """List saved GraphQL services. Execution remains a separate explicit action."""
    frappe.only_for("System Manager")
    return frappe.get_all(
        "HERO GraphQL Service",
        fields=[
            "name",
            "service_name",
            "operation_name",
            "schema_snapshot",
            "schema_fingerprint",
            "service_revision",
            "status",
            "validation_summary",
            "modified",
        ],
        order_by="modified desc",
        limit_page_length=100,
    )


@frappe.whitelist()
def graphql_service_detail(service_name: str) -> dict[str, object]:
    """Read one saved definition; this endpoint never executes GraphQL."""
    frappe.only_for("System Manager")
    service = frappe.get_doc("HERO GraphQL Service", service_name)
    return {
        "name": service.name,
        "service_name": service.service_name,
        "operation_name": service.operation_name,
        "query_document": service.query_document,
        "schema_snapshot": service.schema_snapshot,
        "schema_fingerprint": service.schema_fingerprint,
        "service_revision": service.service_revision,
        "status": service.status,
        "validation_summary": service.validation_summary,
    }


@frappe.whitelist()
def graph_view(snapshot_name: str) -> dict[str, object]:
    """Return a stored schema graph without contacting HERO."""
    frappe.only_for("System Manager")
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name)
    from hero_integration.services.graphql_graph import analyze_snapshot

    graph = analyze_snapshot(json.loads(snapshot.snapshot_json))
    if graph.fingerprint != snapshot.schema_fingerprint:
        frappe.throw("Stored GraphQL schema fingerprint is stale.")
    return {
        "fingerprint": graph.fingerprint,
        "nodes": graph.nodes,
        "edges": graph.edges,
        "existing_mappings": _existing_mappings(graph.fingerprint),
    }


# The following endpoints are an explicit, finite compatibility boundary for
# the preserved workspace client.  They never proxy to another app or execute
# a stored GraphQL document.  Their input and output shapes intentionally keep
# the workspace usable with a saved Frappe snapshot only.
@frappe.whitelist()
def workspace_context(snapshot_name: str = "") -> dict[str, object]:
    frappe.only_for("System Manager")
    snapshots = [_workspace_snapshot_metadata(row) for row in _schema_snapshot_rows()]
    selected = next((item for item in snapshots if item["name"] == snapshot_name and item["usable"]), None)
    return {
        "connection_id": selected["name"] if selected else "",
        "binding_generation": 0,
        "schema_fingerprint": selected["schema_fingerprint"] if selected else "",
        "enabled": bool(selected),
        "can_write": bool(frappe.has_permission("HERO GraphQL Service", "write")),
        "needs_binding_update": False,
        "mutations_enabled": False,
        "execution_enabled": False,
        "binding_refresh_available": False,
        "schema_refresh_available": True,
        "selected_snapshot": selected["name"] if selected else "",
        "selected_snapshot_metadata": selected,
        "snapshots": snapshots,
    }


def _workspace_snapshot_metadata(row: dict[str, object]) -> dict[str, object]:
    """Expose only selection metadata derived from an already saved snapshot."""
    item = {
        "name": row["name"],
        "schema_fingerprint": row["schema_fingerprint"],
        "analysis_status": row["analysis_status"],
        "modified": row["modified"],
        "usable": False,
        "coverage": {},
        "kind": "Unavailable snapshot",
        "source_instance": row.get("source_instance") or "",
        "captured_at": row.get("captured_at") or row.get("modified") or "",
        "execution_contract_status": row.get("execution_contract_status") or "Compatible",
    }
    if row["analysis_status"] != "Validated":
        return item
    try:
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", row["name"])
        from hero_integration.services.workspace_compat import graph_counts
        from hero_integration.services.graphql_graph import analyze_snapshot

        graph = analyze_snapshot(json.loads(snapshot.snapshot_json))
    except (TypeError, ValueError):
        return item
    if graph.fingerprint != row["schema_fingerprint"]:
        return item
    item["usable"] = True
    item["coverage"] = graph_counts(graph)
    item["kind"] = "Full schema" if item["coverage"]["types"] >= 100 else "Synthetic/test schema"
    return item


@frappe.whitelist()
def workspace_settings_get() -> dict[str, object]:
    """Return editable local settings while never returning the stored token."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    token_configured = bool(settings.get_password("hero_api_token", raise_exception=False))
    stored_credential = settings.get("hero_api_token")
    token_status = "CONFIGURED" if token_configured else (
        "CREDENTIAL_ERROR" if stored_credential else "NOT_CONFIGURED"
    )
    return {
        "enabled": bool(settings.hero_api_url),
        "api_url": settings.hero_api_url or "",
        "allowed_hero_hosts": getattr(settings, "allowed_hero_hosts", "") or "",
        "hero_source_instance": getattr(settings, "hero_source_instance", "default") or "default",
        "erpnext_company": getattr(settings, "erpnext_company", "") or "",
        "erpnext_company_confirmed": bool(getattr(settings, "erpnext_company_confirmed", 0)),
        "allow_deletion": bool(getattr(settings, "allow_deletion", 0)),
        "admin_notification_recipients": getattr(settings, "admin_notification_recipients", "") or "",
        "api_token_configured": token_configured,
        "api_token_status": token_status,
        "timeout_seconds": int(settings.timeout_seconds or 30),
        "testing": _testing_settings(settings),
        "polling": {
            "mode": getattr(settings, "polling_mode", "OFF") or "OFF",
            "interval_minutes": int(getattr(settings, "polling_interval_minutes", 60) or 60),
            "timezone": getattr(settings, "polling_timezone", "UTC") or "UTC",
            "execution_window": getattr(settings, "polling_execution_window", "") or "",
            "data_sources": _polling_lines(getattr(settings, "polling_data_sources", "")),
            "filters": _polling_filters(getattr(settings, "polling_filters_json", "{}")),
            "bootstrap": bool(getattr(settings, "polling_bootstrap", 0)),
            "page_size": int(getattr(settings, "polling_page_size", 100) or 100),
            "pagination_strategy": getattr(settings, "polling_pagination_strategy", "OFFSET") or "OFFSET",
            "allow_backfill": bool(getattr(settings, "polling_allow_backfill", 0)),
            "full_inventory": bool(getattr(settings, "polling_full_inventory", 0)),
            "reconciliation": bool(getattr(settings, "polling_reconciliation", 0)),
            "retry_limit": int(getattr(settings, "polling_retry_limit", 0) or 0),
            "retry_backoff_seconds": int(getattr(settings, "polling_retry_backoff_seconds", 30) or 30),
            "timeout_seconds": int(getattr(settings, "polling_timeout_seconds", settings.timeout_seconds) or settings.timeout_seconds),
            "rate_limit": int(getattr(settings, "polling_rate_limit", 30) or 30),
            "concurrency": int(getattr(settings, "polling_concurrency", 1) or 1),
            "notifications": bool(getattr(settings, "polling_notifications", 0)),
        },
        **_mutation_execution_state(settings),
    }


@frappe.whitelist(methods=["POST"])
def workspace_settings_save(
    enabled: int | bool, api_url: str, api_token: str = "", timeout_seconds: int = 30,
    allowed_hero_hosts: str | None = None, hero_source_instance: str | None = None,
    erpnext_company: str | None = None, erpnext_company_confirmed: int | bool | None = None,
    polling_mode: str | None = None, polling_interval_minutes: int | None = None,
    polling_timezone: str | None = None, polling_execution_window: str | None = None,
    polling_data_sources: str | None = None, polling_filters_json: str | None = None,
    polling_bootstrap: int | bool | None = None, polling_page_size: int | None = None,
    polling_pagination_strategy: str | None = None, polling_allow_backfill: int | bool | None = None,
    polling_full_inventory: int | bool | None = None, polling_reconciliation: int | bool | None = None,
    polling_retry_limit: int | None = None, polling_retry_backoff_seconds: int | None = None,
    polling_timeout_seconds: int | None = None, polling_rate_limit: int | None = None,
    polling_concurrency: int | None = None, polling_notifications: int | bool | None = None,
    allow_deletion: int | bool | None = None, admin_notification_recipients: str | None = None,
    testing_enabled: int | bool | None = None, testing_max_actions: int | str | None = None,
) -> dict[str, object]:
    """Persist only native integration configuration, with DocType validation."""
    frappe.only_for("System Manager")
    if int(timeout_seconds) < 1:
        frappe.throw("Timeout must be at least one second.")
    settings = frappe.get_single("HERO Integration Settings")
    settings.hero_api_url = api_url.strip() if bool(enabled) else ""
    settings.timeout_seconds = int(timeout_seconds)
    if allowed_hero_hosts is not None:
        settings.allowed_hero_hosts = allowed_hero_hosts.strip()
    if hero_source_instance is not None:
        settings.hero_source_instance = hero_source_instance.strip()
    if erpnext_company is not None:
        settings.erpnext_company = erpnext_company.strip()
    if erpnext_company_confirmed is not None:
        settings.erpnext_company_confirmed = int(_as_bool(erpnext_company_confirmed))
    if allow_deletion is not None:
        settings.allow_deletion = int(_as_bool(allow_deletion))
    if admin_notification_recipients is not None:
        recipients = _validated_admin_recipients(admin_notification_recipients)
        settings.admin_notification_recipients = "\n".join(recipients)
    if testing_enabled is not None:
        settings.testing_enabled = int(_as_bool(testing_enabled))
    if testing_max_actions is not None:
        settings.testing_max_actions = _testing_max_actions(testing_max_actions)
    if polling_mode is not None:
        mode = polling_mode.upper().strip()
        if mode not in {"OFF", "MONITOR", "AUTO_SYNC"}:
            frappe.throw("Polling mode must be OFF, MONITOR, or AUTO_SYNC.")
        settings.polling_mode = mode
    if polling_interval_minutes is not None:
        if not 1 <= int(polling_interval_minutes) <= 24 * 60:
            frappe.throw("Polling interval must be between 1 and 1440 minutes.")
        settings.polling_interval_minutes = int(polling_interval_minutes)
    _save_polling_configuration(
        settings,
        timezone=polling_timezone, execution_window=polling_execution_window, data_sources=polling_data_sources,
        filters_json=polling_filters_json, bootstrap=polling_bootstrap, page_size=polling_page_size,
        pagination_strategy=polling_pagination_strategy, allow_backfill=polling_allow_backfill,
        full_inventory=polling_full_inventory, reconciliation=polling_reconciliation, retry_limit=polling_retry_limit,
        retry_backoff_seconds=polling_retry_backoff_seconds, timeout_seconds=polling_timeout_seconds,
        rate_limit=polling_rate_limit, concurrency=polling_concurrency, notifications=polling_notifications,
    )
    if api_token:
        settings.hero_api_token = api_token
    settings.save()
    return workspace_settings_get()


@frappe.whitelist(methods=["POST"])
def workspace_testing_settings_save(
    testing_enabled: int | bool | str, testing_max_actions: int | str,
) -> dict[str, object]:
    """Persist only the bounded, non-executing testing preview controls."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    settings.testing_enabled = int(_as_bool(testing_enabled))
    settings.testing_max_actions = _testing_max_actions(testing_max_actions)
    settings.save()
    return workspace_settings_get()


@frappe.whitelist(methods=["POST"])
def workspace_erpnext_company_save(erpnext_company: str) -> dict[str, object]:
    """Store a Company selection; the DocType invalidates confirmation on change."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    settings.erpnext_company = str(erpnext_company or "").strip()
    settings.save()
    return workspace_settings_get()


@frappe.whitelist(methods=["POST"])
def workspace_erpnext_company_confirm(erpnext_company: str) -> dict[str, object]:
    """Confirm exactly the Company already persisted on the server."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    requested = str(erpnext_company or "").strip()
    stored = str(getattr(settings, "erpnext_company", "") or "").strip()
    if not stored:
        frappe.throw("Save an ERPNext Company before confirming it.")
    if requested != stored:
        frappe.throw("Save the selected ERPNext Company before confirming it.")
    settings.erpnext_company_confirmed = 1
    settings.save()
    return workspace_settings_get()


def _polling_lines(value: object) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _validated_admin_recipients(value: str) -> list[str]:
    recipients = sorted(set(_polling_lines(value)))
    for user in recipients:
        if not frappe.db.get_value("User", user, "enabled") or not frappe.db.exists(
            "Has Role", {"parent": user, "parenttype": "User", "role": "System Manager"}
        ):
            frappe.throw(f"Conflict notification recipient is not an enabled System Manager: {user}")
    return recipients


def _polling_filters(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bounded_int(value: int | str, label: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        frappe.throw(f"{label} must be an integer.")
    if not minimum <= result <= maximum:
        frappe.throw(f"{label} must be between {minimum} and {maximum}.")
    return result


def _testing_max_actions(value: int | str) -> int:
    """Reject booleans, decimals and non-canonical input before persistence."""
    if isinstance(value, bool):
        frappe.throw("Testing Max. Actions must be an integer.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        frappe.throw("Testing Max. Actions must be an integer.")
    if not 1 <= result <= 100:
        frappe.throw("Testing Max. Actions must be between 1 and 100.")
    return result


def _testing_settings(settings) -> dict[str, object]:
    return {
        "enabled": _as_bool(getattr(settings, "testing_enabled", 0)),
        "max_actions": _testing_max_actions(getattr(settings, "testing_max_actions", 5) or 5),
    }


def _testing_preview_configuration() -> dict[str, object]:
    """Freeze the Settings value onto a manually requested Preview run."""
    return _testing_settings(frappe.get_single("HERO Integration Settings"))


def _save_polling_configuration(settings, **values: object) -> None:
    """Validate optional persisted polling controls without scheduling any work."""
    scalar_fields = {
        "timezone": "polling_timezone", "execution_window": "polling_execution_window",
        "data_sources": "polling_data_sources", "pagination_strategy": "polling_pagination_strategy",
    }
    for input_name, fieldname in scalar_fields.items():
        value = values[input_name]
        if value is not None:
            setattr(settings, fieldname, str(value).strip())
    if values["data_sources"] is not None and not _polling_lines(values["data_sources"]):
        frappe.throw("At least one polling data source is required.")
    if values["filters_json"] is not None:
        try:
            parsed = json.loads(str(values["filters_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            frappe.throw("Polling filters must be a JSON object.")
        if not isinstance(parsed, dict):
            frappe.throw("Polling filters must be a JSON object.")
        settings.polling_filters_json = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    if values["pagination_strategy"] is not None and settings.polling_pagination_strategy != "OFFSET":
        frappe.throw("Polling pagination strategy must be OFFSET for the approved HERO V1 reader.")
    numeric = {
        "page_size": ("polling_page_size", "Polling page size", 1, 1000),
        "retry_limit": ("polling_retry_limit", "Polling retry limit", 0, 3),
        "retry_backoff_seconds": ("polling_retry_backoff_seconds", "Polling retry backoff", 0, 3600),
        "timeout_seconds": ("polling_timeout_seconds", "Polling timeout", 1, 120),
        "rate_limit": ("polling_rate_limit", "Polling rate limit", 1, 10000),
        "concurrency": ("polling_concurrency", "Polling concurrency", 1, 1),
    }
    for input_name, (fieldname, label, minimum, maximum) in numeric.items():
        if values[input_name] is not None:
            setattr(settings, fieldname, _bounded_int(values[input_name], label, minimum, maximum))
    for input_name, fieldname in {
        "bootstrap": "polling_bootstrap", "allow_backfill": "polling_allow_backfill",
        "full_inventory": "polling_full_inventory", "reconciliation": "polling_reconciliation",
        "notifications": "polling_notifications",
    }.items():
        if values[input_name] is not None:
            setattr(settings, fieldname, int(_as_bool(values[input_name])))


@frappe.whitelist(methods=["POST"])
def workspace_mutation_execution_set(allow_execution: int | bool) -> dict[str, object]:
    """Persist the global System Manager execution switch; it remains OFF by default."""
    frappe.only_for("System Manager")
    requested = _as_bool(allow_execution)
    settings = frappe.get_single("HERO Integration Settings")
    settings.allow_graphql_mutation_execution = int(requested)
    settings.save()
    return _mutation_execution_state(settings)


def _mutation_execution_state(settings=None) -> dict[str, object]:
    """Report the global gate; individual contracts and confirmations still apply."""
    if settings is None:
        settings = frappe.get_single("HERO Integration Settings")
    requested = _as_bool(getattr(settings, "allow_graphql_mutation_execution", 0))
    enabled = requested
    return {
        "mutation_execution_requested": requested,
        "mutation_execution_released": True,
        "mutation_execution_enabled": enabled,
        "mutation_execution_status": "ENABLED" if enabled else "DISABLED",
        "mutation_execution_reason": (
            "Execution is OFF until a System Manager enables it. Each mutation also requires an active schema-bound "
            "contract, validated variables, an exact preview fingerprint, and a fresh explicit confirmation."
        ),
    }


def _as_bool(value: int | bool | str | None) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off"}:
            return False
        return False
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return False


def _assert_mutation_execution_released() -> None:
    """Single fail-closed global gate for direct and workspace execution facades."""
    if not _mutation_execution_state()["mutation_execution_enabled"]:
        frappe.throw(MUTATION_EXECUTION_DISABLED)


@frappe.whitelist()
def workspace_schema(snapshot_name: str, search: str = "", kind: str = "", start: int = 0, page_size: int = 50) -> dict[str, object]:
    frappe.only_for("System Manager")
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.workspace_compat import schema_projection

    projection = schema_projection(schema, search=search, kind=kind)
    rows = projection.pop("types")
    result = _page(projection, rows, start, page_size)
    result["types"] = result.pop("rows")
    result["schema_fingerprint"] = result["fingerprint"]
    result["complete_analysis"] = True
    result["captured_at"] = snapshot.modified
    return result


@frappe.whitelist()
def schema_operation_catalog(snapshot_name: str, operation_kind: str = "QUERY") -> list[dict[str, object]]:
    """List every operation advertised by the explicitly selected saved schema."""
    frappe.only_for("System Manager")
    _, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.schema_operations import SchemaOperationError, catalog

    try:
        return catalog(schema, operation_kind)
    except SchemaOperationError as error:
        frappe.throw(str(error))


@frappe.whitelist()
def graphql_mapping_list(snapshot_name: str, operation_kind: str = "") -> list[dict[str, object]]:
    """List dynamic mappings for one explicit snapshot; no implicit activation."""
    frappe.only_for("System Manager")
    filters = {"schema_snapshot": snapshot_name}
    if operation_kind:
        filters["operation_kind"] = operation_kind.upper()
    return frappe.get_all(
        "HERO GraphQL Mapping", filters=filters,
        fields=["name", "operation_kind", "operation_name", "direction", "status", "mapping_revision", "confidence", "suggestion_reason"],
        order_by="modified desc",
    )


def _mapping_reader_authorized() -> None:
    """Permit the small employee-facing mapping projection, never raw documents."""
    roles = set(getattr(frappe, "get_roles", lambda: ())())
    if {"Employee", "System Manager"}.isdisjoint(roles):
        frappe.throw(
            "Employee or System Manager authority is required to inspect mapping status.",
            getattr(frappe, "PermissionError", PermissionError),
        )


def _row_value(row: object, field: str, default: object = "") -> object:
    if isinstance(row, dict):
        return row.get(field, default)
    return getattr(row, field, default)


_FRONTEND_MAPPING_KEYS = frozenset({
    "alias", "app", "argument", "doctype", "field", "field_path", "graphql_type", "identity_path", "input_type", "kind", "mapping_definition_id",
    "mapping_definition_ids", "missing", "operation", "operation_name", "path", "purpose", "record_path",
    "records_path", "relation", "result_bindings", "selected_paths", "source", "source_path", "target",
    "target_field", "target_operation", "transform", "transformation", "type",
})
_FRONTEND_SECRET_KEYS = frozenset({
    "api_key", "api_token", "authorization", "credential", "credentials", "endpoint", "password", "payload",
    "secret", "token", "value", "variables",
})


def _sanitized_mapping_definition(raw: object) -> dict[str, object]:
    """Return only structural mapping data suitable for an employee-facing list."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}

    def sanitize(item: object) -> object:
        if isinstance(item, dict):
            result: dict[str, object] = {}
            for key, child in item.items():
                normalized = str(key).lower()
                if normalized in _FRONTEND_SECRET_KEYS or key not in _FRONTEND_MAPPING_KEYS:
                    continue
                result[str(key)] = sanitize(child)
            return result
        if isinstance(item, (list, tuple)):
            return [sanitize(child) for child in item]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return ""

    sanitized = sanitize(value)
    return sanitized if isinstance(sanitized, dict) else {}


@frappe.whitelist(methods=["GET"])
def frontend_mapping_list(search: str = "", start: int | str = 0, page_size: int | str = 50, direction: str = ""):
    """Read-only, redacted mapping/status projection for Employee and System Manager users."""
    _mapping_reader_authorized()
    _mapping_request_fields({"search", "start", "page_size", "direction"})
    search = _mapping_text(search, "search", 120, allow_empty=True)
    direction = _mapping_text(direction, "direction", 40, allow_empty=True)
    if direction not in {"", "QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION"}:
        frappe.throw("MAPPING_INPUT_INVALID: Invalid direction.")
    start, page_size = _mapping_int(start, "start", 0, 10000), _mapping_int(page_size, "page_size", 1, 100)
    graphql_filters = {"status": "Active"}
    if direction:
        graphql_filters["direction"] = direction
    graphql_search = [[field, "like", "%" + search + "%"] for field in ("name", "operation_name")]
    frappe_search = [[field, "like", "%" + search + "%"] for field in ("name", "definition_id", "operation")]
    graphql_rows = frappe.get_all(
        "HERO GraphQL Mapping",
        filters=graphql_filters, or_filters=graphql_search if search else None,
        fields=[
            "name", "definition_id", "schema_snapshot", "schema_fingerprint", "source_instance", "operation_kind", "operation_name",
            "direction", "status", "mapping_revision", "mapping_json",
        ],
        order_by="name asc", limit_start=start, limit=page_size,
    )
    frappe_rows = [] if direction == "FRAPPE_TO_MUTATION" else frappe.get_all(
        "HERO Frappe Mapping",
        filters={"status": "Active"}, or_filters=frappe_search if search else None,
        fields=[
            "name", "definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field",
            "purpose", "transformation", "relation", "priority", "status", "mapping_revision", "schema_snapshot",
            "schema_fingerprint",
        ],
        order_by="name asc", limit_start=start, limit=page_size,
    )
    return {
        "graphql_mappings": [
            {
                "name": _row_value(row, "name"),
                "definition_id": _row_value(row, "definition_id"),
                "schema_snapshot": _row_value(row, "schema_snapshot"),
                "schema_fingerprint": _row_value(row, "schema_fingerprint"),
                "source_instance": _row_value(row, "source_instance"),
                "operation_kind": _row_value(row, "operation_kind"),
                "operation_name": _row_value(row, "operation_name"),
                "direction": _row_value(row, "direction"),
                "status": _row_value(row, "status"),
                "revision": int(_row_value(row, "mapping_revision", 0) or 0),
                "definition": _sanitized_mapping_definition(_row_value(row, "mapping_json")),
            }
            for row in graphql_rows if _row_value(row, "status") == "Active"
        ],
        "frappe_mappings": [
            {
                field: _row_value(row, field)
                for field in (
                    "name", "definition_id", "operation", "source_path", "source_type", "target_doctype",
                    "target_field", "purpose", "transformation", "relation", "priority", "status",
                    "schema_snapshot", "schema_fingerprint",
                )
            }
            | {"revision": int(_row_value(row, "mapping_revision", 0) or 0)}
            for row in frappe_rows if _row_value(row, "status") == "Active"
        ],
        "pagination": {
            "start": start, "page_size": page_size,
            "graphql_total": _active_mapping_count("HERO GraphQL Mapping", graphql_search if search else None, graphql_filters),
            "frappe_total": 0 if direction == "FRAPPE_TO_MUTATION" else _active_mapping_count("HERO Frappe Mapping", frappe_search if search else None),
        },
    }


def _mapping_request_fields(allowed):
    fields = set(getattr(frappe, "form_dict", {}) or {}) - {"cmd", "csrf_token"}
    if fields - allowed:
        frappe.throw("MAPPING_INPUT_INVALID: Unsupported request field.")


def _mapping_text(value, label, maximum=140, allow_empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not value and not allow_empty)
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        frappe.throw(f"MAPPING_INPUT_INVALID: Invalid {label}.")
    return value


def _mapping_int(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, str) and len(value) <= 10 and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        frappe.throw(f"MAPPING_INPUT_INVALID: Invalid {label}.")
    return value


def _active_mapping_count(doctype, or_filters, filters=None):
    rows = frappe.get_all(doctype, filters=filters or {"status": "Active"}, or_filters=or_filters,
                          fields=[{"COUNT": "name", "as": "total"}], limit=1)
    return int(_row_value(rows[0], "total", 0)) if rows else 0


@frappe.whitelist(methods=["GET"])
def frontend_mapping_detail(mapping_type: str, name: str):
    _mapping_reader_authorized()
    _mapping_request_fields({"mapping_type", "name"})
    name = _mapping_text(name, "name")
    doctype = {"graphql": "HERO GraphQL Mapping", "frappe": "HERO Frappe Mapping"}.get(mapping_type)
    if not doctype:
        frappe.throw("MAPPING_INPUT_INVALID: Invalid mapping_type.")
    # Filter before loading or serializing; missing and inactive IDs are identical.
    if not frappe.db.exists(doctype, {"name": name, "status": "Active"}):
        frappe.throw("Active mapping not found.", frappe.DoesNotExistError)
    row = frappe.get_doc(doctype, name)
    if row.status != "Active":
        frappe.throw("Active mapping not found.", frappe.DoesNotExistError)
    if mapping_type == "graphql":
        fields = ("name", "definition_id", "schema_snapshot", "schema_fingerprint", "source_instance", "operation_kind", "operation_name", "direction", "status")
        return {field: row.get(field) for field in fields} | {
            "revision": int(row.mapping_revision or 0),
            "definition": _sanitized_mapping_definition(row.mapping_json),
        }
    fields = ("name", "definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field",
              "purpose", "transformation", "relation", "priority", "status", "schema_snapshot", "schema_fingerprint")
    return {field: row.get(field) for field in fields} | {"revision": int(row.mapping_revision or 0)}


@frappe.whitelist(methods=["GET"])
def frontend_mapping_bindings(
    definition_id: str, start: int | str = 0, page_size: int | str = 20,
) -> dict[str, object]:
    """Read the existing, site-scoped binding contract without changing it.

    Bindings contain target-record identities and are consequently an
    administrative read surface.  The Mapping editor uses this narrow
    projection rather than a new binding model or direct DocType CRUD.
    """
    frappe.only_for("System Manager")
    _mapping_request_fields({"definition_id", "start", "page_size"})
    definition_id = _mapping_text(definition_id, "definition_id")
    start, page_size = _mapping_int(start, "start", 0, 10000), _mapping_int(page_size, "page_size", 1, 100)
    filters = {"site_scope": frappe.local.site, "mapping_definition_id": definition_id}
    rows = frappe.get_all(
        "HERO Field Binding", filters=filters,
        fields=[
            "identity_key", "object_binding", "source_instance", "source_field", "target_app",
            "target_doctype", "target_name", "target_field", "mapping_definition_id",
            "mapping_revision", "direction", "binding_revision", "sync_state", "acknowledged_version",
        ],
        order_by="identity_key asc", limit_start=start, limit=page_size,
    )
    object_keys = sorted({str(_row_value(row, "object_binding")) for row in rows if _row_value(row, "object_binding")})
    objects = frappe.get_all(
        "HERO Object Binding",
        filters={"site_scope": frappe.local.site, "identity_key": ["in", object_keys]},
        fields=[
            "identity_key", "source_instance", "object_type", "external_object_id", "target_app",
            "target_doctype", "target_name", "mapping_definition_id", "mapping_revision",
            "binding_revision", "lifecycle",
        ],
        order_by="identity_key asc",
    ) if object_keys else []
    parent_by_key = {
        str(_row_value(row, "identity_key")): {
            field: _row_value(row, field)
            for field in (
                "identity_key", "source_instance", "object_type", "external_object_id", "target_app",
                "target_doctype", "target_name", "mapping_definition_id", "mapping_revision",
                "binding_revision", "lifecycle",
            )
        }
        for row in objects
    }
    item_fields = (
        "identity_key", "object_binding", "source_instance", "source_field", "target_app", "target_doctype",
        "target_name", "target_field", "mapping_definition_id", "mapping_revision", "direction",
        "binding_revision", "sync_state", "acknowledged_version",
    )
    return {
        "items": [
            {field: _row_value(row, field) for field in item_fields}
            | {"object": parent_by_key.get(str(_row_value(row, "object_binding")))}
            for row in rows
        ],
        "pagination": {
            "start": start, "page_size": page_size,
            "total": int(frappe.db.count("HERO Field Binding", filters=filters)),
        },
    }


def _personal_draft_identity(definition_id):
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_draft_service import draft_key
    definition_id = _mapping_text(definition_id, "definition_id")
    return definition_id, draft_key(frappe.local.site, definition_id, frappe.session.user)


def _personal_draft_document(definition_id):
    definition_id, key = _personal_draft_identity(definition_id)
    # No open_draft call: a read must never create a draft or change generation.
    if not frappe.db.exists("HERO Mapping Draft", {"name": key, "definition_id": definition_id,
            "site_scope": frappe.local.site, "owner_user": frappe.session.user}):
        return None
    doc = frappe.get_doc("HERO Mapping Draft", key)
    doc.check_permission("read")
    return doc


def _personal_draft_warnings(raw):
    try:
        value = json.loads(raw) if raw else {}
        warnings = value.get('warnings') if isinstance(value, dict) else None
        if isinstance(warnings, list) and 0 < len(warnings) <= 100 and all(isinstance(w, str) and 0 < len(w) <= 1000 for w in warnings):
            return warnings
    except (ValueError, TypeError):
        pass
    return []


def _personal_draft_result(value):
    from dataclasses import asdict
    result = asdict(value)
    result.pop("base_content", None)
    result.pop("reconciliation", None)
    result["content_json"] = json.dumps(value.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["base_content_json"] = json.dumps(value.base_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) if value.base_content is not None else None
    result["reconciliation_json"] = json.dumps(value.reconciliation, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) if value.reconciliation is not None else None
    warning, diagnostics = frappe.db.get_value("HERO Mapping Draft", value.key, ["warning_fingerprint", "diagnostics_json"])
    result["warning_fingerprint"] = warning or ""
    result["warnings"] = _personal_draft_warnings(diagnostics)
    return result


def _personal_draft_doc_result(doc):
    if doc is None:
        return {"draft": None}
    return {"draft": {
        "key": doc.draft_key, "site_scope": doc.site_scope, "definition_id": doc.definition_id, "owner": doc.owner_user,
        "generation": int(doc.generation), "state": doc.state, "content": json.loads(doc.content_json),
        "content_json": doc.content_json,
        "content_hash": doc.content_hash, "content_revision": int(doc.content_revision),
        "base_active_name": doc.base_active_mapping, "base_active_revision": int(doc.base_active_revision),
        "base_active_hash": doc.base_active_hash, "base_schema_fingerprint": doc.base_schema_fingerprint,
        "base_content_json": doc.base_content_json or None,
        "reconciliation_revision": int(doc.reconciliation_revision or 0),
        "reconciliation_json": doc.reconciliation_json or None,
        "warning_fingerprint": doc.warning_fingerprint or "",
        "warnings": _personal_draft_warnings(doc.diagnostics_json),
    }}


@contextmanager
def _personal_draft_errors():
    from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError
    try:
        yield
    except DraftConflict:
        frappe.throw("MAPPING_DRAFT_CONFLICT: Reload the current personal draft.", frappe.TimestampMismatchError)
    except PermissionError:
        frappe.throw("Personal mapping draft access denied.", frappe.PermissionError)
    except MappingDraftError:
        frappe.throw("MAPPING_DRAFT_INVALID: Invalid content or unavailable active definition.")


@frappe.whitelist(methods=["POST"])
def mapping_draft_create(definition_id: str):
    _mapping_request_fields({"definition_id"})
    definition_id, _ = _personal_draft_identity(definition_id)
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    with _personal_draft_errors():
        draft = FrappeMappingDraftStorage().service().open_draft(definition_id, frappe.session.user)
    return {"draft": _personal_draft_result(draft)}


@frappe.whitelist(methods=["GET"])
def mapping_draft_read(definition_id: str):
    _mapping_request_fields({"definition_id"})
    return _personal_draft_doc_result(_personal_draft_document(definition_id))


@frappe.whitelist(methods=["GET"])
def mapping_draft_exchange_export(source_instance: str, snapshot_name: str, direction: str) -> dict[str, str]:
    """Export only the current administrator's OPEN drafts for one direction."""
    frappe.only_for("System Manager")
    _mapping_request_fields({"source_instance", "snapshot_name", "direction"})
    source_instance = _mapping_text(source_instance, "source_instance")
    snapshot_name = _mapping_text(snapshot_name, "snapshot_name")
    from hero_integration.services.mapping_draft_exchange import MappingDraftExchangeError, export_package

    try:
        content = export_package(source_instance, snapshot_name, direction)
    except MappingDraftExchangeError as error:
        frappe.throw(str(error))
    suffix = "query" if direction == "QUERY_TO_FRAPPE" else "mutation"
    return {"filename": f"hero-personal-{suffix}-drafts.json", "content": content}


@frappe.whitelist(methods=["POST"])
def mapping_draft_exchange_preview(payload_json: str, source_instance: str, snapshot_name: str, direction: str) -> dict[str, object]:
    """Read-only validation for a directional personal-Draft replacement."""
    frappe.only_for("System Manager")
    _mapping_request_fields({"payload_json", "source_instance", "snapshot_name", "direction"})
    source_instance = _mapping_text(source_instance, "source_instance")
    snapshot_name = _mapping_text(snapshot_name, "snapshot_name")
    from hero_integration.services.mapping_draft_exchange import MappingDraftExchangeError, preview_import

    try:
        return preview_import(payload_json, source_instance, snapshot_name, direction)
    except MappingDraftExchangeError as error:
        frappe.throw(str(error))


@frappe.whitelist(methods=["POST"])
def mapping_draft_exchange_save(payload_json: str, source_instance: str, snapshot_name: str, direction: str,
                                expected_states_json: str, preview_fingerprint: str = "", confirmed=False) -> dict[str, object]:
    """Persist a confirmed directional replacement as the current actor's Drafts only."""
    frappe.only_for("System Manager")
    _mapping_request_fields({"payload_json", "source_instance", "snapshot_name", "direction", "expected_states_json", "preview_fingerprint", "confirmed"})
    source_instance = _mapping_text(source_instance, "source_instance")
    snapshot_name = _mapping_text(snapshot_name, "snapshot_name")
    try:
        expected_states = json.loads(expected_states_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw("MAPPING_INPUT_INVALID: expected draft states must be JSON.")
    from hero_integration.services.mapping_draft_exchange import MappingDraftExchangeError, save_import
    from hero_integration.services.schema_provisioning import SchemaProvisioningError

    try:
        return save_import(payload_json, source_instance, snapshot_name, direction, expected_states,
                           confirmed=confirmed in (True, 1, "1", "true", "True"), preview_fingerprint=preview_fingerprint)
    except (MappingDraftExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


def _personal_draft_content(raw):
    try:
        valid_size = isinstance(raw, str) and len(raw.encode("utf-8")) <= 65536
    except UnicodeError:
        valid_size = False
    if not valid_size:
        frappe.throw("MAPPING_INPUT_INVALID: mapping_json must be JSON text of at most 65536 bytes.")
    try:
        def pairs(items):
            value = {}
            for key, child in items:
                if key in value:
                    raise ValueError("Duplicate JSON key")
                value[key] = child
            return value
        def invalid_constant(value):
            raise ValueError("Non-finite JSON constant")
        content = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(content, dict):
            raise ValueError
        remaining = 4096
        def bound(value, depth=0):
            nonlocal remaining
            remaining -= 1
            if depth > 16 or remaining < 0:
                raise ValueError
            if isinstance(value, dict):
                for child in value.values(): bound(child, depth + 1)
            elif isinstance(value, list):
                for child in value: bound(child, depth + 1)
        bound(content)
        return content
    except (ValueError, TypeError, RecursionError):
        frappe.throw("MAPPING_INPUT_INVALID: Invalid or over-complex mapping JSON object.")


def _personal_draft_json_object(raw, label, maximum=65536):
    try:
        valid_size = isinstance(raw, str) and len(raw.encode("utf-8")) <= maximum
    except UnicodeError:
        valid_size = False
    if not valid_size:
        frappe.throw(f"MAPPING_INPUT_INVALID: {label} must be JSON text within its size limit.")
    try:
        def pairs(items):
            value = {}
            for key, child in items:
                if key in value:
                    raise ValueError("Duplicate JSON key")
                value[key] = child
            return value
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw(f"MAPPING_INPUT_INVALID: Invalid {label} JSON.")
    if not isinstance(value, dict):
        frappe.throw(f"MAPPING_INPUT_INVALID: {label} must be a JSON object.")
    return value


def _reconciliation_preview_result(value):
    return {
        "definition_id": value.definition_id,
        "generation": value.generation,
        "draft_revision": value.draft_revision,
        "reconciliation_revision": value.reconciliation_revision,
        "base": {"name": value.base_active_name, "revision": value.base_active_revision, "hash": value.base_active_hash},
        "active": {"name": value.active_name, "revision": value.active_revision, "hash": value.active_hash,
                   "content_hash": value.active_content_hash},
        "candidate_content_json": json.dumps(value.candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
        "items": [{"identifier": item.identifier, "kind": item.kind, "state": item.state,
                    "base_json": item.base_json, "active_json": item.active_json,
                    "draft_json": item.draft_json, "identities": list(item.identities)} for item in value.items],
    }


def _reconciliation_values(raw, label):
    value = _personal_draft_json_object(raw, label, 32768)
    return value


@frappe.whitelist(methods=["POST"])
def mapping_draft_save(definition_id: str, expected_generation: int | str,
                       expected_draft_revision: int | str, expected_active_revision: int | str, mapping_json: str,
                       expected_reconciliation_revision: int | str = 0, reconciliation_json: str = ""):
    _mapping_request_fields({"definition_id", "expected_generation", "expected_draft_revision", "expected_active_revision", "mapping_json", "expected_reconciliation_revision", "reconciliation_json"})
    definition_id, _ = _personal_draft_identity(definition_id)
    generation = _mapping_int(expected_generation, "expected_generation", 1)
    revision = _mapping_int(expected_draft_revision, "expected_draft_revision")
    active = _mapping_int(expected_active_revision, "expected_active_revision", 1)
    reconciliation_revision = _mapping_int(expected_reconciliation_revision, "expected_reconciliation_revision")
    content = _personal_draft_content(mapping_json)
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    from hero_integration.services.mapping_draft_service import reconciliation_proof_from_json
    with _personal_draft_errors():
        proof = reconciliation_proof_from_json(reconciliation_json) if reconciliation_json else None
        result = FrappeMappingDraftStorage().service().save(definition_id, frappe.session.user, revision, active,
                                                          content, expected_generation=generation,
                                                          expected_reconciliation_revision=reconciliation_revision,
                                                          reconciliation=proof)
    return {"draft": _personal_draft_result(result.draft), "unchanged": result.unchanged,
            "reconciliation_updated": result.reconciliation_updated}


@frappe.whitelist(methods=['POST'])
def mapping_draft_publish(definition_id: str, expected_generation: int | str,
                          expected_draft_revision: int | str, expected_active_revision: int | str,
                          expected_reconciliation_revision: int | str, confirmed=False, warning_fingerprint: str = ''):
    _mapping_request_fields({'definition_id', 'expected_generation', 'expected_draft_revision',
                             'expected_active_revision', 'expected_reconciliation_revision', 'confirmed', 'warning_fingerprint'})
    definition_id, _ = _personal_draft_identity(definition_id)
    generation = _mapping_int(expected_generation, 'expected_generation', 1)
    revision = _mapping_int(expected_draft_revision, 'expected_draft_revision', 1)
    active = _mapping_int(expected_active_revision, 'expected_active_revision', 1)
    reconciliation_revision = _mapping_int(expected_reconciliation_revision, 'expected_reconciliation_revision')
    if not isinstance(warning_fingerprint, str) or len(warning_fingerprint) > 140:
        frappe.throw('MAPPING_INPUT_INVALID: Invalid warning fingerprint.')
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    with _personal_draft_errors():
        return FrappeMappingDraftStorage().publish(definition_id, frappe.session.user, generation, revision, active,
            reconciliation_revision, confirmed=confirmed in (True, 1, '1', 'true', 'True'),
            warning_fingerprint=warning_fingerprint)


def _reconciliation_request(definition_id, expected_generation, expected_draft_revision,
                            expected_active_revision, expected_reconciliation_revision, candidate_json):
    definition_id, _ = _personal_draft_identity(definition_id)
    generation = _mapping_int(expected_generation, "expected_generation", 1)
    revision = _mapping_int(expected_draft_revision, "expected_draft_revision")
    active = _mapping_int(expected_active_revision, "expected_active_revision", 1)
    reconciliation_revision = _mapping_int(expected_reconciliation_revision, "expected_reconciliation_revision")
    candidate = _personal_draft_content(candidate_json)
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    service = FrappeMappingDraftStorage().service()
    preview = service.reconcile_preview(definition_id, frappe.session.user, revision, active, candidate,
                                        expected_generation=generation,
                                        expected_reconciliation_revision=reconciliation_revision)
    return service, preview


@frappe.whitelist(methods=["POST"])
def mapping_draft_reconcile_preview(definition_id: str, expected_generation: int | str,
                                    expected_draft_revision: int | str, expected_active_revision: int | str,
                                    expected_reconciliation_revision: int | str, candidate_json: str):
    _mapping_request_fields({"definition_id", "expected_generation", "expected_draft_revision", "expected_active_revision", "expected_reconciliation_revision", "candidate_json"})
    with _personal_draft_errors():
        _, preview = _reconciliation_request(definition_id, expected_generation, expected_draft_revision,
                                             expected_active_revision, expected_reconciliation_revision, candidate_json)
    return _reconciliation_preview_result(preview)


@frappe.whitelist(methods=["POST"])
def mapping_draft_reconcile_apply(definition_id: str, expected_generation: int | str,
                                  expected_draft_revision: int | str, expected_active_revision: int | str,
                                  expected_reconciliation_revision: int | str, candidate_json: str,
                                  accepted_suggestions_json: str, conflict_choices_json: str):
    _mapping_request_fields({"definition_id", "expected_generation", "expected_draft_revision", "expected_active_revision", "expected_reconciliation_revision", "candidate_json", "accepted_suggestions_json", "conflict_choices_json"})
    with _personal_draft_errors():
        service, preview = _reconciliation_request(definition_id, expected_generation, expected_draft_revision,
                                                    expected_active_revision, expected_reconciliation_revision, candidate_json)
        accepted = _reconciliation_values(accepted_suggestions_json, "accepted reconciliation suggestions")
        if set(accepted) != {"items"} or not isinstance(accepted["items"], list):
            frappe.throw("MAPPING_INPUT_INVALID: Invalid accepted reconciliation suggestions.")
        choices = _reconciliation_values(conflict_choices_json, "reconciliation conflict choices")
        result = service.reconcile_apply(preview, accepted_suggestions=accepted["items"], conflict_choices=choices)
        from hero_integration.services.mapping_draft_service import reconciliation_proof_json
        result_content_json = json.dumps(result.content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"preview": _reconciliation_preview_result(preview), "result_content": result.content,
                "result_content_json": result_content_json, "reconciliation_json": reconciliation_proof_json(result.proof)}


@frappe.whitelist(methods=["GET"])
def mapping_draft_revisions(definition_id: str, start: int | str = 0, page_size: int | str = 20, revision_name: str = ""):
    _mapping_request_fields({"definition_id", "start", "page_size", "revision_name"})
    definition_id, key = _personal_draft_identity(definition_id)
    start, page_size = _mapping_int(start, "start", 0, 10000), _mapping_int(page_size, "page_size", 1, 100)
    revision_name = _mapping_text(revision_name, "revision_name", allow_empty=True)
    filters = {"draft_key": key, "actor": frappe.session.user}
    if revision_name:
        filters["name"] = revision_name
    rows = frappe.get_all("HERO Mapping Revision", filters=filters,
        fields=["name", "generation", "revision", "kind", "actor", "content_json", "content_hash",
                "base_active_mapping", "base_active_revision", "base_active_hash", "base_schema_fingerprint"],
        order_by="generation desc, revision desc", limit_start=start, limit=page_size)
    if revision_name and not rows:
        frappe.throw("Personal mapping revision not found.", frappe.DoesNotExistError)
    for row in rows:
        row["content"] = json.loads(row["content_json"])
    return {"items": rows, "total": frappe.db.count("HERO Mapping Revision", filters), "start": start, "page_size": page_size}


@frappe.whitelist(methods=["POST"])
def mapping_draft_discard(definition_id: str, expected_generation: int | str, expected_draft_revision: int | str, confirmed=False):
    _mapping_request_fields({"definition_id", "expected_generation", "expected_draft_revision", "confirmed"})
    definition_id, _ = _personal_draft_identity(definition_id)
    generation = _mapping_int(expected_generation, "expected_generation", 1)
    revision = _mapping_int(expected_draft_revision, "expected_draft_revision")
    if confirmed not in (True, "true", "1"):
        frappe.throw("MAPPING_CONFIRMATION_REQUIRED: Explicit discard confirmation is required.")
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    with _personal_draft_errors():
        draft = FrappeMappingDraftStorage().service().discard(definition_id, frappe.session.user, generation, revision, confirmed=True)
    return {"draft": _personal_draft_result(draft)}


def _required_revision(value: int | str, label: str) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError):
        frappe.throw(f"{label} is invalid.")
    if isinstance(value, bool) or revision < 1:
        frappe.throw(f"{label} is invalid.")
    return revision


def _mapping_semantic_content(item, *, snapshot_name: str, operation_kind: str, operation_name: str, direction: str, parsed: dict[str, object]) -> bool:
    """Compare canonical, executable mapping fields; UI-only suggestion metadata is excluded."""
    try:
        stored = json.loads(item.mapping_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        item.schema_snapshot == snapshot_name
        and item.operation_kind == operation_kind
        and item.operation_name == operation_name
        and item.direction == direction
        and stored == parsed
    )


def _unique_mapping_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Writeback mapping contains duplicate JSON object keys.")
        result[key] = value
    return result


def _mapping_values_preserved(original, normalized):
    if isinstance(original, dict):
        return isinstance(normalized, dict) and all(
            key in normalized and _mapping_values_preserved(value, normalized[key])
            for key, value in original.items()
        )
    if isinstance(original, list):
        return isinstance(normalized, list) and len(original) == len(normalized) and all(
            _mapping_values_preserved(value, replacement) for value, replacement in zip(original, normalized)
        )
    return type(original) is type(normalized) and original == normalized


def _mapping_save_result(item, *, unchanged: bool) -> dict[str, object]:
    return {
        "name": item.name,
        "status": item.status,
        "revision": int(item.mapping_revision or 1),
        "schema_fingerprint": item.schema_fingerprint,
        "unchanged": unchanged,
        "activation_required": item.status == "Draft",
    }


def _lock_mapping_save_context(snapshot_name, direction):
    """Acquire the exchange lock before mapping rows or writeback field locks."""
    snapshot, _ = _validated_snapshot(snapshot_name)
    source = getattr(snapshot, 'source_instance', '') or ''
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    lock_exchange_context(source, snapshot_name, direction)
    current, schema = _validated_snapshot(snapshot_name, for_update=True)
    if (getattr(current, 'source_instance', '') or '') != source or current.schema_fingerprint != snapshot.schema_fingerprint:
        frappe.throw('The mapping source context changed; retry the save.')
    return schema


@frappe.whitelist(methods=["POST"])
def graphql_mapping_save(
    snapshot_name: str, operation_kind: str, operation_name: str, direction: str, mapping_json: str,
    mapping_name: str = "", expected_revision: int | str = 0, activate: int | bool = 0,
    suggestion_reason: str | None = None, confidence: float | None = None,
) -> dict[str, object]:
    """Persist a revision-checked Draft, or explicitly activate its current owner's Draft."""
    frappe.only_for("System Manager")
    activate_requested = _as_bool(activate)
    try:
        parsed = json.loads(mapping_json, object_pairs_hook=_unique_mapping_object if direction == "FRAPPE_TO_MUTATION" else None)
        if not isinstance(parsed, dict):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        if direction == "FRAPPE_TO_MUTATION" and str(error) == "Writeback mapping contains duplicate JSON object keys.":
            frappe.throw(str(error))
        frappe.throw("GraphQL mapping JSON must be an object.")
    if direction not in {"QUERY_TO_FRAPPE", "FRAPPE_TO_MUTATION"}:
        frappe.throw("Unsupported GraphQL mapping direction.")
    schema = _lock_mapping_save_context(snapshot_name, direction)
    if direction == "FRAPPE_TO_MUTATION":
        submitted_mapping = parsed
        from hero_integration.services.frappe_mapping_catalog import validate_draft_writeback_source
        from hero_integration.services.frappe_writeback import (
            WritebackContractError, draft_source_type_compatible, normalise_draft_mapping, normalise_mapping,
        )

        try:
            if activate_requested:
                parsed = normalise_mapping(parsed, schema)
            else:
                try:
                    parsed = normalise_mapping(parsed, schema)
                except WritebackContractError:
                    parsed = normalise_draft_mapping(parsed, schema)
                    source_field = validate_draft_writeback_source(
                        parsed["source"]["doctype"], parsed["source"]["field"], _FrappePickerMetadata(),
                    )
                    if not draft_source_type_compatible(source_field.fieldtype, parsed["target"]["graphql_type"]):
                        frappe.throw("The selected Frappe source and HERO scalar or enum input types are incompatible.")
            if not _mapping_values_preserved(submitted_mapping, parsed):
                frappe.throw("Writeback mapping contains parameters that would not be preserved.")
            source = parsed["source"]
        except (ValueError, WritebackContractError) as error:
            frappe.throw(str(error))
        if not mapping_name:
            # Serialize only API-created drafts sharing this exact source.
            # The existing controller lock is connection-scoped and released
            # at the full transaction boundary, including rollback.
            from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
            _lock_active_target("writeback:" + snapshot_name, source["doctype"], direction + ":" + source["field"])
            matches = []
            rows = frappe.db.sql(
                "SELECT name, mapping_json FROM `tabHERO GraphQL Mapping` "
                "WHERE schema_snapshot = %s AND direction = %s FOR UPDATE",
                (snapshot_name, direction), as_dict=True,
            )
            for row in rows:
                try:
                    stored = json.loads(row.mapping_json, object_pairs_hook=_unique_mapping_object)
                    stored_source = stored.get("source") if isinstance(stored, dict) else None
                except (TypeError, ValueError):
                    stored_source = None
                if not isinstance(stored_source, dict) or not all(isinstance(stored_source.get(key), str) for key in ("doctype", "field")):
                    frappe.throw("Existing writeback mapping source cannot be safely inspected.")
                if all(stored_source[key] == source[key] for key in ("doctype", "field")):
                    if stored_source != source:
                        frappe.throw("Existing writeback source contains parameters that cannot be safely reconciled.")
                    matches.append(row.name)
            if len(matches) > 1:
                frappe.throw("More than one stored writeback mapping owns this Frappe field; resolve the duplicate before saving.")
            mapping_name = matches[0] if matches else ""
    if not mapping_name:
        item = frappe.new_doc("HERO GraphQL Mapping")
    else:
        item = frappe.get_doc("HERO GraphQL Mapping", mapping_name, for_update=True)
    if mapping_name and item.schema_snapshot != snapshot_name:
        frappe.throw("The GraphQL mapping belongs to another schema snapshot.")
    if mapping_name and item.direction != direction:
        frappe.throw("An existing GraphQL mapping cannot change direction.")
    if mapping_name:
        expected = _required_revision(expected_revision, "GraphQL mapping revision")
        if expected != int(item.mapping_revision or 0):
            frappe.throw("GraphQL mapping revision is stale; reload the current Draft.")
    elif expected_revision not in (0, "", None):
        frappe.throw("A new GraphQL mapping cannot specify an existing revision.")

    if mapping_name and direction == "FRAPPE_TO_MUTATION" and item.direction == direction:
        try:
            stored_mapping = json.loads(item.mapping_json, object_pairs_hook=_unique_mapping_object)
            try:
                normalized_stored = normalise_mapping(stored_mapping, schema)
            except WritebackContractError:
                normalized_stored = normalise_draft_mapping(stored_mapping, schema)
        except (TypeError, ValueError, WritebackContractError):
            frappe.throw("Existing writeback mapping cannot be safely replaced; preserve its parameters.")
        if not _mapping_values_preserved(stored_mapping, normalized_stored):
            frappe.throw("Existing writeback mapping contains parameters that would not be preserved.")

    writeback_scope = direction == "FRAPPE_TO_MUTATION" or (mapping_name and item.direction == "FRAPPE_TO_MUTATION")
    if mapping_name and writeback_scope and item.status == "Draft" and item.modified_by != frappe.session.user:
        message = ("Only the administrator who last changed this Draft may activate it." if activate_requested
                   else "Foreign legacy mapping drafts cannot be overwritten.")
        frappe.throw(message, frappe.PermissionError)

    canonical_kind = operation_kind.upper()
    content_unchanged = mapping_name and _mapping_semantic_content(
        item,
        snapshot_name=snapshot_name,
        operation_kind=canonical_kind,
        operation_name=operation_name,
        direction=direction,
        parsed=parsed,
    )
    reason = (suggestion_reason[:1000] if suggestion_reason is not None
              else (item.suggestion_reason or "") if mapping_name else "")
    score = (max(0, min(float(confidence or 0), 1)) if confidence is not None
             else float(item.confidence or 0) if mapping_name else 0)
    if not writeback_scope:
        reason = suggestion_reason[:1000] if suggestion_reason is not None else ""
        score = max(0, min(float(confidence or 0), 1))
    unchanged = bool(content_unchanged and (not writeback_scope or (reason == (item.suggestion_reason or "")
                     and score == float(item.confidence or 0))))
    if mapping_name and writeback_scope and content_unchanged and not unchanged and item.status != "Draft":
        frappe.throw("Review metadata can only be edited on a Draft.")
    if activate_requested:
        if mapping_name and not unchanged:
            frappe.throw("Save GraphQL mapping changes as a Draft before activation.")
        if mapping_name and item.status == "Draft" and item.modified_by != frappe.session.user:
            frappe.throw("Only the administrator who last changed this Draft may activate it.")
        if mapping_name and item.status not in {"Draft", "Active"}:
            frappe.throw("Only a current Draft may be activated.")
        if mapping_name and item.status == "Active":
            return _mapping_save_result(item, unchanged=True)
        if mapping_name:
            item.status = "Active"
            item.save()
            return _mapping_save_result(item, unchanged=False)
    if mapping_name and unchanged:
        return _mapping_save_result(item, unchanged=True)

    item.schema_snapshot = snapshot_name
    item.operation_kind = canonical_kind
    item.operation_name = operation_name
    item.direction = direction
    item.mapping_json = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    item.suggestion_reason = reason
    item.confidence = score
    if activate_requested:
        item.status = "Active"
    elif not content_unchanged:
        item.status = "Draft"
    item.save()
    return _mapping_save_result(item, unchanged=False)


def _runtime_plan_result(plan) -> dict[str, object]:
    selection = plan.selection
    return {
        "state": plan.state.value,
        "plan_key": plan.plan_key,
        "source_instance": selection.source_instance,
        "revision": plan.revision,
        "generation": plan.generation,
        "execution_mode": selection.execution_mode,
        "operation_name": selection.operation_name,
        "graphql_service": selection.graphql_service,
        "service_revision": selection.service_revision,
        "graphql_mapping": selection.graphql_mapping,
        "mapping_revision": selection.mapping_revision,
        "schema_snapshot": selection.schema_snapshot,
        "schema_fingerprint": selection.schema_fingerprint,
        "approved_by": plan.approved_by,
    }


@frappe.whitelist(methods=["GET"])
def runtime_plan_status(source_instance: str) -> dict[str, object]:
    """Return redacted lifecycle status through the canonical runtime-plan service."""
    frappe.only_for("System Manager")
    from hero_integration.services.sync_runtime_adapter import _runtime_plan_service

    from hero_integration.services.runtime_plan import RuntimePlanConflict, RuntimePlanError

    try:
        plan = _runtime_plan_service(frappe).current(frappe.local.site, source_instance)
    except (RuntimePlanConflict, RuntimePlanError, PermissionError, ValueError) as error:
        frappe.throw(str(error))
    if plan is None:
        return {"state": "NOT_FOUND", "source_instance": source_instance}
    return _runtime_plan_result(plan)


@frappe.whitelist(methods=["POST"])
def runtime_plan_approve(plan_key: str, expected_revision: int | str) -> dict[str, object]:
    """Approve exactly one current Draft through the B8 CAS lifecycle owner."""
    frappe.only_for("System Manager")
    from hero_integration.services.runtime_plan import RuntimePlanConflict, RuntimePlanError
    from hero_integration.services.sync_runtime_adapter import _runtime_plan_service

    try:
        plan = _runtime_plan_service(frappe).approve(
            plan_key,
            expected_revision=_required_revision(expected_revision, "Runtime plan revision"),
            actor=frappe.session.user,
        )
    except (RuntimePlanConflict, RuntimePlanError, PermissionError, ValueError) as error:
        frappe.throw(str(error))
    return _runtime_plan_result(plan)


@frappe.whitelist()
def workspace_graph(snapshot_name: str, start: int = 0, page_size: int = 200, node_id: str = "") -> dict[str, object]:
    frappe.only_for("System Manager")
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.workspace_compat import graph_projection, node_projection

    if node_id:
        result = node_projection(schema, node_id)
        result["schema_fingerprint"] = result.pop("fingerprint", snapshot.schema_fingerprint)
        return result
    projection = graph_projection(schema)
    nodes = projection.pop("nodes")
    result = _page(projection, nodes, start, page_size)
    result["nodes"] = result.pop("rows")
    fingerprint = result.pop("fingerprint")
    result["schema_fingerprint"] = fingerprint
    result["existing_mappings"] = _existing_mappings(snapshot.schema_fingerprint)
    return result


@frappe.whitelist()
def workspace_service_list(snapshot_name: str, operation_kind: str = "query", start: int = 0, page_size: int = 50) -> dict[str, object]:
    frappe.only_for("System Manager")
    _assert_service_kind(operation_kind)
    rows = [
        _workspace_service(row)
        for row in frappe.get_all(
            "HERO GraphQL Service",
            filters={"schema_snapshot": snapshot_name},
            fields=["name", "service_name", "operation_name", "query_document", "service_revision", "status", "validation_summary"],
            order_by="modified desc",
        )
    ]
    return _page({}, [row for row in rows if row["service_kind"] == operation_kind], start, page_size)


@frappe.whitelist()
def workspace_service_detail(query_id: str) -> dict[str, object]:
    frappe.only_for("System Manager")
    return {"query": _workspace_service(frappe.get_doc("HERO GraphQL Service", query_id).as_dict())}


@frappe.whitelist(methods=["POST"])
def workspace_service_save(snapshot_name: str, payload_json: str, query_id: str = "", expected_revision: int = 0) -> dict[str, object]:
    frappe.only_for("System Manager")
    payload = _workspace_payload(payload_json, allow_generated=True)
    kind = str(payload.get("operation_kind", "query"))
    _assert_service_kind(kind)
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mutation_draft import operation_kind as document_kind, validate as validate_mutation_draft

    if 'mapping_generation' in payload or 'personal_mapping_generation' in payload:
        if kind != 'query' or payload.get('query'):
            frappe.throw('Generated service input requires query direction and no supplied document.')
        if 'mapping_generation' in payload and 'personal_mapping_generation' in payload:
            frappe.throw('Select exactly one mapping generation source.')
        if 'personal_mapping_generation' in payload:
            request = _workspace_personal_task_query(snapshot, payload['personal_mapping_generation'])
        else:
            request = _workspace_generated_query(snapshot, payload['mapping_generation']).request
        payload['query'] = request.document
        payload['operation_name'] = request.operation_name
    if document_kind(payload["query"]) != kind:
        frappe.throw("The declared GraphQL service kind does not match the document.")
    if kind == "mutation":
        validate_mutation_draft(schema, payload["query"])
    if query_id:
        service = frappe.get_doc("HERO GraphQL Service", query_id)
        if service.schema_snapshot != snapshot.name:
            frappe.throw("The saved service belongs to another schema snapshot.")
        if int(service.service_revision or 0) != int(expected_revision or 0):
            frappe.throw("The saved service has changed; reload it before saving.")
    else:
        service = frappe.new_doc("HERO GraphQL Service")
        service.schema_snapshot = snapshot.name
    service.service_name = payload["name"]
    service.operation_name = payload["operation_name"] or payload["name"]
    service.query_document = payload["query"]
    service.status = "Draft"
    service.save()
    return {"query": _workspace_service(service.as_dict())}


@frappe.whitelist(methods=["POST"])
def workspace_service_validate(snapshot_name: str, payload_json: str = "", query_id: str = "", expected_revision: int = 0) -> dict[str, object]:
    frappe.only_for("System Manager")
    if not query_id:
        payload = _workspace_payload(payload_json)
        kind = str(payload.get("operation_kind", "query"))
        _assert_service_kind(kind)
        _snapshot, schema = _validated_snapshot(snapshot_name)
        if kind == "mutation":
            from hero_integration.services.mutation_draft import validate as validate_mutation_draft

            detail = validate_mutation_draft(schema, payload["query"])
            return {
                "valid": True, "can_execute": False, "mutation": detail,
                "reason": "Mutation drafts are local-only; HERO mutation execution is disabled.",
            }
        from hero_integration.client.graphql import assert_read_only

        assert_read_only(payload["query"])
        return {"valid": True, "can_execute": False, "reason": "Saved read-only definitions can be validated; remote execution is disabled in this workspace."}
    service = frappe.get_doc("HERO GraphQL Service", query_id)
    if service.schema_snapshot != snapshot_name or int(service.service_revision or 0) != int(expected_revision or 0):
        frappe.throw("The saved service changed or belongs to another schema snapshot.")
    from hero_integration.services.mutation_draft import operation_kind as document_kind, validate as validate_mutation_draft

    if document_kind(service.query_document) == "mutation":
        _snapshot, schema = _validated_snapshot(snapshot_name)
        detail = validate_mutation_draft(schema, service.query_document)
        return {
            "valid": True, "can_execute": False, "mutation": detail, "query": _workspace_service(service.as_dict()),
            "reason": "Mutation drafts are local-only; HERO mutation execution is disabled.",
        }
    service.status = "Validated"
    service.save()
    return {"valid": True, "can_execute": False, "query": _workspace_service(service.as_dict()), "reason": "Remote execution is disabled in this workspace."}


@frappe.whitelist()
def workspace_mutation_draft_validate(snapshot_name: str, payload_json: str) -> dict[str, object]:
    """Validate a mutation draft against stored introspection only; never persist or execute it."""
    frappe.only_for("System Manager")
    payload = _workspace_payload(payload_json)
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mutation_draft import validate

    detail = validate(schema, payload["query"])
    return {
        "valid": True,
        "can_execute": False,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "mutation": detail,
        "reason": "Mutation draft is validated only against the saved schema; HERO mutation execution is disabled.",
    }


@frappe.whitelist(methods=["POST"])
def workspace_service_delete(query_id: str, expected_revision: int = 0) -> dict[str, str]:
    frappe.only_for("System Manager")
    service = frappe.get_doc("HERO GraphQL Service", query_id)
    if int(service.service_revision or 0) != int(expected_revision or 0):
        frappe.throw("The saved service has changed; reload it before deleting.")
    from hero_integration.services.guards import assert_deletion_allowed

    assert_deletion_allowed(action="GraphQL service deletion")
    frappe.delete_doc("HERO GraphQL Service", query_id)
    return {"status": "DELETED"}


def _page(base: dict[str, object], rows: list[object], start: int | str, page_size: int | str) -> dict[str, object]:
    offset, limit = max(0, int(start or 0)), min(max(1, int(page_size or 50)), 500)
    return {**base, "rows": rows[offset:offset + limit], "has_more": offset + limit < len(rows), "total": len(rows)}


def _workspace_payload(payload_json: str, *, allow_generated: bool = False) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as error:
        frappe.throw(f"Service payload must be JSON: {error.msg}")
    if not isinstance(payload, dict):
        frappe.throw("Service payload must be an object.")
    fields = ("name",) if allow_generated and ({'mapping_generation', 'personal_mapping_generation'} & set(payload)) else ("name", "query")
    for field in fields:
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            frappe.throw(f"Service {field} is required.")
    if payload.get("operation_name") is not None and not isinstance(payload["operation_name"], str):
        frappe.throw("Service operation name must be text.")
    return payload


def _workspace_generated_query(snapshot, specification):
    """Generate a Draft service from the current registry; this grants no read authority."""
    required = {'registry_revision', 'definition_ids', 'operation', 'arguments', 'identity_reference'}
    allowed = required | {'alias', 'pagination_argument'}
    if not isinstance(specification, dict) or not required <= set(specification) or set(specification) - allowed:
        frappe.throw('Invalid generated mapping service specification.')
    ids = specification['definition_ids']
    if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(x, str) or not x for x in ids):
        frappe.throw('Generated mapping service requires selected definition IDs.')
    from hero_integration.services.frappe_field_metadata_provider import FrappeV16FieldMetadataProvider
    from hero_integration.services.frappe_field_registry import build_frappe_field_registry
    from hero_integration.services.mapping_contract_v1 import StoredSnapshot
    from hero_integration.services.mapping_registry_service import MappingRegistryService
    from hero_integration.services.mapping_registry_storage import FrappeMappingRegistryStorage
    from hero_integration.services.schema_registry import build_schema_registry

    stored = StoredSnapshot(snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint,
                            snapshot.source_instance, snapshot.analysis_status)
    registry = build_schema_registry(stored)
    fields = build_frappe_field_registry(frappe.local.site, FrappeV16FieldMetadataProvider(frappe))
    service = MappingRegistryService(FrappeMappingRegistryStorage(frappe), registry, fields)
    return service.compile_active_query(stored, expected_revision=specification['registry_revision'],
        definition_ids=tuple(ids), operation=specification['operation'], arguments=specification['arguments'],
        identity_reference=specification['identity_reference'], alias=specification.get('alias'),
        pagination_argument=specification.get('pagination_argument'))


def _workspace_personal_task_query(snapshot, specification):
    required = {'definition_id', 'generation', 'draft_revision', 'active_revision', 'reconciliation_revision'}
    if not isinstance(specification, dict) or set(specification) != required:
        frappe.throw('Invalid personal Task generation specification.')
    definition_id, _ = _personal_draft_identity(specification['definition_id'])
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    with _personal_draft_errors():
        return FrappeMappingDraftStorage().materialize_native_task(
            definition_id, frappe.session.user,
            _mapping_int(specification['generation'], 'generation', 1),
            _mapping_int(specification['draft_revision'], 'draft_revision', 1),
            _mapping_int(specification['active_revision'], 'active_revision', 1),
            _mapping_int(specification['reconciliation_revision'], 'reconciliation_revision'), snapshot_name=snapshot.name)


def _workspace_service(service: dict[str, object]) -> dict[str, object]:
    from hero_integration.services.mutation_draft import operation_kind

    return {
        "query_id": service["name"], "name": service["service_name"], "operation_name": service["operation_name"],
        "query": service["query_document"], "revision": int(service.get("service_revision") or 1),
        "provenance": "HERO Integration", "service_kind": operation_kind(service["query_document"]), "status": service.get("status"),
        "validation_summary": service.get("validation_summary"), "variables": {},
    }


def _assert_service_kind(operation_kind: str) -> None:
    if operation_kind not in {"query", "mutation"}:
        frappe.throw("Only GraphQL query and local-only mutation draft definitions are supported.")


@frappe.whitelist()
def preview_direct_mapping(operation: str, source_record_json: str) -> dict[str, object]:
    """Show an active explicit mapping plan. This endpoint never writes documents."""
    frappe.only_for("System Manager")
    try:
        record = json.loads(source_record_json)
    except json.JSONDecodeError as error:
        frappe.throw(f"Source record must be JSON: {error.msg}")
    if not isinstance(record, dict):
        frappe.throw("Source record must be a JSON object.")
    from hero_integration.services.mapping_runtime import MappingPlanError, active_mapping_plan

    try:
        plan = active_mapping_plan(operation)
        values = plan.values(record)
    except MappingPlanError as error:
        frappe.throw(str(error))
    return {"operation": operation, "values": values, "writes": False}


@frappe.whitelist(methods=['GET'])
def mapping_object_catalog(snapshot_name: str, object_type: str = '', search: str = '',
                           start: int | str = 0, page_size: int | str = 50) -> dict[str, object]:
    """Inspect schema objects and relation paths, without authorizing a fetch."""
    frappe.only_for('System Manager')
    _mapping_request_fields({'snapshot_name', 'object_type', 'search', 'start', 'page_size'})
    snapshot_name = _mapping_text(snapshot_name, 'snapshot_name')
    object_type = _mapping_text(object_type, 'object_type', 140, allow_empty=True)
    search = _mapping_text(search, 'search', 120, allow_empty=True)
    start, page_size = _mapping_int(start, 'start', 0, 10000), _mapping_int(page_size, 'page_size', 1, 50)
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.object_mapping_catalog import catalog
    try:
        data = catalog(schema, object_type, search)
    except ValueError as error:
        frappe.throw(str(error))
    entries = data['items']
    return {'objects': data['objects'], 'areas': data['areas'], 'items': entries[start:start + page_size], 'start': start,
            'page_size': page_size, 'total': len(entries), 'has_more': start + page_size < len(entries),
            'source_instance': snapshot.source_instance, 'snapshot_name': snapshot.name,
            'schema_fingerprint': snapshot.schema_fingerprint, 'execution_authorized': False}


@frappe.whitelist()
def mapping_source_picker(snapshot_name: str, operation: str, search: str = "") -> list[dict[str, object]]:
    frappe.only_for("System Manager")
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mapping_suggestions import source_fields

    mappings_by_path: dict[str, list[dict[str, object]]] = {}
    for mapping in _existing_mappings(snapshot.schema_fingerprint, operation):
        mappings_by_path.setdefault(mapping["source_path"], []).append(mapping)
    result = []
    for item in source_fields(operation, schema, search):
        payload = item.__dict__.copy()
        payload["existing_mappings"] = mappings_by_path.get(item.path, [])
        result.append(payload)
    return result


@frappe.whitelist()
def mapping_target_catalog(app_name: str = "", search: str = "") -> dict[str, object]:
    """Discover installed-app targets without granting an external-app write path."""
    frappe.only_for("System Manager")
    from hero_integration.services.frappe_mapping_catalog import catalog

    return catalog(_FrappePickerMetadata(), app_name, search)


@frappe.whitelist(methods=["GET"])
def mapping_target_catalog_page(
    app_name: str = "", doctype: str = "", search: str = "", start: int | str = 0, page_size: int | str = 50,
) -> dict[str, object]:
    """Page the existing installed-app field catalog without suggesting a mapping."""
    frappe.only_for("System Manager")
    _mapping_request_fields({"app_name", "doctype", "search", "start", "page_size"})
    app_name = _mapping_text(app_name, "app_name", 140, allow_empty=True)
    doctype = _mapping_text(doctype, "doctype", 140, allow_empty=True)
    search = _mapping_text(search, "search", 120, allow_empty=True)
    start, page_size = _mapping_int(start, "start", 0, 10000), _mapping_int(page_size, "page_size", 1, 50)
    from hero_integration.services.frappe_mapping_catalog import fields

    entries = fields(_FrappePickerMetadata(), app_name, doctype, search)
    return {
        "items": [entry.__dict__ for entry in entries[start:start + page_size]],
        "total": len(entries), "start": start, "page_size": page_size,
        "has_more": start + page_size < len(entries),
    }


@frappe.whitelist()
def writeback_mapper_catalog(
    snapshot_name: str, app_name: str = "", source_doctype: str = "",
) -> dict[str, object]:
    """Return independent Frappe source pickers without widening writeback."""
    frappe.only_for("System Manager")
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.frappe_mapping_catalog import doctypes as catalog_doctypes, writeback_fields
    from hero_integration.services.frappe_writeback import DEFINITIONS, WritebackContractError, reviewed_input_target

    metadata = _FrappePickerMetadata()
    allowed = [item for item in catalog_doctypes(metadata) if item.selectable]
    apps = sorted({item.app_name for item in allowed if item.app_name})
    candidates = [item for item in allowed if (not app_name or item.app_name == app_name)
                  and (not source_doctype or item.doctype == source_doctype)]
    if source_doctype and not candidates:
        frappe.throw("The selected source is not available.", frappe.PermissionError)
    doctypes = [item.__dict__ for item in candidates]
    source_fields: list[dict[str, object]] = []
    source_candidates = (field for field in writeback_fields(metadata, targets=candidates) if field.selectable)
    for field in source_candidates:
        payload = field.__dict__.copy()
        doctype = field.doctype
        fieldname = field.path
        try:
            if doctype in DEFINITIONS and fieldname in DEFINITIONS[doctype].fields:
                payload["target"] = reviewed_input_target(doctype, fieldname, schema)
        except WritebackContractError as error:
            payload["selectable"] = False
            payload["reason"] = str(error)
        if payload["selectable"]:
            source_fields.append(payload)
    contract_rows = frappe.get_all(
        "HERO Mutation Contract",
        filters={"schema_snapshot": snapshot.name, "operation_name": ["in", [item.operation for item in DEFINITIONS.values()]]},
        fields=["name", "operation_name", "status", "contract_revision", "graphql_service"],
        order_by="modified desc",
    )
    contracts = []
    for operation in sorted({item.operation for item in DEFINITIONS.values()}):
        rows = [row for row in contract_rows if row.operation_name == operation]
        active = [row for row in rows if row.status == "Active"]
        if len(rows) == 1 and len(active) == 1:
            state = {"activation_ready": True, "blocker": ""}
        elif len(rows) == 1:
            state = {"activation_ready": False, "blocker": f"Technical contract {rows[0].name} is {rows[0].status}; it must be approved before this mapping can be activated."}
        elif not rows:
            state = {"activation_ready": False, "blocker": "No schema-bound technical mutation contract has been prepared for this operation."}
        else:
            state = {"activation_ready": False, "blocker": "Multiple technical mutation contracts exist for this operation; activation remains fail-closed."}
        contracts.append({"operation": operation, "contracts": rows, **state})
    return {"apps": apps, "doctypes": doctypes, "fields": source_fields, "contracts": contracts}


@frappe.whitelist()
def mapping_target_picker(
    operation: str, search: str = "", app_name: str = "", target_doctype: str = "",
) -> list[dict[str, object]]:
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_suggestions import target_fields

    return [item.__dict__ for item in target_fields(operation, _FrappePickerMetadata(), search, app_name, target_doctype)]


@frappe.whitelist()
def mapping_source_picker_page(
    snapshot_name: str, operation: str, search: str = "", start: int = 0, page_size: int = 50,
) -> dict[str, object]:
    """Return at most 50 searchable HERO source fields for the mapping picker."""
    frappe.only_for("System Manager")
    _, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mapping_suggestions import picker_page, source_fields

    return picker_page(source_fields(operation, schema, search), start, page_size)


@frappe.whitelist()
def mapping_target_picker_page(
    operation: str, search: str = "", app_name: str = "", target_doctype: str = "",
    start: int = 0, page_size: int = 50,
) -> dict[str, object]:
    """Return at most 50 fields from the selected Frappe target DocType."""
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_suggestions import picker_page, target_fields

    return picker_page(target_fields(operation, _FrappePickerMetadata(), search, app_name, target_doctype), start, page_size)


@frappe.whitelist()
def mapping_suggestions(
    snapshot_name: str, operation: str, source_path: str = "", target_field: str = "", target_doctype: str = "",
) -> list[dict[str, object]]:
    frappe.only_for("System Manager")
    _, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mapping_suggestions import suggest_from_source, suggest_from_target

    metadata = _FrappePickerMetadata()
    if source_path:
        return suggest_from_source(operation, source_path, schema, metadata, target_doctype=target_doctype)
    if target_field:
        return suggest_from_target(operation, target_field, schema, metadata, target_doctype=target_doctype)
    frappe.throw("Choose a source path or a target field.")


@frappe.whitelist()
def preview_mapping_candidate(
    snapshot_name: str, operation: str, source_path: str, target_field: str, source_record_json: str,
    target_doctype: str = "",
) -> dict[str, object]:
    """Preview one suggested draft mapping without persisting a mapping or a document."""
    frappe.only_for("System Manager")
    _validated_snapshot(snapshot_name)
    try:
        record = json.loads(source_record_json)
    except json.JSONDecodeError as error:
        frappe.throw(f"Source record must be JSON: {error.msg}")
    if not isinstance(record, dict):
        frappe.throw("Source record must be a JSON object.")
    from hero_integration.services.direct_mapping import MappingSpec, SOURCE_CONTRACTS, source_value, transformed_value
    from hero_integration.services.frappe_mapping_catalog import validate_draft_target

    contract = SOURCE_CONTRACTS.get(operation)
    if not contract or source_path not in contract["paths"]:
        frappe.throw("Source path is not selected by the approved HERO operation.")
    target_doctype = target_doctype or contract["target_doctype"]
    spec = MappingSpec(operation, source_path, target_doctype, target_field, contract["paths"][source_path])
    try:
        target = validate_draft_target(operation, spec.source_type, target_doctype, target_field, _FrappePickerMetadata())
        value = transformed_value(spec, source_value(record, source_path))
    except ValueError as error:
        frappe.throw(str(error))
    return {
        "operation": operation,
        "source_path": source_path,
        "target_field": target_field,
        "target_doctype": target_doctype,
        "target_app": target.app_name,
        "values": {target_field: value} if value not in (None, "") else {},
        "writes": False,
        "activation": "Draft only; explicit review is required. Non-canonical app targets cannot be activated by this app.",
    }


@frappe.whitelist()
def export_mapping_json(snapshot_name: str) -> dict[str, str]:
    """Return deterministic mapping JSON without credentials or migrated data."""
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_exchange import export_json

    return {"filename": "hero-frappe-mapping.json", "content": export_json(snapshot_name)}


@frappe.whitelist()
def preview_mapping_import(snapshot_name: str, payload_json: str) -> dict[str, object]:
    """Legacy preview endpoint. It returns the current shared checklist."""
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_exchange import MappingExchangeError
    from hero_integration.services.schema_provisioning import SchemaProvisioningError, preview_package

    try:
        return preview_package(snapshot_name, payload_json)
    except (MappingExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


@frappe.whitelist()
def import_mapping_json(snapshot_name: str, payload_json: str) -> dict[str, object]:
    """Legacy endpoint retained as a non-mutating checklist preview.

    Mapping packages now use ``adopt_mapping_package`` after one explicit
    selection and confirmation. This endpoint cannot bypass that workflow.
    """
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_exchange import MappingExchangeError
    from hero_integration.services.schema_provisioning import SchemaProvisioningError, preview_package

    try:
        return {"status": "PREVIEW_ONLY", "checklist": preview_package(snapshot_name, payload_json)}
    except (MappingExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


@frappe.whitelist()
def preview_mapping_package(snapshot_name: str, payload_json: str) -> dict[str, object]:
    """Return a non-mutating, checked checklist for schema and mapping adoption."""
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_exchange import MappingExchangeError
    from hero_integration.services.schema_provisioning import SchemaProvisioningError, preview_package

    try:
        return preview_package(snapshot_name, payload_json)
    except (MappingExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


@frappe.whitelist(methods=["POST"])
def adopt_mapping_package(
    snapshot_name: str, payload_json: str, selected_ids_json: str, confirmed: bool = False, preview_revision: str = "",
) -> dict[str, object]:
    """Provision checked HERO-owned schema, then activate the selected mappings."""
    frappe.only_for("System Manager")
    try:
        selected_ids = json.loads(selected_ids_json)
    except json.JSONDecodeError as error:
        frappe.throw(f"Selected checklist items must be JSON: {error.msg}")
    if not isinstance(selected_ids, list) or not all(isinstance(value, str) for value in selected_ids):
        frappe.throw("Selected checklist items must be a JSON list of IDs.")
    from hero_integration.services.mapping_exchange import MappingExchangeError
    from hero_integration.services.schema_provisioning import SchemaProvisioningError

    try:
        if confirmed not in (True, 1, "1", "true", "True"):
            raise SchemaProvisioningError("Administrator confirmation is required before adoption.")
        from hero_integration.services.adoption import start
        return start(snapshot_name, payload_json, selected_ids, preview_revision)
    except (MappingExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


@frappe.whitelist(methods=["POST"])
def resume_mapping_adoption(adoption_name: str) -> dict[str, object]:
    """Retry the exact checked package from a failed adoption record."""
    frappe.only_for("System Manager")
    from hero_integration.services.adoption import execute
    from hero_integration.services.mapping_exchange import MappingExchangeError
    from hero_integration.services.schema_provisioning import SchemaProvisioningError

    try:
        return execute(adoption_name)
    except (MappingExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


@frappe.whitelist()
def workspace_mutation_preview(contract_name: str, variables_json: str) -> dict[str, object]:
    """Return a redacted, exact-effect preview.  It never contacts HERO or writes state."""
    frappe.only_for("System Manager")
    contract, service, snapshot, variables, request_fingerprint = _validated_mutation_request(contract_name, variables_json)
    return {
        "can_execute": bool(_mutation_execution_state()["mutation_execution_enabled"] and contract.status == "Active"),
        "operation": service.operation_name,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "mapping_revision": int(contract.contract_revision or 1),
        "request_fingerprint": request_fingerprint,
        "effect_summary": "One explicitly approved HERO mutation will be sent once; no automatic retry is performed.",
        "variables_validated": sorted(variables),
    }


@frappe.whitelist(methods=["POST"])
def workspace_mutation_contract_save(
    service_name: str, mapping_json: str, contract_name: str = "", activate: int | bool = 0,
) -> dict[str, object]:
    """Create or revise a per-operation draft contract; activation is explicit."""
    frappe.only_for("System Manager")
    service = frappe.get_doc("HERO GraphQL Service", service_name)
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    if snapshot.analysis_status != "Validated" or snapshot.execution_contract_status == "Blocked":
        frappe.throw("A compatible validated schema snapshot is required.")
    try:
        mapping = json.loads(mapping_json)
        if not isinstance(mapping, dict) or mapping.get("mode") not in {"ADMIN_INPUT", "VALIDATED_RESOLVER"}:
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        frappe.throw("Mutation mapping must be an object with mode ADMIN_INPUT or VALIDATED_RESOLVER.")
    contract = frappe.get_doc("HERO Mutation Contract", contract_name) if contract_name else frappe.new_doc("HERO Mutation Contract")
    if contract_name and contract.graphql_service != service.name:
        frappe.throw("The mutation contract belongs to another GraphQL service.")
    contract.graphql_service = service.name
    contract.mapping_json = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
    contract.status = "Active" if activate in (True, 1, "1", "true", "True") else "Draft"
    contract.save()
    return {
        "contract_name": contract.name, "status": contract.status, "operation": contract.operation_name,
        "schema_fingerprint": contract.schema_fingerprint, "revision": int(contract.contract_revision or 1),
    }


@frappe.whitelist(methods=["POST"])
def execute_graphql_service(
    service_name: str, variables_json: str = "{}", confirmation_fingerprint: str = "", confirmed: int | bool = 0,
) -> dict[str, object]:
    """Execute exactly one contract-bound mutation after all server-side gates pass."""
    frappe.only_for("System Manager")
    _assert_mutation_execution_released()
    contract, service, snapshot, variables, request_fingerprint = _validated_mutation_request(service_name, variables_json)
    if contract.status != "Active":
        frappe.throw("The mutation contract is not active.")
    if confirmed not in (True, 1, "1", "true", "True") or confirmation_fingerprint != request_fingerprint:
        frappe.throw("Explicit confirmation for this exact mutation preview is required.")
    audit = _reserve_mutation(
        operation_name=service.operation_name,
        schema_fingerprint=snapshot.schema_fingerprint,
        mapping_revision=int(contract.contract_revision or 1),
        request_fingerprint=request_fingerprint,
    )
    try:
        from hero_integration.client.graphql_mutation import HeroMutationClient
        from hero_integration.client.settings import get_configuration

        configuration = get_configuration()
        HeroMutationClient(configuration.endpoint, lambda: configuration.api_token, configuration.timeout_seconds).execute_once(
            service.query_document, variables
        )
    except Exception as error:
        _finalize_mutation(
            audit,
            "Timeout" if error.__class__.__name__ in {"TimeoutError", "URLError"} else "Failed",
            "HERO mutation did not complete. No automatic retry was performed; inspect the remote effect before another explicit submission.",
        )
        frappe.throw("HERO mutation failed safely; no automatic retry was performed.")
    _finalize_mutation(audit, "Succeeded", "HERO mutation transport completed; remote response data is intentionally not persisted.")
    return {"status": "SUCCEEDED", "correlation_id": audit.correlation_id, "operation": service.operation_name}


@frappe.whitelist()
def writeback_available(doctype: str, docname: str) -> dict[str, object]:
    """List active, link-bound field actions without exposing write variables."""
    frappe.only_for("System Manager")
    document = frappe.get_doc(doctype, docname)
    frappe.has_permission(doctype, "write", document, throw=True)
    rows = frappe.get_all(
        "HERO GraphQL Mapping",
        filters={"direction": "FRAPPE_TO_MUTATION", "status": "Active"},
        fields=["name", "direction", "status", "mapping_json", "mapping_revision", "schema_snapshot", "source_instance"],
    )
    actions = []
    for row in rows:
        try:
            context = _writeback_context(row.name, document, mapping_row=row)
        except Exception:
            # Availability is deliberately quiet: a stale or ambiguous link
            # must never reveal a send action, but it is explained on preview
            # once an administrator selects a configured field.
            continue
        actions.append({
            "mapping_name": row.name,
            "field": context["mapping"]["source"]["field"],
            "hero_field": context["mapping"]["target"]["field"],
            "operation": context["mapping"]["target"]["operation"],
        })
    return {"actions": actions}


@frappe.whitelist()
def writeback_preview(mapping_name: str, doctype: str, docname: str) -> dict[str, object]:
    """Return one server-built, exact preview; this never contacts HERO."""
    frappe.only_for("System Manager")
    document = frappe.get_doc(doctype, docname)
    frappe.has_permission(doctype, "write", document, throw=True)
    context = _writeback_context(mapping_name, document)
    mapping = context["mapping"]
    return {
        "can_execute": bool(_mutation_execution_state()["mutation_execution_enabled"]),
        "request_fingerprint": context["fingerprint"],
        "target": {"source_instance": context["link"].source_instance, "external_id": context["link"].source_id},
        "source": {"doctype": doctype, "document": document.name, "field": mapping["source"]["field"], "value": document.get(mapping["source"]["field"])},
        "hero": {"operation": mapping["target"]["operation"], "field": mapping["target"]["field"], "value": context["value"]},
        "effect_summary": "One approved HERO field mutation will be sent once after explicit confirmation. No automatic retry is performed.",
    }


@frappe.whitelist(methods=["POST"])
def writeback_execute(mapping_name: str, doctype: str, docname: str, confirmation_fingerprint: str = "", confirmed: int | bool = 0) -> dict[str, object]:
    """Send exactly one revalidated Customer/Project writeback mutation."""
    frappe.only_for("System Manager")
    _assert_mutation_execution_released()
    document = frappe.get_doc(doctype, docname)
    frappe.has_permission(doctype, "write", document, throw=True)
    context = _writeback_context(mapping_name, document)
    if confirmed not in (True, 1, "1", "true", "True") or confirmation_fingerprint != context["fingerprint"]:
        frappe.throw("Explicit confirmation for this exact writeback preview is required.")
    audit = _reserve_mutation(
        operation_name=context["mapping"]["target"]["operation"],
        schema_fingerprint=context["snapshot"].schema_fingerprint,
        mapping_revision=int(context["mapping_row"].mapping_revision or 1),
        request_fingerprint=context["fingerprint"],
    )
    try:
        from hero_integration.client.graphql_mutation import HeroMutationClient
        from hero_integration.client.settings import get_configuration

        configuration = get_configuration()
        result = HeroMutationClient(configuration.endpoint, lambda: configuration.api_token, configuration.timeout_seconds).execute_once(
            context["document"], context["variables"]
        )
        returned = (result.get("data") or {}).get(context["mapping"]["target"]["operation"]) if isinstance(result, dict) else None
        if not isinstance(returned, dict) or str(returned.get("id")) != str(context["link"].source_id):
            raise RuntimeError("HERO mutation response could not confirm the selected remote identity.")
    except Exception as error:
        _finalize_mutation(
            audit,
            "Timeout" if error.__class__.__name__ in {"TimeoutError", "URLError"} else "Failed",
            "HERO writeback outcome is not confirmed. No automatic retry was performed; inspect the selected remote record before another explicit submission.",
        )
        frappe.throw("HERO writeback is blocked or uncertain; no automatic retry was performed.")
    _finalize_mutation(audit, "Succeeded", "Exact Customer/Project writeback completed; field values were not retained in the audit.")
    return {"status": "SUCCEEDED", "correlation_id": audit.correlation_id, "operation": context["mapping"]["target"]["operation"]}


def _writeback_context(mapping_name: str, document, mapping_row=None) -> dict[str, object]:
    """Rebuild every preview input from current server-side state."""
    from hero_integration.services.frappe_writeback import (
        WritebackContractError,
        build_request_checked,
        definition,
        fingerprint_payload,
        normalise_mapping,
    )

    row = mapping_row or frappe.get_doc("HERO GraphQL Mapping", mapping_name)
    if row.status != "Active" or row.direction != "FRAPPE_TO_MUTATION":
        frappe.throw("The writeback mapping is not active.")
    snapshot, schema = _validated_snapshot(row.schema_snapshot)
    try:
        mapping = normalise_mapping(json.loads(row.mapping_json), schema)
        source = mapping["source"]
        if source["doctype"] != document.doctype:
            frappe.throw("The writeback mapping does not belong to this Frappe document type.")
        expected = definition(document.doctype)
    except (TypeError, ValueError, WritebackContractError):
        frappe.throw("The active writeback mapping is no longer safe.")
    contracts = frappe.get_all(
        "HERO Mutation Contract",
        filters={"schema_snapshot": snapshot.name, "operation_name": mapping["target"]["operation"], "status": "Active"},
        fields=["name", "contract_revision", "contract_fingerprint"],
    )
    if len(contracts) != 1:
        frappe.throw("Exactly one active schema-bound HERO Mutation Contract is required for writeback.")
    contract = contracts[0]
    links = frappe.get_all(
        "HERO Integration Link",
        filters={"source_type": expected.source_type, "target_doctype": document.doctype, "target_name": document.name},
        fields=["name", "source_instance", "source_id", "sync_status", "source_payload_hash", "observed_source_hash"],
    )
    if len(links) != 1:
        frappe.throw("Exactly one valid HERO Integration Link is required for writeback.")
    link = links[0]
    if link.source_instance != (row.source_instance or "") or link.sync_status != "Synchronized":
        frappe.throw("The HERO Integration Link is stale, conflicting, or belongs to another source instance.")
    if link.observed_source_hash and link.observed_source_hash != link.source_payload_hash:
        frappe.throw("The linked HERO source changed after its synchronized baseline; writeback is blocked.")
    field = source["field"]
    value = document.get(field)
    outbound_document, variables = build_request_checked(mapping, schema, link.source_id, value)
    canonical = fingerprint_payload(
        row.name, int(row.mapping_revision or 1), document.modified, link.source_instance,
        link.source_id, outbound_document, variables,
    )
    canonical += "|" + contract.name + "|" + str(contract.contract_revision or 1) + "|" + str(contract.contract_fingerprint or "")
    return {
        "mapping_row": row, "mapping": mapping, "snapshot": snapshot, "link": link,
        "contract": contract,
        "value": value, "document": outbound_document, "variables": variables,
        "fingerprint": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _reserve_mutation(*, operation_name: str, schema_fingerprint: str, mapping_revision: int, request_fingerprint: str):
    """Durably reserve the one outbound mutation slot before contacting HERO.

    A unique ``reservation_key`` lets MariaDB serialize competing confirmations
    without relying on a stale read of a previous audit row.  The commit is
    intentional: an external request must never occur before its recovery
    record is visible to another request.
    """
    correlation_id = uuid4().hex
    audit = frappe.get_doc({
        "doctype": "HERO Mutation Audit", "correlation_id": correlation_id, "user": frappe.session.user,
        "site": frappe.local.site, "operation_name": operation_name, "schema_fingerprint": schema_fingerprint,
        "mapping_revision": mapping_revision, "request_fingerprint": request_fingerprint,
        "reservation_key": ACTIVE_MUTATION_RESERVATION, "status": "Approved",
        "detail": "Explicit System Manager confirmation recorded; variables are intentionally not retained.",
    })
    try:
        audit.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.throw("Another HERO mutation is currently in progress.")
    frappe.db.commit()
    return audit


def _finalize_mutation(audit, status: str, detail: str) -> None:
    """Release a completed reservation without erasing its audit provenance."""
    audit.status = status
    audit.detail = detail
    audit.reservation_key = audit.correlation_id
    audit.save(ignore_permissions=True)
    frappe.db.commit()


def _validated_mutation_request(contract_name: str, variables_json: str):
    try:
        variables = json.loads(variables_json)
    except json.JSONDecodeError:
        frappe.throw("Mutation variables must be a JSON object.")
    contract = frappe.get_doc("HERO Mutation Contract", contract_name)
    service = frappe.get_doc("HERO GraphQL Service", contract.graphql_service)
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", contract.schema_snapshot)
    if service.schema_snapshot != snapshot.name or service.schema_fingerprint != snapshot.schema_fingerprint:
        frappe.throw("The mutation service no longer matches its schema snapshot.")
    if snapshot.analysis_status != "Validated" or snapshot.execution_contract_status == "Blocked":
        frappe.throw("The mutation schema snapshot is not executable.")
    if contract.schema_fingerprint != snapshot.schema_fingerprint:
        frappe.throw("The mutation contract no longer matches its schema snapshot.")
    try:
        mapping = json.loads(contract.mapping_json)
        if not isinstance(mapping, dict) or mapping.get("mode") not in {"ADMIN_INPUT", "VALIDATED_RESOLVER"}:
            raise ValueError
        from hero_integration.services.graphql_contract import parse_mutation, validate_mutation

        parsed = parse_mutation(service.query_document)
        if parsed.fingerprint != contract.contract_fingerprint or parsed.root_field != contract.operation_name:
            frappe.throw("The saved mutation document differs from its approved contract.")
        values = validate_mutation(json.loads(snapshot.snapshot_json), parsed, variables)
    except ValueError:
        frappe.throw("The mutation contract or variables are invalid.")
    canonical = json.dumps(
        {"contract": contract.contract_fingerprint, "revision": int(contract.contract_revision or 1), "variables": values},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return contract, service, snapshot, values, hashlib.sha256(canonical.encode()).hexdigest()


class _FrappePickerMetadata:
    def doctypes(self):
        from frappe.modules.utils import get_module_app

        installed = set(frappe.get_installed_apps())
        rows = frappe.get_all(
            "DocType",
            fields=["name", "module", "istable", "issingle", "is_virtual", "custom"],
            order_by="module asc, name asc",
        )
        result = []
        for row in rows:
            item = dict(row)
            module = str(item.get("module") or "")
            try:
                app_name = get_module_app(module) if module else ""
            except Exception:
                app_name = "Custom" if item.get("custom") else ""
            if app_name and app_name not in installed and app_name != "Custom":
                continue
            item["app_name"] = app_name or ("Custom" if item.get("custom") else "")
            result.append(item)
        return result

    def can_write(self, doctype: str) -> bool:
        return bool(frappe.has_permission(doctype, "write"))

    def fields(self, doctype: str):
        frappe.has_permission(doctype, "write", throw=True)
        return frappe.get_meta(doctype).fields


class _FrappeMetadata:
    def ensure_write(self, doctype: str):
        frappe.has_permission(doctype, "write", throw=True)

    def field(self, doctype: str, fieldname: str):
        return frappe.get_meta(doctype).get_field(fieldname)


def _validated_snapshot(snapshot_name: str, *, for_update=False):
    snapshot = (frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name, for_update=True) if for_update
                else frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name))
    if snapshot.analysis_status != "Validated":
        frappe.throw("The GraphQL schema snapshot is not valid.")
    from hero_integration.services.graphql_graph import analyze_snapshot

    schema = json.loads(snapshot.snapshot_json)
    if analyze_snapshot(schema).fingerprint != snapshot.schema_fingerprint:
        frappe.throw("Stored GraphQL schema fingerprint is stale.")
    return snapshot, schema


def _existing_mappings(schema_fingerprint: str, operation: str | None = None) -> list[dict[str, object]]:
    filters = {"schema_fingerprint": schema_fingerprint}
    if operation:
        filters["operation"] = operation
    return frappe.get_all(
        "HERO Frappe Mapping",
        filters=filters,
        fields=["name", "operation", "source_path", "target_doctype", "target_field", "status", "mapping_revision"],
        order_by="modified desc",
    )


def _has_active_run() -> bool:
    state = frappe.get_single("HERO Integration Sync State")
    if not state.active_run:
        return False
    return frappe.get_value("HERO Integration Run", state.active_run, "state") not in TERMINAL_STATES


def _require_selected_mappings(action: str, service, mapping, dependencies=None) -> None:
    """Preflight the exact mapping definitions a new run will freeze."""
    from hero_integration.services.mapping_runtime import MappingPlanError
    from hero_integration.services.migration import validate_run_mappings

    try:
        validate_run_mappings(
            SimpleNamespace(
                action=action,
                graphql_service=service.name,
                service_revision=service.service_revision,
                graphql_mapping=mapping.name,
                mapping_revision=mapping.mapping_revision,
                **(dependencies or {}),
            )
        )
    except MappingPlanError as error:
        frappe.throw(str(error))
