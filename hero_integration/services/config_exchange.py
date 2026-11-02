"""Complete HERO integration configuration export and import (2026-11-02).

One JSON file carries everything a new site needs besides credentials and data:

- ``mapping_package``: the field rules with their Custom Field / Custom DocType
  schema (the existing checked package, adopted through the existing adoption
  workflow);
- ``read_services``: the approved read service of every sync area (GraphQL
  document and read-mapping configuration).  A reference to the customer read
  service is exported by its area, never by the site-specific document name.

Import is idempotent: an identical configuration reports "Unchanged" and writes
nothing.  The target site's own validated schema snapshot is used; snapshot,
approvals and revisions are created there.  No HERO request is made.
"""

from __future__ import annotations

import json

import frappe

FORMAT_VERSION = "hero-integration-config-v1"
AREA_ORDER = ("HeroCustomers", "HeroProjectMatches", "HeroCalendarEvents", "HeroTasks")
SERVICE_REFERENCE = "$read_service:"
SERVICE_KEYS = {"target_operation", "service_name", "operation_name", "mapping_operation_name", "query", "mapping"}


class ConfigExchangeError(ValueError):
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
    return json.dumps({"format_version": FORMAT_VERSION, "mapping_package": json.loads(export_json(snapshot_name)),
                       "read_services": exported}, ensure_ascii=False, sort_keys=True, indent=1)


def preview_config(snapshot_name: str, payload_json: str, source_instance: str = "default") -> dict:
    """What an import would do; never writes."""
    from hero_integration.services.schema_provisioning import preview_package

    payload = _parse(payload_json)
    package_json, rebased_from = _rebased_package(snapshot_name, payload)
    package = preview_package(snapshot_name, package_json)
    services = [{"target_operation": item["target_operation"], "state": _query_problem(snapshot_name, item)
                 or _service_state(item, source_instance)} for item in payload["read_services"]]
    return {"format_version": FORMAT_VERSION, "mapping_package": package["counts"], "rebased_from": rebased_from,
            "adopt": len(_selection(package)), "read_services": services}


def _rebased_package(snapshot_name: str, payload: dict) -> tuple[str, str | None]:
    """Bind the package to the target site's snapshot.

    HERO changes its schema often, so a new site almost never has the source
    site's fingerprint.  Compatibility is checked by content instead: every
    field rule is validated by the package preview and every read query
    against the target schema before anything is written.
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
    package_json, _ = _rebased_package(snapshot_name, payload)
    package = preview_package(snapshot_name, package_json)
    blocked = [item for item in package["items"] if item["state"] in {"Conflict", "Invalid"}]
    if blocked:
        raise ConfigExchangeError("The mapping package has conflicts or invalid entries: "
                                  + ", ".join(str(item.get("id")) for item in blocked[:10]))
    invalid = [f"{item['target_operation']}: {problem}" for item in payload["read_services"]
               if (problem := _query_problem(snapshot_name, item))]
    if invalid:
        raise ConfigExchangeError("A read query does not fit the target HERO schema: " + "; ".join(invalid))
    selected = _selection(package)
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
    return {"adopted": len(selected), "adoption": (adopted or {}).get("adoption_name"), "read_services": results}


def _parse(payload_json: str) -> dict:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as error:
        raise ConfigExchangeError("The configuration file is not valid JSON.") from error
    if (not isinstance(payload, dict) or payload.get("format_version") != FORMAT_VERSION
            or set(payload) != {"format_version", "mapping_package", "read_services"}
            or not isinstance(payload["read_services"], list)):
        raise ConfigExchangeError("This is not a HERO integration configuration file of format " + FORMAT_VERSION + ".")
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
                raise ConfigExchangeError(f"{item['target_operation']} needs the {operation} read service first.")
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
