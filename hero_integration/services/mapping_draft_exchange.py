"""Direction-scoped exchange of existing personal mapping drafts.

The package is deliberately a collection of the authenticated actor's
already-existing Draft records.  It is not a second mapping store and cannot
create, activate or execute a GraphQL mapping.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
from pathlib import Path
from uuid import uuid4

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError
from hero_integration.services.personal_mapping_content import bounded, prepare_exchange_content, references, RULE_FIELDS, validate_exchange_action
from hero_integration.services.mapping_draft_service import draft_key
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
    _lock_context(source_instance, snapshot_name, direction)
    active = _lock_active_definitions(_fresh_active_definitions(source_instance, snapshot_name, direction),
                                      source_instance, snapshot_name, direction)
    fingerprints = {str(_value(row, "schema_fingerprint")) for row in active.values()}
    if fingerprints != {package["schema_fingerprint"]}:
        raise MappingDraftExchangeError("The import schema fingerprint does not match the selected active context.")
    drafts = _lock_open_drafts(set(active))
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
        prepared = _prepare_item(item, row, local_content, package['schema'])
        state = "NEW" if local is None else "UNCHANGED" if _same(local_content, item["content"]) else "REPLACE"
        items.append({
            "definition_id": definition_id, "state": state, "content_hash": _hash(item["content"]),
            "expected": _expected(row, local), "removal": False, "references": prepared['references'],
            "metadata": prepared.get('metadata', {}),
            "conflicts": prepared['conflicts'],
        })
    for definition_id, local in sorted(drafts.items()):
        if definition_id not in incoming:
            items.append({
                "definition_id": definition_id, "state": "REMOVE", "content_hash": str(_value(local, "content_hash")),
                "expected": _expected(active[definition_id], local), "removal": True,
            })
    schema = preview_schema(package["schema"])
    invalid_schema = [item for item in schema["items"] if item["state"] in {"Conflict", "Invalid"}]
    result = {
        "format_version": FORMAT_VERSION, "direction": direction, "source_instance": source_instance,
        "snapshot_name": snapshot_name, "schema_fingerprint": package["schema_fingerprint"],
        "context_definitions": [{"definition_id": identity, "name": _value(row, 'name'),
                                 "revision": _positive_int(_value(row, 'mapping_revision')),
                                 "content_hash": _hash(_content(_value(row, 'mapping_json')))}
                                for identity, row in sorted(active.items())],
        "items": items, "schema": schema, "persisted": False,
        "can_save": not invalid_schema and not any(item.get('conflicts') for item in items),
    }
    result['preview_fingerprint'] = _preview_binding(package, result)
    return result


def _prepare_item(item, row, local_content, schema):
    storage = FrappeMappingDraftStorage()
    try:
        with storage.transaction(item['definition_id'], frappe.session.user):
            active = storage.active(item['definition_id'])
            _assert_base(item, row)
            if (active.name, active.revision, active.content_hash, active.schema_fingerprint) != (
                    item['base_active_name'], item['base_active_revision'], item['base_active_hash'],
                    item['base_schema_fingerprint']):
                raise DraftConflict('Active mapping changed during preview.')
            draft = storage.draft(draft_key(frappe.local.site, item['definition_id'], frappe.session.user))
            if local_content is None:
                if draft is not None and draft.state == 'OPEN':
                    raise DraftConflict('Draft changed during preview.')
                local_content = _initial_locked_content(active.content, storage)
            elif draft is None or draft.state != 'OPEN' or not _same(draft.content, local_content):
                raise DraftConflict('Draft changed during preview.')
            # Hold every extant version for these identities until the outer
            # transaction ends. Re-read the scoped rule only after locking it.
            identities = item['content'].get('field_rules', {})
            if isinstance(identities, dict) and all(isinstance(key, str) for key in identities):
                for identity in sorted(identities):
                    frappe.db.sql('select name from `tabHERO Frappe Mapping` where definition_id=%s for update',
                                  (identity,))
            prepared = prepare_exchange_content(item['content'], local_content,
                                                lambda identity: _current_rule_snapshot(storage, identity))
            if storage._definition.direction == 'QUERY_TO_FRAPPE':
                prepared['metadata'] = {'rule_targets': _rule_target_metadata(prepared['content'], schema)}
            assignments = list(prepared['content'].get('additional_mappings') or [])
            if storage._definition.direction == 'FRAPPE_TO_MUTATION':
                assignments.append(prepared['content'])
            if assignments:
                prepared.setdefault('metadata', {})['assignments'] = _assignment_metadata(storage, assignments)
            if storage._definition.direction == 'FRAPPE_TO_MUTATION':
                prepared['metadata']['primary_action'] = validate_exchange_action(
                    prepared['content'], storage._publication_snapshot[1], storage._definition.operation_name)
            if prepared['content'].get('additional_mappings'):
                storage._validate_additional_mappings(prepared['content']['additional_mappings'])
            if (prepared['content'].get('field_contexts') or (isinstance(prepared['content'].get('source'), dict) and
                    ({'object_type', 'schema_context'} & set(prepared['content']['source'])))):
                storage._validate_object_contexts(prepared['content'])
            return prepared
    except (MappingDraftError, ValueError) as error:
        raise MappingDraftExchangeError(str(error)) from error


def _rule_target_metadata(content, schema):
    """Lock and fingerprint selected rule targets, excluding declared schema creations.

    Declared fields are instead checked by the schema preview and rechecked
    after provisioning; their metadata ranges are still locked here.
    """
    rules = content.get('field_rules', {})
    targets = {}
    for identity in references(content):
        rule = rules.get(identity, {})
        doctype, field = rule.get('target_doctype'), rule.get('target_field')
        if doctype and field:
            targets.setdefault(doctype, set()).add(field)
    planned = {(field.get('doctype'), field.get('fieldname')): field
               for field in schema.get('custom_fields', []) if isinstance(field, dict)} if isinstance(schema, dict) else {}
    result = {}
    for doctype in sorted(targets):
        docs = frappe.db.sql('select name, module, custom, istable, issingle, is_virtual from `tabDocType` '
                             'where name=%s for update', (doctype,), as_dict=True)
        fields = frappe.db.sql('select name, fieldname, fieldtype, options, reqd, read_only, hidden, permlevel, is_virtual '
                               'from `tabDocField` force index (parent) where parent=%s and parenttype=%s for update',
                               (doctype, 'DocType'), as_dict=True)
        custom = frappe.db.sql('select name, fieldname, fieldtype, options, reqd, read_only, hidden, permlevel, is_virtual '
                               'from `tabCustom Field` force index (dt) where dt=%s for update', (doctype,), as_dict=True)
        setters = frappe.db.sql('select name, doctype_or_field, field_name, property, value from `tabProperty Setter` '
                                'force index (doc_type) where doc_type=%s for update', (doctype,), as_dict=True)
        if len(docs) != 1:
            raise MappingDraftExchangeError('Selected rule target DocType is missing.')
        for fieldname in sorted(targets[doctype]):
            definition = planned.get((doctype, fieldname))
            if not definition:
                continue
            existing = [row for row in list(fields) + list(custom) if row['fieldname'] == fieldname]
            overrides = [row for row in setters if row['field_name'] == fieldname and row['property'] in {
                'fieldtype', 'options', 'reqd', 'read_only', 'hidden', 'permlevel', 'is_virtual'}]
            if len(existing) > 1 or overrides:
                raise MappingDraftExchangeError('Declared rule target metadata requires reconciliation.')
            if existing:
                row = existing[0]
                if ((row['fieldtype'] or '') != (definition.get('fieldtype') or '')
                        or (row['options'] or '') != (definition.get('options') or '')
                        or any(int(row[key] or 0) != int(definition.get(key) or 0) for key in ('reqd', 'read_only'))
                        or any(int(row[key] or 0) for key in ('hidden', 'permlevel', 'is_virtual'))):
                    raise MappingDraftExchangeError('Declared rule target metadata differs from the schema contract.')
        selected = [dict(row) for row in list(fields) + list(custom)
                    if row['fieldname'] in targets[doctype] and (doctype, row['fieldname']) not in planned]
        overlays = [dict(row) for row in setters if row['doctype_or_field'] == 'DocType'
                    or (row['field_name'] in targets[doctype] and (doctype, row['field_name']) not in planned)]
        result[doctype] = {'doctype': dict(docs[0]), 'fields': sorted(selected, key=lambda x: x['name']),
                           'setters': sorted(overlays, key=lambda x: x['name'])}
    return result


def _current_rule_snapshot(storage, identity):
    """Read the unique rule by a current, locking read, never a stale MVCC snapshot."""
    rows = frappe.db.sql('select * from `tabHERO Frappe Mapping` where definition_id=%s for update',
                         (identity,), as_dict=True)
    parent = storage._definition
    config = json.loads(parent.mapping_json)
    if len(rows) != 1:
        raise MappingDraftError('Exactly one visible active field-rule template in this definition scope is required.')
    rule = frappe.get_doc(dict(rows[0], doctype='HERO Frappe Mapping'))
    rule.check_permission('read')
    if (rule.status != 'Active' or rule.schema_snapshot != parent.schema_snapshot
            or rule.schema_fingerprint != parent.schema_fingerprint
            or rule.operation != config.get('target_operation', parent.operation_name)
            or (rule.personal_graphql_mapping and rule.personal_graphql_mapping != parent.name)):
        raise MappingDraftError('The field-rule version is outside this mapping scope.')
    return ({field: rule.get(field) for field in RULE_FIELDS},
            {'name': rule.name, 'revision': int(rule.mapping_revision), 'operation': rule.operation,
             'schema_snapshot': rule.schema_snapshot, 'schema_fingerprint': rule.schema_fingerprint})


def _initial_locked_content(content, storage):
    value = _object(content)
    ids = references(value)
    if not ids:
        return bounded(value)
    if 'field_rules' in value or 'field_rule_bases' in value:
        raise MappingDraftError('Active content already uses reserved personal snapshot fields.')
    value['field_rules'], value['field_rule_bases'] = {}, {}
    for identity in sorted(ids):
        value['field_rules'][identity], value['field_rule_bases'][identity] = _current_rule_snapshot(storage, identity)
    return bounded(value)


def _assignment_metadata(storage, mappings):
    from hero_integration.api import _validated_snapshot
    snapshot, schema = _validated_snapshot(storage._definition.schema_snapshot, for_update=True)
    storage._publication_snapshot = (snapshot, schema)
    # Lock only the referenced DocTypes and their field/overlay index ranges.
    # RR next-key locks on the parent indexes also protect against a new field
    # or property setter for the same DocType until Draft DML commits.
    targets = {}
    for item in mappings:
        side = item['target'] if storage._definition.direction == 'QUERY_TO_FRAPPE' else item['source']
        targets.setdefault(side['doctype'], set()).add(side['field'])
    locked = {}
    for doctype in sorted(targets):
        doc = frappe.db.sql('select name, module, custom, istable, issingle, is_virtual from `tabDocType` '
                            'where name=%s for update', (doctype,), as_dict=True)
        docfields = frappe.db.sql('select name, fieldname, fieldtype, options, reqd, read_only, hidden, '
                                  'permlevel, is_virtual from `tabDocField` force index (parent) '
                                  'where parent=%s and parenttype=%s for update', (doctype, 'DocType'), as_dict=True)
        custom = frappe.db.sql('select name, fieldname, fieldtype, options, reqd, read_only, hidden, '
                               'permlevel, is_virtual from `tabCustom Field` force index (dt) '
                               'where dt=%s for update', (doctype,), as_dict=True)
        setters = frappe.db.sql('select name, doctype_or_field, field_name, property, value from `tabProperty Setter` '
                                'force index (doc_type) where doc_type=%s for update', (doctype,), as_dict=True)
        if len(doc) != 1:
            raise MappingDraftExchangeError('Assigned Frappe DocType no longer exists.')
        selected = [dict(row) for row in list(docfields) + list(custom) if row['fieldname'] in targets[doctype]]
        doctype_properties = {'module', 'custom', 'istable', 'issingle', 'is_virtual'}
        field_properties = {'fieldtype', 'options', 'reqd', 'read_only', 'hidden', 'permlevel', 'is_virtual'}
        overlays = [dict(row) for row in setters if
                    (row['doctype_or_field'] == 'DocType' and row['property'] in doctype_properties)
                    or (row['field_name'] in targets[doctype] and row['property'] in field_properties)]
        # A property override changes effective metadata. Do not infer its
        # precedence from a possibly old transaction snapshot or cache.
        if overlays:
            raise MappingDraftExchangeError('Assigned field has metadata overrides requiring reconciliation.')
        locked[doctype] = {'doctype': dict(doc[0]), 'fields': sorted(selected, key=lambda row: row['name']),
                           'setters': sorted(overlays, key=lambda row: row['name'])}
    fields = []
    for item in mappings:
        side = item['target'] if storage._definition.direction == 'QUERY_TO_FRAPPE' else item['source']
        meta = frappe.get_meta(side['doctype'])
        current_doctype = locked[side['doctype']]['doctype']
        if ((meta.get('module') or '') != (current_doctype.get('module') or '')
                or any(int(meta.get(key) or 0) != int(current_doctype.get(key) or 0)
                       for key in ('custom', 'istable', 'issingle', 'is_virtual'))):
            raise MappingDraftExchangeError('Cached Frappe DocType metadata is stale; retry the import.')
        field = meta.get_field(side['field'])
        if field is None:
            raise MappingDraftExchangeError('Assigned Frappe field no longer exists.')
        current = [row for row in locked[side['doctype']]['fields'] if row['fieldname'] == side['field']]
        if len(current) != 1:
            raise MappingDraftExchangeError('Assigned Frappe field metadata is ambiguous.')
        flags = ('reqd', 'read_only', 'hidden', 'permlevel', 'is_virtual')
        if (any((field.get(key) or '') != (current[0].get(key) or '') for key in ('fieldtype', 'options'))
                or any(int(field.get(key) or 0) != int(current[0].get(key) or 0) for key in flags)):
            raise MappingDraftExchangeError('Cached Frappe field metadata is stale; retry the import.')
        fields.append({'doctype': side['doctype'], 'field': side['field'],
                       **{key: field.get(key) for key in ('fieldtype', 'options', 'reqd', 'read_only',
                                                          'hidden', 'permlevel', 'is_virtual')}})
    return {'snapshot': snapshot.name, 'schema_fingerprint': snapshot.schema_fingerprint,
            'schema_hash': _hash(schema), 'fields': sorted(fields, key=lambda value: (value['doctype'], value['field'])),
            'locked_metadata': locked}


def save_import(payload_json: str, source_instance: str, snapshot_name: str, direction: str,
                expected_states: object, *, confirmed: bool, preview_fingerprint: str) -> dict[str, object]:
    """Persist a previewed replacement after optional schema provisioning.

    Schema DDL intentionally runs before the Draft-only DML savepoint.  If a
    later CAS/save fails, the error reports created schema elements; the DML
    savepoint restores the actor's previous Draft collection.
    """
    if confirmed is not True:
        raise MappingDraftExchangeError("Explicit import save confirmation is required.")
    package = _parse(payload_json, source_instance, snapshot_name, direction)
    preview = preview_import(payload_json, source_instance, snapshot_name, direction)
    if not isinstance(preview_fingerprint, str) or not hmac.compare_digest(preview_fingerprint, preview['preview_fingerprint']):
        raise MappingDraftExchangeError('Draft import preview changed; preview again before saving.')
    if not preview["can_save"]:
        raise MappingDraftExchangeError("Schema preview contains conflicts or invalid definitions.")
    expected = _expected_states(expected_states, preview["items"])
    schema_result = provision_schema(package["schema"])
    savepoint = "hero_draft_import_" + uuid4().hex
    frappe.db.savepoint(savepoint)
    created = {"drafts": [], "discarded": [], "unchanged": []}
    try:
        # DDL may implicitly commit and release every earlier lock. Obtain a
        # fresh preview under new locks before performing any Draft DML.
        post_schema = preview_import(payload_json, source_instance, snapshot_name, direction)
        if (not post_schema['can_save'] or not _same(post_schema['items'], preview['items'])
                or not _same(post_schema['context_definitions'], preview['context_definitions'])):
            raise DraftConflict('Draft or referenced configuration changed during schema provisioning.')
        for item in package["mappings"]:
            definition_id = item["definition_id"]
            state = expected[definition_id]
            active = _active_definitions(source_instance, snapshot_name, direction)[definition_id]
            local = _open_drafts({definition_id}).get(definition_id)
            prepared = _prepare_item(item, active, _content(_value(local, 'content_json')) if local else None, package['schema'])
            if not prepared['can_save']:
                raise DraftConflict('Imported field-rule reference changed.')
            expected_refs = next(row['references'] for row in preview['items'] if row['definition_id'] == definition_id)
            if not _same(prepared['references'], expected_refs):
                raise DraftConflict('Imported field-rule reference changed after preview.')
            expected_metadata = next(row['metadata'] for row in preview['items'] if row['definition_id'] == definition_id)
            if not _same(prepared.get('metadata', {}), expected_metadata):
                raise DraftConflict('Assignment metadata changed after preview.')
            service = FrappeMappingDraftStorage().service()
            if state["generation"] == 0:
                draft = service.open_draft(definition_id, frappe.session.user)
                _assert_opened_base(item, draft)
                result = service.save(definition_id, frappe.session.user, 0, state["active_revision"], prepared['content'],
                                      expected_generation=draft.generation, expected_reconciliation_revision=0)
                created["drafts"].append(definition_id)
            else:
                result = service.save(definition_id, frappe.session.user, state["draft_revision"], state["active_revision"], prepared['content'],
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
        package = json.loads(payload_json, object_pairs_hook=_unique_json_fields,
                             parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
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


def _unique_json_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


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


def _lock_context(source_instance, snapshot_name, direction):
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    lock_exchange_context(source_instance, snapshot_name, direction)


def _fresh_active_definitions(source_instance: str, snapshot_name: str, direction: str) -> dict[str, object]:
    """Read the committed context in a fresh transaction under its application lock.

    The request connection may already have an older REPEATABLE READ snapshot.
    A separate read avoids a table-wide locking scan on sites without a context
    index; the caller subsequently locks each observed unique definition ID.
    """
    if direction not in _DIRECTIONS or not all(isinstance(value, str) and value for value in (source_instance, snapshot_name)):
        raise MappingDraftExchangeError('Selected draft context is invalid.')
    from frappe.database import get_db
    conf = frappe.local.conf
    db = get_db(socket=conf.db_socket, host=conf.db_host, port=conf.db_port,
                user=conf.db_user or conf.db_name, password=conf.db_password, cur_db_name=conf.db_name)
    try:
        rows = db.sql('select name, definition_id, schema_fingerprint, operation_kind, operation_name, direction, '
                      'mapping_revision, mapping_json from `tabHERO GraphQL Mapping` '
                      'where status=%s and source_instance=%s and schema_snapshot=%s and direction=%s',
                      ('Active', source_instance, snapshot_name, direction), as_dict=True)
    finally:
        db.close()
    result = {str(_value(row, 'definition_id')): row for row in rows if _value(row, 'operation_kind') == _DIRECTIONS[direction]}
    if len(result) != len(rows) or not result:
        raise MappingDraftExchangeError('Selected draft context has no valid active mapping definitions.')
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


def _lock_active_definitions(active, source_instance, snapshot_name, direction):
    """Lock observed parent identities in stable order and read their latest state.

    This deliberately does not table-lock an administrator's ability to add a
    different definition to the context. Such a definition is never included
    in the set of Drafts this preview can remove.
    """
    result = {}
    for identity in sorted(active):
        rows = frappe.db.sql(
            'select name, definition_id, schema_fingerprint, operation_kind, operation_name, direction, '
            'mapping_revision, mapping_json, status, source_instance, schema_snapshot '
            'from `tabHERO GraphQL Mapping` where definition_id=%s for update', (identity,), as_dict=True)
        if len(rows) != 1:
            raise MappingDraftExchangeError('Mapping context changed; preview again before saving.')
        row = rows[0]
        if (row['status'], row['source_instance'], row['schema_snapshot'], row['direction'], row['operation_kind']) != (
                'Active', source_instance, snapshot_name, direction, _DIRECTIONS[direction]):
            raise MappingDraftExchangeError('Mapping context changed; preview again before saving.')
        result[identity] = row
    return result


def _lock_open_drafts(definition_ids):
    """Current owner Draft reads under the already-held parent locks."""
    result = {}
    for identity in sorted(definition_ids):
        key = draft_key(frappe.local.site, identity, frappe.session.user)
        rows = frappe.db.sql('select * from `tabHERO Mapping Draft` where draft_key=%s for update',
                             (key,), as_dict=True)
        if len(rows) > 1:
            raise MappingDraftExchangeError('Personal draft identities are not unique.')
        if not rows:
            continue
        row = rows[0]
        if (row['site_scope'], row['owner_user'], row['definition_id']) != (
                frappe.local.site, frappe.session.user, identity):
            raise MappingDraftExchangeError('Personal draft identity or ownership is invalid.')
        if row['state'] == 'OPEN':
            result[identity] = row
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


def _preview_binding(package, preview):
    key = frappe.local.conf.get('encryption_key')
    if not isinstance(key, str) or not key:
        raise MappingDraftExchangeError('Site encryption key is required for import preview.')
    body = _json({'site': frappe.local.site, 'owner': frappe.session.user,
                  'package': package, 'preview': preview}).encode('utf-8')
    return hmac.new(key.encode('utf-8'), b'hero-draft-import-preview-v1\0' + body, sha256).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
