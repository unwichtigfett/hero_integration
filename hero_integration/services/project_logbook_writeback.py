"""Explicit creation of one local Project activity in the HERO logbook.

Project identity, permissions, source approval and duplicate protection remain
server-owned. JSON transfers configuration only; no import executes a mutation.
"""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID

import frappe

from hero_integration.services.localized_error import LocalizedError

USAGE = 'PROJECT_LOGBOOK_V1'
OPERATION = 'add_logbook_entry'
AREA = 'HeroProjectMatches'
OUTBOUND_VERSION = 'project-logbook-outbound-v1'
PENDING_TYPE = 'project_logbook_pending'
PARENT = {'doctype': 'Project', 'reference_doctype_field': 'reference_doctype',
          'reference_name_field': 'reference_name', 'source_type': 'project', 'target': 'project_match'}
VERSION_SOURCE = {'doctype': 'Version', 'field': 'data', 'reference_doctype_field': 'ref_doctype',
                  'reference_name_field': 'docname', 'resolver': 'CHANGE_SUMMARY'}
COMMENT_TYPES = frozenset({'Comment', 'Like', 'Info', 'Label', 'Workflow', 'Created', 'Submitted', 'Cancelled',
    'Updated', 'Deleted', 'Assigned', 'Assignment Completed', 'Attachment', 'Attachment Removed',
    'Shared', 'Unshared', 'Bot', 'Relinked', 'Edit'})


class ProjectLogbookWritebackError(LocalizedError):
    pass


def _fail(message):
    raise ProjectLogbookWritebackError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str, allow_nan=False)


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def normalise_mapping(content, raw_schema):
    """Validate the closed activity-create profile without any I/O."""
    from hero_integration.services.mapped_update_v2 import _schema, _input_type
    from hero_integration.services.personal_mapping_content import bounded
    from graphql import get_named_type, is_enum_type
    bounded(content)
    keys = {'usage', 'version', 'area', 'read_service', 'source', 'target', 'parent', 'assignments'}
    if (not isinstance(content, dict) or set(content) - {'comment_types', 'version_source'} != keys or content.get('usage') != USAGE
            or type(content.get('version')) is not int or content['version'] != 1
            or content.get('area') != AREA or content.get('source') != {'doctype': 'Comment'}
            or content.get('target') != {'operation': OPERATION} or content.get('parent') != PARENT
            or not isinstance(content.get('read_service'), str) or not 0 < len(content['read_service']) <= 140
            or not isinstance(content.get('assignments'), list) or len(content['assignments']) != 1):
        _fail('Invalid Project logbook mutation configuration.')
    kinds = content.get('comment_types', ['Comment'])
    if (not isinstance(kinds, list) or not kinds or any(not isinstance(kind, str) or kind not in COMMENT_TYPES for kind in kinds)
            or len(kinds) != len(set(kinds)) or 'version_source' in content and content['version_source'] != VERSION_SOURCE):
        _fail('Invalid automatic Project activity source configuration.')
    item = content['assignments'][0]
    if (not isinstance(item, dict) or set(item) != {'id', 'source', 'input_path', 'resolver', 'options'}
            or item.get('source') != {'doctype': 'Comment', 'field': 'content'}
            or item.get('input_path') != 'logbook_entry.custom_text'
            or item.get('resolver') != 'PLAIN_TEXT' or item.get('options') != {}):
        _fail('Project logbook mapping requires the native activity content and an explicit plain-text conversion.')
    try:
        if not isinstance(item.get('id'), str) or str(UUID(item['id'])) != item['id']:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail('Activity assignment identity must be a canonical UUID.')
    schema = _schema(raw_schema)
    text = _input_type(schema, OPERATION, 'logbook_entry.custom_text')
    target = get_named_type(_input_type(schema, OPERATION, 'logbook_entry.target'))
    identity = get_named_type(_input_type(schema, OPERATION, 'logbook_entry.target_id'))
    response = schema.mutation_type.fields[OPERATION]
    output = get_named_type(response.type)
    if (str(get_named_type(text)) != 'String' or not is_enum_type(target) or 'project_match' not in target.values
            or str(identity) != 'Int' or 'id' not in output.fields
            or str(get_named_type(output.fields['id'].type)) != 'Int'):
        _fail('The saved HERO schema does not support the approved Project logbook inputs and response.')
    return deepcopy(content)


