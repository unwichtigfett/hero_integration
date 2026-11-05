"""Confirmed JSON Mutation import through the existing draft publication gates."""
from contextlib import contextmanager
from copy import deepcopy
import json
import hmac
from uuid import uuid4

import frappe

from hero_integration.services import mapping_draft_exchange as exchange
from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import MappingDraftError, draft_key


DIRECTION = 'FRAPPE_TO_MUTATION'


def _context(snapshot_name, direction):
    from hero_integration.api import _new_mapping_snapshot
    if direction != DIRECTION:
        raise MappingDraftError('Query mappings must be imported as a configuration package.')
    return _new_mapping_snapshot(snapshot_name)


def _activation_blockers(package, preview, schema):
    from hero_integration.api import _mapping_values_preserved
    from hero_integration.services.frappe_writeback import normalise_mapping
    blockers = []
    by_id = {row['definition_id']: row for row in preview['items']}
    for item in package['mappings']:
        identity = item['definition_id']
        state = by_id[identity]
        contracts = frappe.get_all('HERO Mutation Contract', filters={
            'schema_snapshot': preview['snapshot_name'], 'operation_name': item['operation_name'],
            'status': 'Active'}, pluck='name')
        if len(contracts) != 1:
            blockers.append({'definition_id': identity,
                'reason': 'Exactly one active schema-bound HERO Mutation Contract is required for writeback activation.'})
            continue
        local_id = state.get('local_definition_id') or identity
        key = draft_key(frappe.local.site, local_id, frappe.session.user)
        pending = frappe.get_all('HERO Mapping Draft', filters={'draft_key': key, 'owner_user': frappe.session.user,
            'site_scope': frappe.local.site, 'state': 'OPEN'}, fields=['warning_fingerprint'])
        if any(row.get('warning_fingerprint') for row in pending):
            blockers.append({'definition_id': identity, 'reason': 'Current draft warnings require separate review before import.'})
            continue
        content = deepcopy(item['content'])
        try:
            additions = content.get('additional_mappings') or []
            if content.get('usage') in {'SEND_TO_HERO', 'MAPPED_UPDATE_V2', 'PROJECT_LOGBOOK_V1'}:
                normalized = FrappeMappingDraftStorage()._validate_configured_send(content, schema)
                if not _mapping_values_preserved(content, normalized):
                    raise MappingDraftError('Active writeback normalization would lose draft parameters.')
            else:
                if additions and not state.get('local_definition_id'):
                    raise MappingDraftError('First publication of multiple mutation assignments is not supported.')
                content.pop('additional_mappings', None)
                assignments = [content] + [{key: child[key] for key in ('source', 'target', 'transformation') if key in child}
                                           for child in additions]
                for assignment in assignments:
                    normalized = normalise_mapping(assignment, schema)
                    if not _mapping_values_preserved(assignment, normalized):
                        raise MappingDraftError('Active writeback normalization would lose draft parameters.')
        except (ValueError, TypeError, KeyError) as error:
            blockers.append({'definition_id': identity, 'reason': str(error)})
    return blockers


def _preview_current(payload, snapshot_name, direction):
    snapshot, schema = _context(snapshot_name, direction)
    package = exchange._parse_portable(payload, direction)
    if any(package.get('schema', {}).values()):
        raise MappingDraftError('Mutation mapping imports cannot provision document fields.')
    result = exchange.preview_import(payload, snapshot.source_instance, snapshot_name, direction)
    blockers = _activation_blockers(package, result, schema)
    result = {**result, 'draft_preview_fingerprint': result['preview_fingerprint'],
              'can_apply': bool(result['can_save']) and not blockers, 'blockers': blockers,
              'activation': True, 'execution': False}
    result['preview_fingerprint'] = exchange._preview_binding(package, result)
    return result


def _apply_current(payload, snapshot_name, direction, expected_states, preview_fingerprint, *, confirmed=False):
    if confirmed is not True:
        raise MappingDraftError('Explicit import and publication confirmation is required.')
    checked = preview(payload, snapshot_name, direction)
    if not checked['can_apply']:
        raise MappingDraftError('Mutation import has unresolved publication blockers.')
    if not isinstance(preview_fingerprint, str) or not hmac.compare_digest(preview_fingerprint, checked['preview_fingerprint']):
        raise MappingDraftError('Mutation import preview changed; preview again before importing.')
    savepoint = 'hero_mutation_import_' + uuid4().hex
    frappe.db.savepoint(savepoint)
    published = []
    try:
        exchange.save_import(payload, checked['source_instance'], snapshot_name, direction, expected_states,
                             confirmed=True, preview_fingerprint=checked['draft_preview_fingerprint'])
        for item in sorted(checked['items'], key=lambda row: row['definition_id']):
            identity = item.get('local_definition_id') or item['definition_id']
            storage = FrappeMappingDraftStorage()
            draft = storage.service().open_draft(identity, frappe.session.user)
            if draft.content_revision == 0 and draft.base_active_name:
                # Saving identical active content intentionally creates no saved
                # revision. Retain that active definition instead of publishing
                # an unsaved baseline draft.
                from hero_integration.services.personal_mapping_content import same_value
                with storage.transaction(identity, frappe.session.user):
                    active = storage.active(identity)
                    if (draft.base_active_revision != active.revision or draft.base_active_hash != active.content_hash
                            or not same_value(draft.content, storage.initial_content(active.content))):
                        raise MappingDraftError('Active mapping changed before unchanged import confirmation.')
                    result = {'name': active.name, 'revision': active.revision, 'unchanged': True}
                storage.service().discard(identity, frappe.session.user, draft.generation,
                                          draft.content_revision, confirmed=True)
                published.append({'definition_id': identity, **result})
                continue
            result = storage.publish(identity, frappe.session.user, draft.generation, draft.content_revision,
                                     draft.base_active_revision, draft.reconciliation_revision, confirmed=True)
            published.append({'definition_id': identity, **result})
            # Publication of an existing mapping can leave the helper draft open.
            # Discard only this actor's exact successfully published revision.
            current = frappe.get_doc('HERO Mapping Draft',
                draft_key(frappe.local.site, identity, frappe.session.user), for_update=True)
            if current.state == 'OPEN':
                storage.service().discard(identity, frappe.session.user, int(current.generation),
                                          int(current.content_revision), confirmed=True)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return {'status': 'IMPORTED_MAPPINGS', 'direction': direction, 'activation': True,
            'execution': False, 'mappings': published}


