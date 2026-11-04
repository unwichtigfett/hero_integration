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
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

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
    from hero_integration.services import mapping_exchange
    from hero_integration.services.schema_provisioning import _definitions, _target_dependency

    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name)
    if snapshot.source_instance != source_instance:
        raise ConfigExchangeError("Export snapshot belongs to another HERO source.")
    services = api._sync_area_read_services(source_instance)
    names = {rows[0]["service"]: operation for operation, rows in services.items() if rows}
    def portable(value):
        if isinstance(value, dict):
            return {key: SERVICE_REFERENCE + names[child] if key.endswith("_service") and isinstance(child, str) and child in names
                    else portable(child) for key, child in value.items()}
        if isinstance(value, list):
            return [portable(child) for child in value]
        return value
    exported, selected, manifests = [], {}, []
    for operation in AREA_ORDER:
        if not services.get(operation):
            continue
        service = frappe.get_doc("HERO GraphQL Service", services[operation][0]["service"])
        mapping = frappe.get_doc("HERO GraphQL Mapping", services[operation][0]["mapping"])
        if mapping.schema_fingerprint != snapshot.schema_fingerprint:
            raise ConfigExchangeError("Approved readers use different schema versions; reconcile before export.")
        config = json.loads(mapping.mapping_json)
        config.pop("query_adapter_seal", None)
        config.pop("generated_task_seal", None)
        manifests.append({"schema_manifest": mapping.get("portable_schema_manifest")})
        for identity in config["mapping_definition_ids"]:
            rows = frappe.get_all("HERO Frappe Mapping", filters={"definition_id": identity, "status": "Active"},
                                  fields=["*"], limit_page_length=2)
            if len(rows) != 1:
                raise ConfigExchangeError("An approved mapping field rule is unavailable for export.")
            selected[identity] = rows[0]
        exported.append({"target_operation": operation, "service_name": service.service_name,
                         "operation_name": service.operation_name, "mapping_operation_name": mapping.operation_name,
                         "query": service.query_document, "mapping": portable(config)})
    schema = mapping_exchange._merge_export_schemas([*selected.values(), *manifests])
    definitions = _definitions(schema)
    rules = []
    for row in selected.values():
        rule = mapping_exchange._exportable(row)
        dependency = _target_dependency(definitions, rule)
        if dependency and dependency not in rule["schema_dependencies"]:
            rule["schema_dependencies"].append(dependency)
        rules.append(rule)
    package = {"format_version": mapping_exchange.FORMAT_VERSION, "schema_fingerprint": snapshot.schema_fingerprint,
               "schema": schema, "mappings": rules}
    settings = frappe.get_single("HERO Integration Settings")
    from hero_integration.services.config_mutation_drafts import export as export_mutations
    mutations = export_mutations(source_instance, snapshot_name)
    return json.dumps({"format_version": FORMAT_VERSION, "replace_mappings": True,
        "schema_snapshot": {"schema_fingerprint": snapshot.schema_fingerprint, "schema": json.loads(snapshot.snapshot_json)},
        "settings": {key: settings.get(key) for key in SETTINGS_KEYS}, "mapping_package": package,
        "read_services": exported, **({'mutation_mappings': mutations} if mutations else {})}, ensure_ascii=False, sort_keys=True, indent=1)


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
    if payload.get("replace_mappings"):
        from hero_integration.services.config_mutation_drafts import summary
        return {**_replacement_preview(snapshot_name, payload, source_instance), **summary(payload)}
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
    if payload.get("replace_mappings"):
        return _replace_config(snapshot_name, payload, source_instance)
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
        from hero_integration.services.personal_mapping_content import unique_json_object
        payload = json.loads(payload_json, object_pairs_hook=unique_json_object)
    except (TypeError, ValueError) as error:
        raise ConfigExchangeError("The configuration file is not valid JSON.") from error
    keys = {FORMAT_V1: {"format_version", "mapping_package", "read_services"},
            FORMAT_VERSION: {"format_version", "schema_snapshot", "settings", "mapping_package", "read_services"}}
    if (not isinstance(payload, dict) or payload.get("format_version") not in keys
            or set(payload) - ({"replace_mappings", "mutation_mappings"} if payload.get("format_version") == FORMAT_VERSION else set()) != keys[payload["format_version"]]
            or not isinstance(payload["read_services"], list)):
        raise ConfigExchangeError("This is not a HERO integration configuration file ({0}).", FORMAT_VERSION)
    if "replace_mappings" in payload and type(payload["replace_mappings"]) is not bool:
        raise ConfigExchangeError("replace_mappings must be a boolean.")
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
    if 'mutation_mappings' in payload:
        if not payload.get('replace_mappings'):
            raise ConfigExchangeError('Combined mutation mappings require a replacement configuration package.')
        from hero_integration.services.config_mutation_drafts import validate
        try:
            validate(payload)
        except ValueError as error:
            raise ConfigExchangeError.from_error(error) from error
    return payload