def validate_sources(content, *, field_provider=None):
    """Check installed native fields; no new visible fields are needed."""
    sources = [('Comment', name, types) for name, types in {
        'content': {'HTML Editor', 'Text Editor', 'Long Text', 'Text'}, 'reference_doctype': {'Link'},
        'reference_name': {'Dynamic Link'}, 'comment_type': {'Select'}}.items()]
    if content.get('version_source'):
        sources += [('Version', 'data', {'Code', 'Long Text', 'Text'}), ('Version', 'ref_doctype', {'Link'}),
                    ('Version', 'docname', {'Data'})]
    for doctype, name, types in sources:
        field = field_provider(doctype, name) if field_provider else frappe.get_meta(doctype).get_field(name)
        if field is None or field.fieldtype not in types or getattr(field, 'is_virtual', False):
            _fail('The native Project activity fields are missing or incompatible.')
    return content


def _comment_scope(source, comment, activity_doctype='Comment'):
    identity = comment if activity_doctype == 'Comment' else activity_doctype + ':' + comment
    return _hash(['hero-project-logbook-create-v1', frappe.local.site, source, identity])


def _assert_not_submitted(source, comment, *, activity_doctype='Comment', exclude=None):
    rows = frappe.get_all('HERO Mutation Audit', filters={'request_fingerprint': _comment_scope(source, comment, activity_doctype)},
                          fields=['correlation_id'], limit_page_length=0)
    if any(row.correlation_id != exclude for row in rows):
        _fail('This activity was already submitted or has an uncertain logbook result; reconcile it before continuing.')


def project_marker(source, project, *, lock=False, frappe_api=None):
    f = frappe_api or frappe
    filters = {'source_system': 'HERO', 'source_instance': source, 'source_type': PENDING_TYPE,
               'target_doctype': 'Project', 'target_name': project}
    if lock:
        rows = f.db.sql('SELECT name, sync_status, conflict_detail FROM `tabHERO Integration Link` '
            'WHERE source_system=%s AND source_instance=%s AND source_type=%s '
            'AND target_doctype=%s AND target_name=%s FOR UPDATE', tuple(filters.values()), as_dict=True)
    else:
        rows = f.get_all('HERO Integration Link', filters=filters,
                        fields=['name', 'sync_status', 'conflict_detail'], limit_page_length=2)
    if len(rows) > 1:
        _fail('The Project logbook reservation is ambiguous; reconciliation is required.')
    return rows[0] if rows else None


def assert_project_import_clear(source, project, *, frappe_api=None):
    marker = project_marker(source, project, lock=True, frappe_api=frappe_api)
    if marker and marker.sync_status != 'Synchronized':
        _fail('The Project has an unconfirmed outbound logbook activity; reconcile it before importing its logbook.')


def _activity_raw(document):
    return document.data if getattr(document, 'doctype', 'Comment') == 'Version' else document.content


def _activity_parent(document, activity_doctype):
    return ((document.reference_doctype, document.reference_name) if activity_doctype == 'Comment'
            else (document.ref_doctype, document.docname))


def _native_comment(comment, *, activity_doctype='Comment', lock=False, receipt=None):
    if activity_doctype not in {'Comment', 'Version'}:
        _fail('Unsupported Project activity document type.')
    document = frappe.get_doc(activity_doctype, comment)
    frappe.has_permission(activity_doctype, 'read', doc=document, throw=True)
    if activity_doctype == 'Comment':
        frappe.has_permission('Comment', 'write', doc=document, throw=True)
    parent_type, parent_name = _activity_parent(document, activity_doctype)
    if (parent_type != 'Project' or not parent_name
            or activity_doctype == 'Comment' and document.comment_type not in COMMENT_TYPES):
        _fail('Only a native activity on a Project can be sent to the HERO logbook.')
    project = frappe.get_doc('Project', parent_name, for_update=lock)
    frappe.has_permission('Project', 'write', doc=project, throw=True)
    if lock:
        document = frappe.get_doc(activity_doctype, comment, for_update=True)
        frappe.has_permission(activity_doctype, 'write' if activity_doctype == 'Comment' else 'read', doc=document, throw=True)
        if _activity_parent(document, activity_doctype) != ('Project', project.name):
            _fail('The activity changed its Project or content; review it again.')
    if lock:
        rows = frappe.db.sql('SELECT name FROM `tabHERO Integration Link` WHERE source_system=%s '
            'AND source_type=%s AND target_doctype=%s AND target_name=%s FOR UPDATE',
            ('HERO', 'project_history', activity_doctype, document.name), as_dict=True)
        links = [row.name for row in rows]
    else:
        links = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_type': 'project_history',
                'target_doctype': activity_doctype, 'target_name': document.name}, pluck='name', limit_page_length=0)
    if links and links != [receipt]:
        _fail('Imported or already linked logbook activities cannot be submitted again.')
    return document, project


