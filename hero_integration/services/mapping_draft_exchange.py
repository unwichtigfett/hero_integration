"""Direction-scoped exchange of existing personal mapping drafts.

The package is deliberately a collection of the authenticated actor's
already-existing Draft records.  It is not a second mapping store and cannot
create, activate or execute a GraphQL mapping.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError
from hero_integration.services.personal_mapping_content import bounded
from hero_integration.services.schema_provisioning import (
    SchemaProvisioningError,
    preview_schema,
    provision_schema,
)


FORMAT_VERSION = "hero-personal-mapping-draft-exchange-v1"
_DIRECTIONS = {
    "QUERY_TO_FRAPPE": "QUERY",
    "FRAPPE_TO_MUTATION": "MUTATION",
}
_PACKAGE_KEYS = {
    "format_version", "source_instance", "snapshot_name", "schema_fingerprint", "direction", "schema", "mappings",
}
_ITEM_KEYS = {
    "definition_id", "operation_kind", "operation_name", "direction", "base_active_name",
    "base_active_revision", "base_active_hash", "base_schema_fingerprint", "content",
}


class MappingDraftExchangeError(ValueError):
    pass


def export_package(source_instance: str, snapshot_name: str, direction: str) -> str:
    """Export only the current actor's OPEN drafts in one exact direction."""
    active = _active_definitions(source_instance, snapshot_name, direction)
    fingerprints = {str(_value(row, "schema_fingerprint")) for row in active.values()}
    if len(fingerprints) != 1:
        raise MappingDraftExchangeError("The selected source context has no single validated schema fingerprint.")
    drafts = _open_drafts(set(active))
    mappings = []
    for definition_id in sorted(drafts):
        row, draft = active[definition_id], drafts[definition_id]
        mappings.append({
            "definition_id": definition_id,
            "operation_kind": str(_value(row, "operation_kind")),
            "operation_name": str(_value(row, "operation_name")),
            "direction": direction,
            "base_active_name": str(_value(draft, "base_active_mapping")),
            "base_active_revision": _positive_int(_value(draft, "base_active_revision")),
            "base_active_hash": str(_value(draft, "base_active_hash")),
            "base_schema_fingerprint": str(_value(draft, "base_schema_fingerprint")),
            "content": _content(_value(draft, "content_json")),
        })
    return _json({
        "format_version": FORMAT_VERSION,
        "source_instance": source_instance,
        "snapshot_name": snapshot_name,
        "schema_fingerprint": next(iter(fingerprints)),
        "direction": direction,
        "schema": _export_schema(mappings, direction),
        "mappings": mappings,
    })


def _export_schema(mappings, direction):
    """Carry the fixed external-deadline schema only for a selected native rule."""
    empty = {"custom_doctypes": [], "custom_fields": [], "relations": []}
    if direction != 'QUERY_TO_FRAPPE':
        return empty
    for item in mappings:
        content = item['content']
        if content.get('target_operation') != 'HeroTasks' or not content.get('native_task_onboarding'):
            continue
        rules = content.get('field_rules', {})
        for identity in content.get('mapping_definition_ids', []):
            rule = rules.get(identity, {})
            if (rule.get('source_path'), rule.get('target_doctype'), rule.get('target_field'),
                    rule.get('purpose'), rule.get('transformation')) == (
                    'due_date', 'Task', 'custom_hero_due_date', 'Native Binding', 'Native Adapter'):
                return json.loads(Path(__file__).with_name('task_external_deadline_schema.json').read_text())
    return empty


def preview_import(payload_json: str, source_instance: str, snapshot_name: str, direction: str) -> dict[str, object]:
    """Validate a proposed replacement without persisting drafts or schema."""
    package = _parse(payload_json, source_instance, snapshot_name, direction)
    active = _active_definitions(source_instance, snapshot_name, direction)
    fingerprints = {str(_value(row, "schema_fingerprint")) for row in active.values()}
    if fingerprints != {package["schema_fingerprint"]}:
        raise MappingDraftExchangeError("The import schema fingerprint does not match the selected active context.")
    drafts = _open_drafts(set(active))
    incoming = {item["definition_id"]: item for item in package["mappings"]}
    if len(incoming) != len(package["mappings"]):
        raise MappingDraftExchangeError("Import contains duplicate mapping definition identities.")
    if set(incoming) - set(active):
        raise MappingDraftExchangeError("Import contains a definition outside the selected active source and direction.")

    items = []
    for definition_id, item in sorted(incoming.items()):
        row = active[definition_id]
        _assert_identity(item, row, direction)
        _assert_base(item, row)
        local = drafts.get(definition_id)
        local_content = _content(_value(local, "content_json")) if local else None
        state = "NEW" if local is None else "UNCHANGED" if _same(local_content, item["content"]) else "REPLACE"
        items.append({
            "definition_id": definition_id, "state": state, "content_hash": _hash(item["content"]),
            "expected": _expected(row, local), "removal": False,
        })
    for definition_id, local in sorted(drafts.items()):
        if definition_id not in incoming:
            items.append({
                "definition_id": definition_id, "state": "REMOVE", "content_hash": str(_value(local, "content_hash")),
                "expected": _expected(active[definition_id], local), "removal": True,
            })
    schema = preview_schema(package["schema"])
    invalid_schema = [item for item in schema["items"] if item["state"] in {"Conflict", "Invalid"}]
    return {
        "format_version": FORMAT_VERSION, "direction": direction, "source_instance": source_instance,
        "snapshot_name": snapshot_name, "schema_fingerprint": package["schema_fingerprint"],
        "items": items, "schema": schema, "persisted": False,
        "can_save": not invalid_schema,
    }


