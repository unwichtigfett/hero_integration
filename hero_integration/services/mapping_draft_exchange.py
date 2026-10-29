"""Direction-scoped exchange of authenticated administrators' personal drafts.

V1 exchanges drafts for existing active definitions. The currently supported
V2 subset exchanges parentless drafts and never activates or executes them.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
from pathlib import Path
from uuid import UUID, uuid4

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage, assert_portable_identity_schema, lock_definition_drafts, lock_identity_drafts
from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError
from hero_integration.services.personal_mapping_content import bounded, prepare_exchange_content, references, RULE_FIELDS, validate_exchange_action
from hero_integration.services.mapping_draft_service import draft_key
from hero_integration.services.schema_provisioning import (
    SchemaProvisioningError,
    preview_schema,
    provision_schema,
)


FORMAT_VERSION = "hero-personal-mapping-draft-exchange-v1"
PORTABLE_FORMAT_VERSION = "hero-personal-mapping-portable-v2"
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


def _portable_version(payload):
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > 1_000_000:
        raise MappingDraftExchangeError('Draft import exceeds the permitted size.')
    try:
        value = json.loads(payload, object_pairs_hook=_unique_json_fields,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MappingDraftExchangeError('Draft import is not valid JSON.') from error
    return isinstance(value, dict) and value.get('format_version') == PORTABLE_FORMAT_VERSION


def _portable_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MappingDraftExchangeError('Portable identities must be canonical UUIDs.') from None
    return value


def _uuid_matches(value, canonical):
    try:
        return str(UUID(value)) == canonical
    except (ValueError, TypeError, AttributeError):
        return False


def enroll_portable_identity(definition_id, portable_id, expected_revision):
    """Enroll one active legacy definition without changing its semantic revision.

    The UUID is supplied explicitly, making retries deterministic. The caller's
    transaction owns the update and must not commit on error.
    """
    portable_id = _portable_uuid(portable_id)
    if not isinstance(definition_id, str) or not definition_id or type(expected_revision) is not int or expected_revision < 1:
        raise MappingDraftExchangeError('Identity enrollment requires a definition and active revision.')
    storage = FrappeMappingDraftStorage()
    storage._authorize(frappe.session.user)
    assert_portable_identity_schema(frappe)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    rows = frappe.db.sql('select source_instance, schema_snapshot, direction from `tabHERO GraphQL Mapping` '
                         'where definition_id=%s', (definition_id,), as_dict=True)
    if len(rows) != 1:
        raise MappingDraftExchangeError('Exactly one active definition is required for enrollment.')
    scope = tuple(_value(rows[0], key) for key in ('source_instance', 'schema_snapshot', 'direction'))
    _lock_context(*scope)
    for identity in sorted({portable_id, definition_id}):
        _lock_active_target('personal-new-definition', frappe.local.site, identity)
    with storage.transaction(definition_id, frappe.session.user):
        parent = storage._definition
        if parent is None or (parent.source_instance, parent.schema_snapshot, parent.direction) != scope:
            raise DraftConflict('Definition context changed before identity enrollment.')
        parent.check_permission('write')
        if int(parent.mapping_revision) != expected_revision:
            raise DraftConflict('Active mapping revision changed before identity enrollment.')
        current = parent.get('portable_definition_id') or ''
        if current:
            if current != portable_id:
                raise DraftConflict('This definition already has a different portable identity.')
        collisions = frappe.db.sql('select name, definition_id from `tabHERO GraphQL Mapping` '
                                   'where portable_definition_id=%s or definition_id=%s for update',
                                   (portable_id, portable_id), as_dict=True)
        if any(_value(row, 'definition_id') != definition_id for row in collisions):
            raise DraftConflict('Portable identity is already used by another definition.')
        drafts = lock_definition_drafts(frappe, [portable_id])
        if drafts and portable_id != definition_id:
            raise DraftConflict('Portable identity is already used by a personal draft.')
        if lock_identity_drafts(frappe, [portable_id], 'child'):
            raise DraftConflict('Portable identity is already used by a mutation assignment.')
        if current:
            return {'definition_id': definition_id, 'portable_definition_id': current, 'unchanged': True}
        frappe.db.sql('update `tabHERO GraphQL Mapping` set portable_definition_id=%s '
                      'where definition_id=%s and mapping_revision=%s '
                      'and (portable_definition_id is null or portable_definition_id=\'\')',
                      (portable_id, definition_id, expected_revision))
        observed = frappe.db.sql('select portable_definition_id, mapping_revision from `tabHERO GraphQL Mapping` '
                                 'where definition_id=%s for update', (definition_id,), as_dict=True)
        if (len(observed) != 1 or _value(observed[0], 'portable_definition_id') != portable_id
                or int(_value(observed[0], 'mapping_revision')) != expected_revision):
            raise DraftConflict('Identity enrollment changed concurrently.')
        return {'definition_id': definition_id, 'portable_definition_id': portable_id, 'unchanged': False}


def enroll_portable_rule_identity(definition_id, rule_id, portable_id, expected_active_revision, expected_rule_revision):
    """Explicitly enroll one scoped field-rule version, preserving its local identity."""
    portable_id = _portable_uuid(portable_id)
    if (not isinstance(definition_id, str) or not definition_id or not isinstance(rule_id, str) or not rule_id
            or any(type(value) is not int or value < 1 for value in (expected_active_revision, expected_rule_revision))):
        raise MappingDraftExchangeError('Rule enrollment requires scoped identities and positive revisions.')
    storage = FrappeMappingDraftStorage()
    storage._authorize(frappe.session.user)
    assert_portable_identity_schema(frappe)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    rows = frappe.db.sql('select source_instance, schema_snapshot, direction from `tabHERO GraphQL Mapping` '
                         'where definition_id=%s', (definition_id,), as_dict=True)
    if len(rows) != 1 or _value(rows[0], 'direction') != 'QUERY_TO_FRAPPE':
        raise MappingDraftExchangeError('An active query definition is required for rule enrollment.')
    scope = tuple(_value(rows[0], key) for key in ('source_instance', 'schema_snapshot', 'direction'))
    _lock_context(*scope)
    _lock_active_target('personal-new-definition', frappe.local.site, definition_id)
    _lock_active_target('portable-field-rule', frappe.local.site, portable_id)
    with storage.transaction(definition_id, frappe.session.user):
        parent = storage._definition
        if parent is None or (parent.source_instance, parent.schema_snapshot, parent.direction) != scope:
            raise DraftConflict('Mapping context changed before rule enrollment.')
        parent.check_permission('write')
        if int(parent.mapping_revision) != expected_active_revision:
            raise DraftConflict('Active mapping revision changed before rule enrollment.')
        selected = set(references(_content(parent.mapping_json)))
        own_draft = storage.draft(draft_key(frappe.local.site, definition_id, frappe.session.user))
        if own_draft and own_draft.state == 'OPEN':
            selected.update(references(own_draft.content))
        if rule_id not in selected:
            raise MappingDraftExchangeError('The field rule is not selected by this mapping or its owned draft.')
        _, base = _current_rule_snapshot(storage, rule_id)
        if base['revision'] != expected_rule_revision:
            raise DraftConflict('Field-rule revision changed before enrollment.')
        rule = frappe.get_doc('HERO Frappe Mapping', base['name'], for_update=True)
        rule.check_permission('write')
        current = rule.get('portable_definition_id') or ''
        if current and current != portable_id:
            raise DraftConflict('Field rule already has a different portable identity.')
        collisions = frappe.db.sql('select name from `tabHERO Frappe Mapping` where portable_definition_id=%s '
                                   'or definition_id in (%s,%s) for update',
                                   (portable_id, portable_id, UUID(portable_id).hex), as_dict=True)
        if any(_value(row, 'name') != base['name'] for row in collisions):
            raise DraftConflict('Portable field-rule identity is already in use.')
        drafts = lock_identity_drafts(frappe, [portable_id], 'rule')
        for row in drafts:
            value = _content(_value(row, 'content_json'))
            if value.get('field_rule_bases', {}).get(rule_id, {}).get('name') != base['name']:
                raise DraftConflict('Portable field-rule identity is already used by another draft.')
        if current:
            return {'definition_id': rule_id, 'portable_definition_id': current, 'unchanged': True}
        frappe.db.sql('update `tabHERO Frappe Mapping` set portable_definition_id=%s '
                      'where name=%s and mapping_revision=%s and (portable_definition_id is null or portable_definition_id=\'\')',
                      (portable_id, base['name'], expected_rule_revision))
        observed = frappe.db.sql('select portable_definition_id, mapping_revision from `tabHERO Frappe Mapping` '
                                 'where name=%s for update', (base['name'],), as_dict=True)
        if len(observed) != 1 or (_value(observed[0], 'portable_definition_id'), int(_value(observed[0], 'mapping_revision'))) != (
                portable_id, expected_rule_revision):
            raise DraftConflict('Field-rule enrollment changed concurrently.')
        return {'definition_id': rule_id, 'portable_definition_id': portable_id, 'unchanged': False}


def _portable_content(content, direction):
    """Accept only understood configuration, never local provenance or authority."""
    value = bounded(_object(content))
    common = {'source', 'target', 'transformation'}
    query = {'records_path', 'record_path', 'identity_path', 'target_operation',
             'mapping_definition_ids', 'field_rules'}
    mutation = {'version', 'additional_mappings'}
    if set(value) - (common | (query if direction == 'QUERY_TO_FRAPPE' else mutation)):
        raise MappingDraftExchangeError('Portable content contains unsupported or local-only configuration.')
    from hero_integration.services.personal_mapping_content import EDITABLE
    for side in ('source', 'target'):
        if side in value and (not isinstance(value[side], dict) or set(value[side]) - EDITABLE[side]
                              or any(not isinstance(v, str) for v in value[side].values())):
            raise MappingDraftExchangeError('Portable assignment has unsupported fields.')
    if set(value.get('source', {})) & {'object_type', 'schema_context'}:
        raise MappingDraftExchangeError('Portable object-context references are not yet supported.')
    if 'transformation' in value and value['transformation'] not in {'Identity', 'Trim'}:
        raise MappingDraftExchangeError('Portable transformation is unsupported.')
    if value.get('additional_mappings'):
        from hero_integration.services.personal_mapping_content import validate_additional_mappings
        validate_additional_mappings(value['additional_mappings'])
    if direction == 'QUERY_TO_FRAPPE':
        ids = references(value)
        rules = value.get('field_rules', {})
        if not isinstance(rules, dict) or set(ids) != set(rules):
            raise MappingDraftExchangeError('Portable field-rule closure is incomplete.')
        for identity in ids:
            _portable_uuid(identity)
        from hero_integration.services.personal_mapping_content import validate_rule
        for rule in rules.values():
            validate_rule(rule)
            relation = rule.get('relation') or ''
            allowed_relations = {('Customer Home Phone', 'Customer Primary Contact'),
                ('Customer Relation', 'HERO Customer Link'), ('Confirmed Company', 'Confirmed ERPNext Company'),
                ('Native Binding', 'Customer Primary Address')}
            if relation and (rule.get('purpose'), relation) not in allowed_relations:
                raise MappingDraftExchangeError('Portable relationship is not supported by a reviewed resolver.')
    return value


def _portable_snapshot(source_instance, snapshot_name, direction, *, for_update=True):
    if direction not in _DIRECTIONS or not source_instance or not snapshot_name:
        raise MappingDraftExchangeError('Selected draft context is invalid.')
    storage = FrappeMappingDraftStorage()
    storage._authorize(frappe.session.user)
    from hero_integration.api import _validated_snapshot
    snapshot, schema = _validated_snapshot(snapshot_name, for_update=for_update)
    snapshot.check_permission('read')
    if snapshot.source_instance != source_instance:
        raise MappingDraftExchangeError('Selected source does not match the validated snapshot.')
    return snapshot, schema


def _validate_portable_query(content, operation, schema):
    from hero_integration.services.graphql_graph import normalize_schema
    normalized = normalize_schema(schema)
    types = {item.get('name'): item for item in normalized.get('types', [])}
    root = (normalized.get('queryType') or {}).get('name')

    def resolve(start, path):
        if not isinstance(path, str) or not path or any(not part for part in path.split('.')):
            raise MappingDraftExchangeError('Portable query path is invalid.')
        current = start
        has_list = False
        nullable = False
        for part in path.split('.'):
            fields = types.get(current, {}).get('fields') or []
            field = next((entry for entry in fields if entry.get('name') == part), None)
            if field is None:
                raise MappingDraftExchangeError('Portable query path is absent from the target schema.')
            ref = field.get('type') or {}
            nullable = nullable or ref.get('kind') != 'NON_NULL'
            while ref.get('kind') in {'LIST', 'NON_NULL'}:
                has_list = has_list or ref.get('kind') == 'LIST'
                ref = ref.get('ofType') or {}
            current = ref.get('name')
        return current, ref.get('kind'), nullable, has_list

    records_path = content.get('records_path', content.get('record_path'))
    if not isinstance(records_path, str) or records_path.split('.')[0] != operation:
        raise MappingDraftExchangeError('Portable record path does not match its query operation.')
    record_type, record_kind, _, _ = resolve(root, records_path)
    if record_kind != 'OBJECT':
        raise MappingDraftExchangeError('Portable record path must resolve to an object.')
    _, kind, _, identity_list = resolve(record_type, content.get('identity_path'))
    if kind not in {'SCALAR', 'ENUM'} or identity_list:
        raise MappingDraftExchangeError('Portable identity path must resolve to a scalar.')
    for rule in content.get('field_rules', {}).values():
        name, kind, nullable, is_list = resolve(record_type, rule.get('source_path'))
        expected = rule.get('source_type', '')
        allowed = {'string': {'String', 'ID'}, 'int': {'Int'}, 'boolean': {'Boolean'}, 'float': {'Float'}}
        base = expected.removesuffix('|null') if isinstance(expected, str) else ''
        if (kind != 'SCALAR' or name not in allowed.get(base, set()) or is_list
                or (nullable and not expected.endswith('|null'))):
            raise MappingDraftExchangeError('Portable field-rule type or cardinality differs from the target schema.')


def _export_portable(source_instance, snapshot_name, direction):
    FrappeMappingDraftStorage()._authorize(frappe.session.user)
    assert_portable_identity_schema(frappe)
    _lock_context(source_instance, snapshot_name, direction)
    snapshot, schema = _portable_snapshot(source_instance, snapshot_name, direction, for_update=False)
    rows = frappe.get_all('HERO Mapping Draft', filters={
        'site_scope': frappe.local.site, 'owner_user': frappe.session.user, 'state': 'OPEN'},
        fields=['definition_id', 'new_definition_json', 'content_json'])
    parents = frappe.get_all('HERO GraphQL Mapping', filters={'source_instance': source_instance,
        'schema_snapshot': snapshot_name, 'direction': direction, 'status': 'Active'}, fields=['definition_id', 'mapping_json'])
    owned = {_value(row, 'definition_id'): row for row in rows}
    selected = []
    for row in rows:
        raw = _value(row, 'new_definition_json')
        if raw:
            definition = _content(raw)
            if (definition.get('source_instance'), definition.get('snapshot_name'), definition.get('direction')) == (
                    source_instance, snapshot_name, direction):
                selected.append((_value(row, 'definition_id'), _content(_value(row, 'content_json'))))
    for row in parents:
        identity = _value(row, 'definition_id')
        draft = owned.get(identity)
        selected.append((identity, _content(_value(draft, 'content_json') if draft else _value(row, 'mapping_json'))))

    def collect(content):
        from hero_integration.services.personal_mapping_content import identity_sets
        rule_ids, child_ids = identity_sets(content, direction)
        rule_locks = set()
        for local in rule_ids:
            try:
                rule_locks.add(str(UUID(local)))
            except (ValueError, TypeError, AttributeError):
                pass
            found = frappe.get_all('HERO Frappe Mapping', filters={'definition_id': local},
                                   fields=['portable_definition_id'])
            if len(found) > 1:
                raise MappingDraftExchangeError('Field-rule identity is ambiguous.')
            portable = _value(found[0], 'portable_definition_id') if found else None
            if portable:
                rule_locks.add(_portable_uuid(portable))
        return child_ids, rule_locks

    definition_locks = {identity for identity, _ in selected}
    rule_locks = set()
    for _, content in selected:
        children, rules = collect(content)
        definition_locks.update(children)
        rule_locks.update(rules)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    for identity in sorted(definition_locks):
        _lock_active_target('personal-new-definition', frappe.local.site, identity)
    for identity in sorted(rule_locks):
        _lock_active_target('portable-field-rule', frappe.local.site, identity)

    lock_identity_drafts(frappe, rule_locks, 'rule')

    def check_locked(content):
        children, rules = collect(content)
        if not children <= definition_locks or not rules <= rule_locks:
            raise DraftConflict('Portable export identities changed while locks were acquired.')

    mappings, manifests = [], []
    for row in rows:
        raw = _value(row, 'new_definition_json')
        if not raw:
            continue
        definition = _content(raw)
        if (definition.get('source_instance'), definition.get('snapshot_name'), definition.get('direction')) != (
                source_instance, snapshot_name, direction):
            continue
        identity = _portable_uuid(_value(row, 'definition_id'))
        storage = FrappeMappingDraftStorage()
        with storage.transaction(identity, frappe.session.user):
            draft = storage.parentless(identity, frappe.session.user)
            if draft is None or draft.state != 'OPEN' or draft.new_definition != definition:
                raise MappingDraftExchangeError('Portable draft changed during export.')
            frappe.get_doc(storage.DRAFT, draft.key).check_permission('read')
            check_locked(draft.content)
            content = _object(draft.content)
            envelope = content.pop('_portable_exchange_schema', None)
            dependencies = []
            if envelope is not None:
                if not isinstance(envelope, dict) or set(envelope) != {'schema', 'dependencies'}:
                    raise MappingDraftExchangeError('Stored portable schema context is invalid.')
                manifests.append({'schema_manifest': _json(envelope['schema'])})
                dependencies = envelope['dependencies']
            content = _portable_content(content, direction)
            storage.validate_new_definition(definition, draft.content)
            if direction == 'QUERY_TO_FRAPPE':
                _validate_portable_query(content, definition['operation_name'], schema)
            mappings.append({'definition_id': identity, 'operation_kind': definition['operation_kind'],
                             'operation_name': definition['operation_name'], 'direction': direction,
                             'content': content, 'dependencies': dependencies})
    for row in parents:
        local_id = _value(row, 'definition_id')
        storage = FrappeMappingDraftStorage()
        with storage.transaction(local_id, frappe.session.user):
            parent = storage._definition
            if parent is None or (parent.source_instance, parent.schema_snapshot, parent.direction,
                                  parent.schema_fingerprint, parent.operation_kind) != (
                    source_instance, snapshot_name, direction, snapshot.schema_fingerprint, _DIRECTIONS[direction]):
                raise DraftConflict('Active definition context changed during portable export.')
            portable_id = parent.get('portable_definition_id') or ''
            if not portable_id:
                raise MappingDraftExchangeError('Enroll a portable UUID for this active definition before export.')
            portable_id = _portable_uuid(portable_id)
            draft = storage.draft(draft_key(frappe.local.site, local_id, frappe.session.user))
            if draft and draft.state == 'OPEN':
                frappe.get_doc(storage.DRAFT, draft.key).check_permission('read')
                content = draft.content
                if (draft.base_active_name, draft.base_active_revision, draft.base_active_hash,
                        draft.base_schema_fingerprint) != (parent.name, int(parent.mapping_revision),
                                                           storage.active(local_id).content_hash, snapshot.schema_fingerprint):
                    raise DraftConflict('Personal draft baseline changed before portable export.')
            else:
                content = storage.initial_content(storage.active(local_id).content)
            content = _object(content)
            check_locked(content)
            dependencies = []
            manifest = parent.get('portable_schema_manifest') or ''
            declared_dependencies = parent.get('portable_schema_dependencies') or ''
            if manifest or declared_dependencies:
                try:
                    if not manifest or not declared_dependencies:
                        raise ValueError
                    mapping_schema = json.loads(manifest, object_pairs_hook=_unique_json_fields)
                    dependencies = json.loads(declared_dependencies, object_pairs_hook=_unique_json_fields)
                    if not isinstance(dependencies, list) or any(not isinstance(v, str) or not v for v in dependencies):
                        raise ValueError
                    from hero_integration.services.schema_provisioning import _definitions, _recheck_required_schema_current
                    definitions = _definitions(mapping_schema)
                    declared = {entry['id'] for group in definitions.values() for entry in group}
                    if not declared or len(dependencies) != len(set(dependencies)) or set(dependencies) != declared:
                        raise ValueError
                    _recheck_required_schema_current(definitions, declared)
                    manifests.append({'schema_manifest': _json(mapping_schema)})
                except (ValueError, TypeError):
                    raise MappingDraftExchangeError('Stored mapping schema provenance is invalid.') from None
            if references(content):
                locked_rules = {}
                for rule_id in sorted(references(content)):
                    _, current_base = _current_rule_snapshot(storage, rule_id)
                    if not _same(content.get('field_rule_bases', {}).get(rule_id), current_base):
                        raise DraftConflict('Selected field-rule provenance changed before portable export.')
                    locked = frappe.db.sql('select * from `tabHERO Frappe Mapping` where definition_id=%s for update',
                                           (rule_id,), as_dict=True)
                    if len(locked) != 1:
                        raise DraftConflict('Selected field rule changed before portable export.')
                    locked_rules[rule_id] = locked[0]
                schema_manifest = _export_schema([{'content': content, 'base_active_name': parent.name,
                    'base_schema_fingerprint': snapshot.schema_fingerprint, 'operation_name': parent.operation_name}],
                    direction, snapshot_name, locked_rule_rows=locked_rules)
                if any(schema_manifest.values()):
                    manifests.append({'schema_manifest': _json(schema_manifest)})
                    dependencies = sorted(set(dependencies) | {entry['id'] for group in schema_manifest.values() for entry in group})
                rule_ids = {}
                for rule_id in references(content):
                    if _value(locked_rules[rule_id], 'portable_lineage_json'):
                        raise MappingDraftExchangeError('This rule revision requires a lineage-aware portable package format.')
                    portable_rule = _value(locked_rules[rule_id], 'portable_definition_id') or ''
                    if not portable_rule:
                        raise MappingDraftExchangeError('Enroll a portable UUID for the selected field rule before export.')
                    rule_ids[rule_id] = _portable_uuid(portable_rule)
                if len(set(rule_ids.values())) != len(rule_ids):
                    raise MappingDraftExchangeError('Selected field-rule identities are ambiguous.')
                rules = content.get('field_rules', {})
                if set(rules) != set(rule_ids):
                    raise MappingDraftExchangeError('Unselected field-rule snapshots require reconciliation before export.')
                content['mapping_definition_ids'] = [rule_ids[identity] for identity in references(content)]
                content['field_rules'] = {rule_ids[identity]: rule for identity, rule in rules.items()}
            content.pop('field_rule_bases', None)
            content = _portable_content(content, direction)
            definition = {'site_scope': frappe.local.site, 'definition_id': portable_id,
                          'source_instance': source_instance, 'snapshot_name': snapshot_name,
                          'schema_fingerprint': snapshot.schema_fingerprint, 'direction': direction,
                          'operation_kind': parent.operation_kind, 'operation_name': parent.operation_name}
            storage.validate_new_definition(definition, content)
            if direction == 'QUERY_TO_FRAPPE':
                _validate_portable_query(content, parent.operation_name, schema)
            mappings.append({'definition_id': portable_id, 'operation_kind': parent.operation_kind,
                             'operation_name': parent.operation_name, 'direction': direction,
                             'content': content, 'dependencies': dependencies})
    if not mappings:
        raise MappingDraftExchangeError('No portable parentless personal drafts are available in this context.')
    if len({item['definition_id'] for item in mappings}) != len(mappings):
        raise MappingDraftExchangeError('Portable definition identities are ambiguous in this context.')
    from hero_integration.services.mapping_exchange import _merge_export_schemas
    declarations = _merge_export_schemas(manifests)
    payload = _json({'format_version': PORTABLE_FORMAT_VERSION, 'schema_fingerprint': snapshot.schema_fingerprint,
                     'direction': direction, 'schema': declarations,
                     'mappings': sorted(mappings, key=lambda item: item['definition_id'])})
    _parse_portable(payload, direction)
    return payload


def _parse_portable(payload, direction):
    if not _portable_version(payload) or direction not in _DIRECTIONS:
        raise MappingDraftExchangeError('Unsupported portable format or direction.')
    package = json.loads(payload, object_pairs_hook=_unique_json_fields)
    if set(package) != {'format_version', 'schema_fingerprint', 'direction', 'schema', 'mappings'}:
        raise MappingDraftExchangeError('Portable package has unknown or missing fields.')
    fingerprint = package['schema_fingerprint']
    if (package['direction'] != direction or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in fingerprint)):
        raise MappingDraftExchangeError('Portable package direction or schema fingerprint is invalid.')
    from hero_integration.services.mapping_exchange import _validate_schema
    _validate_schema(package['schema'])
    declarations = {}
    for group in package['schema'].values():
        for entry in group:
            if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not entry['id'] or entry['id'] in declarations:
                raise MappingDraftExchangeError('Portable schema declaration identity is invalid or duplicated.')
            declarations[entry['id']] = entry
    nested_field_owner = {}
    for doctype in package['schema']['custom_doctypes']:
        fields = doctype.get('fields')
        if not isinstance(fields, list):
            raise MappingDraftExchangeError('Portable DocType fields are invalid.')
        for field in fields:
            identity = field.get('id') if isinstance(field, dict) else None
            if not isinstance(identity, str) or not identity or identity in declarations or identity in nested_field_owner:
                raise MappingDraftExchangeError('Portable schema field identity is invalid or duplicated.')
            nested_field_owner[identity] = doctype['id']
    for entry in package['schema']['relations']:
        if not isinstance(entry.get('dependencies'), list) or any(not isinstance(dep, str) for dep in entry['dependencies']):
            raise MappingDraftExchangeError('Portable relationship dependencies are invalid.')
    doctype_ids = {entry.get('name'): entry['id'] for entry in package['schema']['custom_doctypes']}
    edges = {identity: [] for identity in declarations}
    for entry in package['schema']['custom_fields']:
        if entry.get('doctype') in doctype_ids:
            edges[entry['id']].append(doctype_ids[entry['doctype']])
    for entry in package['schema']['relations']:
        edges[entry['id']].extend(entry['dependencies'])
        edges[entry['id']].append(entry.get('field_id'))
        if entry.get('target_doctype') in doctype_ids:
            edges[entry['id']].append(doctype_ids[entry['target_doctype']])
    def closure(identities):
        visited, pending = set(), list(identities)
        while pending:
            identity = pending.pop()
            identity = nested_field_owner.get(identity, identity)
            if identity in visited:
                continue
            if identity not in declarations:
                raise MappingDraftExchangeError('Portable schema dependency is missing.')
            visited.add(identity)
            pending.extend(edges[identity])
        return visited
    mappings = package['mappings']
    if not isinstance(mappings, list) or not mappings or len(mappings) > 100:
        raise MappingDraftExchangeError('Portable mappings must be a nonempty bounded list.')
    seen, shared_rules, referenced = set(), {}, set()
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {'definition_id', 'operation_kind', 'operation_name',
                                                      'direction', 'content', 'dependencies'}:
            raise MappingDraftExchangeError('Portable mapping has unknown or missing fields.')
        identity = _portable_uuid(item['definition_id'])
        if identity in seen:
            raise MappingDraftExchangeError('Duplicate portable definition identity.')
        seen.add(identity)
        if (item['direction'] != direction or item['operation_kind'] != _DIRECTIONS[direction]
                or not isinstance(item['operation_name'], str) or not item['operation_name']
                or not isinstance(item['dependencies'], list)
                or any(not isinstance(dep, str) or not dep for dep in item['dependencies'])
                or len(set(item['dependencies'])) != len(item['dependencies'])):
            raise MappingDraftExchangeError('Portable operation or dependencies are invalid or unsupported.')
        resolved = closure(item['dependencies'])
        referenced.update(resolved)
        item['content'] = _portable_content(item['content'], direction)
        from hero_integration.services.schema_provisioning import _target_dependency
        for rule in item['content'].get('field_rules', {}).values():
            try:
                target = _target_dependency(package['schema'], rule)
            except (KeyError, TypeError, ValueError) as error:
                raise MappingDraftExchangeError('Portable target schema declaration is invalid.') from error
            if target and target not in resolved:
                raise MappingDraftExchangeError('Portable field rule lacks its target schema dependency.')
        for rule_id, rule in item['content'].get('field_rules', {}).items():
            if rule_id in shared_rules and not _same(shared_rules[rule_id], rule):
                raise MappingDraftExchangeError('A portable field-rule identity has conflicting content.')
            shared_rules[rule_id] = rule
    if referenced != set(declarations):
        raise MappingDraftExchangeError('Portable schema contains unreferenced declarations.')
    return package


def _portable_existing_candidate(item, local_id, source_instance, snapshot_name, direction, fingerprint):
    storage = FrappeMappingDraftStorage()
    with storage.transaction(local_id, frappe.session.user):
        parent = storage._definition
        if parent is None or (parent.get('portable_definition_id'), parent.source_instance, parent.schema_snapshot,
                parent.direction, parent.schema_fingerprint, parent.operation_kind, parent.operation_name) != (
                item['definition_id'], source_instance, snapshot_name, direction, fingerprint,
                item['operation_kind'], item['operation_name']):
            raise MappingDraftExchangeError('Portable identity belongs to a different target context.')
        active = storage.active(local_id)
        draft = storage.draft(draft_key(frappe.local.site, local_id, frappe.session.user))
        if draft and draft.state != 'OPEN':
            draft = None
        if draft:
            frappe.get_doc(storage.DRAFT, draft.key).check_permission('read')
        incoming = _object(item['content'])
        remapping, bases, rules = {}, {}, {}
        for identity in sorted(references(incoming)):
            rows = frappe.db.sql('select name, definition_id from `tabHERO Frappe Mapping` '
                                 'where portable_definition_id=%s for update', (identity,), as_dict=True)
            if len(rows) != 1:
                raise MappingDraftExchangeError('Portable field-rule identity is missing or ambiguous on the target.')
            target_id = _value(rows[0], 'definition_id')
            _, bases[target_id] = _current_rule_snapshot(storage, target_id)
            remapping[identity] = target_id
            rules[target_id] = incoming['field_rules'][identity]
        if remapping:
            incoming['mapping_definition_ids'] = [remapping[identity] for identity in references(incoming)]
            incoming['field_rules'], incoming['field_rule_bases'] = rules, bases
        row = {'name': active.name, 'mapping_revision': active.revision,
               'mapping_json': _json(active.content), 'schema_fingerprint': active.schema_fingerprint}
        candidate = {'definition_id': local_id, 'operation_name': item['operation_name'],
                     'base_active_name': active.name, 'base_active_revision': active.revision,
                     'base_active_hash': active.content_hash, 'base_schema_fingerprint': active.schema_fingerprint,
                     'content': incoming}
        return candidate, row, draft, remapping


def _portable_target_metadata(content, direction):
    """Lock and bind the actual target-side metadata used by parentless drafts."""
    if direction == 'QUERY_TO_FRAPPE':
        probe = content
    else:
        source = content.get('source', {})
        sources = [source] + [item.get('source', {}) for item in content.get('additional_mappings', [])]
        probe = {'mapping_definition_ids': [str(i) for i in range(len(sources))], 'field_rules': {
            str(i): {'target_doctype': item.get('doctype'), 'target_field': item.get('field')}
            for i, item in enumerate(sources)}}
    metadata = _rule_target_metadata(probe, {'custom_doctypes': [], 'custom_fields': [], 'relations': []})
    targets = {}
    for identity in references(probe):
        rule = probe['field_rules'][identity]
        targets.setdefault(rule.get('target_doctype'), set()).add(rule.get('target_field'))
    for doctype, fields in targets.items():
        rows = metadata.get(doctype, {}).get('fields', [])
        for field in fields:
            matches = [row for row in rows if row['fieldname'] == field]
            if len(matches) != 1 or any(int(matches[0].get(flag) or 0) for flag in ('hidden', 'permlevel', 'is_virtual')):
                raise MappingDraftExchangeError('Portable target metadata is missing, ambiguous or restricted.')
    return metadata


def _portable_existing_schema(candidate, item, package, direction, snapshot_name):
    """Require matching target-owned rule manifests; never import source-local bases."""
    expected_ids = set(item['dependencies'])
    if direction != 'QUERY_TO_FRAPPE' or not references(candidate['content']):
        if expected_ids:
            raise MappingDraftExchangeError('Existing definition lacks target-owned schema provenance.')
        return
    storage = FrappeMappingDraftStorage()
    with storage.transaction(candidate['definition_id'], frappe.session.user):
        locked = {}
        for identity in sorted(references(candidate['content'])):
            _, base = _current_rule_snapshot(storage, identity)
            if not _same(candidate['content'].get('field_rule_bases', {}).get(identity), base):
                raise DraftConflict('Target field-rule provenance changed during schema reconciliation.')
            rows = frappe.db.sql('select * from `tabHERO Frappe Mapping` where definition_id=%s for update',
                                 (identity,), as_dict=True)
            if len(rows) != 1:
                raise DraftConflict('Target field-rule manifest is ambiguous.')
            locked[identity] = rows[0]
        local_schema = _export_schema([candidate], direction, snapshot_name, locked_rule_rows=locked)
    expected = {group: [entry for entry in entries if entry['id'] in expected_ids]
                for group, entries in package['schema'].items()}
    if not _same(local_schema, expected):
        raise MappingDraftExchangeError('Target field-rule schema provenance differs from the portable package.')


def _portable_storage_content(item, package):
    content = _object(item['content'])
    if any(package['schema'].values()):
        content['_portable_exchange_schema'] = {'schema': package['schema'], 'dependencies': item['dependencies']}
    return content


def _preview_portable(payload, source_instance, snapshot_name, direction):
    package = _parse_portable(payload, direction)
    FrappeMappingDraftStorage()._authorize(frappe.session.user)
    assert_portable_identity_schema(frappe)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    _lock_context(source_instance, snapshot_name, direction)
    mapping_ids = {item['definition_id'] for item in package['mappings']}
    additional_ids = [child['id'] for item in package['mappings'] for child in item['content'].get('additional_mappings', [])]
    additional_owners = {child['id']: item['definition_id'] for item in package['mappings']
                         for child in item['content'].get('additional_mappings', [])}
    if len(additional_ids) != len(set(additional_ids)) or mapping_ids.intersection(additional_ids):
        raise MappingDraftExchangeError('Portable mutation assignment identities are ambiguous.')
    preliminary, preliminary_content = {}, []
    for identity in sorted(mapping_ids):
        rows = frappe.db.sql('select definition_id, portable_definition_id, mapping_json from `tabHERO GraphQL Mapping` '
                             'where portable_definition_id=%s or definition_id=%s', (identity, identity), as_dict=True)
        if len(rows) > 1:
            raise MappingDraftExchangeError('Portable identity is ambiguous on this site.')
        if rows:
            preliminary[identity] = _value(rows[0], 'definition_id')
            preliminary_content.append(_content(_value(rows[0], 'mapping_json')))
    preliminary_drafts = _open_drafts(mapping_ids | set(preliminary.values()))
    preliminary_content.extend(_content(_value(row, 'content_json')) for row in preliminary_drafts.values())
    preliminary_content.extend(item['content'] for item in package['mappings'])
    definition_locks, old_rule_locks = _legacy_identity_lock_sets(mapping_ids | set(preliminary.values()),
                                                                   preliminary_content, direction)
    for identity in sorted(definition_locks):
        _lock_active_target('personal-new-definition', frappe.local.site, identity)
    rule_ids = {identity for item in package['mappings'] for identity in references(item['content'])}
    rule_locks = set(rule_ids) | old_rule_locks
    for identity in sorted(rule_ids):
        rows = frappe.db.sql('select definition_id from `tabHERO Frappe Mapping` '
                             'where portable_definition_id=%s', (identity,), as_dict=True)
        if len(rows) > 1:
            raise MappingDraftExchangeError('Portable field-rule identity is ambiguous on this site.')
        if rows:
            try:
                rule_locks.add(str(UUID(_value(rows[0], 'definition_id'))))
            except (ValueError, TypeError, AttributeError):
                pass  # Non-UUID local IDs have no rule UUID lock alias.
    for identity in sorted(rule_locks):
        _lock_active_target('portable-field-rule', frappe.local.site, identity)
    rule_drafts = lock_identity_drafts(frappe, rule_ids, 'rule')
    child_drafts = lock_identity_drafts(frappe, additional_ids, 'child')
    for identity in sorted(additional_ids):
        collisions = frappe.db.sql('select definition_id from `tabHERO GraphQL Mapping` '
                                   'where portable_definition_id=%s or definition_id=%s for update',
                                   (identity, identity), as_dict=True)
        drafts = lock_definition_drafts(frappe, [identity])
        if collisions or drafts:
            raise MappingDraftExchangeError('A mutation assignment identity is already in use on the target.')
        owner_id = additional_owners[identity]
        allowed_id = preliminary.get(owner_id, owner_id)
        for draft_row in child_drafts:
            existing = _content(_value(draft_row, 'content_json'))
            if not any(isinstance(child, dict) and child.get('id') == identity
                       for child in existing.get('additional_mappings', [])):
                continue
            if (_value(draft_row, 'owner_user'), _value(draft_row, 'definition_id')) != (frappe.session.user, allowed_id):
                raise DraftConflict('A mutation assignment identity is already used by a different draft.')
    snapshot, schema = _portable_snapshot(source_instance, snapshot_name, direction)
    if snapshot.schema_fingerprint != package['schema_fingerprint']:
        raise MappingDraftExchangeError('Portable package does not match the target schema fingerprint.')
    schema_preview = preview_schema(package['schema']) if any(package['schema'].values()) else {'items': [], 'counts': {}}
    blocked_schema = [item for item in schema_preview['items'] if item['state'] != 'Unchanged']
    if blocked_schema:
        result = {'format_version': PORTABLE_FORMAT_VERSION, 'direction': direction, 'source_instance': source_instance,
                  'snapshot_name': snapshot_name, 'schema_fingerprint': snapshot.schema_fingerprint,
                  'context_definitions': [], 'schema': schema_preview, 'persisted': False, 'can_save': False,
                  'items': [{'definition_id': item['definition_id'], 'state': 'BLOCKED', 'removal': False,
                             'expected': {'generation': 0, 'draft_revision': 0, 'active_revision': 0,
                                          'reconciliation_revision': 0},
                             'conflicts': [{'state': 'SCHEMA_REQUIRED', 'detail': 'Target schema requires reconciliation.'}]}
                            for item in package['mappings']]}
        result['preview_fingerprint'] = _preview_binding(package, result)
        return result
    if schema_preview['items']:
        from hero_integration.services.schema_provisioning import _definitions, _recheck_required_schema_current
        definitions = _definitions(package['schema'])
        _recheck_required_schema_current(definitions, {item['id'] for item in schema_preview['items']})
    items = []
    for item in sorted(package['mappings'], key=lambda value: value['definition_id']):
        identity = item['definition_id']
        matches = frappe.db.sql('select definition_id, portable_definition_id from `tabHERO GraphQL Mapping` '
                                'where portable_definition_id=%s or definition_id=%s for update',
                                (identity, identity), as_dict=True)
        if len(matches) > 1:
            raise MappingDraftExchangeError('Portable identity is ambiguous on this site.')
        if matches:
            match = matches[0]
            if preliminary.get(identity) != _value(match, 'definition_id'):
                raise DraftConflict('Portable identity changed while locks were acquired; preview again.')
            if _value(match, 'portable_definition_id') != identity:
                raise MappingDraftExchangeError('Portable identity collides with an unenrolled local definition.')
            local_id = _value(match, 'definition_id')
            candidate, row, draft, remapping = _portable_existing_candidate(
                item, local_id, source_instance, snapshot_name, direction, snapshot.schema_fingerprint)
            current_content = [_content(row['mapping_json'])] + ([draft.content] if draft else []) + [candidate['content']]
            current_defs, current_rules = _legacy_identity_lock_sets({identity, local_id}, current_content, direction)
            if not current_defs <= definition_locks or not current_rules <= rule_locks:
                raise DraftConflict('Draft identities changed while locks were acquired; preview again.')
            for target_id in remapping.values():
                try:
                    local_lock = str(UUID(target_id))
                except (ValueError, TypeError, AttributeError):
                    continue
                if local_lock not in rule_locks:
                    raise DraftConflict('Field-rule identity changed while locks were acquired; preview again.')
            _portable_existing_schema(candidate, item, package, direction, snapshot_name)
            local_content = draft.content if draft else None
            state = {'generation': draft.generation if draft else 0,
                     'draft_revision': draft.content_revision if draft else 0,
                     'active_revision': row['mapping_revision'],
                     'reconciliation_revision': draft.reconciliation_revision if draft else 0}
            prepared = _prepare_item(candidate, row, local_content, package['schema'], portable=True)
            items.append({'definition_id': identity, 'local_definition_id': local_id,
                          'rule_remapping': remapping,
                          'state': 'NEW' if draft is None else 'UNCHANGED' if _same(local_content, prepared['content']) else 'REPLACE',
                          'expected': state, 'removal': False, 'content_hash': _hash(item['content']),
                          'references': prepared['references'], 'metadata': prepared.get('metadata', {}),
                          'conflicts': prepared['conflicts']})
            continue
        if identity in preliminary:
            raise DraftConflict('Portable identity changed while locks were acquired; preview again.')
        for rule_identity in sorted(references(item['content'])):
            collisions = frappe.db.sql('select name from `tabHERO Frappe Mapping` where portable_definition_id=%s '
                                       'or definition_id in (%s,%s) for update',
                                       (rule_identity, rule_identity, UUID(rule_identity).hex), as_dict=True)
            if collisions:
                raise MappingDraftExchangeError('Portable field-rule identity already exists; target rule reuse requires reconciliation.')
            for draft_row in rule_drafts:
                existing = _content(_value(draft_row, 'content_json'))
                from hero_integration.services.frappe_mapping_draft_storage import _draft_identity_direction
                from hero_integration.services.personal_mapping_content import identity_sets
                existing_rules, _ = identity_sets(existing, _draft_identity_direction(frappe, draft_row))
                matched = [value for value in existing_rules
                           if _uuid_matches(value, rule_identity)]
                if not matched:
                    continue
                try:
                    context = _content(_value(draft_row, 'new_definition_json'))
                except MappingDraftExchangeError:
                    raise DraftConflict('Portable field-rule identity is already in use by a different draft.') from None
                if (_value(draft_row, 'owner_user') != frappe.session.user
                        or (context.get('site_scope'), context.get('source_instance'), context.get('snapshot_name'),
                            context.get('direction'), context.get('schema_fingerprint')) != (
                            frappe.local.site, source_instance, snapshot_name, direction, package['schema_fingerprint'])
                        or len(matched) != 1 or not _same(existing.get('field_rules', {}).get(matched[0]),
                                                          item['content']['field_rules'][rule_identity])):
                    raise DraftConflict('Portable field-rule identity is already in use by a different draft.')
        definition = {'site_scope': frappe.local.site, 'definition_id': identity, 'source_instance': source_instance,
                      'snapshot_name': snapshot_name, 'schema_fingerprint': snapshot.schema_fingerprint,
                      'direction': direction, 'operation_kind': item['operation_kind'], 'operation_name': item['operation_name']}
        storage = FrappeMappingDraftStorage()
        with storage.transaction(identity, frappe.session.user):
            if storage.active_or_none(identity) is not None:
                raise MappingDraftExchangeError('Import into an existing active definition is not yet supported.')
            storage.assert_new_identity_available(identity, frappe.session.user)
            stored_content = _portable_storage_content(item, package)
            storage.validate_new_definition(definition, stored_content)
            if direction == 'QUERY_TO_FRAPPE':
                _validate_portable_query(item['content'], item['operation_name'], schema)
            target_metadata = _portable_target_metadata(item['content'], direction)
            draft = storage.parentless(identity, frappe.session.user)
            if draft and (draft.state != 'OPEN' or draft.new_definition != definition):
                raise MappingDraftExchangeError('Portable identity conflicts with an existing personal draft.')
            if draft:
                frappe.get_doc(storage.DRAFT, draft.key).check_permission('read')
            expected = {'generation': draft.generation if draft else 0,
                        'draft_revision': draft.content_revision if draft else 0, 'active_revision': 0,
                        'reconciliation_revision': draft.reconciliation_revision if draft else 0}
            state = 'NEW' if not draft else 'UNCHANGED' if _same(draft.content, stored_content) else 'REPLACE'
            items.append({'definition_id': identity, 'state': state, 'expected': expected, 'removal': False,
                          'content_hash': _hash(item['content']), 'references': [],
                          'metadata': {'targets': target_metadata}, 'conflicts': []})
    result = {'format_version': PORTABLE_FORMAT_VERSION, 'direction': direction, 'source_instance': source_instance,
              'snapshot_name': snapshot_name, 'schema_fingerprint': snapshot.schema_fingerprint,
              'context_definitions': [], 'items': items, 'schema': schema_preview,
              'persisted': False, 'can_save': not any(item.get('conflicts') for item in items)}
    result['preview_fingerprint'] = _preview_binding(package, result)
    return result


def _save_portable(payload, source_instance, snapshot_name, direction, expected_states, *, confirmed, preview_fingerprint):
    if confirmed is not True:
        raise MappingDraftExchangeError('Explicit import save confirmation is required.')
    package = _parse_portable(payload, direction)
    preview = _preview_portable(payload, source_instance, snapshot_name, direction)
    if not isinstance(preview_fingerprint, str) or not hmac.compare_digest(preview_fingerprint, preview['preview_fingerprint']):
        raise MappingDraftExchangeError('Portable preview changed; preview again before saving.')
    if not preview['can_save']:
        raise MappingDraftExchangeError('Portable import contains unresolved conflicts.')
    expected = _expected_states(expected_states, preview['items'])
    savepoint = 'hero_portable_import_' + uuid4().hex
    frappe.db.savepoint(savepoint)
    created = {'drafts': [], 'discarded': [], 'unchanged': []}
    try:
        for item in sorted(package['mappings'], key=lambda value: value['definition_id']):
            identity = item['definition_id']
            state = expected[identity]
            service = FrappeMappingDraftStorage().service()
            preview_item = next(row for row in preview['items'] if row['definition_id'] == identity)
            local_id = preview_item.get('local_definition_id')
            if local_id:
                candidate, row, draft, remapping = _portable_existing_candidate(
                    item, local_id, source_instance, snapshot_name, direction, package['schema_fingerprint'])
                _portable_existing_schema(candidate, item, package, direction, snapshot_name)
                local_content = draft.content if draft else None
                prepared = _prepare_item(candidate, row, local_content, package['schema'], portable=True)
                if (not _same(remapping, preview_item['rule_remapping']) or not prepared['can_save']
                        or not _same(prepared['references'], preview_item['references'])
                        or not _same(prepared.get('metadata', {}), preview_item['metadata'])):
                    raise DraftConflict('Portable target configuration changed after preview.')
                if state['generation'] == 0:
                    opened = service.open_draft(local_id, frappe.session.user)
                    generation, revision = opened.generation, 0
                else:
                    generation, revision = state['generation'], state['draft_revision']
                result = service.save(local_id, frappe.session.user, revision, state['active_revision'],
                                      prepared['content'], expected_generation=generation,
                                      expected_reconciliation_revision=state['reconciliation_revision'])
                created['unchanged' if result.unchanged else 'drafts'].append(identity)
                continue
            if state['generation'] == 0:
                definition = {'site_scope': frappe.local.site, 'definition_id': identity, 'source_instance': source_instance,
                              'snapshot_name': snapshot_name, 'schema_fingerprint': package['schema_fingerprint'],
                              'direction': direction, 'operation_kind': item['operation_kind'], 'operation_name': item['operation_name']}
                service.open_new_draft(definition, frappe.session.user, _portable_storage_content(item, package))
                created['drafts'].append(identity)
            else:
                result = service.save(identity, frappe.session.user, state['draft_revision'], 0, _portable_storage_content(item, package),
                                      expected_generation=state['generation'],
                                      expected_reconciliation_revision=state['reconciliation_revision'])
                created['unchanged' if result.unchanged else 'drafts'].append(identity)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return {'status': 'SAVED_DRAFTS_ONLY', 'direction': direction, 'schema': {'created': {}},
            'drafts': created, 'activation': False}


def export_package(source_instance: str, snapshot_name: str, direction: str, *, format_version: str = FORMAT_VERSION) -> str:
    """Export only the current actor's OPEN drafts in one exact direction."""
    if format_version == PORTABLE_FORMAT_VERSION:
        return _export_portable(source_instance, snapshot_name, direction)
    if format_version != FORMAT_VERSION:
        raise MappingDraftExchangeError('Unsupported draft exchange format.')
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
        "schema": _export_schema(mappings, direction, snapshot_name),
        "mappings": mappings,
    })


