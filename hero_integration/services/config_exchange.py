"""Export and import a complete HERO integration configuration without HERO I/O.

Format v2 contains the schema snapshot, operational settings, checked mapping
package/schema and approved area read services. Customer-service references
use areas rather than site-specific names. Excluded: credentials, URL/hosts,
recipients, mutation/deletion switches, testing mode and runtime status.

Import uses the existing adoption workflow and target-side approvals/revisions.
Identical configurations write nothing. Format v1 remains supported.
"""

from __future__ import annotations

import json

import frappe

from hero_integration.services.localized_error import LocalizedError

FORMAT_VERSION = "hero-integration-config-v2"
FORMAT_V1 = "hero-integration-config-v1"
# Operational settings that move with the configuration.  Everything else stays
# site-specific: credentials, hosts, recipients (personal data), safety switches.
SETTINGS_KEYS = (
    "hero_source_instance", "schema_refresh_interval_minutes", "timeout_seconds",
    "scheduled_sync_enabled", "sync_service_paused",
    "sync_area_contacts", "sync_area_projects", "sync_area_events", "sync_area_tasks",
    "polling_mode", "polling_interval_minutes", "polling_timezone", "polling_execution_window",
    "polling_bootstrap", "polling_page_size", "polling_pagination_strategy", "polling_allow_backfill",
    "polling_full_inventory", "polling_reconciliation", "polling_retry_limit", "polling_retry_backoff_seconds",
    "polling_timeout_seconds", "polling_rate_limit", "polling_concurrency", "polling_notifications",
)
AREA_ORDER = ("HeroCustomers", "HeroProjectMatches", "HeroCalendarEvents", "HeroTasks",
              "HeroProducts", "HeroDocuments", "HeroPictures", "HeroServices", "HeroEmployees")
SERVICE_REFERENCE = "$read_service:"
SERVICE_KEYS = {"target_operation", "service_name", "operation_name", "mapping_operation_name", "query", "mapping"}


class ConfigExchangeError(LocalizedError):
    pass


def export_config(snapshot_name: str, source_instance: str = "default") -> str:
    from hero_integration import api
    from hero_integration.services.mapping_exchange import export_json

    services = api._sync_area_read_services(source_instance)
    names = {operation: rows[0]["service"] for operation, rows in services.items() if rows}
    exported = []
    for operation in AREA_ORDER:
        if operation not in services:
            continue
        service = frappe.get_doc("HERO GraphQL Service", services[operation][0]["service"])
        mapping = frappe.get_doc("HERO GraphQL Mapping", services[operation][0]["mapping"])
        config = json.loads(mapping.mapping_json)
        for key, value in list(config.items()):
            reference = next((op for op, name in names.items() if value == name), None)
            if key.endswith("_service") and reference:
                config[key] = SERVICE_REFERENCE + reference
        exported.append({"target_operation": operation, "service_name": service.service_name,
                         "operation_name": service.operation_name, "mapping_operation_name": mapping.operation_name,
                         "query": service.query_document, "mapping": config})
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name)
    settings = frappe.get_single("HERO Integration Settings")
    return json.dumps({
        "format_version": FORMAT_VERSION,
        "schema_snapshot": {"schema_fingerprint": snapshot.schema_fingerprint, "schema": json.loads(snapshot.snapshot_json)},
        "settings": {key: settings.get(key) for key in SETTINGS_KEYS},
        "mapping_package": json.loads(export_json(snapshot_name)),
        "read_services": exported,
    }, ensure_ascii=False, sort_keys=True, indent=1)


def _package_snapshot(payload: dict, *, create: bool) -> tuple[str | None, str]:
    """The site's snapshot for the package's schema: existing, created (import) or pending (preview)."""
    item = payload.get("schema_snapshot")
    if not item:
        return None, "not in file"
    rows = frappe.get_all("HERO GraphQL Schema Snapshot", filters={
        "schema_fingerprint": item["schema_fingerprint"], "analysis_status": "Validated"},
        pluck="name", order_by="creation desc", limit_page_length=1)
    if rows:
        return rows[0], "Unchanged"
    if not create:
        return None, "New"
    from hero_integration.services.schema_refresh import capture_from_schema

    result = capture_from_schema(item["schema"], frappe.session.user)
    return result["snapshot_name"], "Created"


def _resolve_snapshot(snapshot_name: str, payload: dict, *, create: bool) -> tuple[str | None, str]:
    if snapshot_name:
        return snapshot_name, "selected"
    name, state = _package_snapshot(payload, create=create)
    if name or state == "New":
        return name, state
    names = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"analysis_status": "Validated"},
                           order_by="creation desc", pluck="name", limit_page_length=1)
    if not names:
        raise ConfigExchangeError("No validated HERO schema snapshot exists and the file contains none.")
    return names[0], "newest on this site"


