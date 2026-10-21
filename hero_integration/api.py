from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import frappe


ACTIONS = {"Customer Migration", "Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
TERMINAL_STATES = {"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked"}
MUTATION_EXECUTION_DISABLED = "HERO_MUTATION_EXECUTION_DISABLED"


@frappe.whitelist()
def start_migration(action: str, dry_run: int | bool = 1) -> dict[str, str]:
    """Queue a manually requested migration; this RPC is always administrative."""
    frappe.only_for("System Manager")
    return _start_migration(action=action, dry_run=dry_run, scheduled=False)


def start_scheduled_sync() -> dict[str, str]:
    """Internal scheduler entrypoint.  It is deliberately not whitelisted."""
    return _start_migration(action="Sync", dry_run=0, scheduled=True)


def _start_migration(action: str, dry_run: int | bool, scheduled: bool) -> dict[str, str]:
    if action not in ACTIONS:
        frappe.throw("Unsupported HERO migration action.")
    _require_active_mappings(action)
    if scheduled and _has_active_run():
        return {"status": "SKIPPED_ACTIVE_RUN"}
    run = frappe.get_doc({"doctype": "HERO Integration Run", "action": action, "mode": "Preview" if int(dry_run) else "Execute"})
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


@frappe.whitelist()
def cancel_run(run_name: str) -> dict[str, str]:
    frappe.only_for("System Manager")
    run = frappe.get_doc("HERO Integration Run", run_name)
    if run.state not in TERMINAL_STATES:
        run.state = "Cancelled"
        run.save()
    return {"status": run.state, "run": run.name}


@frappe.whitelist()
def resume_run(run_name: str) -> dict[str, str]:
    frappe.only_for("System Manager")
    run = frappe.get_doc("HERO Integration Run", run_name)
    if run.state not in {"Failed", "Cancelled", "Blocked"}:
        frappe.throw("Only failed, cancelled, or blocked runs can resume.")
    _require_active_mappings(run.action)
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
def schema_snapshot_list() -> list[dict[str, object]]:
    """List saved snapshots without exposing configuration or making network calls."""
    frappe.only_for("System Manager")
    return _schema_snapshot_rows()


@frappe.whitelist()
def workspace_schema_refresh(connection_id: str = "", binding_generation: int | str = 0) -> dict[str, object]:
    """Capture a new complete, validated live HERO schema snapshot.

    This is intentionally an explicit System Manager action.  Opening a page,
    loading settings, or generating mapping suggestions never invokes it.
    """
    frappe.only_for("System Manager")
    del binding_generation  # preserved compatibility parameter; no hidden binding state exists.
    from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient
    from hero_integration.client.settings import HeroConfigurationError, get_configuration
    from hero_integration.services.introspection import (
        INTROSPECTION_QUERY,
        IntrospectionError,
        canonical_schema,
        captured_at_utc,
        endpoint_fingerprint,
        structural_diff,
    )

    try:
        configuration = get_configuration()
        endpoint_hash = endpoint_fingerprint(configuration.endpoint.url, configuration.source_instance)
        if connection_id and connection_id not in {endpoint_hash, configuration.source_instance}:
            frappe.throw("The selected schema connection does not match current HERO settings.")
        client = HeroGraphQLClient(
            configuration.endpoint,
            token_provider=lambda: configuration.api_token,
            timeout=configuration.timeout_seconds,
            max_retries=1,
        )
        schema = canonical_schema(client.introspect(INTROSPECTION_QUERY))
    except (HeroClientError, HeroConfigurationError, IntrospectionError, ValueError) as error:
        frappe.throw(f"HERO schema refresh failed safely: {error}")

    schema_json = {"__schema": schema}
    from hero_integration.services.graphql_graph import analyze_snapshot

    fingerprint = analyze_snapshot(schema_json).fingerprint
    existing = frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        filters={"schema_fingerprint": fingerprint},
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
        }

    previous_rows = frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        filters={"source_instance": configuration.source_instance, "endpoint_fingerprint": endpoint_hash},
        fields=["name"], order_by="modified desc", limit_page_length=1,
    )
    previous = frappe.get_doc("HERO GraphQL Schema Snapshot", previous_rows[0]["name"]) if previous_rows else None
    diff = structural_diff(json.loads(previous.snapshot_json) if previous else None, schema)
    snapshot = frappe.get_doc({
        "doctype": "HERO GraphQL Schema Snapshot",
        "snapshot_json": json.dumps(schema_json, ensure_ascii=False, sort_keys=True),
        "source_instance": configuration.source_instance,
        "endpoint_fingerprint": endpoint_hash,
        "schema_version": 1,
        "captured_at": captured_at_utc(),
        "structural_diff_json": json.dumps(diff, ensure_ascii=False, sort_keys=True),
        "execution_contract_status": "Blocked" if diff["breaking"] else "Compatible",
    })
    snapshot.insert()
    return {
        "status": "CAPTURED",
        "snapshot_name": snapshot.name,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "execution_contract_status": snapshot.execution_contract_status,
        "structural_diff": diff,
    }


def _schema_snapshot_rows() -> list[dict[str, object]]:
    return frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        fields=[
            "name", "schema_fingerprint", "analysis_status", "analysis_detail", "modified",
            "source_instance", "endpoint_fingerprint", "captured_at", "execution_contract_status",
        ],
        order_by="modified desc",
        limit_page_length=100,
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
        "api_token_configured": token_configured,
        "api_token_status": token_status,
        "timeout_seconds": int(settings.timeout_seconds or 30),
        **_mutation_execution_state(settings),
    }