def _export_schema(mappings, direction, snapshot_name, *, locked_rule_rows=None):
    """Export schema manifests referenced by the selected personal field rules."""
    empty = {"custom_doctypes": [], "custom_fields": [], "relations": []}
    if direction != 'QUERY_TO_FRAPPE':
        return empty
    from hero_integration.services.mapping_exchange import MappingExchangeError, _exportable, _merge_export_schemas
    from hero_integration.services.schema_provisioning import _target_dependency

    rows, selected = [], []
    for item in mappings:
        content = item['content']
        rules = content.get('field_rules', {})
        bases = content.get('field_rule_bases', {})
        for identity in references(content):
            if not isinstance(rules, dict) or not isinstance(bases, dict) or identity not in rules or identity not in bases:
                raise MappingDraftExchangeError('Selected field rule lacks its saved provenance.')
            found = ([locked_rule_rows[identity]] if locked_rule_rows is not None and identity in locked_rule_rows else []
                     if locked_rule_rows is not None else frappe.get_all('HERO Frappe Mapping',
                filters={'definition_id': identity}, fields=['name', 'definition_id', 'status', 'schema_snapshot',
                        'schema_fingerprint', 'mapping_revision', 'operation', 'personal_graphql_mapping',
                        'schema_manifest', 'schema_dependencies']))
            if len(found) != 1:
                raise MappingDraftExchangeError('Selected field rule is missing or ambiguous.')
            row, base = found[0], bases[identity]
            current = {'name': _value(row, 'name'), 'revision': _positive_int(_value(row, 'mapping_revision')),
                       'operation': _value(row, 'operation'), 'schema_snapshot': _value(row, 'schema_snapshot'),
                       'schema_fingerprint': _value(row, 'schema_fingerprint')}
            if (_value(row, 'status') != 'Active' or not _same(current, base)
                    or current['schema_snapshot'] != snapshot_name
                    or current['schema_fingerprint'] != item['base_schema_fingerprint']
                    or (_value(row, 'personal_graphql_mapping') or '') not in ('', item['base_active_name'])
                    or current['operation'] != (content.get('target_operation') or item['operation_name'])):
                raise MappingDraftExchangeError('Selected field-rule provenance is stale or out of scope.')
            rule = rules[identity]
            rows.append(row)
            deadline = (content.get('target_operation') == 'HeroTasks' and content.get('native_task_onboarding')
                    and (rule.get('source_path'), rule.get('target_doctype'), rule.get('target_field'),
                    rule.get('purpose'), rule.get('transformation')) == (
                    'due_date', 'Task', 'custom_hero_due_date', 'Native Binding', 'Native Adapter'))
            selected.append((row, rule, deadline))
            if deadline:
                rows.append({'schema_manifest': Path(__file__).with_name('task_external_deadline_schema.json').read_text()})
    try:
        schema = _merge_export_schemas(rows)
        declared = {entry['id'] for group in schema.values() for entry in group}
        for row, rule, deadline in selected:
            dependencies = _exportable(row)['schema_dependencies']
            target = _target_dependency(schema, rule)
            if any(identity not in declared for identity in dependencies) or (target and target not in dependencies
                    and not (deadline and target == 'task-external-deadline')):
                raise MappingExchangeError('Selected field rule has incomplete schema dependencies.')
    except (MappingExchangeError, KeyError, TypeError, ValueError) as error:
        raise MappingDraftExchangeError('Selected field-rule schema dependencies are invalid or incomplete.') from error
    return schema