def preview(payload, snapshot_name, direction):
    snapshot, _schema = _context(snapshot_name, direction)
    from hero_integration.services.mapping_import_context import resolve
    context = resolve(snapshot_name, snapshot.source_instance, payload)
    with _target_mutations(context['payload'], snapshot, direction):
        result = _preview_current(context['payload'], context['snapshot_name'], direction)
    return {**result, 'validated_against_snapshot': snapshot_name}


def apply(payload, snapshot_name, direction, expected_states, preview_fingerprint, *, confirmed=False):
    if confirmed is not True:
        raise MappingDraftError('Explicit import and publication confirmation is required.')
    checked = preview(payload, snapshot_name, direction)
    if not checked['can_apply']:
        raise MappingDraftError('Mutation import has unresolved publication blockers.')
    if not isinstance(preview_fingerprint, str) or not hmac.compare_digest(preview_fingerprint, checked['preview_fingerprint']):
        raise MappingDraftError('Mutation import preview changed; preview again before importing.')
    snapshot, _schema = _context(snapshot_name, direction)
    savepoint = 'hero_snapshot_mapping_import_' + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        from hero_integration.services.mapping_import_context import resolve
        context = resolve(snapshot_name, snapshot.source_instance, payload)
        actual = context['snapshot_name']
        with _target_mutations(context['payload'], snapshot, direction, persist=True):
            result = _apply_current(context['payload'], actual, direction, expected_states, preview_fingerprint, confirmed=True)
        return {**result, 'snapshot_name': actual, 'validated_against_snapshot': snapshot_name}
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


@contextmanager
def _target_mutations(payload, snapshot, direction, *, persist=False, existing_content=None):
    """Republish imported mutation identities in the selected local context.

    Preview rolls back native saves; apply owns the enclosing import savepoint.
    Only definitions explicitly present in the file can move. Their identities
    and history survive, and native saves revalidate against the target schema
    and contracts instead of carrying an old approval across endpoints.
    """
    frappe.only_for('System Manager')
    package = exchange._parse_portable(payload, direction)
    if any(package.get('schema', {}).values()):
        raise MappingDraftError('Mutation mapping imports cannot provision document fields.')
    point = 'hero_mutation_target_' + uuid4().hex
    frappe.db.savepoint(point)
    changed = []
    try:
        selected, scopes = [], {(snapshot.source_instance, snapshot.name, direction)}
        for item in sorted(package['mappings'], key=lambda row: row['definition_id']):
            matches = frappe.get_all('HERO GraphQL Mapping',
                filters={'portable_definition_id': item['definition_id']}, pluck='name', limit_page_length=2)
            if not matches:
                continue
            if len(matches) != 1:
                raise MappingDraftError('Portable identity is ambiguous on this site.')
            parent = frappe.get_doc('HERO GraphQL Mapping', matches[0])
            scope = (parent.source_instance, parent.schema_snapshot, parent.direction)
            selected.append((item, parent.name, scope))
            scopes.add(scope)
        for scope in sorted(scopes):
            exchange._lock_context(*scope)
        parents = []
        for item, name, scope in selected:
            parent = frappe.get_doc('HERO GraphQL Mapping', name, for_update=True)
            if (parent.source_instance, parent.schema_snapshot, parent.direction) != scope:
                raise MappingDraftError('Mapping context changed; preview the import again.')
            if parent.schema_snapshot == snapshot.name:
                continue
            if (parent.source_instance != snapshot.source_instance or parent.direction != direction
                    or parent.operation_kind != item['operation_kind'] or parent.operation_name != item['operation_name']):
                raise MappingDraftError('Portable identity belongs to a different target context.')
            parent.check_permission('write')
            if existing_content is not None:
                if parent.status != 'Active':
                    raise MappingDraftError('Existing inactive mutation requires review before switching schema contexts.')
                item['content'] = existing_content(parent, item)
            parents.append((parent, item))
        exchange._bind_mapped_update_readbacks(package, snapshot.source_instance, snapshot.name)
        for parent, item in parents:
            # Native edit invalidates the old active revision. Native activation
            # then requires this actor and a validated target Mutation Contract.
            changed.append(parent.name)
            from hero_integration.services.mapping_draft_access import configuration_import_mapping
            with configuration_import_mapping(parent):
                parent.schema_snapshot = snapshot.name
                parent.mapping_json = json.dumps(item['content'], ensure_ascii=False)
                parent.status = 'Draft'
                parent.save()
                parent.status = 'Active'
                parent.save()
        yield
    finally:
        if not persist:
            frappe.db.rollback(save_point=point)
        for name in changed:
            frappe.clear_document_cache('HERO GraphQL Mapping', name)
