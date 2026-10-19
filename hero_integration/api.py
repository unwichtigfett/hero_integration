from __future__ import annotations

import json

import frappe


ACTIONS = {"Customer Migration", "Project Migration", "Cheap Data Migration", "Full Data Migration", "Sync"}
TERMINAL_STATES = {"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked"}


@frappe.whitelist()
def start_migration(action: str, dry_run: int | bool = 1, scheduled: bool = False) -> dict[str, str]:
    if not scheduled:
        frappe.only_for("System Manager")
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
    return frappe.get_all(
        "HERO GraphQL Schema Snapshot",
        fields=["name", "schema_fingerprint", "analysis_status", "analysis_detail", "modified"],
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
    snapshots = schema_snapshot_list()
    selected = next((item for item in snapshots if item["name"] == snapshot_name), None) or (snapshots[0] if snapshots else None)
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
        "schema_refresh_available": False,
        "selected_snapshot": selected["name"] if selected else "",
    }


@frappe.whitelist()
def workspace_settings_get() -> dict[str, object]:
    """Return editable local settings while never returning the stored token."""
    frappe.only_for("System Manager")
    settings = frappe.get_single("HERO Integration Settings")
    return {
        "enabled": bool(settings.hero_api_url),
        "api_url": settings.hero_api_url or "",
        "api_token_configured": bool(settings.get_password("hero_api_token")),
        "timeout_seconds": int(settings.timeout_seconds or 30),
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
    result["existing_mappings"] = _existing_mappings(fingerprint)
    return result


@frappe.whitelist()
def workspace_service_list(snapshot_name: str, operation_kind: str = "query", start: int = 0, page_size: int = 50) -> dict[str, object]:
    frappe.only_for("System Manager")
    _assert_query_kind(operation_kind)
    rows = [
        _workspace_service(row)
        for row in frappe.get_all(
            "HERO GraphQL Service",
            filters={"schema_snapshot": snapshot_name},
            fields=["name", "service_name", "operation_name", "query_document", "service_revision", "status", "validation_summary"],
            order_by="modified desc",
        )
    ]
    return _page({}, rows, start, page_size)


@frappe.whitelist()
def workspace_service_detail(query_id: str) -> dict[str, object]:
    frappe.only_for("System Manager")
    return {"query": _workspace_service(frappe.get_doc("HERO GraphQL Service", query_id).as_dict())}


@frappe.whitelist()
def workspace_service_save(snapshot_name: str, payload_json: str, query_id: str = "", expected_revision: int = 0) -> dict[str, object]:
    frappe.only_for("System Manager")
    payload = _workspace_payload(payload_json)
    _assert_query_kind(payload.get("operation_kind", "query"))
    snapshot, _ = _validated_snapshot(snapshot_name)
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
        _assert_query_kind(payload.get("operation_kind", "query"))
        _validated_snapshot(snapshot_name)
        from hero_integration.client.graphql import assert_read_only

        assert_read_only(payload["query"])
        return {"valid": True, "can_execute": False, "reason": "Saved read-only definitions can be validated; remote execution is disabled in this workspace."}
    service = frappe.get_doc("HERO GraphQL Service", query_id)
    if service.schema_snapshot != snapshot_name or int(service.service_revision or 0) != int(expected_revision or 0):
        frappe.throw("The saved service changed or belongs to another schema snapshot.")
    service.status = "Validated"
    service.save()
    return {"valid": True, "can_execute": False, "query": _workspace_service(service.as_dict()), "reason": "Remote execution is disabled in this workspace."}


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
    return {
        "query_id": service["name"], "name": service["service_name"], "operation_name": service["operation_name"],
        "query": service["query_document"], "revision": int(service.get("service_revision") or 1),
        "provenance": "HERO Integration", "service_kind": "query", "status": service.get("status"),
        "validation_summary": service.get("validation_summary"), "variables": {},
    }


def _assert_query_kind(operation_kind: str) -> None:
    if operation_kind != "query":
        frappe.throw("HERO mutations are disabled; only stored read-only query definitions are supported.")


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
def mapping_target_picker(operation: str, search: str = "") -> list[dict[str, object]]:
    frappe.only_for("System Manager")
    from hero_integration.services.mapping_suggestions import target_fields

    return [item.__dict__ for item in target_fields(operation, _FrappePickerMetadata(), search)]


@frappe.whitelist()
def mapping_suggestions(snapshot_name: str, operation: str, source_path: str = "", target_field: str = "") -> list[dict[str, object]]:
    frappe.only_for("System Manager")
    _, schema = _validated_snapshot(snapshot_name)
    from hero_integration.services.mapping_suggestions import suggest_from_source, suggest_from_target

    metadata = _FrappePickerMetadata()
    if source_path:
        return suggest_from_source(operation, source_path, schema, metadata)
    if target_field:
        return suggest_from_target(operation, target_field, schema, metadata)
    frappe.throw("Choose a source path or a target field.")


@frappe.whitelist()
def preview_mapping_candidate(
    snapshot_name: str, operation: str, source_path: str, target_field: str, source_record_json: str,
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
    from hero_integration.services.direct_mapping import MappingSpec, SOURCE_CONTRACTS, transformed_value, validate_spec

    contract = SOURCE_CONTRACTS.get(operation)
    if not contract or source_path not in contract["paths"]:
        frappe.throw("Source path is not selected by the approved HERO operation.")
    frappe.has_permission(contract["target_doctype"], "write", throw=True)
    spec = MappingSpec(operation, source_path, contract["target_doctype"], target_field, contract["paths"][source_path])
    validate_spec(spec, _FrappeMetadata())
    value = transformed_value(spec, record.get(source_path))
    return {
        "operation": operation,
        "source_path": source_path,
        "target_field": target_field,
        "values": {target_field: value} if value not in (None, "") else {},
        "writes": False,
        "activation": "Draft only; explicit review and activation are required.",
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
def execute_graphql_service(service_name: str, variables_json: str = "{}") -> dict[str, object]:
    """Execute one validated saved query through the existing read-only client."""
    frappe.only_for("System Manager")
    service = frappe.get_doc("HERO GraphQL Service", service_name)
    if service.status != "Validated":
        frappe.throw("Only validated GraphQL services may execute.")
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    if snapshot.analysis_status != "Validated" or snapshot.schema_fingerprint != service.schema_fingerprint:
        frappe.throw("The service schema fingerprint is stale.")
    try:
        variables = json.loads(variables_json)
    except json.JSONDecodeError as error:
        frappe.throw(f"Variables must be JSON: {error.msg}")
    if not isinstance(variables, dict):
        frappe.throw("Variables must be a JSON object.")
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    client = HeroGraphQLClient(
        config.endpoint,
        lambda: config.api_token,
        config.timeout_seconds,
    )
    return client.query(service.query_document, variables)


class _FrappePickerMetadata:
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