def _version_text(document):
    from hero_integration.services.personal_mapping_content import unique_json_object
    from hero_integration.services.task_send import plain_text
    try:
        value = json.loads(document.data, object_pairs_hook=unique_json_object)
        if not isinstance(value, dict): raise ValueError
    except (ValueError, TypeError):
        _fail('The automatic Project activity contains invalid change data.')
    if value.get('_hero_import') is True:
        return ''
    metadata = frappe.get_meta('Project')
    lines = []
    if any(not isinstance(value.get(key, []), list) for key in ('changed', 'added', 'removed', 'row_changed')):
        _fail('The automatic Project activity contains invalid change data.')
    for change in value.get('changed', []):
        if not isinstance(change, list) or len(change) != 3: continue
        name, before, after = change
        field = metadata.get_field(name) if isinstance(name, str) else None
        if field is None or field.fieldtype == 'Password' or getattr(field, 'hidden', False): continue
        if isinstance(before, (dict, list)) or isinstance(after, (dict, list)): continue
        previous = plain_text(str(before)) if before is not None else '—'
        current = plain_text(str(after)) if after is not None else '—'
        lines.append(str(frappe._(field.label or name)) + ': ' + str(previous or '—') + ' → ' + str(current or '—'))
    for key, label in (('added', 'Added'), ('removed', 'Removed'), ('row_changed', 'Changed')):
        counts = {}
        for row in value.get(key, []):
            if isinstance(row, list) and row and isinstance(row[0], str):
                counts[row[0]] = counts.get(row[0], 0) + 1
        for name, count in counts.items():
            field = metadata.get_field(name)
            if field is not None and not getattr(field, 'hidden', False) and field.fieldtype == 'Table':
                lines.append(str(frappe._(field.label or name)) + ': ' + str(frappe._(label)) + ' (' + str(count) + ')')
    return '\n'.join(lines)


def _activity_text(document, content):
    from hero_integration.services.task_send import plain_text
    kind = getattr(document, 'doctype', 'Comment')
    if kind == 'Version':
        if not content.get('version_source'):
            _fail('The mapping does not include automatic Project change activities.')
        return _version_text(document)
    if document.comment_type not in content.get('comment_types', ['Comment']):
        _fail('This automatic activity type is not selected by the logbook mapping.')
    text = plain_text(document.content)
    if not text and document.comment_type != 'Comment': text = str(frappe._(document.comment_type))
    return text