def _selection(package) -> list[str]:
    return [item["id"] for item in package["items"] if item["state"] in {"New", "Changed"}]


def _resolved_mapping(item, source_instance, created=None) -> dict:
    from hero_integration import api

    current = None
    def resolve(value, field=""):
        nonlocal current
        if isinstance(value, dict):
            return {key: resolve(child, key) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child, field) for child in value]
        if field.endswith("_service") and isinstance(value, str) and value.startswith(SERVICE_REFERENCE):
            operation = value[len(SERVICE_REFERENCE):]
            if created and operation in created:
                return created[operation]
            current = current if current is not None else api._sync_area_read_services(source_instance)
            rows = current.get(operation) or []
            if not rows:
                raise ConfigExchangeError("{0} needs the {1} read service first.", item['target_operation'], operation)
            return rows[0]["service"]
        return value
    return resolve(item["mapping"])


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


# Replacement is explicit, source-bound and uses the normal publication service.
# Old revisions remain available; no business document or HERO endpoint is touched.
def _replacement_rules(payload):
    from hero_integration.services.personal_mapping_content import RULE_FIELDS

    from hero_integration.services.mapping_exchange import _parse_payload
    packaged = payload.get("mapping_package")
    if not isinstance(packaged, dict):
        raise ConfigExchangeError("Replacement requires a mapping package.")
    _parse_payload(json.dumps(packaged), payload["schema_snapshot"]["schema_fingerprint"])
    rows = packaged.get("mappings")
    if not isinstance(rows, list):
        raise ConfigExchangeError("Replacement requires an explicit field mapping package.")
    rules = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("definition_id") or row["definition_id"] in rules:
            raise ConfigExchangeError("Replacement field identities are missing or duplicated.")
        if not RULE_FIELDS <= set(row):
            raise ConfigExchangeError("Replacement field rules are incomplete.")
        rules[row["definition_id"]] = row
    used = set()
    for item in payload["read_services"]:
        config = item["mapping"]
        ids = config.get("mapping_definition_ids")
        if (config.get("target_operation") != item["target_operation"] or not isinstance(ids, list)
                or not ids or len(ids) != len(set(ids)) or not set(ids) <= rules.keys()):
            raise ConfigExchangeError("Replacement readers require a complete, unique field-rule selection.")
        if any(rules[key].get("operation") != item["target_operation"] for key in ids):
            raise ConfigExchangeError("Replacement field rules cross an operation boundary.")
        used.update(ids)
    if used != rules.keys():
        raise ConfigExchangeError("Replacement package contains unselected field rules.")
    return rules


def _source_snapshot(snapshot_name, payload, source_instance, *, create=False):
    """A matching fingerprint in another HERO source is never reusable."""
    if payload.get("settings", {}).get("hero_source_instance", source_instance) != source_instance:
        raise ConfigExchangeError("Replacement cannot change the configured HERO source instance.")
    if snapshot_name:
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name)
        if snapshot.source_instance != source_instance or snapshot.analysis_status != "Validated":
            raise ConfigExchangeError("Replacement snapshot belongs to another or unvalidated HERO source.")
        return snapshot, "selected"
    packaged = payload["schema_snapshot"]
    names = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"source_instance": source_instance,
        "schema_fingerprint": packaged["schema_fingerprint"], "analysis_status": "Validated"},
        pluck="name", order_by="creation desc", limit_page_length=1)
    if names:
        return frappe.get_doc("HERO GraphQL Schema Snapshot", names[0]), "Unchanged"
    from hero_integration.services.graphql_graph import analyze_snapshot
    if analyze_snapshot(packaged["schema"]).fingerprint != packaged["schema_fingerprint"]:
        raise ConfigExchangeError("Packaged schema fingerprint does not match its content.")
    if create:
        from hero_integration.services.schema_refresh import capture_from_schema
        name = capture_from_schema(packaged["schema"], frappe.session.user)["snapshot_name"]
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", name)
        if snapshot.source_instance != source_instance:
            raise ConfigExchangeError("Created schema belongs to another HERO source.")
        return snapshot, "Created"
    return SimpleNamespace(name="package-preview", source_instance=source_instance, analysis_status="Validated",
        schema_fingerprint=packaged["schema_fingerprint"], snapshot_json=json.dumps(packaged["schema"])), "New"