@frappe.whitelist()
def workspace_settings_save(enabled: int | bool, api_url: str, api_token: str = "", timeout_seconds: int = 30) -> dict[str, object]:
    """Persist only native integration configuration, with DocType validation."""
    frappe.only_for("System Manager")
    if int(timeout_seconds) < 1:
        frappe.throw("Timeout must be at least one second.")
    settings = frappe.get_single("HERO Integration Settings")
    settings.hero_api_url = api_url.strip() if bool(enabled) else ""
    settings.timeout_seconds = int(timeout_seconds)
    if api_token:
        settings.hero_api_token = api_token
    settings.save()
    return workspace_settings_get()


@frappe.whitelist()
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


@frappe.whitelist()
def workspace_service_save(snapshot_name: str, payload_json: str, query_id: str = "", expected_revision: int = 0) -> dict[str, object]:
    frappe.only_for("System Manager")
    payload = _workspace_payload(payload_json)
    kind = str(payload.get("operation_kind", "query"))
    _assert_service_kind(kind)
    snapshot, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mutation_draft import operation_kind as document_kind, validate as validate_mutation_draft

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


@frappe.whitelist()
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


@frappe.whitelist()
def workspace_service_delete(query_id: str, expected_revision: int = 0) -> dict[str, str]:
    frappe.only_for("System Manager")
    service = frappe.get_doc("HERO GraphQL Service", query_id)
    if int(service.service_revision or 0) != int(expected_revision or 0):
        frappe.throw("The saved service has changed; reload it before deleting.")
    frappe.delete_doc("HERO GraphQL Service", query_id)
    return {"status": "DELETED"}


def _page(base: dict[str, object], rows: list[object], start: int | str, page_size: int | str) -> dict[str, object]:
    offset, limit = max(0, int(start or 0)), min(max(1, int(page_size or 50)), 500)
    return {**base, "rows": rows[offset:offset + limit], "has_more": offset + limit < len(rows), "total": len(rows)}


def _workspace_payload(payload_json: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as error:
        frappe.throw(f"Service payload must be JSON: {error.msg}")
    if not isinstance(payload, dict):
        frappe.throw("Service payload must be an object.")
    for field in ("name", "query"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            frappe.throw(f"Service {field} is required.")
    if payload.get("operation_name") is not None and not isinstance(payload["operation_name"], str):
        frappe.throw("Service operation name must be text.")
    return payload


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
    target = validate_draft_target(operation, spec.source_type, target_doctype, target_field, _FrappePickerMetadata())
    value = transformed_value(spec, source_value(record, source_path))
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


@frappe.whitelist()
def adopt_mapping_package(
    snapshot_name: str, payload_json: str, selected_ids_json: str, confirmed: bool = False,
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
        return start(snapshot_name, payload_json, selected_ids)
    except (MappingExchangeError, SchemaProvisioningError) as error:
        frappe.throw(str(error))


@frappe.whitelist()
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


@frappe.whitelist()
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


@frappe.whitelist()
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
    if frappe.db.exists("HERO Mutation Audit", {"status": "Approved"}):
        frappe.throw("Another HERO mutation is currently in progress.")
    correlation_id = uuid4().hex
    audit = frappe.get_doc({
        "doctype": "HERO Mutation Audit", "correlation_id": correlation_id, "user": frappe.session.user,
        "site": frappe.local.site, "operation_name": service.operation_name,
        "schema_fingerprint": snapshot.schema_fingerprint, "mapping_revision": int(contract.contract_revision or 1),
        "request_fingerprint": request_fingerprint, "status": "Approved", "detail": "Explicit System Manager confirmation recorded; variables are intentionally not retained.",
    })
    audit.insert(ignore_permissions=True)
    try:
        from hero_integration.client.graphql_mutation import HeroMutationClient
        from hero_integration.client.settings import get_configuration

        configuration = get_configuration()
        HeroMutationClient(configuration.endpoint, lambda: configuration.api_token, configuration.timeout_seconds).execute_once(
            service.query_document, variables
        )
    except Exception as error:
        audit.status = "Timeout" if error.__class__.__name__ in {"TimeoutError", "URLError"} else "Failed"
        audit.detail = "HERO mutation did not complete. No automatic retry was performed; inspect the remote effect before another explicit submission."
        audit.save(ignore_permissions=True)
        frappe.throw("HERO mutation failed safely; no automatic retry was performed.")
    audit.status = "Succeeded"
    audit.detail = "HERO mutation transport completed; remote response data is intentionally not persisted."
    audit.save(ignore_permissions=True)
    return {"status": "SUCCEEDED", "correlation_id": correlation_id, "operation": service.operation_name}


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


def _validated_snapshot(snapshot_name: str):
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name)
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


def _require_active_mappings(action: str) -> None:
    from hero_integration.services.mapping_runtime import MappingPlanError, active_mapping_plan

    operations = []
    # Every project import first performs the Customer phase in the same run.
    if action in {"Customer Migration", "Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}:
        operations.append("HeroCustomers")
    if action in {"Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}:
        operations.append("HeroProjectMatches")
    try:
        for operation in operations:
            active_mapping_plan(operation)
    except MappingPlanError as error:
        frappe.throw(str(error))
