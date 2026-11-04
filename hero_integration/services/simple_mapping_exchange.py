"""Confirmed JSON Mutation import through the existing draft publication gates."""
from copy import deepcopy
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
    from hero_integration.services.mapping_snapshot_rebase import preview_target
    snapshot, _schema = _context(snapshot_name, direction)
    with preview_target(snapshot_name, snapshot.source_instance):
        return _preview_current(payload, snapshot_name, direction)


def apply(payload, snapshot_name, direction, expected_states, preview_fingerprint, *, confirmed=False):
    if confirmed is not True:
        raise MappingDraftError('Explicit import and publication confirmation is required.')
    checked = preview(payload, snapshot_name, direction)
    if not checked['can_apply']:
        raise MappingDraftError('Mutation import has unresolved publication blockers.')
    if not isinstance(preview_fingerprint, str) or not hmac.compare_digest(preview_fingerprint, checked['preview_fingerprint']):
        raise MappingDraftError('Mutation import preview changed; preview again before importing.')
    from hero_integration.services.mapping_snapshot_rebase import prepare
    snapshot, _schema = _context(snapshot_name, direction)
    savepoint = 'hero_snapshot_mapping_import_' + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        plan = prepare(snapshot_name, snapshot.source_instance)
        if plan['needs_rebase']:
            prepare(snapshot_name, snapshot.source_instance, apply=True, expected_fingerprint=plan['fingerprint'])
        return _apply_current(payload, snapshot_name, direction, expected_states, preview_fingerprint, confirmed=True)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