def _source_mappings(source_instance, operations):
    result = []
    for name in frappe.get_all("HERO GraphQL Mapping", filters={"source_instance": source_instance,
            "direction": "QUERY_TO_FRAPPE", "status": "Active"}, pluck="name", limit_page_length=0):
        doc = frappe.get_doc("HERO GraphQL Mapping", name)
        if json.loads(doc.mapping_json).get("target_operation") in operations:
            result.append(doc)
    return result


def _rule_values(rule):
    from hero_integration.services.personal_mapping_content import RULE_FIELDS
    return {key: rule.get(key) for key in RULE_FIELDS}


def _semantic_mapping(config, rules):
    """Compare portable configuration without site names, seals or rule UUIDs."""
    result = deepcopy(config)
    ids = result.pop("mapping_definition_ids", [])
    for key in ("query_adapter_seal", "generated_task_seal", "field_rules", "field_rule_bases", "_portable_exchange_schema"):
        result.pop(key, None)
    options = result.get("query_adapter") or {}
    options.pop("read_service", None)
    transformations = options.pop("transform_options", {})
    result["selected_rules"] = sorted(({"rule": _rule_values(rules[key]), "options": transformations.get(key, {})}
                                       for key in ids), key=lambda row: json.dumps(row, sort_keys=True))
    for key in list(result):
        if key.endswith("_service"):
            result[key] = "resolved target service"
    return result


def _existing_equivalent(item, rules, candidates, created=None):
    wanted = _semantic_mapping(item["mapping"], rules)
    for mapping in candidates:
        config = json.loads(mapping.mapping_json)
        if config.get("target_operation") != item["target_operation"]:
            continue
        if any(isinstance(value, str) and value.startswith(SERVICE_REFERENCE)
               and created and value[len(SERVICE_REFERENCE):] in created
               and config.get(key) != created[value[len(SERVICE_REFERENCE):]]
               for key, value in item["mapping"].items() if key.endswith("_service")):
            continue
        ids = config.get("mapping_definition_ids") or []
        existing = {row.definition_id: row for row in frappe.get_all("HERO Frappe Mapping",
            filters={"definition_id": ["in", ids], "status": "Active"}, fields=["*"], limit_page_length=0)}
        if set(existing) == set(ids) and _semantic_mapping(config, existing) == wanted:
            return mapping
    return None