def preview_import(payload_json: str, source_instance: str, snapshot_name: str, direction: str) -> dict[str, object]:
    """Validate a proposed replacement without persisting drafts or schema."""
    if _portable_version(payload_json):
        return _preview_portable(payload_json, source_instance, snapshot_name, direction)
    package = _parse(payload_json, source_instance, snapshot_name, direction)
    _lock_context(source_instance, snapshot_name, direction)
    observed = _fresh_active_definitions(source_instance, snapshot_name, direction)
    observed_drafts = _open_drafts(set(observed))
    contents = [item['content'] for item in package['mappings']]
    contents.extend(_content(_value(row, 'mapping_json')) for row in observed.values())
    contents.extend(_content(_value(row, 'content_json')) for row in observed_drafts.values())
    definition_locks, rule_locks = _legacy_identity_lock_sets(set(observed), contents, direction)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    for identity in sorted(definition_locks):
        _lock_active_target('personal-new-definition', frappe.local.site, identity)
    for identity in sorted(rule_locks):
        _lock_active_target('portable-field-rule', frappe.local.site, identity)
    active = _lock_active_definitions(observed, source_instance, snapshot_name, direction)
    fingerprints = {str(_value(row, "schema_fingerprint")) for row in active.values()}
    if fingerprints != {package["schema_fingerprint"]}:
        raise MappingDraftExchangeError("The import schema fingerprint does not match the selected active context.")
    drafts = _lock_open_drafts(set(active))
    current_contents = [item['content'] for item in package['mappings']]
    current_contents.extend(_content(_value(row, 'mapping_json')) for row in active.values())
    current_contents.extend(_content(_value(row, 'content_json')) for row in drafts.values())
    current_defs, current_rules = _legacy_identity_lock_sets(set(active), current_contents, direction)
    if not current_defs <= definition_locks or not current_rules <= rule_locks:
        raise DraftConflict('Draft identities changed while locks were acquired; preview again.')
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


