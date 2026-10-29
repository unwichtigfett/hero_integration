"""Scoped, revision-checked writes for existing shared field-rule drafts."""
from __future__ import annotations

import json
from uuid import UUID

import frappe

from hero_integration.services.frappe_mapping_draft_storage import assert_portable_identity_schema
from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError


def _require_global_consumer_authority():
    # Runtime may select shared rules operation-wide, even outside this source.
    raise MappingDraftError('Shared rule activation requires global consumer reconciliation before it can be enabled.')


def activate_draft_rule(mapping_name, rule_name, source_instance, snapshot_name, expected_mapping_revision,
                        expected_rule_revision, *, target_doctype=None, target_field=None):
    """Activate an owned shared Draft after locking both its old and proposed target."""
    frappe.only_for('System Manager')
    actor = frappe.session.user
    if not actor or actor == 'Guest':
        raise PermissionError('An authenticated System Manager is required.')
    if not all(isinstance(value, str) and value for value in (mapping_name, rule_name, source_instance, snapshot_name)):
        raise MappingDraftError('Exact mapping, rule, source and snapshot are required.')
    if any(type(value) is not int or value < 1 for value in (expected_mapping_revision, expected_rule_revision)):
        raise MappingDraftError('Positive mapping and rule revisions are required.')
    if (target_doctype is None) != (target_field is None):
        raise MappingDraftError('Both target fields must be supplied together.')
    if target_doctype is not None and not all(isinstance(value, str) and value for value in (target_doctype, target_field)):
        raise MappingDraftError('The proposed target is invalid.')
    assert_portable_identity_schema(frappe)
    parent = frappe.get_doc('HERO GraphQL Mapping', mapping_name)
    rule = frappe.get_doc('HERO Frappe Mapping', rule_name)
    parent.check_permission('read')
    rule.check_permission('write')
    scope = (source_instance, snapshot_name, 'QUERY_TO_FRAPPE')
    if (parent.source_instance, parent.schema_snapshot, parent.direction) != scope or parent.status != 'Active':
        raise DraftConflict('The active mapping is outside the selected source and direction.')
    if rule.schema_snapshot != snapshot_name or rule.get('personal_graphql_mapping'):
        raise DraftConflict('The rule is outside the selected shared mapping scope.')
    if rule.status != 'Draft' or rule.owner != actor or rule.modified_by != actor:
        raise PermissionError('Only the last author may activate a shared rule Draft.')
    enrolled = rule.get('portable_definition_id') or ''
    try:
        portable = str(UUID(enrolled or rule.definition_id))
    except (ValueError, TypeError, AttributeError):
        raise MappingDraftError('Enroll the rule portable identity before activation.') from None
    if enrolled and portable != enrolled:
        raise MappingDraftError('The rule portable identity is invalid.')
    old_target = (rule.operation, rule.target_doctype, rule.target_field)
    proposed = (rule.operation, target_doctype or rule.target_doctype, target_field or rule.target_field)
    from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    lock_exchange_context(*scope)
    if rule.purpose != 'Record Name':
        for operation, doctype, field in sorted({old_target, proposed}):
            _lock_active_target(operation, doctype, field)
    _lock_active_target('personal-new-definition', frappe.local.site, parent.definition_id)
    identities = {portable}
    try:
        identities.add(str(UUID(rule.definition_id)))
    except (ValueError, TypeError, AttributeError):
        pass
    for identity in sorted(identities):
        _lock_active_target('portable-field-rule', frappe.local.site, identity)
    for identity in sorted(identities):
        collisions = frappe.db.sql('select name from `tabHERO Frappe Mapping` where portable_definition_id=%s '
                                   'or definition_id in (%s,%s) for update',
                                   (identity, identity, UUID(identity).hex), as_dict=True)
        if any(row.get('name') != rule_name for row in collisions):
            raise DraftConflict('The portable rule identity collides with another rule.')
    locked_parent = frappe.get_doc('HERO GraphQL Mapping', mapping_name, for_update=True)
    locked_rule = frappe.get_doc('HERO Frappe Mapping', rule_name, for_update=True)
    locked_parent.check_permission('read')
    locked_rule.check_permission('write')
    if ((locked_parent.source_instance, locked_parent.schema_snapshot, locked_parent.direction) != scope
            or locked_parent.status != 'Active' or locked_parent.definition_id != parent.definition_id
            or int(locked_parent.mapping_revision or 0) != expected_mapping_revision):
        raise DraftConflict('Mapping context or revision changed; reload before activation.')
    if (locked_rule.status != 'Draft' or locked_rule.owner != actor or locked_rule.modified_by != actor
            or locked_rule.schema_snapshot != snapshot_name or locked_rule.get('personal_graphql_mapping')
            or locked_rule.definition_id != rule.definition_id or (locked_rule.get('portable_definition_id') or '') != enrolled
            or (locked_rule.operation, locked_rule.target_doctype, locked_rule.target_field) != old_target
            or locked_rule.purpose != rule.purpose or int(locked_rule.mapping_revision or 0) != expected_rule_revision):
        raise DraftConflict('Rule ownership, identity, target or revision changed; reload before activation.')
    try:
        config = json.loads(locked_parent.mapping_json)
    except (TypeError, ValueError):
        raise DraftConflict('The active mapping cannot be safely inspected.') from None
    if not isinstance(config, dict) or (config.get('target_operation') or locked_parent.operation_name) != locked_rule.operation:
        raise DraftConflict('The rule operation is outside the selected mapping scope.')
    from hero_integration.services.personal_mapping_content import identity_sets, references
    from hero_integration.services.mapping_draft_service import draft_key
    identity_sets(config, 'QUERY_TO_FRAPPE')
    selected = set(references(config)) | {item['id'] for item in config.get('additional_mappings', [])}
    if locked_rule.definition_id not in selected:
        rows = frappe.db.sql('select owner_user, state, content_json from `tabHERO Mapping Draft` '
                             'where draft_key=%s for update',
                             (draft_key(frappe.local.site, locked_parent.definition_id, actor),), as_dict=True)
        if len(rows) != 1 or rows[0].get('owner_user') != actor or rows[0].get('state') != 'OPEN':
            raise DraftConflict('The rule is not selected by this mapping or its owned Draft.')
        try:
            draft_content = json.loads(rows[0].get('content_json') or '')
            identity_sets(draft_content, 'QUERY_TO_FRAPPE')
            selected = set(references(draft_content)) | {item['id'] for item in draft_content.get('additional_mappings', [])}
        except (ValueError, TypeError):
            raise DraftConflict('The owned Draft cannot be safely inspected.') from None
        if locked_rule.definition_id not in selected:
            raise DraftConflict('The rule is not selected by this mapping or its owned Draft.')
    from hero_integration.api import _validated_snapshot
    snapshot, _ = _validated_snapshot(snapshot_name, for_update=True)
    if (snapshot.source_instance != source_instance or snapshot.schema_fingerprint != locked_parent.schema_fingerprint
            or snapshot.schema_fingerprint != locked_rule.schema_fingerprint):
        raise DraftConflict('Mapping or rule schema provenance changed; reload before activation.')
    _require_global_consumer_authority()
    locked_rule.target_doctype, locked_rule.target_field = proposed[1:]
    locked_rule.status = 'Active'
    locked_rule.save()
    return {'status': locked_rule.status, 'definition_id': locked_rule.definition_id,
            'revision': int(locked_rule.mapping_revision or 0)}