def _replacement_check(snapshot, payload, rules):
    from hero_integration.services.direct_mapping import MappingSpec, validate_spec
    from hero_integration.services.mapping_runtime import plan_from_definitions
    from hero_integration.services.query_mapping import validate_v2_spec, seal_query
    from hero_integration.services.schema_provisioning import _definitions, _PlannedMetadata, preview_schema
    from graphql import build_client_schema, parse, validate
    from hero_integration.services.graphql_graph import normalize_schema
    from hero_integration.client.graphql import assert_read_only

    schema = payload["mapping_package"]["schema"]
    schema_preview = preview_schema(schema)
    if any(row["state"] in {"Invalid", "Conflict"} for row in schema_preview["items"]):
        raise ConfigExchangeError("Replacement schema contains invalid or conflicting declarations.")
    metadata = _PlannedMetadata(_definitions(schema))
    graphql_schema = build_client_schema({"__schema": normalize_schema(json.loads(snapshot.snapshot_json))})
    for item in payload["read_services"]:
        assert_read_only(item["query"])
        if validate(graphql_schema, parse(item["query"])):
            raise ConfigExchangeError("Replacement query does not match its schema: {0}", item["target_operation"])
        config = deepcopy(item["mapping"])
        specs = [MappingSpec(operation=item["target_operation"], definition_id=identity, **_rule_values(rules[identity]))
                 for identity in config["mapping_definition_ids"]]
        options = config.get("query_adapter")
        for spec in specs:
            try:
                if options:
                    validate_v2_spec(spec, metadata, options)
                else:
                    validate_spec(spec, metadata)
            except ValueError as error:
                raise ConfigExchangeError('Mapping field {0}.{1}: {2}', spec.target_doctype, spec.target_field, str(error)) from error
        from hero_integration.services import direct_operations as direct
        targets = [direct.target_key(spec) if item["target_operation"] in direct.OPERATIONS
                   else (spec.target_doctype, spec.target_field) for spec in specs if spec.purpose != "Record Name"]
        if len(targets) != len(set(targets)):
            raise ConfigExchangeError("Replacement mapping contains duplicate field targets.")
        plan = plan_from_definitions(item["mapping_operation_name"], specs, target_operation=item["target_operation"],
                                    adapter_options=options, native_task=bool(config.get("native_task_onboarding")))
        if options:
            options["read_service"] = "replacement-preview"
            service = SimpleNamespace(name="replacement-preview", operation_name=item["operation_name"], query_document=item["query"])
            seal_query(service, snapshot, config, plan)
    if payload.get('mutation_mappings'):
        from hero_integration.services.mapped_update_v2 import validate_sources
        try:
            for mutation in payload['mutation_mappings']:
                validate_sources(mutation['content'], field_provider=metadata.field)
        except ValueError as error:
            raise ConfigExchangeError.from_error(error) from error
    return schema_preview


def _replacement_preview(snapshot_name, payload, source_instance):
    rules = _replacement_rules(payload)
    snapshot, state = _source_snapshot(snapshot_name, payload, source_instance)
    schema = _replacement_check(snapshot, payload, rules)
    operations = [operation for operation in AREA_ORDER if any(item["target_operation"] == operation for item in payload["read_services"])]
    mappings = _source_mappings(source_instance, operations)
    _check_native_parents(snapshot, payload, mappings)
    keep, created, services, new_rules = set(), {}, [], 0
    planned_native_ids = {}
    for item in sorted(payload["read_services"], key=lambda value: AREA_ORDER.index(value["target_operation"])):
        equivalent = _existing_equivalent(item, rules, mappings, created)
        reader = None
        if equivalent is not None and equivalent.schema_snapshot != snapshot.name:
            equivalent = None
        if equivalent is not None:
            if item["mapping"].get("query_adapter"):
                name = json.loads(equivalent.mapping_json)["query_adapter"].get("read_service")
                reader = frappe.get_doc("HERO GraphQL Service", name) if name else None
                if reader is None or reader.query_document.strip() != item["query"].strip():
                    equivalent, reader = None, None
            else:
                reader = _matching_reader(equivalent, item)
        status = "Replace"
        if equivalent is not None:
            keep.add(equivalent.name)
            status = "Approve"
            if (reader is not None and reader.execution_status == "Approved" and reader.read_mapping == equivalent.name
                    and int(reader.approved_service_revision or 0) == int(reader.service_revision)
                    and int(reader.approved_mapping_revision or 0) == int(equivalent.mapping_revision)):
                status = "Unchanged"
        else:
            additions = len(item["mapping"]["mapping_definition_ids"])
            if not item["mapping"].get("query_adapter"):
                native = _native_parent(snapshot, item, mappings)
                planned_native_ids[native.name] = _unchanged_native_rule_ids(snapshot, item, rules, native)
                additions -= len(planned_native_ids[native.name])
                keep.add(native.name)
                reader = _native_bound_reader(native, mappings)
            new_rules += additions
        created[item["target_operation"]] = reader.name if reader else "replacement-preview:" + item["target_operation"]
        services.append({"target_operation": item["target_operation"], "state": status})
    retired = {doc.name for doc in mappings if doc.name not in keep}
    protected = set()
    for name in frappe.get_all("HERO GraphQL Mapping", filters={"status": "Active", "direction": "QUERY_TO_FRAPPE"},
                              pluck="name", limit_page_length=0):
        if name in planned_native_ids:
            protected.update(planned_native_ids[name])
        elif name not in retired:
            protected.update(json.loads(frappe.get_doc("HERO GraphQL Mapping", name).mapping_json).get("mapping_definition_ids", []))
    snapshots = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"source_instance": source_instance}, pluck="name", limit_page_length=0)
    fields = frappe.get_all("HERO Frappe Mapping", filters={"schema_snapshot": ["in", snapshots], "operation": ["in", operations],
                           "status": "Active"}, fields=["definition_id"], limit_page_length=0) if snapshots else []
    return {"format_version": payload["format_version"], "snapshot": state, "settings": sorted(_settings_changes(payload)),
            "mapping_package": {**schema["counts"], "New": schema["counts"].get("New", 0) + new_rules},
            "rebased_from": None, "adopt": new_rules, "replace_mappings": True,
            "replacement": {"operations": operations, "field_mappings": sum(row.definition_id not in protected for row in fields),
                            "graphql_mappings": len(retired)}, "read_services": services}