def _load_context(comment, *, activity_doctype='Comment', lock=False, audit=None, receipt=None):
    from hero_integration.client.settings import get_configuration
    from hero_integration.services import mapped_send
    from hero_integration.services.personal_mapping_content import unique_json_object
    from hero_integration.services.writeback_source import capture_source
    from hero_integration.services.config_mutation_contracts import _scope
    from hero_integration.services.graphql_contract import parse_mutation
    if not mapped_send._roles():
        _fail('Only System Managers and HERO Writeback users may send to HERO.')
    document, project = _native_comment(comment, activity_doctype=activity_doctype, lock=lock, receipt=receipt)
    config = get_configuration(fresh=True)
    rows = frappe.get_all('HERO GraphQL Mapping', filters={'status': 'Active', 'direction': 'FRAPPE_TO_MUTATION',
        'operation_kind': 'MUTATION', 'source_instance': config.source_instance, 'operation_name': OPERATION}, pluck='name', limit_page_length=2)
    if len(rows) != 1:
        _fail('Exactly one active Project logbook mutation mapping is required.')
    mapping = frappe.get_doc('HERO GraphQL Mapping', rows[0], for_update=lock); mapping.check_permission('read')
    snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', mapping.schema_snapshot, for_update=lock); snapshot.check_permission('read')
    if (mapping.status != 'Active' or mapping.source_instance != config.source_instance
            or mapping.direction != 'FRAPPE_TO_MUTATION' or mapping.operation_name != OPERATION):
        _fail('The active logbook mapping, snapshot and confirmed HERO source no longer agree.')
    if (snapshot.analysis_status != 'Validated' or getattr(snapshot, 'execution_contract_status', '') == 'Blocked'
            or mapping.schema_fingerprint != snapshot.schema_fingerprint or snapshot.source_instance != config.source_instance):
        _fail('The active logbook mapping, snapshot and confirmed HERO source no longer agree.')
    raw_schema = json.loads(snapshot.snapshot_json)
    content = normalise_mapping(json.loads(mapping.mapping_json, object_pairs_hook=unique_json_object), raw_schema)
    validate_sources(content)
    if not _activity_text(document, content):
        _fail('The activity must contain nonempty text within the permitted length.')
    proof = capture_source(snapshot, config.source_instance, config)
    filters = {'source_system': 'HERO', 'source_type': 'project',
        'source_instance': proof.source_instance, 'target_doctype': 'Project', 'target_name': project.name}
    if lock:
        links = frappe.db.sql('SELECT name, source_id, sync_status, source_payload_hash, observed_source_hash '
            'FROM `tabHERO Integration Link` WHERE source_system=%s AND source_type=%s '
            'AND source_instance=%s AND target_doctype=%s AND target_name=%s FOR UPDATE',
            tuple(filters.values()), as_dict=True)
    else:
        links = frappe.get_all('HERO Integration Link', filters=filters,
            fields=['name', 'source_id', 'sync_status', 'source_payload_hash', 'observed_source_hash'], limit_page_length=2)
    if (len(links) != 1 or links[0].sync_status != 'Synchronized'
            or links[0].observed_source_hash and links[0].observed_source_hash != links[0].source_payload_hash):
        _fail('Logbook sending requires one synchronized Project binding without source conflicts.')
    try:
        hero_id = int(links[0].source_id)
        if hero_id <= 0 or str(hero_id) != str(links[0].source_id): raise ValueError
    except (ValueError, TypeError):
        _fail('The Project binding has an invalid HERO identity.')
    _assert_not_submitted(proof.source_instance, comment, activity_doctype=activity_doctype, exclude=getattr(audit, 'correlation_id', None))
    marker = project_marker(proof.source_instance, project.name, lock=lock)
    if marker and marker.sync_status != 'Synchronized' and (audit is None or marker.conflict_detail != audit.correlation_id):
        _fail('The Project has an unconfirmed outbound logbook activity; reconcile it before importing its logbook.')
    read = mapped_send._load_read(content['read_service'], snapshot)
    if lock:
        from hero_integration.services.read_contract import from_documents
        locked_service = frappe.get_doc('HERO GraphQL Service', read.service.name, for_update=True)
        locked_mapping = frappe.get_doc('HERO GraphQL Mapping', locked_service.read_mapping, for_update=True)
        if locked_service.schema_snapshot != snapshot.name:
            _fail('The approved mutation contract or service changed.')
        read = SimpleNamespace(service=locked_service, mapping=locked_mapping,
            contract=from_documents(locked_service, locked_mapping, snapshot))
    if (read.contract.target_operation != AREA or read.contract.identity_path != 'id'
            or not (read.contract.adapter_options or {}).get('logbook')):
        _fail('Logbook sending requires the approved Project reader including its logbook.')
    _require_readback_paths(read)
    contracts = frappe.get_all('HERO Mutation Contract', filters={'schema_snapshot': snapshot.name,
        'operation_name': OPERATION, 'status': 'Active'}, pluck='name', limit_page_length=2)
    if len(contracts) != 1:
        _fail('Exactly one active schema-bound mutation contract is required.')
    contract = frappe.get_doc('HERO Mutation Contract', contracts[0], for_update=lock)
    if contract.status != 'Active' or contract.operation_name != OPERATION:
        _fail('The approved mutation contract or service changed.')
    scope = _scope(json.loads(contract.mapping_json, object_pairs_hook=unique_json_object), OPERATION)
    identity = mapping.get('portable_definition_id') or mapping.definition_id
    try: identity = str(UUID(identity))
    except (ValueError, TypeError, AttributeError): _fail('The logbook mapping has no valid portable identity.')
    if (scope['resolver'] != USAGE or scope['areas'] != [AREA] or scope['mapping_definition_ids'] != [identity]):
        _fail('The active mutation contract does not authorize this mapping identity and area.')
    service = frappe.get_doc('HERO GraphQL Service', contract.graphql_service, for_update=lock)
    from hero_integration.services.config_mutation_contracts import _query
    from hero_integration.services.mapped_update_v2 import _schema
    _query(service.query_document, OPERATION, _schema(raw_schema))
    parsed = parse_mutation(service.query_document)
    if (service.status != 'Validated' or service.schema_snapshot != snapshot.name
            or service.schema_fingerprint != snapshot.schema_fingerprint or contract.schema_fingerprint != snapshot.schema_fingerprint
            or contract.contract_fingerprint != parsed.fingerprint or set(parsed.argument_variables) != {'logbook_entry'}):
        _fail('The approved mutation contract or service changed.')
    return SimpleNamespace(document=document, project=project, mapping=mapping, snapshot=snapshot, content=content,
        proof=proof, link=links[0], read=read, contract=contract, service=service, parsed=parsed,
        raw_schema=raw_schema, hero_id=str(hero_id), marker=marker)