def _settings_changes(payload: dict) -> dict:
    wanted = payload.get("settings") or {}
    settings = frappe.get_single("HERO Integration Settings")
    return {key: value for key, value in wanted.items()
            if key in SETTINGS_KEYS and settings.get(key) != value
            and not (settings.get(key) in (None, "", 0) and value in (None, "", 0))}


def _apply_settings(changes: dict) -> None:
    for key, value in changes.items():
        frappe.db.set_single_value("HERO Integration Settings", key, value)


def preview_config(snapshot_name: str, payload_json: str, source_instance: str = "default") -> dict:
    """What an import would do; never writes.

    Without a snapshot for the file's schema the detailed package check runs
    during the import, after the snapshot was created (nothing is activated if
    that check fails).
    """
    from hero_integration.services.schema_provisioning import preview_package

    payload = _parse(payload_json)
    snapshot_name, snapshot_state = _resolve_snapshot(snapshot_name, payload, create=False)
    result = {"format_version": payload["format_version"], "snapshot": snapshot_state,
              "settings": sorted(_settings_changes(payload))}
    if not snapshot_name:
        return {**result, "mapping_package": None, "rebased_from": None, "adopt": None,
                "read_services": [{"target_operation": item["target_operation"], "state": "checked during import"}
                                  for item in payload["read_services"]]}
    package_json, rebased_from = _rebased_package(snapshot_name, payload)
    package = preview_package(snapshot_name, package_json)
    services = [{"target_operation": item["target_operation"], "state": _query_problem(snapshot_name, item)
                 or _service_state(item, source_instance)} for item in payload["read_services"]]
    return {**result, "mapping_package": package["counts"], "rebased_from": rebased_from,
            "adopt": len(_selection(package)), "read_services": services}


def _rebased_package(snapshot_name: str, payload: dict) -> tuple[str, str | None]:
    """Bind the package to the target snapshot by content, not source fingerprint.

    Validate every field rule and read query against the target schema before
    writing; the source schema may differ.
    """
    package = dict(payload["mapping_package"])
    target = frappe.db.get_value("HERO GraphQL Schema Snapshot", snapshot_name, "schema_fingerprint")
    if not target:
        raise ConfigExchangeError("The selected schema snapshot does not exist.")
    source = package.get("schema_fingerprint")
    package["schema_fingerprint"] = target
    return json.dumps(package), (source if source != target else None)


def _query_problem(snapshot_name: str, item: dict) -> str | None:
    """'Invalid: …' when the read query does not fit the target schema (local check, no HERO request)."""
    from hero_integration import api

    body = json.dumps({"name": item["service_name"], "operation_name": item["operation_name"], "query": item["query"]})
    try:
        api._workspace_service_validate(snapshot_name, body, "", 0)
    except Exception as error:  # frappe.throw raises ValidationError
        return "Invalid: " + str(error)[:200]
    return None


def import_config(snapshot_name: str, payload_json: str, source_instance: str = "default") -> dict:
    """Adopt the mapping package, then create, validate and approve each changed read service."""
    from hero_integration.services import adoption
    from hero_integration.services.schema_provisioning import preview_package

    frappe.only_for("System Manager")
    payload = _parse(payload_json)
    snapshot_name, snapshot_state = _resolve_snapshot(snapshot_name, payload, create=True)
    package_json, _ = _rebased_package(snapshot_name, payload)
    package = preview_package(snapshot_name, package_json)
    blocked = [item for item in package["items"] if item["state"] in {"Conflict", "Invalid"}]
    if blocked:
        raise ConfigExchangeError("The mapping package has conflicts or invalid entries: {0}",
                                  ", ".join(str(item.get("id")) for item in blocked[:10]))
    invalid = [f"{item['target_operation']}: {problem}" for item in payload["read_services"]
               if (problem := _query_problem(snapshot_name, item))]
    if invalid:
        raise ConfigExchangeError("A read query does not fit the target HERO schema: {0}", "; ".join(invalid))
    selected = _selection(package)
    settings = _settings_changes(payload)
    adopted = adoption.start(snapshot_name, package_json, selected, package["preview_revision"]) if selected else None
    results = []
    created: dict[str, str] = {}
    for item in sorted(payload["read_services"], key=lambda entry: AREA_ORDER.index(entry["target_operation"])):
        state = _service_state(item, source_instance, created)
        if state == "Unchanged":
            results.append({"target_operation": item["target_operation"], "state": state})
            continue
        service = _import_service(snapshot_name, item, source_instance, created)
        created[item["target_operation"]] = service
        results.append({"target_operation": item["target_operation"], "state": "Approved", "service": service})
    _apply_settings(settings)  # last: settings change only after mappings and read services succeeded
    return {"snapshot": snapshot_state, "snapshot_name": snapshot_name, "settings": sorted(settings),
            "adopted": len(selected), "adoption": (adopted or {}).get("adoption_name"), "read_services": results}