def save_import(payload_json: str, source_instance: str, snapshot_name: str, direction: str,
                expected_states: object, *, confirmed: bool) -> dict[str, object]:
    """Persist a previewed replacement after optional schema provisioning.

    Schema DDL intentionally runs before the Draft-only DML savepoint.  If a
    later CAS/save fails, the error reports created schema elements; the DML
    savepoint restores the actor's previous Draft collection.
    """
    if confirmed is not True:
        raise MappingDraftExchangeError("Explicit import save confirmation is required.")
    package = _parse(payload_json, source_instance, snapshot_name, direction)
    preview = preview_import(payload_json, source_instance, snapshot_name, direction)
    if not preview["can_save"]:
        raise MappingDraftExchangeError("Schema preview contains conflicts or invalid definitions.")
    expected = _expected_states(expected_states, preview["items"])
    schema_result = provision_schema(package["schema"])
    savepoint = "hero_draft_import_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    created = {"drafts": [], "discarded": [], "unchanged": []}
    try:
        for item in package["mappings"]:
            definition_id = item["definition_id"]
            state = expected[definition_id]
            service = FrappeMappingDraftStorage().service()
            if state["generation"] == 0:
                draft = service.open_draft(definition_id, frappe.session.user)
                _assert_opened_base(item, draft)
                result = service.save(definition_id, frappe.session.user, 0, state["active_revision"], item["content"],
                                      expected_generation=draft.generation, expected_reconciliation_revision=0)
                created["drafts"].append(definition_id)
            else:
                result = service.save(definition_id, frappe.session.user, state["draft_revision"], state["active_revision"], item["content"],
                                      expected_generation=state["generation"],
                                      expected_reconciliation_revision=state["reconciliation_revision"])
            (created["unchanged"] if result.unchanged else created["drafts"]).append(definition_id)
        for item in preview["items"]:
            if not item["removal"]:
                continue
            state = expected[item["definition_id"]]
            FrappeMappingDraftStorage().service().discard(
                item["definition_id"], frappe.session.user, state["generation"], state["draft_revision"], confirmed=True,
            )
            created["discarded"].append(item["definition_id"])
    except Exception as error:
        frappe.db.rollback(save_point=savepoint)
        detail = json.dumps(schema_result["created"], sort_keys=True, separators=(",", ":"))
        raise MappingDraftExchangeError(
            "Import Draft persistence failed; prior Drafts were restored. Schema provisioning result: " + detail
        ) from error
    return {"status": "SAVED_DRAFTS_ONLY", "direction": direction, "schema": schema_result,
            "drafts": {key: sorted(set(value)) for key, value in created.items()}, "activation": False}


