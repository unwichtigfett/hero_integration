"""Versioned, deterministic mapping configuration import and export."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

import frappe

from hero_integration.services.direct_mapping import MappingSpec, validate_spec
from hero_integration.services.direct_mapping import PURPOSE_RECORD_NAME
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.localized_error import LocalizedError


FORMAT_VERSION = "hero-frappe-mapping-package-v2"
LEGACY_FORMAT_VERSION = "hero-frappe-mapping-v1"
MAX_IMPORT_BYTES = 1_000_000
EXPORTED_FIELDS = (
    "definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field",
    "purpose", "transformation", "relation", "priority", "schema_dependencies", "mapping_revision",
)


class MappingExchangeError(LocalizedError):
    pass


def export_json(snapshot_name: str) -> str:
    snapshot = _validated_snapshot(snapshot_name)
    mappings = frappe.get_all(
        "HERO Frappe Mapping",
        filters={"schema_fingerprint": snapshot.schema_fingerprint},
        fields=[*EXPORTED_FIELDS, "schema_manifest", "personal_graphql_mapping"],
        order_by="operation asc, priority asc, definition_id asc",
    )
    mappings = [row for row in mappings if not row.get('personal_graphql_mapping')]
    schema = _merge_export_schemas(mappings)
    exported = [_exportable(row) for row in mappings]
    from hero_integration.services.schema_provisioning import _target_dependency
    checklist_ids = {item["id"] for group in schema.values() for item in group}
    for item in exported:
        dependencies = item["schema_dependencies"]
        required = _target_dependency(schema, item)
        if any(dependency not in checklist_ids for dependency in dependencies) or (required and required not in dependencies):
            raise MappingExchangeError("Mapping export contains invalid schema dependencies.")
    payload = {
        "format_version": FORMAT_VERSION,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "schema": schema,
        "mappings": exported,
    }
    return _canonical_json(payload)


def _merge_export_schemas(mappings) -> dict[str, list[object]]:
    """Union identical declarations, then validate the complete exported schema."""
    from hero_integration.services.schema_provisioning import SchemaProvisioningError, _definitions

    groups = {key: {} for key in _empty_schema()}
    global_ids = {}
    for row in mappings:
        manifest = row.get("schema_manifest")
        if manifest is None or manifest == "":
            continue
        if not isinstance(manifest, str):
            raise MappingExchangeError("Mapping export contains an invalid schema manifest.")
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON field")
                    result[key] = value
                return result
            parsed = json.loads(manifest, object_pairs_hook=unique_fields)
            _validate_schema(parsed)
            for key, entries in parsed.items():
                for entry in entries:
                    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                        raise ValueError("Invalid schema definition ID")
                    identity = entry["id"]
                    canonical = _canonical_json(entry)
                    if identity in global_ids and global_ids[identity] != (key, canonical):
                        raise ValueError("Conflicting schema definition ID")
                    global_ids[identity] = (key, canonical)
                    groups[key][identity] = entry
        except (TypeError, ValueError, MappingExchangeError) as error:
            raise MappingExchangeError("Mapping export contains an invalid or conflicting schema manifest.") from error
    schema = {key: [entries[identity] for identity in sorted(entries)] for key, entries in groups.items()}
    try:
        _definitions(schema)
    except (SchemaProvisioningError, KeyError, TypeError, ValueError) as error:
        raise MappingExchangeError("Mapping export contains invalid or conflicting schema definitions.") from error
    return schema


def preview_import(snapshot_name: str, payload_json: str) -> dict[str, object]:
    snapshot = _validated_snapshot(snapshot_name)
    payload = _parse_payload(payload_json, snapshot.schema_fingerprint)
    return preview_import_from_package(payload, snapshot.schema_fingerprint)


def preview_import_from_package(payload: dict[str, object], fingerprint: str | None = None, metadata=None) -> dict[str, object]:
    """Preview a package already checked against a selected snapshot."""
    existing = {
        row.definition_id: row
        for row in frappe.get_all(
            "HERO Frappe Mapping",
            fields=["name", "definition_id", "status", "mapping_revision", "personal_graphql_mapping", *EXPORTED_FIELDS[1:-1]],
        )
    }
    rows = []
    seen = set()
    claimed_targets: dict[tuple[str, str, str], str] = {}
    active_targets = {
        (row.operation, row.target_doctype, row.target_field): row.definition_id
        for row in existing.values() if row.status == "Active" and not getattr(row, 'personal_graphql_mapping', None)
    }
    for item in payload["mappings"]:
        result = _preview_item(item, existing, seen, metadata)
        _mark_target_conflict(item, result, claimed_targets, active_targets)
        rows.append(result)
    counts = {status: sum(row["state"] == status for row in rows) for status in ("New", "Unchanged", "Changed", "Conflict", "Invalid")}
    return {
        "format_version": FORMAT_VERSION,
        "schema_fingerprint": fingerprint or str(payload["schema_fingerprint"]),
        "schema": payload["schema"],
        "items": rows,
        "counts": counts,
    }


def import_json(snapshot_name: str, payload_json: str) -> dict[str, object]:
    preview = preview_import(snapshot_name, payload_json)
    if preview["schema"] != _empty_schema():
        raise MappingExchangeError("Schema packages require checked bulk adoption.")
    blocked = [item for item in preview["items"] if item["state"] in {"Conflict", "Invalid"}]
    if blocked:
        raise MappingExchangeError("Import contains conflicts or invalid definitions.")
    payload = _parse_payload(payload_json, preview["schema_fingerprint"])
    existing = {
        row.definition_id: row
        for row in frappe.get_all(
            "HERO Frappe Mapping",
            fields=["name", "definition_id", "status", "mapping_revision", "personal_graphql_mapping", *EXPORTED_FIELDS[1:-1]],
        )
    }
    savepoint = "hero_mapping_import"
    frappe.db.savepoint(savepoint)
    try:
        created = changed = unchanged = 0
        for item in payload["mappings"]:
            local = existing.get(item["definition_id"])
            if local and _definition_hash(_exportable(local)) == _definition_hash(item):
                unchanged += 1
                continue
            values = {key: item[key] for key in EXPORTED_FIELDS if key not in {"mapping_revision"}}
            values["schema_dependencies"] = _canonical_json({"ids": item["schema_dependencies"]})
            values.update({
                "doctype": "HERO Frappe Mapping",
                "schema_snapshot": snapshot_name,
                "schema_manifest": _canonical_json(payload["schema"]),
                "status": "Draft",
            })
            if local:
                document = frappe.get_doc("HERO Frappe Mapping", local.name)
                document.update(values)
                document.save()
                changed += 1
            else:
                document = frappe.get_doc(values)
                document.mapping_revision = max(1, int(item["mapping_revision"] or 1))
                document.insert()
                created += 1
        return {"created": created, "changed": changed, "unchanged": unchanged, "status": "DRAFT_ONLY"}
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _preview_item(item: dict[str, object], existing: dict[str, object], seen: set[str], metadata=None) -> dict[str, object]:
    definition_id = str(item.get("definition_id") or "")
    if not definition_id or definition_id in seen:
        return {"definition_id": definition_id, "state": "Invalid", "detail": "Definition ID is missing or duplicated."}
    seen.add(definition_id)
    try:
        spec = MappingSpec(**{key: item[key] for key in asdict(MappingSpec("", "", "", "", "")).keys()})
        validate_spec(spec, metadata or _FrappeMetadata())
    except (KeyError, TypeError, ValueError) as error:
        return {"definition_id": definition_id, "state": "Invalid", "detail": str(error)}
    local = existing.get(definition_id)
    if not local:
        return {"definition_id": definition_id, "state": "New", "detail": "Validated import definition."}
    if getattr(local, 'personal_graphql_mapping', None):
        return {"definition_id": definition_id, "state": "Conflict", "detail": "A scoped published rule cannot be replaced by a shared mapping import."}
    if _definition_hash(_exportable(local)) == _definition_hash(item):
        return {"definition_id": definition_id, "state": "Unchanged", "detail": "Definition is already present."}
    if local.status == "Active":
        return {"definition_id": definition_id, "state": "Conflict", "detail": "An active local definition cannot be replaced by import."}
    return {"definition_id": definition_id, "state": "Changed", "detail": "A local Draft or Disabled definition will be revised as Draft."}


def _mark_target_conflict(
    item: dict[str, object], result: dict[str, object], claimed: dict[tuple[str, str, str], str], active: dict[tuple[str, str, str], str],
) -> None:
    if result["state"] in {"Invalid", "Conflict"} or item["purpose"] == PURPOSE_RECORD_NAME:
        return
    target = (str(item["operation"]), str(item["target_doctype"]), str(item["target_field"]))
    active_owner = active.get(target)
    if active_owner and active_owner != item["definition_id"]:
        result.update(state="Conflict", detail="An active local definition already owns this target field.")
        return
    owner = claimed.get(target)
    if owner and owner != item["definition_id"]:
        result.update(state="Conflict", detail="Another imported definition already owns this target field.")
        return
    claimed[target] = str(item["definition_id"])


def _parse_payload(payload_json: str, fingerprint: str) -> dict[str, object]:
    if not isinstance(payload_json, str) or len(payload_json.encode()) > MAX_IMPORT_BYTES:
        raise MappingExchangeError("Mapping import exceeds the permitted size.")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise MappingExchangeError('Mapping import is not valid JSON: {0}', error.msg) from error
    if not isinstance(payload, dict) or payload.get("format_version") not in {FORMAT_VERSION, LEGACY_FORMAT_VERSION}:
        raise MappingExchangeError("Mapping import format version is not supported.")
    expected_keys = {"format_version", "schema_fingerprint", "mappings"}
    if payload["format_version"] == FORMAT_VERSION:
        expected_keys.add("schema")
    if set(payload) != expected_keys:
        raise MappingExchangeError("Mapping import has unknown or missing package fields.")
    if payload.get("schema_fingerprint") != fingerprint:
        raise MappingExchangeError("Mapping import schema fingerprint does not match the selected snapshot.")
    schema = _empty_schema() if payload.get("format_version") == LEGACY_FORMAT_VERSION else payload.get("schema")
    _validate_schema(schema)
    mappings = payload.get("mappings")
    if not isinstance(mappings, list):
        raise MappingExchangeError("Mapping import must contain a mappings list.")
    normalized = []
    for item in mappings:
        if not isinstance(item, dict):
            raise MappingExchangeError("Mapping import has an unknown or missing definition field.")
        if payload.get("format_version") == LEGACY_FORMAT_VERSION:
            item = {**item, "schema_dependencies": []}
        if set(item) != set(EXPORTED_FIELDS):
            raise MappingExchangeError("Mapping import has an unknown or missing definition field.")
        if not isinstance(item["schema_dependencies"], list) or not all(isinstance(value, str) for value in item["schema_dependencies"]):
            raise MappingExchangeError("Mapping schema dependencies must be a list of definition IDs.")
        normalized.append({field: item[field] for field in EXPORTED_FIELDS})
    return {"format_version": FORMAT_VERSION, "schema_fingerprint": fingerprint, "schema": schema, "mappings": normalized}


def _validated_snapshot(snapshot_name: str):
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", snapshot_name)
    if snapshot.analysis_status != "Validated":
        raise MappingExchangeError("The selected GraphQL schema snapshot is not valid.")
    if analyze_snapshot(json.loads(snapshot.snapshot_json)).fingerprint != snapshot.schema_fingerprint:
        raise MappingExchangeError("The selected GraphQL schema snapshot is stale.")
    return snapshot


def _exportable(row) -> dict[str, object]:
    result = {field: row.get(field, "") for field in EXPORTED_FIELDS}
    stored = result["schema_dependencies"]
    if not isinstance(stored, str):
        raise MappingExchangeError("Mapping has invalid schema dependencies.")
    def unique_fields(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise MappingExchangeError("Mapping has duplicate schema dependency fields.")
            fields[key] = value
        return fields

    try:
        dependencies = json.loads(stored, object_pairs_hook=unique_fields)
    except (TypeError, ValueError) as error:
        raise MappingExchangeError("Mapping has invalid schema dependencies.") from error
    # The DocType default is the legacy empty JSON array.
    if dependencies == []:
        result["schema_dependencies"] = []
    elif (isinstance(dependencies, dict) and set(dependencies) == {"ids"}
          and isinstance(dependencies["ids"], list)
          and all(isinstance(value, str) for value in dependencies["ids"])):
        result["schema_dependencies"] = dependencies["ids"]
    else:
        raise MappingExchangeError("Mapping has invalid or unsupported schema dependencies.")
    return result


def _definition_hash(item: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(item).encode()).hexdigest()


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _empty_schema() -> dict[str, list[object]]:
    return {"custom_doctypes": [], "custom_fields": [], "relations": []}


def _validate_schema(schema: object) -> None:
    """Accept data-only schema declarations. They cannot carry code or permissions."""
    if not isinstance(schema, dict) or set(schema) != set(_empty_schema()):
        raise MappingExchangeError("Mapping schema must contain only custom doctypes, custom fields, and relations.")
    for key in _empty_schema():
        if not isinstance(schema[key], list):
            raise MappingExchangeError('Mapping schema {0} must be a list.', key)


class _FrappeMetadata:
    def ensure_write(self, doctype: str):
        from hero_integration.services.doctype_permissions import ensure_doctype_write
        ensure_doctype_write(doctype, frappe)

    def field(self, doctype: str, fieldname: str):
        return frappe.get_meta(doctype).get_field(fieldname)