def _read_project(ctx, client):
    from hero_integration.services.mapped_update_v2 import _read_record
    # Reuse the approved Project reader and its complete nested history pagination.
    reader = SimpleNamespace(**{**vars(ctx), 'content': {**ctx.content, 'identity': {'read_path': 'id'}}})
    return _read_record(reader, client)


def _require_readback_paths(read):
    from graphql import parse
    from graphql.language.ast import FieldNode, OperationDefinitionNode
    selected = set()
    def walk(selection, prefix=''):
        for field in selection.selections:
            if not isinstance(field, FieldNode) or field.alias or field.directives:
                _fail('The approved Project query must explicitly select the logbook readback fields.')
            path = prefix + field.name.value
            if field.selection_set: walk(field.selection_set, path + '.')
            else: selected.add(path)
    tree = parse(read.service.query_document)
    if len(tree.definitions) != 1 or not isinstance(tree.definitions[0], OperationDefinitionNode):
        _fail('The approved Project query must explicitly select the logbook readback fields.')
    walk(tree.definitions[0].selection_set)
    config = read.contract.adapter_options['logbook']
    prefix = read.contract.records_path + '.' + config['path'] + '.'
    if not {prefix + field for field in (config['identity_path'], 'custom_text', 'target', 'target_id')} <= selected:
        _fail('The approved Project query must explicitly select the logbook readback fields.')


def _prepare(ctx, client):
    from hero_integration.services.task_send import plain_text
    from hero_integration.services.graphql_contract import validate_mutation
    from hero_integration.services.read_contract import value_at
    remote = _read_project(ctx, client)
    options = ctx.read.contract.adapter_options['logbook']
    histories = value_at(remote, options['path'])
    if not isinstance(histories, list):
        _fail('The approved Project readback omitted its logbook.')
    identities = [entry.get(options['identity_path']) if isinstance(entry, dict) else None for entry in histories]
    if any(type(identity) is not int or identity <= 0 for identity in identities) or len(identities) != len(set(identities)):
        _fail('The approved logbook readback returned missing or repeated activity identities.')
    text = _activity_text(ctx.document, ctx.content)
    if not text or len(text) > 100_000:
        _fail('The activity must contain nonempty text within the permitted length.')
    variable = ctx.parsed.argument_variables['logbook_entry']
    variables = validate_mutation(ctx.raw_schema, ctx.parsed, {variable: {
        'target': ctx.content['parent']['target'], 'target_id': int(ctx.hero_id), 'custom_text': text}})
    return SimpleNamespace(text=text, variables=variables, fingerprint=_fingerprint(ctx, text=text),
        history_ids={str(value_at(entry, options['identity_path'])) for entry in histories})