def _legacy_identity_lock_sets(definitions, contents, direction):
    """Plan v1 locks from both saved and proposed content before any row locks."""
    definitions, rules = set(definitions), set()
    from hero_integration.services.personal_mapping_content import identity_sets
    for content in contents:
        ids, children = identity_sets(content, direction)
        definitions.update(children)
        for identity in ids:
            try:
                rules.add(str(UUID(identity)))
            except (TypeError, ValueError, AttributeError):
                pass
            rows = frappe.get_all('HERO Frappe Mapping', filters={'definition_id': identity}, fields=['portable_definition_id'])
            if len(rows) > 1:
                raise DraftConflict('Field-rule identity is ambiguous.')
            portable = _value(rows[0], 'portable_definition_id') if rows else None
            if portable:
                rules.add(_portable_uuid(portable))
    return definitions, rules


def _prepare_item(item, row, local_content, schema, *, portable=False):
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
                if portable:
                    from hero_integration.api import _mapping_values_preserved
                    from hero_integration.services.frappe_writeback import normalise_draft_mapping, draft_source_type_compatible
                    action = {key: prepared['content'][key] for key in ('version', 'source', 'target', 'transformation')
                              if key in prepared['content']}
                    normalized = normalise_draft_mapping(action, storage._publication_snapshot[1])
                    source, target = normalized['source'], normalized['target']
                    if (not _mapping_values_preserved(action, normalized)
                            or target['operation'] != storage._definition.operation_name):
                        raise MappingDraftError('Imported draft action is incompatible with its schema operation.')
                    from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata, validate_draft_writeback_source
                    field = validate_draft_writeback_source(source['doctype'], source['field'], FrappePickerMetadata())
                    if not draft_source_type_compatible(field.fieldtype, target['graphql_type']):
                        raise MappingDraftError('Imported draft action has incompatible source and target types.')
                    prepared['metadata']['primary_action'] = {'mapping': normalized, 'graphql_type': target['graphql_type']}
                else:
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
    if _portable_version(payload_json):
        return _save_portable(payload_json, source_instance, snapshot_name, direction, expected_states,
                              confirmed=confirmed, preview_fingerprint=preview_fingerprint)
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
        if item['definition_id'] in expected:
            raise MappingDraftExchangeError('Draft import contains duplicate expected states.')
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