def _parse(payload_json: str) -> dict:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as error:
        raise ConfigExchangeError("The configuration file is not valid JSON.") from error
    keys = {FORMAT_V1: {"format_version", "mapping_package", "read_services"},
            FORMAT_VERSION: {"format_version", "schema_snapshot", "settings", "mapping_package", "read_services"}}
    if (not isinstance(payload, dict) or payload.get("format_version") not in keys
            or set(payload) != keys[payload["format_version"]]
            or not isinstance(payload["read_services"], list)):
        raise ConfigExchangeError("This is not a HERO integration configuration file ({0}).", FORMAT_VERSION)
    if payload["format_version"] == FORMAT_VERSION:
        snapshot, settings = payload["schema_snapshot"], payload["settings"]
        if (not isinstance(snapshot, dict) or set(snapshot) != {"schema_fingerprint", "schema"}
                or not isinstance(snapshot["schema"], dict) or "__schema" not in snapshot["schema"]):
            raise ConfigExchangeError("The schema snapshot in the file is incomplete.")
        if not isinstance(settings, dict) or set(settings) - set(SETTINGS_KEYS):
            raise ConfigExchangeError("The file contains settings that are never transferred (e.g. credentials).")
    seen = set()
    for item in payload["read_services"]:
        if (not isinstance(item, dict) or set(item) != SERVICE_KEYS or item["target_operation"] not in AREA_ORDER
                or item["target_operation"] in seen or not isinstance(item["mapping"], dict)
                or not isinstance(item["query"], str) or not item["query"].strip()):
            raise ConfigExchangeError("A read service entry is incomplete or duplicated.")
        seen.add(item["target_operation"])
    return payload


def _selection(package) -> list[str]:
    return [item["id"] for item in package["items"] if item["state"] in {"New", "Changed"}]


def _resolved_mapping(item, source_instance, created=None) -> dict:
    from hero_integration import api

    config = dict(item["mapping"])
    current = None
    for key, value in list(config.items()):
        if isinstance(value, str) and value.startswith(SERVICE_REFERENCE):
            operation = value[len(SERVICE_REFERENCE):]
            if created and operation in created:
                config[key] = created[operation]
                continue
            current = current if current is not None else api._sync_area_read_services(source_instance)
            rows = current.get(operation) or []
            if not rows:
                raise ConfigExchangeError("{0} needs the {1} read service first.", item['target_operation'], operation)
            config[key] = rows[0]["service"]
    return config


def _service_state(item, source_instance, created=None) -> str:
    from hero_integration import api

    rows = api._sync_area_read_services(source_instance).get(item["target_operation"]) or []
    if not rows:
        return "New"
    try:
        config = _resolved_mapping(item, source_instance, created)
    except ConfigExchangeError:
        return "Changed"
    service = frappe.get_doc("HERO GraphQL Service", rows[0]["service"])
    mapping = frappe.get_doc("HERO GraphQL Mapping", rows[0]["mapping"])
    same = (service.query_document.strip() == item["query"].strip()
            and json.loads(mapping.mapping_json) == config and service.service_name == item["service_name"])
    return "Unchanged" if same else "Changed"


def _import_service(snapshot_name, item, source_instance, created) -> str:
    from hero_integration import api

    config = _resolved_mapping(item, source_instance, created)
    rows = api._sync_area_read_services(source_instance).get(item["target_operation"]) or []
    existing = frappe.get_doc("HERO GraphQL Mapping", rows[0]["mapping"]) if rows else None
    reuse = existing if existing is not None and existing.schema_snapshot == snapshot_name else None
    draft = api.graphql_mapping_save(snapshot_name, "QUERY", item["mapping_operation_name"], "QUERY_TO_FRAPPE",
                                     json.dumps(config), mapping_name=reuse.name if reuse else "",
                                     expected_revision=reuse.mapping_revision if reuse else 0, activate=0)
    saved = api.graphql_mapping_save(snapshot_name, "QUERY", item["mapping_operation_name"], "QUERY_TO_FRAPPE",
                                     json.dumps(config), mapping_name=draft["name"], expected_revision=draft["revision"],
                                     activate=1)
    mapping = frappe.get_doc("HERO GraphQL Mapping", saved["name"])
    service_doc = frappe.get_doc("HERO GraphQL Service", rows[0]["service"]) if rows else None
    if service_doc is not None and service_doc.schema_snapshot != snapshot_name:
        service_doc = None
    body = json.dumps({"name": item["service_name"], "operation_name": item["operation_name"], "query": item["query"]})
    saved_service = api._save_workspace_service(
        snapshot_name, api._workspace_payload(body), service_doc.name if service_doc else "",
        int(service_doc.service_revision or 0) if service_doc else 0, "GraphQL Services")["query"]
    service = frappe.get_doc("HERO GraphQL Service", saved_service["query_id"])
    api._workspace_service_validate(snapshot_name, "", service.name, int(service.service_revision or 0))
    service.reload()
    api.approve_read_service(service.name, mapping.name, int(service.service_revision or 0), int(mapping.mapping_revision or 0))
    return service.name