def _save_replacement_reader(snapshot, item, existing=None):
    from hero_integration import api
    body = {"name": item["service_name"], "operation_name": item["operation_name"], "query": item["query"]}
    # Native reader identity is also a Project.customer_service relation. Its
    # revisioned update invalidates approval until the new mapping is published.
    if existing is not None and existing.schema_snapshot != snapshot.name:
        raise ConfigExchangeError("The native read service belongs to another snapshot.")
    saved = api._save_workspace_service(snapshot.name, api._workspace_payload(json.dumps(body)),
        existing.name if existing else "", int(existing.service_revision or 0) if existing else 0, "GraphQL Services")["query"]
    service = frappe.get_doc("HERO GraphQL Service", saved["query_id"])
    api._workspace_service_validate(snapshot.name, "", service.name, int(service.service_revision or 0))
    service.reload()
    return service


def _new_replacement_mapping(snapshot, item, rules, source_instance, created, service, package_key, schema):
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    config = _resolved_mapping(item, source_instance, {**created, item["target_operation"]: service.name})
    config = deepcopy(config)
    config.pop("query_adapter_seal", None)
    config.pop("generated_task_seal", None)
    config.pop("field_rule_bases", None)
    operation = item["target_operation"]
    identity = str(uuid5(NAMESPACE_URL, package_key + "/" + operation))
    remapped = {key: str(uuid5(NAMESPACE_URL, identity + "/" + key)) for key in config["mapping_definition_ids"]}
    config["field_rules"] = {remapped[key]: _rule_values(rules[key]) for key in config["mapping_definition_ids"]}
    config["mapping_definition_ids"] = list(remapped.values())
    if config.get("query_adapter"):
        config["query_adapter"]["read_service"] = service.name
        config["query_adapter"]["transform_options"] = {remapped[key]: value for key, value in
            config["query_adapter"].get("transform_options", {}).items()}
    dependencies = sorted({value for key in remapped for value in rules[key].get("schema_dependencies", [])})
    adapter = config.get('query_adapter') or {}
    owned_targets = {('Project', relation['parent_field']) for relation in adapter.get('native_relations', [])
                     if relation.get('parent_field')}
    if adapter.get('logbook'):
        owned_targets.add(('Comment', 'custom_hero_project'))
    relation_fields = {field['id'] for field in schema.get('custom_fields', [])
                       if (field['doctype'], field['fieldname']) in owned_targets}
    dependencies = sorted(set(dependencies) | relation_fields | {
        relation['id'] for relation in schema.get('relations', []) if relation['field_id'] in relation_fields})
    if dependencies:
        config["_portable_exchange_schema"] = {"schema": deepcopy(schema), "dependencies": dependencies}
    definition = {"site_scope": frappe.local.site, "definition_id": identity, "source_instance": source_instance,
                  "snapshot_name": snapshot.name, "schema_fingerprint": snapshot.schema_fingerprint,
                  "direction": "QUERY_TO_FRAPPE", "operation_kind": "QUERY", "operation_name": item["mapping_operation_name"]}
    storage = FrappeMappingDraftStorage()
    draft = storage.service().open_new_draft(definition, frappe.session.user, config)
    draft = storage.service().save(identity, frappe.session.user, draft.content_revision, 0, config,
        expected_generation=draft.generation,
        expected_reconciliation_revision=draft.reconciliation_revision).draft
    saved = storage.publish(identity, frappe.session.user, draft.generation, draft.content_revision, 0,
                            draft.reconciliation_revision, confirmed=True)
    return frappe.get_doc("HERO GraphQL Mapping", saved["name"])