def _fingerprint(ctx, *, text=None):
    return _hash([ctx.document.name, getattr(ctx.document, 'doctype', 'Comment'), ctx.document.modified, _activity_raw(ctx.document),
        _activity_parent(ctx.document, getattr(ctx.document, 'doctype', 'Comment')), ctx.project.modified,
        _activity_text(ctx.document, ctx.content) if text is None else text,
        ctx.mapping.name, ctx.mapping.mapping_revision, ctx.content, ctx.proof.fingerprint,
        dict(ctx.link), ctx.contract.name, ctx.contract.contract_revision, ctx.contract.contract_fingerprint,
        ctx.service.query_document, vars(ctx.read.contract), ctx.read.service.query_document])


def preview(comment, *, activity_doctype='Comment', client=None):
    from hero_integration.api import _mutation_execution_state
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.services.writeback_source import validate_source
    ctx = _load_context(comment, activity_doctype=activity_doctype); config = validate_source(ctx.proof)
    client = client or HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=0)
    prepared = _prepare(ctx, client)
    enabled = bool(_mutation_execution_state()['mutation_execution_enabled'])
    return {'comment': comment, 'activity_doctype': activity_doctype, 'project': ctx.project.name, 'mapping': ctx.mapping.name, 'fingerprint': prepared.fingerprint,
        'sending_enabled': enabled, 'can_send': enabled, 'changes': [{'label': 'Activity', 'hero': None, 'new': prepared.text}],
        'blocked_reason': '' if enabled else 'Global HERO mutation execution is currently OFF.'}


def candidates(project):
    from hero_integration.services.mapped_send import _roles
    from hero_integration.client.settings import get_configuration
    if not _roles(): _fail('Only System Managers and HERO Writeback users may send to HERO.')
    document = frappe.get_doc('Project', project)
    frappe.has_permission('Project', 'write', doc=document, throw=True)
    source = get_configuration(fresh=True).source_instance
    profiles = frappe.get_all('HERO GraphQL Mapping', filters={'source_instance': source, 'status': 'Active',
        'direction': 'FRAPPE_TO_MUTATION', 'operation_name': OPERATION}, fields=['mapping_json'], limit_page_length=2)
    if len(profiles) != 1: _fail('Exactly one active Project logbook mutation mapping is required.')
    from hero_integration.services.personal_mapping_content import unique_json_object
    content = json.loads(profiles[0].mapping_json, object_pairs_hook=unique_json_object)
    result = []
    kinds = [('Comment', {'reference_doctype': 'Project', 'reference_name': project,
        'comment_type': ['in', content.get('comment_types', ['Comment'])]}, ['name', 'content', 'creation', 'comment_type'])]
    if content.get('version_source'):
        kinds.append(('Version', {'ref_doctype': 'Project', 'docname': project}, ['name', 'data', 'creation']))
    for doctype, filters, fields in kinds:
        if doctype == 'Version' and not frappe.has_permission('Version', 'read'):
            continue
        rows = frappe.get_list(doctype, filters=filters, fields=fields, order_by='creation desc', limit_page_length=100)
        for row in rows:
            if frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_type': 'project_history',
                    'target_doctype': doctype, 'target_name': row.name}, pluck='name', limit_page_length=1): continue
            if frappe.get_all('HERO Mutation Audit', filters={'request_fingerprint': _comment_scope(source, row.name, doctype)},
                    pluck='name', limit_page_length=1): continue
            text = _activity_text(SimpleNamespace(**dict(row), doctype=doctype), content)
            if text: result.append({'name': row.name, 'activity_doctype': doctype, 'content': text, 'creation': row.creation})
    return {'comments': sorted(result, key=lambda row: str(row['creation']), reverse=True)}


def _reserve_project(ctx, audit):
    marker = project_marker(ctx.proof.source_instance, ctx.project.name, lock=True)
    if marker is None:
        marker = frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
            'source_instance': ctx.proof.source_instance, 'source_type': PENDING_TYPE, 'source_id': ctx.hero_id,
            'target_doctype': 'Project', 'target_name': ctx.project.name, 'sync_status': 'Pending Review',
            'mapping_version': USAGE, 'source_payload_hash': _hash(ctx.hero_id),
            'target_payload_hash': _hash(ctx.project.name),
            'conflict_detail': audit.correlation_id}).insert(ignore_permissions=True)
    else:
        if marker.sync_status != 'Synchronized':
            _fail('The Project has an unconfirmed outbound logbook activity; reconcile it before importing its logbook.')
        frappe.db.set_value('HERO Integration Link', marker.name,
            {'sync_status': 'Pending Review', 'conflict_detail': audit.correlation_id}, update_modified=False)
    frappe.db.commit()
    return marker.name


