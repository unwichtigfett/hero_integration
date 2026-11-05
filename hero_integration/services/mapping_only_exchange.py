"""Query mapping files transfer definitions and their reader/schema dependencies only."""
from copy import deepcopy
from hashlib import sha256
import hmac
import json
import re

import frappe

from hero_integration.services import config_exchange as config
from hero_integration.services.localized_error import LocalizedError

FORMAT_VERSION = 'hero-integration-mappings-v1'
KEYS = {'format_version', 'schema_fingerprint', 'replace_mappings', 'mapping_package', 'read_services'}
MAPPING_KEYS = {'records_path', 'record_path', 'identity_path', 'mapping_definition_ids', 'target_operation',
                'query_adapter', 'variables', 'page_size', 'native_options', 'customer_service',
                'customer_contract_version', 'project_contract_version', 'native_task_onboarding'}
MAX_BYTES = 5_000_000


class MappingOnlyError(LocalizedError):
    pass


def parse_file(payload):
    from hero_integration.services.personal_mapping_content import unique_json_object
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_BYTES:
        raise MappingOnlyError('The mapping file exceeds the permitted size.')
    try:
        value = json.loads(payload, object_pairs_hook=unique_json_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError) as error:
        raise MappingOnlyError('The mapping file is not valid JSON.') from error
    if not isinstance(value, dict) or set(value) != KEYS or value.get('format_version') != FORMAT_VERSION:
        raise MappingOnlyError('Import mapping files here. Configuration packages belong in Settings.')
    if (not isinstance(value['schema_fingerprint'], str) or not re.fullmatch('[0-9a-f]{64}', value['schema_fingerprint'])
            or value['replace_mappings'] is not True or not isinstance(value['mapping_package'], dict)
            or value['mapping_package'].get('schema_fingerprint') != value['schema_fingerprint']
            or not isinstance(value['read_services'], list) or not 1 <= len(value['read_services']) <= 9):
        raise MappingOnlyError('The Query mapping package is incomplete or has a different schema identity.')
    for service in value['read_services']:
        if (not isinstance(service, dict) or set(service) != config.SERVICE_KEYS
                or not isinstance(service.get('mapping'), dict) or set(service['mapping']) - MAPPING_KEYS):
            raise MappingOnlyError('Mapping files cannot contain settings, credentials or contract configuration.')
    return value


def export(snapshot_name, source_instance):
    from hero_integration.services.mapping_import_context import resolve
    context = resolve(snapshot_name, source_instance)
    return config.export_config(context['snapshot_name'], source_instance, mappings_only=True)


def _snapshot(snapshot_name, source_instance):
    from hero_integration.api import _new_mapping_snapshot
    snapshot, _schema = _new_mapping_snapshot(snapshot_name)
    if snapshot.source_instance != source_instance:
        raise MappingOnlyError('The selected mapping schema belongs to another HERO source.')
    return snapshot


def _configuration(value, snapshot):
    if value['schema_fingerprint'] != snapshot.schema_fingerprint:
        raise MappingOnlyError('Select a validated snapshot matching the mapping file schema.')
    payload = {'format_version': config.FORMAT_VERSION, 'replace_mappings': True,
        'schema_snapshot': {'schema_fingerprint': snapshot.schema_fingerprint, 'schema': json.loads(snapshot.snapshot_json)},
        'settings': {}, 'mapping_package': deepcopy(value['mapping_package']), 'read_services': deepcopy(value['read_services'])}
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    config._parse(body)
    return body


def _state_rows(doctype, fields, filters, *, lock):
    if not lock:
        return frappe.get_all(doctype, filters=filters, fields=fields, order_by='name asc', limit_page_length=0)
    # This is a current/locking read even on a REPEATABLE READ transaction.
    # Column and table names are supplied exclusively by the static calls below.
    clauses, values = [], []
    for key, value in filters.items():
        if isinstance(value, list):
            operation, members = value
            if operation != 'in' or not members:
                return []
            clauses.append('`' + key + '` IN (' + ','.join(['%s'] * len(members)) + ')')
            values.extend(members)
        else:
            clauses.append('`' + key + '`=%s')
            values.append(value)
    return frappe.db.sql('SELECT ' + ','.join('`' + key + '`' for key in fields)
        + ' FROM `tab' + doctype + '` WHERE ' + ' AND '.join(clauses) + ' ORDER BY name FOR UPDATE', values, as_dict=True)