def _native_bound_reader(parent, candidates=(), *, lock=False):
    rows = frappe.get_all("HERO GraphQL Service", filters={"read_mapping": parent.name,
        "schema_snapshot": parent.schema_snapshot, "status": "Validated"},
        fields=["name", "execution_status"], order_by="modified desc", limit_page_length=0)
    references = {json.loads(mapping.mapping_json).get("customer_service") for mapping in candidates}
    rows.sort(key=lambda row: (row.name not in references, row.execution_status != "Approved"))
    return frappe.get_doc("HERO GraphQL Service", rows[0].name, for_update=lock) if rows else None


def _matching_reader(mapping, item):
    for name in frappe.get_all("HERO GraphQL Service", filters={"read_mapping": mapping.name,
            "schema_snapshot": mapping.schema_snapshot, "status": "Validated"}, pluck="name",
            order_by="modified desc", limit_page_length=0):
        service = frappe.get_doc("HERO GraphQL Service", name)
        if service.query_document.strip() == item["query"].strip() and service.operation_name == item["operation_name"]:
            return service
    return None


def _retire_replaced(source_instance, operations, keep, keep_services):
    """Retain history and foreign references; retire only obsolete source selections.

    Field target locks are the same locks used by personal publication. Current
    active mappings are locked before the final reference check. Revision bumps
    invalidate old readers and drafts without modifying historical field content.
    """
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    old = [doc for doc in _source_mappings(source_instance, operations) if doc.name not in keep]
    old_names = {doc.name for doc in old}
    snapshots = frappe.get_all("HERO GraphQL Schema Snapshot", filters={"source_instance": source_instance}, pluck="name", limit_page_length=0)
    fields = frappe.get_all("HERO Frappe Mapping", filters={"schema_snapshot": ["in", snapshots], "operation": ["in", operations],
        "status": "Active"}, fields=["name", "operation", "target_doctype", "target_field"], limit_page_length=0) if snapshots else []
    for row in sorted(fields, key=lambda row: (row.operation, row.target_doctype, row.target_field)):
        _lock_active_target(row.operation, row.target_doctype, row.target_field)
    selected = set()
    current = frappe.db.sql("select name, mapping_json from `tabHERO GraphQL Mapping` "
                           "where status='Active' and direction='QUERY_TO_FRAPPE' for update", as_dict=True)
    for row in current:
        if row.name not in old_names:
            selected.update(json.loads(row.mapping_json).get("mapping_definition_ids", []))
    retired = 0
    for row in fields:
        field = frappe.get_doc("HERO Frappe Mapping", row.name, for_update=True)
        if field.status == "Active" and field.schema_snapshot in snapshots and field.operation in operations and field.definition_id not in selected:
            frappe.db.set_value("HERO Frappe Mapping", field.name, {"status": "Disabled", "mapping_revision": int(field.mapping_revision or 0) + 1})
            retired += 1
    for previous in old:
        doc = frappe.get_doc("HERO GraphQL Mapping", previous.name, for_update=True)
        if (doc.source_instance != source_instance or doc.status != "Active"
                or int(doc.mapping_revision) != int(previous.mapping_revision)):
            raise ConfigExchangeError("A mapping changed during replacement; preview again.")
        frappe.db.set_value("HERO GraphQL Mapping", doc.name, {"status": "Disabled", "mapping_revision": int(doc.mapping_revision) + 1})
    for name in frappe.get_all("HERO GraphQL Service", filters={"read_mapping": ["in", list(old_names | keep)],
            "schema_snapshot": ["in", snapshots], "execution_status": "Approved"}, pluck="name", limit_page_length=0):
        if name not in keep_services:
            service = frappe.get_doc("HERO GraphQL Service", name, for_update=True)
            frappe.db.set_value("HERO GraphQL Service", name, {"execution_status": "Pending Approval", "approved_service_revision": 0, "approved_mapping_revision": 0})
    return {"operations": list(operations), "field_mappings": retired, "graphql_mappings": len(old)}