def _record_response(ctx, prepared, history_id):
    """Persist the native identity before any potentially long readback."""
    from hero_integration.services.project_logbook import _digest
    key = _digest([ctx.proof.source_instance, ctx.hero_id, str(history_id)])
    rows = frappe.db.sql('SELECT name FROM `tabHERO Integration Link` WHERE source_system=%s '
        'AND source_instance=%s AND source_type=%s AND source_id=%s FOR UPDATE',
        ('HERO', ctx.proof.source_instance, 'project_history', key), as_dict=True)
    if rows: _fail('The returned HERO logbook identity is already bound; reconcile the activity.')
    link = frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
        'source_instance': ctx.proof.source_instance, 'source_type': 'project_history', 'source_id': key,
        'target_doctype': ctx.document.doctype, 'target_name': ctx.document.name, 'sync_status': 'Pending Review',
        'mapping_version': OUTBOUND_VERSION, 'source_payload_hash': _hash(prepared.text),
        'target_payload_hash': _hash(_activity_raw(ctx.document))}).insert(ignore_permissions=True)
    frappe.db.commit()
    return link.name


def _verify_response(ctx, prepared, history_id, client):
    from hero_integration.services.read_contract import value_at
    from hero_integration.services.task_send import plain_text
    remote = _read_project(ctx, client)
    logbook = ctx.read.contract.adapter_options['logbook']
    histories = value_at(remote, logbook['path'])
    matches = [entry for entry in histories if str(value_at(entry, logbook['identity_path'])) == str(history_id)]
    if len(matches) != 1 or plain_text(matches[0].get('custom_text')) != prepared.text:
        _fail('HERO did not confirm the created logbook activity through the approved Project reader.')
    entry = matches[0]
    if (entry.get('target') != 'project_match' or str(entry.get('target_id')) != ctx.hero_id):
        _fail('The created logbook entry belongs to another HERO Project.')