def _target_state(source_instance, operations, *, lock=False):
    """Bind replacement confirmation to current mappings, readers and own drafts."""
    rows = _state_rows('HERO GraphQL Mapping',
        ['name', 'definition_id', 'schema_snapshot', 'mapping_revision', 'mapping_json'],
        {'source_instance': source_instance, 'direction': 'QUERY_TO_FRAPPE', 'status': 'Active'}, lock=lock)
    selected = [row for row in rows if json.loads(row.mapping_json).get('target_operation') in operations]
    mapping_names = [row.name for row in selected]
    services = _state_rows('HERO GraphQL Service',
        ['name', 'service_revision', 'status', 'execution_status', 'read_mapping', 'query_document',
         'approved_service_revision', 'approved_mapping_revision'], {'read_mapping': ['in', mapping_names]}, lock=lock) if mapping_names else []
    drafts = []
    for row in _state_rows('HERO Mapping Draft',
            ['name', 'base_active_mapping', 'new_definition_json', 'content_json', 'content_revision',
             'generation', 'reconciliation_revision'],
            {'site_scope': frappe.local.site, 'owner_user': frappe.session.user, 'state': 'OPEN'}, lock=lock):
        content = json.loads(row.content_json or '{}')
        context = json.loads(row.new_definition_json or '{}')
        if (row.base_active_mapping in mapping_names or (
                context.get('source_instance') == source_instance and context.get('direction') == 'QUERY_TO_FRAPPE'
                and content.get('target_operation') in operations)):
            drafts.append(dict(row))
    # Only the digest goes to the client; no draft content or stored values are exported.
    return sha256(json.dumps([selected, services, drafts], sort_keys=True, default=str).encode()).hexdigest()


def _preview_current(payload, snapshot_name, source_instance):
    frappe.only_for('System Manager')
    value = parse_file(payload)
    snapshot = _snapshot(snapshot_name, source_instance)
    body = _configuration(value, snapshot)
    checked = config.preview_config(snapshot.name, body, source_instance)
    checked.pop('settings', None)
    checked['format_version'] = FORMAT_VERSION
    checked.update(direction='QUERY_TO_FRAPPE', activation=True, execution=False, can_apply=True)
    from hero_integration.services.mapping_draft_exchange import _preview_binding
    state = _target_state(source_instance, [item['target_operation'] for item in value['read_services']])
    checked['target_revision'] = state
    checked['preview_fingerprint'] = _preview_binding(value, {**checked, 'snapshot_name': snapshot.name,
        'source_instance': source_instance, 'target_revision': state})
    return checked


def _apply_current(payload, snapshot_name, source_instance, fingerprint, *, confirmed=False, compatibility_check=None):
    if confirmed is not True:
        raise MappingOnlyError('Explicit mapping import confirmation is required.')
    checked = preview(payload, snapshot_name, source_instance)
    if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, checked['preview_fingerprint']):
        raise MappingOnlyError('The mapping import preview changed; preview the file again.')
    value = parse_file(payload)
    snapshot = _snapshot(snapshot_name, source_instance)
    operations = [item['target_operation'] for item in value['read_services']]
    observed = checked['target_revision']
    rebase_savepoint = None
    def verify_current(locked_snapshot):
        nonlocal rebase_savepoint
        from hero_integration.services.mapping_snapshot_rebase import prepare
        from uuid import uuid4
        # Provisioning may commit DDL implicitly. Start the atomic metadata
        # transition only here, after all schema DDL has completed.
        rebase_savepoint = 'hero_snapshot_query_import_' + uuid4().hex
        frappe.db.savepoint(rebase_savepoint)
        if compatibility_check is not None:
            compatibility_check()
        plan = prepare(snapshot_name, source_instance)
        if plan['needs_rebase']:
            prepare(snapshot_name, source_instance, apply=True, expected_fingerprint=plan['fingerprint'])
        if (locked_snapshot.name != snapshot.name
                or _target_state(source_instance, operations, lock=True) != observed):
            raise MappingOnlyError('The mapping import preview changed; preview the file again.')
    try:
        result = config.import_config(snapshot.name, _configuration(value, snapshot), source_instance,
                                      before_mapping_write=verify_current)
    except Exception:
        if rebase_savepoint:
            frappe.db.rollback(save_point=rebase_savepoint)
        raise
    result.pop('settings', None)
    return {**result, 'direction': 'QUERY_TO_FRAPPE', 'activation': True, 'execution': False}


def preview(payload, snapshot_name, source_instance):
    from hero_integration.services.mapping_snapshot_rebase import preview_target
    from hero_integration.services.mapping_import_context import resolve
    context = resolve(snapshot_name, source_instance, payload)
    with preview_target(context['snapshot_name'], source_instance):
        result = _preview_current(context['payload'], context['snapshot_name'], source_instance)
    return {**result, 'validated_against_snapshot': snapshot_name}


def apply(payload, snapshot_name, source_instance, fingerprint, *, confirmed=False):
    if confirmed is not True:
        raise MappingOnlyError('Explicit mapping import confirmation is required.')
    from hero_integration.services.mapping_import_context import resolve
    context = resolve(snapshot_name, source_instance, payload)
    def recheck():
        current = resolve(snapshot_name, source_instance, payload)
        if current['snapshot_name'] != context['snapshot_name'] or current['payload'] != context['payload']:
            raise MappingOnlyError('The mapping import preview changed; preview the file again.')
    result = _apply_current(context['payload'], context['snapshot_name'], source_instance, fingerprint,
        confirmed=True, compatibility_check=recheck)
    return {**result, 'snapshot_name': context['snapshot_name'], 'validated_against_snapshot': snapshot_name}