def _replace_config(snapshot_name, payload, source_instance):
    from hero_integration import api
    from hero_integration.services.schema_provisioning import provision_schema
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    rules = _replacement_rules(payload)
    snapshot, state = _source_snapshot(snapshot_name, payload, source_instance)
    _replacement_check(snapshot, payload, rules)
    operations = [operation for operation in AREA_ORDER if any(item["target_operation"] == operation for item in payload["read_services"])]
    candidates = _source_mappings(source_instance, operations)
    _check_native_parents(snapshot, payload, candidates)
    # Schema DDL is explicitly completed before mapping writes/savepoints.
    provision_schema(payload["mapping_package"]["schema"])
    if state == "New":
        snapshot, state = _source_snapshot(snapshot_name, payload, source_instance, create=True)
    lock_exchange_context(source_instance, snapshot.name, "QUERY_TO_FRAPPE")
    operations = [operation for operation in AREA_ORDER if any(item["target_operation"] == operation for item in payload["read_services"])]
    candidates = _source_mappings(source_instance, operations)
    package_key = "hero-config:" + source_instance + ":" + snapshot.schema_fingerprint + ":" + sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    created, kept, results = {}, set(), []
    frappe.db.savepoint("hero_configuration_replace")
    try:
        for item in sorted(payload["read_services"], key=lambda value: AREA_ORDER.index(value["target_operation"])):
            equivalent = _existing_equivalent(item, rules, candidates, created)
            parent = _native_parent(snapshot, item, candidates) if not item["mapping"].get("query_adapter") else None
            mapping = equivalent if equivalent is not None and equivalent.schema_snapshot == snapshot.name else None
            if mapping is not None and item["mapping"].get("query_adapter"):
                # Its seal binds its own saved query; retain that query as well.
                current = json.loads(mapping.mapping_json)["query_adapter"]["read_service"]
                reader = frappe.get_doc("HERO GraphQL Service", current)
                if reader.query_document.strip() != item["query"].strip():
                    mapping = None
            if mapping is not None and not item["mapping"].get("query_adapter"):
                reader = _matching_reader(mapping, item)
            if mapping is None or reader is None:
                reader = _save_replacement_reader(snapshot, item, _native_bound_reader(parent, candidates, lock=True) if parent is not None else None)
            if mapping is None:
                if parent is not None:
                    from hero_integration.services.config_native_replace import replace_native_mapping
                    mapping = replace_native_mapping(snapshot, item, rules, source_instance, created, reader, parent, payload["mapping_package"]["schema"])
                else:
                    mapping = _new_replacement_mapping(snapshot, item, rules, source_instance, created, reader, package_key, payload["mapping_package"]["schema"])
            unchanged = (reader.read_mapping == mapping.name and reader.execution_status == "Approved"
                         and int(reader.approved_service_revision or 0) == int(reader.service_revision)
                         and int(reader.approved_mapping_revision or 0) == int(mapping.mapping_revision))
            if unchanged:
                from hero_integration.services.read_contract import from_documents
                from_documents(reader, mapping, snapshot)
            else:
                api.approve_read_service(reader.name, mapping.name, int(reader.service_revision), int(mapping.mapping_revision))
            created[item["target_operation"]] = reader.name
            kept.add(mapping.name)
            results.append({"target_operation": item["target_operation"], "state": "Unchanged" if unchanged else "Approved", "service": reader.name})
        _archive_replaced_drafts(source_instance, operations)
        replacement = _retire_replaced(source_instance, operations, kept, set(created.values()))
        _verify_replacement_readers(snapshot, created)
        from hero_integration.services.config_mutation_drafts import save as save_mutation_drafts
        try:
            mutation_result = save_mutation_drafts(payload, source_instance, snapshot.name)
        except ValueError as error:
            raise ConfigExchangeError.from_error(error) from error
        changes = _settings_changes(payload)
        _apply_settings(changes)
    except Exception:
        frappe.db.rollback(save_point="hero_configuration_replace")
        raise
    return {"snapshot": state, "snapshot_name": snapshot.name, "settings": sorted(changes), "adopted": len(rules),
            "adoption": None, "read_services": results, "replace_mappings": True, "replacement": replacement,
            **mutation_result}