def send(comment, *, activity_doctype='Comment', fingerprint='', confirmed=False, client=None, mutation_client=None):
    from hero_integration.api import _assert_mutation_execution_released, _reserve_mutation, _finalize_mutation
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.graphql_mutation import HeroMutationClient
    from hero_integration.services.writeback_source import validate_source
    _assert_mutation_execution_released()
    ctx = _load_context(comment, activity_doctype=activity_doctype); config = validate_source(ctx.proof)
    client = client or HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=0)
    prepared = _prepare(ctx, client)
    if confirmed not in (True, 1, '1', 'true') or not fingerprint or fingerprint != prepared.fingerprint:
        _fail('The activity preview changed; review and confirm it again.')
    audit = _reserve_mutation(operation_name=OPERATION, schema_fingerprint=ctx.snapshot.schema_fingerprint,
        mapping_revision=int(ctx.mapping.mapping_revision), request_fingerprint=_comment_scope(ctx.proof.source_instance, comment, activity_doctype))
    marker_name = None
    attempted = False
    try:
        fresh = _load_context(comment, activity_doctype=activity_doctype, lock=True, audit=audit)
        if _prepare(fresh, client).fingerprint != prepared.fingerprint:
            _fail('The activity preview changed; review and confirm it again.')
        marker_name = _reserve_project(fresh, audit)
        # Both reservation commits released database locks. Prove all authority
        # again, then hold the same Project lock used by the logbook importer.
        fresh = _load_context(comment, activity_doctype=activity_doctype, lock=True, audit=audit)
        checked = _prepare(fresh, client)
        if checked.fingerprint != prepared.fingerprint:
            _fail('The activity preview changed; review and confirm it again.')
        _assert_mutation_execution_released(); config = validate_source(fresh.proof)
        transport = mutation_client or HeroMutationClient(config.endpoint, lambda: config.api_token, config.timeout_seconds)
        attempted = True
        returned = transport.execute_once(fresh.service.query_document, checked.variables)
        record = returned.get(OPERATION) if isinstance(returned, dict) else None
        identity = record.get('id') if isinstance(record, dict) else None
        if type(identity) is not int or identity <= 0 or returned.get('errors') or str(identity) in checked.history_ids:
            _fail('HERO did not confirm a new logbook identity.')
        validate_source(fresh.proof)
        receipt_name = _record_response(fresh, checked, identity)
        # Known remote identity is durable; no further branch can create it again.
        after = _load_context(comment, activity_doctype=activity_doctype, lock=True, audit=audit, receipt=receipt_name)
        if _fingerprint(after) != prepared.fingerprint:
            _fail('The local activity changed during sending; reconcile the created logbook entry.')
        validate_source(after.proof)
        _verify_response(after, checked, identity, client)
        validate_source(after.proof)
        frappe.db.set_value('HERO Integration Link', receipt_name,
            {'sync_status': 'Synchronized', 'last_synced_on': frappe.utils.now_datetime()}, update_modified=False)
        frappe.db.set_value('HERO Integration Link', marker_name,
            {'sync_status': 'Synchronized', 'conflict_detail': ''}, update_modified=False)
    except Exception:
        frappe.db.rollback()
        if marker_name and not attempted:
            # No call to execute_once occurred. The activity remains reserved,
            # while unrelated Project logbook imports may safely continue.
            frappe.get_doc('Project', ctx.project.name, for_update=True)
            marker = project_marker(ctx.proof.source_instance, ctx.project.name, lock=True)
            if marker and marker.conflict_detail == audit.correlation_id:
                frappe.db.set_value('HERO Integration Link', marker_name,
                    {'sync_status': 'Synchronized', 'conflict_detail': ''}, update_modified=False)
        _finalize_mutation(audit, 'Failed', 'Project activity result is not confirmed. No automatic retry; reconcile HERO and its Project logbook reservation.')
        _fail('The logbook activity could not be confirmed; no automatic retry was performed. Reconcile the Project logbook reservation.')
    _finalize_mutation(audit, 'Succeeded', 'Project activity created once; native Comment preserved and approved logbook readback verified.')
    return {'status': 'SENT', 'comment': comment, 'activity_doctype': activity_doctype, 'project': ctx.project.name, 'hero_id': identity}


def capture_project_activity(document, method=None):
    """Keep native change activities when the opted-in Project lacks tracking.

    The ordinary Frappe Version writer remains the owner if track_changes is
    enabled. Managed HERO imports and maintenance never generate outbound echoes.
    """
    from hero_integration.services.change_detection import managed_target_update_active
    if (getattr(document.meta, 'track_changes', False) or document.flags.ignore_version
            or frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch
            or managed_target_update_active('Project', document.name)):
        return
    before = document.get_doc_before_save()
    if before is None: return
    from hero_integration.api import _configured_source_instance
    source = _configured_source_instance()
    profiles = frappe.get_all('HERO GraphQL Mapping', filters={'source_instance': source, 'status': 'Active',
        'direction': 'FRAPPE_TO_MUTATION', 'operation_name': OPERATION}, fields=['mapping_json'], limit_page_length=2)
    if len(profiles) != 1: return
    from hero_integration.services.personal_mapping_content import unique_json_object
    try: content = json.loads(profiles[0].mapping_json, object_pairs_hook=unique_json_object)
    except (ValueError, TypeError): return
    if content.get('usage') != USAGE or content.get('version_source') != VERSION_SOURCE: return
    bindings = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_instance': source,
        'source_type': 'project', 'target_doctype': 'Project', 'target_name': document.name}, pluck='name', limit_page_length=2)
    if len(bindings) != 1: return
    version = frappe.new_doc('Version')
    if version.update_version_info(before, document) and _version_text(version):
        version.insert(ignore_permissions=True)


def mark_imported_project_version(document, method=None):
    """Retain native audit history while excluding managed HERO import echoes."""
    from hero_integration.services.change_detection import managed_target_update_active
    if document.ref_doctype != 'Project' or not managed_target_update_active('Project', document.docname):
        return
    from hero_integration.services.personal_mapping_content import unique_json_object
    value = json.loads(document.data, object_pairs_hook=unique_json_object)
    if isinstance(value, dict):
        value['_hero_import'] = True
        document.data = _json(value)