def _parse(payload_json: str, source_instance: str, snapshot_name: str, direction: str) -> dict[str, object]:
    if direction not in _DIRECTIONS:
        raise MappingDraftExchangeError("Unsupported mapping direction.")
    if not isinstance(payload_json, str) or len(payload_json.encode("utf-8")) > 1_000_000:
        raise MappingDraftExchangeError("Draft import exceeds the permitted size.")
    try:
        package = json.loads(payload_json, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MappingDraftExchangeError("Draft import is not valid JSON.") from error
    if not isinstance(package, dict) or set(package) != _PACKAGE_KEYS or package.get("format_version") != FORMAT_VERSION:
        raise MappingDraftExchangeError("Draft import format is not supported.")
    if (package.get("source_instance"), package.get("snapshot_name"), package.get("direction")) != (source_instance, snapshot_name, direction):
        raise MappingDraftExchangeError("Draft import does not match the selected source, snapshot and direction.")
    if not isinstance(package.get("schema_fingerprint"), str) or len(package["schema_fingerprint"]) != 64:
        raise MappingDraftExchangeError("Draft import has an invalid schema fingerprint.")
    if not isinstance(package.get("mappings"), list):
        raise MappingDraftExchangeError("Draft import must contain a mappings list.")
    normalized = []
    for item in package["mappings"]:
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            raise MappingDraftExchangeError("Draft import mapping has unknown or missing fields.")
        if item.get("operation_kind") != _DIRECTIONS[direction] or item.get("direction") != direction:
            raise MappingDraftExchangeError("Draft import contains a mixed or wrong mapping direction.")
        if not all(isinstance(item.get(key), str) and item[key] for key in (
            "definition_id", "operation_name", "base_active_name", "base_active_hash", "base_schema_fingerprint",
        )) or type(item.get("base_active_revision")) is not int or item["base_active_revision"] < 1:
            raise MappingDraftExchangeError("Draft import mapping identity is invalid.")
        normalized.append({**item, "content": bounded(_object(item["content"]))})
    return {**package, "mappings": normalized}


def _active_definitions(source_instance: str, snapshot_name: str, direction: str) -> dict[str, object]:
    if direction not in _DIRECTIONS or not all(isinstance(value, str) and value for value in (source_instance, snapshot_name)):
        raise MappingDraftExchangeError("Selected draft context is invalid.")
    rows = frappe.get_all(
        "HERO GraphQL Mapping",
        filters={"status": "Active", "source_instance": source_instance, "schema_snapshot": snapshot_name, "direction": direction},
        fields=["name", "definition_id", "schema_fingerprint", "operation_kind", "operation_name", "direction", "mapping_revision", "mapping_json"],
    )
    result = {str(_value(row, "definition_id")): row for row in rows if _value(row, "operation_kind") == _DIRECTIONS[direction]}
    if len(result) != len(rows) or not result:
        raise MappingDraftExchangeError("Selected draft context has no valid active mapping definitions.")
    return result


def _open_drafts(definition_ids: set[str]) -> dict[str, object]:
    if not definition_ids:
        return {}
    rows = frappe.get_all(
        "HERO Mapping Draft",
        filters={"site_scope": frappe.local.site, "owner_user": frappe.session.user, "state": "OPEN", "definition_id": ["in", sorted(definition_ids)]},
        fields=["definition_id", "content_json", "content_hash", "generation", "content_revision", "base_active_mapping",
                "base_active_revision", "base_active_hash", "base_schema_fingerprint", "reconciliation_revision"],
    )
    result = {str(_value(row, "definition_id")): row for row in rows}
    if len(result) != len(rows):
        raise MappingDraftExchangeError("Personal draft identities are not unique.")
    return result


def _assert_identity(item: dict[str, object], row: object, direction: str) -> None:
    if (item["operation_kind"], item["operation_name"], item["direction"]) != (
        _value(row, "operation_kind"), _value(row, "operation_name"), direction,
    ):
        raise MappingDraftExchangeError("Draft import mapping identity does not match its active definition.")


def _assert_base(item: dict[str, object], row: object) -> None:
    content = _content(_value(row, "mapping_json"))
    if (item["base_active_name"], item["base_active_revision"], item["base_active_hash"], item["base_schema_fingerprint"]) != (
        _value(row, "name"), _positive_int(_value(row, "mapping_revision")), _hash(content), _value(row, "schema_fingerprint"),
    ):
        raise MappingDraftExchangeError("Draft import base is stale for an active mapping definition.")


def _assert_opened_base(item, draft) -> None:
    if (item["base_active_name"], item["base_active_revision"], item["base_active_hash"], item["base_schema_fingerprint"]) != (
        draft.base_active_name, draft.base_active_revision, draft.base_active_hash, draft.base_schema_fingerprint,
    ):
        raise MappingDraftExchangeError("Draft import base changed before the draft could be opened.")


def _expected(row: object, draft: object | None) -> dict[str, int]:
    return {
        "generation": 0 if draft is None else _positive_int(_value(draft, "generation")),
        "draft_revision": 0 if draft is None else int(_value(draft, "content_revision")),
        "active_revision": _positive_int(_value(row, "mapping_revision")),
        "reconciliation_revision": 0 if draft is None else int(_value(draft, "reconciliation_revision") or 0),
    }


def _expected_states(value: object, preview_items: object) -> dict[str, dict[str, int]]:
    if not isinstance(value, list) or not isinstance(preview_items, list):
        raise MappingDraftExchangeError("Draft import expected states are invalid.")
    expected = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"definition_id", "generation", "draft_revision", "active_revision", "reconciliation_revision"}:
            raise MappingDraftExchangeError("Draft import expected states are invalid.")
        values = {key: item[key] for key in item if key != "definition_id"}
        if not isinstance(item["definition_id"], str) or any(type(number) is not int or number < 0 for number in values.values()):
            raise MappingDraftExchangeError("Draft import expected states are invalid.")
        expected[item["definition_id"]] = values
    actual = {item["definition_id"]: item["expected"] for item in preview_items}
    if expected != actual:
        raise MappingDraftExchangeError("Draft import state changed; preview again before saving.")
    return expected


def _content(raw: object) -> dict[str, object]:
    if not isinstance(raw, str):
        raise MappingDraftExchangeError("Stored personal draft content is invalid.")
    try:
        return bounded(_object(json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))))
    except (TypeError, ValueError, json.JSONDecodeError, MappingDraftError) as error:
        raise MappingDraftExchangeError("Stored personal draft content is invalid.") from error


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MappingDraftExchangeError("Draft content must be a JSON object.")
    return json.loads(_json(value))


def _value(row: object, field: str):
    return row.get(field) if isinstance(row, dict) else getattr(row, field)


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise MappingDraftExchangeError("Draft import revision is invalid.")
    return value


def _same(left: object, right: object) -> bool:
    return _json(left) == _json(right)


def _hash(value: object) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