def _archive_replaced_drafts(source_instance, operations):
    from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
    actor = frappe.session.user
    for name in frappe.get_all("HERO Mapping Draft", filters={"owner_user": actor, "site_scope": frappe.local.site,
                                                             "state": "OPEN"}, pluck="name", limit_page_length=0):
        doc = frappe.get_doc("HERO Mapping Draft", name, for_update=True)
        content = json.loads(doc.content_json)
        if content.get("target_operation") not in operations:
            continue
        if doc.base_active_mapping:
            parent = frappe.get_doc("HERO GraphQL Mapping", doc.base_active_mapping)
            matches = parent.source_instance == source_instance and parent.direction == "QUERY_TO_FRAPPE"
        else:
            context = json.loads(doc.new_definition_json or "{}")
            matches = context.get("source_instance") == source_instance and context.get("direction") == "QUERY_TO_FRAPPE"
        if matches:
            FrappeMappingDraftStorage().service().discard(doc.definition_id, actor, int(doc.generation),
                                                        int(doc.content_revision), confirmed=True)


def _native_parent(snapshot, item, candidates):
    matches = [mapping for mapping in candidates if mapping.schema_snapshot == snapshot.name
               and json.loads(mapping.mapping_json).get("target_operation") == item["target_operation"]]
    if len(matches) != 1:
        raise ConfigExchangeError("Native mapping replacement requires one current source-bound parent: {0}", item["target_operation"])
    return matches[0]


def _check_native_parents(snapshot, payload, candidates):
    for item in payload["read_services"]:
        if item["mapping"].get("query_adapter"):
            continue
        parent = _native_parent(snapshot, item, candidates)
        incoming = item["mapping"]
        existing = json.loads(parent.mapping_json)
        mutable = {"mapping_definition_ids"} | {
            "HeroCustomers": {"customer_contract_version"},
            "HeroProjectMatches": {"project_contract_version", "customer_service"},
        }.get(item["target_operation"], set())
        immutable = {"target_operation", "records_path", "identity_path", "variables", "page_size",
                     "native_options", "native_task_onboarding"}
        if set(incoming) - mutable - immutable - {"query_adapter_seal", "generated_task_seal"}:
            raise ConfigExchangeError("Native replacement contains unsupported configuration fields.")
        if incoming.get("native_task_onboarding") != existing.get("native_task_onboarding"):
            raise ConfigExchangeError("Native Task replacement cannot introduce or change onboarding authority.")
        for key in immutable:
            if incoming.get(key) != existing.get(key):
                raise ConfigExchangeError("Native replacement changes a target-owned reader contract: {0}", key)


def _verify_replacement_readers(snapshot, created):
    """Cleanup must never invalidate a newly approved reader or its lineage."""
    from hero_integration.services.read_contract import from_documents
    for operation, name in created.items():
        reader = frappe.get_doc("HERO GraphQL Service", name)
        mapping = frappe.get_doc("HERO GraphQL Mapping", reader.read_mapping)
        contract = from_documents(reader, mapping, snapshot)
        if contract.target_operation != operation:
            raise ConfigExchangeError("A replacement reader points to another mapping area.")


def _unchanged_native_rule_ids(snapshot, item, rules, parent):
    """Mirror the native publisher's target reuse for an accurate read-only preview."""
    from hero_integration.services.config_native_replace import _target
    from hero_integration.services.personal_mapping_content import RULE_FIELDS
    selected = set(json.loads(parent.mapping_json).get("mapping_definition_ids", []))
    available = frappe.get_all("HERO Frappe Mapping", filters={"schema_snapshot": snapshot.name,
        "operation": item["target_operation"], "status": "Active"},
        fields=["definition_id", "personal_graphql_mapping", *sorted(RULE_FIELDS)], limit_page_length=0)
    targets = {}
    for row in sorted(available, key=lambda row: (row.definition_id not in selected, row.definition_id)):
        if row.personal_graphql_mapping in (None, "", parent.name):
            targets.setdefault(_target(row), row)
    unchanged = set()
    for identity in item["mapping"]["mapping_definition_ids"]:
        desired = _rule_values(rules[identity])
        current = targets.get(_target(desired))
        if current is not None and _rule_values(current) == desired:
            unchanged.add(current.definition_id)
    return unchanged
