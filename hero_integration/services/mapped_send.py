"""Grouped Mutation mappings for native Send actions; field writeback stays separate."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
import re
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

import frappe

from hero_integration.services.frappe_writeback import normalise_draft_mapping
from hero_integration.services.localized_error import LocalizedError

USAGE = 'SEND_TO_HERO'
_NAME = re.compile(r'^[_A-Za-z][_0-9A-Za-z]*$')
_PATH = re.compile(r'^[_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*$')
_RESOLVERS = {'FIELD', 'PLAIN_TEXT', 'DATE_TIME', 'USER_LINK', 'PROJECT_LINK', 'PHASE', 'VALUE_MAP'}
_MISSING = object()


class MappedSendError(LocalizedError):
    pass


class _SourceRelationMissing(MappedSendError):
    pass


def is_send_mapping(content):
    return isinstance(content, dict) and content.get('usage') == USAGE


def assignments(content):
    yield 'primary', content
    for item in content.get('additional_mappings', []):
        yield item['id'], item


def send_sources(content):
    sources = []
    for identity, item in assignments(content):
        sources.append((item['source']['doctype'], item['source']['field']))
        condition = content['send_options']['assignments'][identity].get('when')
        if condition:
            sources.append((item['source']['doctype'], condition['field']))
    return tuple(dict.fromkeys(sources))


def _path(value):
    return isinstance(value, str) and bool(_PATH.fullmatch(value))


def normalise_send_mapping(content, schema):
    """Validate one configured mutation group without granting execution authority."""
    from hero_integration.services.personal_mapping_content import bounded
    try:
        bounded(content)
    except (ValueError, TypeError, RecursionError) as error:
        raise MappedSendError('Send configuration must be bounded JSON.') from error
    if (not is_send_mapping(content) or set(content) - {'version', 'usage', 'source', 'target',
            'transformation', 'additional_mappings', 'send_options'}
            or ('version' in content and (type(content['version']) is not int or content['version'] != 1))):
        raise MappedSendError('Invalid Send to HERO mapping configuration.')
    value = deepcopy(content)
    additions = value.get('additional_mappings', [])
    if (not isinstance(additions, list) or len(additions) > 100
            or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in additions)):
        raise MappedSendError('Send mappings must be a bounded list.')
    options = value.get('send_options')
    required = {'read_service', 'mode', 'identity_argument', 'identity_field', 'read_identity_path', 'assignments'}
    if (not isinstance(options, dict) or not required <= set(options)
            or set(options) - (required | {'related_identities'})
            or not isinstance(options['mode'], str) or options['mode'] not in {'UPDATE', 'CREATE'} or options['identity_field'] != 'id'
            or not isinstance(options['read_service'], str) or not options['read_service']
            or len(options['read_service']) > 140 or not _path(options['read_identity_path'])
            or not isinstance(options['identity_argument'], str) or not _NAME.fullmatch(options['identity_argument'])):
        raise MappedSendError('Send requires an explicit update identity and approved read service.')
    settings = options['assignments']
    if not isinstance(settings, dict) or set(settings) != {'primary', *(item.get('id') for item in additions if isinstance(item, dict))}:
        raise MappedSendError('Each Send assignment requires explicit resolver and readback configuration.')
    seen_ids, targets, sources = set(), [], set()
    operation = None
    root_doctype = None
    for identity, item in assignments(value):
        if identity != 'primary':
            try:
                valid_id = str(UUID(identity)) == identity
            except (ValueError, TypeError, AttributeError):
                valid_id = False
            if not valid_id or identity in seen_ids or set(item) != {'id', 'source', 'target', 'transformation'}:
                raise MappedSendError('Send assignment identities must be unique canonical UUIDs.')
            seen_ids.add(identity)
        source, target = item.get('source'), item.get('target')
        if (not isinstance(source, dict) or set(source) != {'doctype', 'field'}
                or not all(isinstance(v, str) and v for v in source.values())
                or not isinstance(target, dict) or set(target) - {'operation', 'argument', 'input_type', 'field', 'field_path', 'graphql_type'}
                or not _path(target.get('field'))):
            raise MappedSendError('Send requires exact Frappe sources and scalar mutation input paths.')
        from hero_integration.services.project_naming import is_local_project_name
        if is_local_project_name(source, target['field']):
            raise MappedSendError('Project naming is local to ERPNext and cannot be sent to HERO.')
        try:
            normalized = normalise_draft_mapping({k: item[k] for k in ('source', 'target', 'transformation') if k in item}, schema, temporal=True)
        except (ValueError, TypeError, KeyError) as error:
            raise MappedSendError('Send assignment is incompatible with the saved mutation schema.') from error
        actual = normalized['target']
        if any(actual.get(key) != val for key, val in target.items()):
            raise MappedSendError('Send input annotations do not match the saved schema.')
        item['target'] = actual
        item['transformation'] = normalized['transformation']
        root_doctype = root_doctype or source['doctype']
        operation = operation or actual['operation']
        if root_doctype not in {'Customer', 'Project', 'Task'} or actual['operation'] != operation:
            raise MappedSendError('A Send mapping must belong to one native document and mutation operation.')
        if source['doctype'] not in ({root_doctype, 'Contact', 'Address'} if root_doctype == 'Customer' else
                                     {root_doctype, 'ToDo'} if root_doctype == 'Task' else {root_doctype}):
            raise MappedSendError('The Send source is outside its verified native document relations.')
        source_key = (source['doctype'], source['field'])
        if source_key in sources:
            raise MappedSendError('A Send source field must have one unambiguous mutation target.')
        sources.add(source_key)
        path = (actual['argument'], *actual['field'].split('.'))
        identity_path = (options['identity_argument'], options['identity_field'])
        if path[-1] == 'id' or path[:len(identity_path)] == identity_path or identity_path[:len(path)] == path:
            raise MappedSendError('The external record identity is server-owned and cannot be mapped.')
        if any(path == other[:len(path)] or other == path[:len(other)] for other in targets):
            raise MappedSendError('Send mutation input paths overlap.')
        targets.append(path)
        setting = settings[identity]
        if (not isinstance(setting, dict) or set(setting) - {'resolver', 'read_path', 'missing', 'null', 'lookup', 'map', 'date_mode', 'query_path', 'when'}
                or not {'resolver', 'read_path', 'missing', 'null'} <= set(setting)
                or not isinstance(setting['resolver'], str) or setting['resolver'] not in _RESOLVERS or not _path(setting['read_path'])
                or ('query_path' in setting and not _path(setting['query_path']))
                or not isinstance(setting['missing'], str) or setting['missing'] not in {'OMIT', 'ERROR'}
                or not isinstance(setting['null'], str) or setting['null'] not in {'OMIT', 'EMPTY_STRING', 'EXPLICIT_NULL'}):
            raise MappedSendError('Send resolver or missing-value configuration is invalid.')
        if ('lookup' in setting and setting['resolver'] not in {'USER_LINK', 'PHASE'}
                or 'date_mode' in setting and setting['resolver'] != 'DATE_TIME'):
            raise MappedSendError('Send resolver options are not applicable to this assignment.')
        condition = setting.get('when')
        if condition is not None and (not isinstance(condition, dict)
                or set(condition) != {'field', 'equals', 'otherwise'}
                or not isinstance(condition['field'], str) or not _NAME.fullmatch(condition['field'])
                or type(condition['equals']) not in (type(None), str, int, float, bool)
                or not isinstance(condition['otherwise'], str)
                or condition['otherwise'] not in {'OMIT', 'EMPTY_STRING', 'EXPLICIT_NULL', 'ERROR'}):
            raise MappedSendError('A Send condition must be an explicit bounded local-field equality.')
        scalar = actual['graphql_type'].rstrip('!')
        reference_targets = {
            'PROJECT_LINK': ('Task', 'update_task', 'task', 'TaskInput', 'target_project_match_id'),
            'USER_LINK': ('Task', 'update_task', 'task', 'TaskInput', 'target_user_id'),
            'PHASE': ('Project', 'update_project_match', 'project_match', 'ProjectMatchInput', 'current_project_match_status.step_id'),
        }
        actual_reference = (root_doctype, actual['operation'], actual['argument'], actual['input_type'], actual['field'])
        required_resolver = next((kind for kind, reference in reference_targets.items() if actual_reference == reference), None)
        if required_resolver and setting['resolver'] != required_resolver:
            raise MappedSendError('This HERO reference input requires its reviewed identity resolver.')
        if actual['field'].split('.')[-1].endswith('_id') and not required_resolver:
            raise MappedSendError('This HERO relationship input has no reviewed identity resolver.')
        expected_reference = reference_targets.get(setting['resolver'])
        if expected_reference and (root_doctype, actual['operation'], actual['argument'], actual['input_type'], actual['field']) != expected_reference:
            raise MappedSendError('The identity resolver does not match its reviewed HERO reference namespace.')
        if setting['resolver'] in {'USER_LINK', 'PROJECT_LINK', 'PHASE'} and scalar not in {'Int', 'ID'}:
            raise MappedSendError('A linked HERO identity requires an Int or ID mutation input.')
        if setting['resolver'] == 'PLAIN_TEXT' and scalar != 'String':
            raise MappedSendError('Plain text resolution requires a String mutation input.')
        if setting['resolver'] == 'DATE_TIME' and scalar not in {'String', 'Date', 'DateTime'}:
            raise MappedSendError('Date resolution requires a String, Date or DateTime mutation input.')
        if scalar in {'Date', 'DateTime'} and setting['resolver'] != 'DATE_TIME':
            raise MappedSendError('Temporal mutation inputs require an explicit DATE_TIME resolver.')
        if scalar == 'Date' and setting.get('date_mode') != 'DATE':
            raise MappedSendError('A Date mutation input requires DATE semantics.')
        if setting['resolver'] == 'DATE_TIME' and setting.get('date_mode') not in {'DATE', 'DATETIME'}:
            raise MappedSendError('Date resolution requires explicit DATE or DATETIME semantics.')
        if setting['resolver'] == 'VALUE_MAP':
            table = setting.get('map')
            if (not isinstance(table, dict) or not table or len(table) > 100
                    or any(not isinstance(k, str) or not k or type(v) not in (str, int, float, bool) for k, v in table.items())
                    or len({_json(v) for v in table.values()}) != len(table)):
                raise MappedSendError('Value mapping requires bounded, unambiguous scalar pairs.')
        elif 'map' in setting:
            raise MappedSendError('Only VALUE_MAP accepts a conversion table.')
        if 'lookup' in setting:
            lookup = setting['lookup']
            if (not isinstance(lookup, dict) or (set(lookup) - {'path', 'service', 'key', 'value', 'active_path', 'active_value', 'missing'}
                    or set(lookup) & {'path', 'service'} not in ({'path'}, {'service'})
                    or not {'key', 'value'} <= set(lookup))
                    or any(not _path(lookup[k]) for k in ('key', 'value'))
                    or ('path' in lookup and not _path(lookup['path']))
                    or ('service' in lookup and (not isinstance(lookup['service'], str) or not lookup['service'] or len(lookup['service']) > 140))
                    or ('active_path' in lookup and (not _path(lookup['active_path']) or type(lookup.get('active_value')) is not bool))
                    or ('active_value' in lookup and 'active_path' not in lookup)
                    or ('missing' in lookup and (not isinstance(lookup['missing'], str) or lookup['missing'] not in {'OMIT', 'ERROR'}))):
                raise MappedSendError('Send lookup must name exact approved query paths.')
        if setting['resolver'] in {'USER_LINK', 'PHASE'} and 'lookup' not in setting:
            raise MappedSendError('User and phase resolution require an explicitly configured lookup query.')
        if setting['resolver'] == 'PHASE':
            lookup = setting.get('lookup', {})
            if (lookup.get('path') != 'type.project_status_steps' or 'service' in lookup
                    or lookup.get('key') != 'id' or lookup.get('active_path') != 'is_active'
                    or lookup.get('active_value') is not True
                    or setting['read_path'] != 'current_project_match_status.step_id'):
                raise MappedSendError('Phase resolution requires the active steps of this bound Project type.')
    related = options.get('related_identities', {})
    if not isinstance(related, dict) or len(related) > 10:
        raise MappedSendError('Related Send identities must be a bounded explicit map.')
    for path, config in related.items():
        if (not _path(path) or len(path.split('.')) < 3 or not path.endswith('.id')
                or not isinstance(config, dict) or set(config) != {'doctype', 'source_type', 'read_path'}
                or root_doctype != 'Customer' or config['doctype'] != 'Address'
                or config['source_type'] != 'customer_address' or not _path(config['read_path'])
                or not any(tuple(path.split('.')[:-1]) == target[:len(path.split('.'))-1] for target in targets)):
            raise MappedSendError('Related Send identity is outside the verified native relation.')
        argument, field = path.split('.', 1)
        resolved = normalise_draft_mapping({'source': value['source'], 'target': {'operation': operation,
            'argument': argument, 'field': field}, 'transformation': 'Identity'}, schema)
        if resolved['target']['graphql_type'].rstrip('!') not in {'Int', 'ID'}:
            raise MappedSendError('A related external identity must be an Int or ID input.')
    if sum(setting['resolver'] == 'PHASE' for setting in settings.values()) > 1:
        raise MappedSendError('A native Send profile has only one explicit phase chooser.')
    # An external ID is meaningful only within the native bound object type.
    from hero_integration.services.graphql_graph import normalize_schema
    from hero_integration.services.frappe_writeback import _named_type
    normalized_schema = normalize_schema(schema)
    types = {item['name']: item for item in normalized_schema['types']}
    mutation_root = types[normalized_schema['mutationType']['name']]
    operation_field = next(item for item in mutation_root['fields'] if item['name'] == operation)
    from hero_integration.services.frappe_writeback import _has_list
    query_root = types[normalized_schema['queryType']['name']]
    native_root = {'Customer': 'contacts', 'Project': 'project_matches', 'Task': 'tasks'}[root_doctype]
    candidates = [item for item in query_root.get('fields', []) if item['name'] == native_root]
    if (len(candidates) != 1 or _has_list(operation_field['type'])
            or _named_type(operation_field['type']) != _named_type(candidates[0]['type'])):
        raise MappedSendError('The mutation returns a different object type than the native bound Query root.')
    for _identity, item in assignments(value):
        argument = next(field for field in operation_field.get('args', []) if field['name'] == item['target']['argument'])
        current_type = types[_named_type(argument['type'])]
        prefix = [item['target']['argument']]
        for part in item['target']['field'].split('.')[:-1]:
            member = next(field for field in current_type.get('inputFields', []) if field['name'] == part)
            current_type = types[_named_type(member['type'])]
            prefix.append(part)
            if current_type.get('name') == 'AddressInput' and '.'.join((*prefix, 'id')) not in related:
                raise MappedSendError('An Address mutation requires its explicit server-owned related identity.')
    # Prove the injected identity path using the same saved schema resolver.
    target = value['target']
    normalise_draft_mapping({'source': value['source'], 'target': {'operation': operation,
        'argument': options['identity_argument'], 'field': options['identity_field']}, 'transformation': 'Identity'}, schema)
    if options['mode'] == 'CREATE':
        inputs = {item['target']['field']: settings[identity]['resolver'] for identity, item in assignments(value)}
        if (root_doctype != 'Task' or operation != 'update_task' or options['identity_argument'] != 'task'
                or 'title' not in inputs or inputs.get('target_project_match_id') != 'PROJECT_LINK'):
            raise MappedSendError('Task creation requires an explicit title and a verified mapped Project.')
        identity = next(member for member in types['TaskInput']['inputFields'] if member['name'] == 'id')
        if identity['type']['kind'] == 'NON_NULL':
            raise MappedSendError('The approved Task mutation does not support creation without an identity.')
    if len(_json(value).encode()) > 65536:
        raise MappedSendError('Send mapping exceeds its size limit.')
    value['version'] = 1
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)


def _at(value, path, missing=_MISSING):
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return missing
        value = value[part]
    return value


def _hash(value):
    from hero_integration.services.change_detection import field_hashes
    return field_hashes({'value': value})['value']


def _roles():
    return bool({'System Manager', 'HERO Writeback'} & set(frappe.get_roles()))


_CREATE_PRETRANSPORT_FAILURE = 'Task creation stopped before transport; no external attempt was made.'


def _create_scope(ctx):
    return sha256(_json(['hero-task-create-v1', frappe.local.site, ctx.proof.source_instance,
                         ctx.document.name]).encode()).hexdigest()


def _assert_create_available(ctx, *, exclude=None):
    """A failed/unknown external create remains latched until manual reconciliation."""
    rows = frappe.get_all('HERO Mutation Audit', filters={'request_fingerprint': _create_scope(ctx)},
                          fields=['correlation_id', 'status', 'detail'])
    for row in rows:
        if row.correlation_id != exclude and not (row.status == 'Failed' and row.detail == _CREATE_PRETRANSPORT_FAILURE):
            raise MappedSendError('A previous Task creation needs manual reconciliation before another attempt.')


def _native_links(doctype, name):
    return frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
        'target_doctype': doctype, 'target_name': name}, fields=['name', 'source_system', 'source_type', 'source_id',
        'source_instance', 'target_doctype', 'target_name', 'sync_status', 'source_field_hashes', 'target_field_hashes',
        'observed_source_hash', 'source_payload_hash', 'mapping_version'])


def _assert_task_unbound(name):
    if _native_links('Task', name) or frappe.get_all('HERO Object Binding',
            filters={'target_doctype': 'Task', 'target_name': name}, pluck='name', limit_page_length=1):
        raise MappedSendError('The Task is already bound; creation cannot be repeated.')



def _profile_name(rows, doctype, mode):
    from hero_integration.services.personal_mapping_content import unique_json_object
    candidates = []
    for row in rows:
        try:
            content = json.loads(row.mapping_json, object_pairs_hook=unique_json_object)
        except (TypeError, ValueError) as error:
            raise MappedSendError('An active mutation mapping cannot be inspected safely.') from error
        if ((is_send_mapping(content) and content.get('send_options', {}).get('mode') == mode
                or content.get('usage') == 'MAPPED_UPDATE_V2' and mode == 'UPDATE')
                and content.get('source', {}).get('doctype') == doctype):
            candidates.append(row.name)
    if len(candidates) != 1:
        raise MappedSendError('Exactly one active Send to HERO mapping is required for this document and source.')
    return candidates[0]

def _load_context(doctype, name):
    from hero_integration.client.settings import get_configuration
    from hero_integration.services.personal_mapping_content import unique_json_object
    from hero_integration.services.writeback_source import capture_source
    from hero_integration.services.graphql_contract import parse_mutation
    if not _roles():
        raise MappedSendError('Only System Managers and HERO Writeback users may send to HERO.')
    document = frappe.get_doc(doctype, name)
    frappe.has_permission(doctype, 'write', doc=document, throw=True)
    configuration = get_configuration(fresh=True)
    source = configuration.source_instance
    links = _native_links(doctype, name)
    mode = 'CREATE' if doctype == 'Task' and not links else 'UPDATE'
    if mode == 'CREATE':
        _assert_task_unbound(name)
    rows = frappe.get_all('HERO GraphQL Mapping', filters={'status': 'Active', 'direction': 'FRAPPE_TO_MUTATION',
        'operation_kind': 'MUTATION', 'source_instance': source}, fields=['name', 'mapping_json'])
    profile_name = _profile_name(rows, doctype, mode)
    mapping = frappe.get_doc('HERO GraphQL Mapping', profile_name)
    snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', mapping.schema_snapshot)
    if (snapshot.analysis_status != 'Validated' or getattr(snapshot, 'execution_contract_status', '') == 'Blocked'
            or mapping.schema_fingerprint != snapshot.schema_fingerprint or snapshot.source_instance != source):
        raise MappedSendError('The Send mapping no longer matches its executable schema and source.')
    raw_content = json.loads(mapping.mapping_json, object_pairs_hook=unique_json_object)
    if raw_content.get('usage') == 'MAPPED_UPDATE_V2':
        from hero_integration.services.mapped_update_v2 import _load_context as load_update
        return load_update(mapping.name, doctype, name)
    content = normalise_send_mapping(raw_content, json.loads(snapshot.snapshot_json))
    from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata, validate_send_mapping_sources
    validate_send_mapping_sources(content, FrappePickerMetadata())
    if (mapping.status != 'Active' or mapping.source_instance != source
            or content['target']['operation'] != mapping.operation_name):
        raise MappedSendError('The active Send mapping operation or source changed.')
    expected_type = {'Customer': 'customer', 'Project': 'project', 'Task': 'Task'}[doctype]
    link, hero_id = None, None
    if mode == 'UPDATE':
        if (len(links) != 1 or links[0].sync_status != 'Synchronized'
                or links[0].source_type != expected_type or links[0].source_instance != source):
            raise MappedSendError('Send requires one synchronized native HERO Integration Link for this source.')
        link = links[0]
        if getattr(link, 'observed_source_hash', None) and link.observed_source_hash != link.source_payload_hash:
            raise MappedSendError('The linked HERO source changed; reconcile before sending.')
        try:
            hero_id = int(link.source_id)
            if hero_id <= 0 or str(hero_id) != str(link.source_id):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise MappedSendError('The bound external identity is invalid.') from error
    contracts = frappe.get_all('HERO Mutation Contract', filters={'schema_snapshot': snapshot.name,
        'operation_name': mapping.operation_name, 'status': 'Active'}, pluck='name')
    if len(contracts) != 1:
        raise MappedSendError('Exactly one active schema-bound mutation contract is required for Send.')
    contract = frappe.get_doc('HERO Mutation Contract', contracts[0])
    service = frappe.get_doc('HERO GraphQL Service', contract.graphql_service)
    parsed = parse_mutation(service.query_document)
    if (contract.schema_fingerprint != snapshot.schema_fingerprint or service.schema_snapshot != snapshot.name
            or service.schema_fingerprint != snapshot.schema_fingerprint
            or parsed.fingerprint != contract.contract_fingerprint or parsed.root_field != mapping.operation_name):
        raise MappedSendError('The mutation service or contract changed after approval.')
    proof = capture_source(snapshot, source, configuration)
    read = _load_read(content['send_options']['read_service'], snapshot)
    if read.contract.target_operation != {'Customer': 'HeroCustomers', 'Project': 'HeroProjectMatches', 'Task': 'HeroTasks'}[doctype]:
        raise MappedSendError('The main Send readback does not belong to the native bound object type.')
    ctx = SimpleNamespace(document=document, mapping=mapping, content=content, snapshot=snapshot, link=link,
        contract=contract, service=service, parsed=parsed, proof=proof, read=read, hero_id=hero_id, related={}, related_selection={}, baseline_links={}, condition_values=[])
    if mode == 'CREATE':
        _assert_create_available(ctx)
    return ctx


def _load_read(name, snapshot):
    from hero_integration.services.read_contract import from_documents
    service = frappe.get_doc('HERO GraphQL Service', name)
    mapping = frappe.get_doc('HERO GraphQL Mapping', service.read_mapping)
    if service.schema_snapshot != snapshot.name:
        raise MappedSendError('Send readback and mutation must use the same approved source snapshot.')
    contract = from_documents(service, mapping, snapshot)
    return SimpleNamespace(service=service, mapping=mapping, contract=contract)


def _read_rows(ctx, plan, client):
    from hero_integration.sources.hero import GraphQLServiceSource
    from hero_integration.services.read_contract import from_documents
    from hero_integration.services.writeback_source import validate_source
    def revalidate():
        service = frappe.get_doc('HERO GraphQL Service', plan.service.name)
        mapping = frappe.get_doc('HERO GraphQL Mapping', plan.mapping.name)
        current = from_documents(service, mapping, ctx.snapshot)
        if service.query_document != plan.service.query_document or current != plan.contract:
            raise MappedSendError('The approved Send readback query changed.')
        return current
    source = GraphQLServiceSource(client, plan.service, plan.contract, ctx.proof.source_instance, revalidate=revalidate)
    result, seen = [], set()
    offset = 0
    for _ in range(100):
        validate_source(ctx.proof)
        page = source._page(offset, plan.contract.target_operation)
        for row in page.records:
            identity = _at(row, plan.contract.identity_path)
            if identity is _MISSING or _json(identity) in seen:
                raise MappedSendError('Send readback returned missing or repeated record identities.')
            seen.add(_json(identity))
            result.append(row)
        if page.next_offset is None:
            return result
        if not page.records or page.next_offset <= offset:
            raise MappedSendError('Send readback pagination did not advance.')
        offset = page.next_offset
    raise MappedSendError('The approved Send readback exceeded its bounded page limit.')


def _read_record(ctx, client):
    if ctx.document.doctype == 'Project':
        from hero_integration.services.mapped_update_v2 import _read_record as read_project
        reader = SimpleNamespace(**{**vars(ctx), 'raw_schema': json.loads(ctx.snapshot.snapshot_json),
            'content': {**ctx.content, 'area': 'HeroProjectMatches', 'identity': {'read_path': ctx.content['send_options']['read_identity_path']}}})
        return read_project(reader, client)
    rows = _read_rows(ctx, ctx.read, client)
    path = ctx.content['send_options']['read_identity_path']
    if path != ctx.read.contract.identity_path:
        raise MappedSendError('The Send identity differs from its approved readback contract.')
    matched = [row for row in rows if str(_at(row, path, None)) == str(ctx.hero_id)]
    if len(matched) != 1:
        raise MappedSendError('The approved query did not prove the unique bound HERO record.')
    return matched[0]


def _source_document(ctx, doctype):
    if doctype == ctx.document.doctype:
        return ctx.document
    if doctype in ctx.related:
        return ctx.related[doctype]
    name = None
    if ctx.document.doctype == 'Customer' and doctype in {'Contact', 'Address'}:
        field = 'customer_primary_contact' if doctype == 'Contact' else 'customer_primary_address'
        name = ctx.document.get(field)
        if name:
            document = frappe.get_doc(doctype, name)
            relations = document.get('links') or []
            if not any(row.link_doctype == 'Customer' and row.link_name == ctx.document.name for row in relations):
                raise MappedSendError('The mapped customer relation is not linked to this Customer.')
    elif ctx.document.doctype == 'Task' and doctype == 'ToDo':
        names = frappe.get_all('ToDo', filters={'reference_type': 'Task', 'reference_name': ctx.document.name,
            'status': 'Open'}, pluck='name', limit_page_length=2)
        if len(names) > 1:
            raise MappedSendError('The mapped Task has multiple open ToDo assignments.')
        if len(names) == 1:
            name = names[0]
            document = frappe.get_doc('ToDo', name)
    if not name:
        raise _SourceRelationMissing('The mapped related Frappe source is missing.')
    frappe.has_permission(doctype, 'read', doc=document, throw=True)
    ctx.related[doctype] = document
    return document



def _validate_user_lookup_namespace(ctx, config, plan):
    """A numeric foreign key must come from the same schema User object as its email."""
    from hero_integration.services.graphql_graph import normalize_schema
    from hero_integration.services.frappe_writeback import _named_type, _has_list
    schema = normalize_schema(json.loads(ctx.snapshot.snapshot_json))
    types = {item['name']: item for item in schema['types']}
    current = types[schema['queryType']['name']]
    root_path = plan.contract.records_path
    if 'path' in config:
        root_path += '.' + config['path']
    try:
        for part in root_path.split('.'):
            member = next(field for field in current.get('fields', []) if field['name'] == part)
            current = types[_named_type(member['type'])]
        key_parts, value_parts = config['key'].split('.'), config['value'].split('.')
        if key_parts[:-1] != value_parts[:-1] or key_parts[-1] != 'email' or value_parts[-1] != 'id':
            raise ValueError
        for part in key_parts[:-1]:
            member = next(field for field in current.get('fields', []) if field['name'] == part)
            if _has_list(member['type']):
                raise ValueError
            current = types[_named_type(member['type'])]
        if current['name'] != 'User':
            raise ValueError
        for name, allowed in (('email', {'String'}), ('id', {'Int', 'ID'})):
            member = next(field for field in current.get('fields', []) if field['name'] == name)
            if _has_list(member['type']) or _named_type(member['type']) not in allowed:
                raise ValueError
    except (KeyError, StopIteration, ValueError) as error:
        raise MappedSendError('The approved user lookup must pair email and identity from the same schema User.') from error


def _lookup(ctx, settings, remote, client):
    config = settings.get('lookup')
    if not config:
        raise MappedSendError('A mapped lookup is required for this resolver.')
    plan = _load_read(config['service'], ctx.snapshot) if 'service' in config else ctx.read
    if settings['resolver'] == 'USER_LINK':
        _validate_user_lookup_namespace(ctx, config, plan)
    if 'service' in config:
        rows = _read_rows(ctx, plan, client)
    else:
        rows = _at(remote, config['path'])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise MappedSendError('The approved lookup did not return the configured records.')
    missing = config.get('missing', 'ERROR')
    if missing == 'OMIT':
        rows = [row for row in rows if all(_at(row, config[path]) is not _MISSING and _at(row, config[path]) is not None
                for path in ('key', 'value'))]
    if 'active_path' in config:
        if any(type(_at(row, config['active_path'])) is not bool for row in rows):
            raise MappedSendError('The approved phase lookup omitted its active-state proof.')
        rows = [row for row in rows if _at(row, config['active_path']) is config['active_value']]
    pairs = [(_at(row, config['key']), _at(row, config['value'])) for row in rows]
    if any(k is _MISSING or v is _MISSING or k is None or v is None for k, v in pairs) or len({_json(k) for k, _ in pairs}) != len(pairs):
        raise MappedSendError('The mapped lookup contains missing or ambiguous keys.')
    if settings['resolver'] == 'USER_LINK' and len({_json(v) for _, v in pairs}) != len(pairs):
        raise MappedSendError('The approved user lookup has ambiguous HERO identities.')
    return pairs


def _source_field(document, field):
    meta = frappe.get_meta(document.doctype).get_field(field)
    if (meta is None or getattr(meta, 'is_virtual', 0)
            or getattr(meta, 'fieldtype', '') == 'Password' or int(getattr(meta, 'permlevel', 0) or 0)):
        raise MappedSendError('The mapped Frappe source field is unavailable or protected.')
    return meta


def _resolve(ctx, item, setting, remote, client, phase_id):
    try:
        document = _source_document(ctx, item['source']['doctype'])
    except _SourceRelationMissing:
        if setting['missing'] == 'OMIT':
            return _MISSING
        raise
    field = item['source']['field']
    _source_field(document, field)
    condition = setting.get('when')
    if condition:
        _source_field(document, condition['field'])
        actual = document.get(condition['field'], _MISSING)
        if actual is _MISSING:
            raise MappedSendError('The configured local condition field was not loaded.')
        ctx.condition_values.append({'doctype': document.doctype, 'name': document.name,
            'modified': str(document.modified), 'field': condition['field'], 'value': actual})
        if _json(actual) != _json(condition['equals']):
            if condition['otherwise'] == 'ERROR':
                raise MappedSendError('The configured local Send condition is not satisfied.')
            return {'OMIT': _MISSING, 'EMPTY_STRING': '', 'EXPLICIT_NULL': None}[condition['otherwise']]
    value = document.get(field, _MISSING)
    if value is _MISSING:
        if setting['missing'] == 'OMIT':
            return _MISSING
        raise MappedSendError('A required mapped Frappe source is absent.')
    if value is None and setting['resolver'] == 'PHASE' and phase_id not in (None, '', 0, '0'):
        value = phase_id
    if value is None:
        if setting['null'] == 'OMIT':
            return _MISSING
        return '' if setting['null'] == 'EMPTY_STRING' else None
    kind = setting['resolver']
    if kind == 'PLAIN_TEXT':
        from hero_integration.services.task_send import plain_text
        value = plain_text(value)
    elif kind == 'DATE_TIME':
        from frappe.utils import get_system_timezone
        zone = ZoneInfo(get_system_timezone())
        if setting['date_mode'] == 'DATE':
            day = value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])
            value = (day.isoformat() if item['target']['graphql_type'].rstrip('!') == 'Date' else
                     datetime.combine(day, time(12)).replace(tzinfo=zone).isoformat())
        else:
            from hero_integration.services.native_projections import _instant
            value = _instant(str(value), zone).replace(microsecond=0).isoformat()
    elif kind == 'PROJECT_LINK':
        rows = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_type': 'project',
            'source_instance': ctx.proof.source_instance, 'target_doctype': 'Project', 'target_name': value,
            'sync_status': 'Synchronized'}, pluck='source_id', limit_page_length=2)
        if len(rows) != 1 or not str(rows[0]).isdigit() or int(rows[0]) <= 0:
            raise MappedSendError('The mapped Project has no unique synchronized HERO identity.')
        frappe.has_permission('Project', 'read', doc=frappe.get_doc('Project', value), throw=True)
        value = int(rows[0])
    elif kind == 'USER_LINK':
        frappe.has_permission('User', 'read', doc=frappe.get_doc('User', value), throw=True)
        user = frappe.get_doc('User', value)
        email = user.get('email')
        matches = [v for k, v in _lookup(ctx, setting, remote, client) if isinstance(k, str)
                   and isinstance(email, str) and k.strip().lower() == email.strip().lower()]
        if len(matches) != 1:
            raise MappedSendError('The mapped User has no unique approved HERO lookup identity.')
        value = matches[0]
        if type(value) not in (int, str) or not str(value).isdigit() or int(value) <= 0:
            raise MappedSendError('The approved user lookup did not return a positive HERO identity.')
        value = int(value)
    elif kind == 'PHASE':
        if phase_id in (None, '', 0, '0'):
            current = _at(remote, setting['read_path'])
            return current if current is not _MISSING else _MISSING
        choices = _lookup(ctx, setting, remote, client)
        try:
            value = int(phase_id)
        except (TypeError, ValueError) as error:
            raise MappedSendError('The selected phase identity is invalid.') from error
        if not any(_json(key) == _json(value) for key, _ in choices):
            raise MappedSendError('The selected phase is absent from the approved project phase lookup.')
    elif kind == 'VALUE_MAP':
        key = str(value)
        if key not in setting['map']:
            raise MappedSendError('The Frappe value has no configured mutation value mapping.')
        value = setting['map'][key]
    if item['transformation'] == 'Trim' and isinstance(value, str):
        value = value.strip()
    scalar = item['target']['graphql_type'].rstrip('!')
    if scalar == 'Boolean' and _source_field(document, field).fieldtype == 'Check':
        if type(value) not in (bool, int) or value not in (0, 1):
            raise MappedSendError('The mapped checkbox does not contain a valid Boolean value.')
        value = bool(value)
    if scalar == 'ID' and type(value) is int:
        value = str(value)
    return value


def _equal_target(setting, observed, expected):
    clear = setting.get('null') == 'EMPTY_STRING' or setting.get('when', {}).get('otherwise') == 'EMPTY_STRING'
    if clear and expected == '':
        return observed is None or observed == ''
    if setting['resolver'] == 'DATE_TIME' and isinstance(observed, str) and isinstance(expected, str):
        try:
            if setting['date_mode'] == 'DATE':
                from frappe.utils import get_system_timezone
                zone = ZoneInfo(get_system_timezone())
                def day(value):
                    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        return date.fromisoformat(value)
                    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    return parsed.astimezone(zone).date() if parsed.tzinfo else _MISSING
                left, right = day(observed), day(expected)
                return left is not _MISSING and right is not _MISSING and left == right
            left = datetime.fromisoformat(observed.replace('Z', '+00:00'))
            right = datetime.fromisoformat(expected.replace('Z', '+00:00'))
            return left.tzinfo is not None and right.tzinfo is not None and left.astimezone(timezone.utc) == right.astimezone(timezone.utc)
        except ValueError:
            return False
    return _json(observed) == _json(expected)


def _query_plan(ctx):
    from hero_integration.services.migration import _mapping_plan_for_run
    if not hasattr(ctx, 'query_plan'):
        ctx.query_plan = _mapping_plan_for_run(SimpleNamespace(graphql_service=ctx.read.service.name,
            graphql_mapping=ctx.read.mapping.name, service_revision=ctx.read.contract.service_revision,
            mapping_revision=ctx.read.contract.mapping_revision), ctx.read.contract.target_operation)
    return ctx.query_plan


def _matching_query_rule(ctx, item, setting):
    plan = _query_plan(ctx)
    query_path = setting.get('query_path', setting['read_path'])
    matches = [rule for rule in plan.definitions if (rule.target_doctype, rule.target_field, rule.source_path) ==
               (item['source']['doctype'], item['source']['field'], query_path)]
    if len(matches) != 1:
        raise MappedSendError('Send requires one approved Query binding for each source field and readback path.')
    return matches[0]


def _baseline_remote(ctx, item, setting, observed, *, record=None):
    """Compare the canonical value projected by the approved Query assignment."""
    from hero_integration.services.direct_mapping import transformed_value
    plan = _query_plan(ctx)
    rule = _matching_query_rule(ctx, item, setting)
    if setting.get('query_path', setting['read_path']) != setting['read_path']:
        observed = _at(record, setting['query_path'])
        if observed is _MISSING:
            raise MappedSendError('The approved Query omitted the configured canonical source projection.')
    if rule.purpose == 'Project Relation' or rule.transformation == 'HERO Project Link':
        from hero_integration.services.migration import _project_name_for_source
        return _project_name_for_source(observed, ctx.proof.source_instance)
    options = getattr(plan, 'adapter_options', None)
    if options is not None:
        from hero_integration.services.query_mapping import transform
        return transform(rule, observed, options, source_instance=ctx.proof.source_instance)
    if rule.operation == 'HeroTasks' and rule.transformation == 'Native Adapter':
        from hero_integration.services.direct_mapping import validate_native_task_rule
        validate_native_task_rule(rule)
        if rule.source_path == 'comment':
            from hero_integration.services.native_projections import _html, _text
            if observed is None or isinstance(observed, str) and not observed.strip():
                return None  # Native intake deliberately leaves an empty description unset.
            return _html(_text(observed, 'Task comment'))
        if rule.source_path == 'target_project_match_id':
            from hero_integration.services.migration import _project_name_for_source
            return _project_name_for_source(observed, ctx.proof.source_instance)
    if rule.transformation in {'Native Adapter', 'HERO Project Link'}:
        raise MappedSendError('This native Query binding needs a verified canonical Send baseline resolver.')
    return transformed_value(rule, observed, source_instance=ctx.proof.source_instance)


def _baseline_link(ctx, item):
    source = item['source']
    if source['doctype'] in {ctx.document.doctype, 'Contact'}:
        return ctx.link
    if source['doctype'] == 'ToDo' and ctx.document.doctype == 'Task':
        return ctx.link
    if source['doctype'] == 'Address' and ctx.document.doctype == 'Customer':
        address = _source_document(ctx, 'Address')
        rows = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
            'source_type': 'customer_address', 'source_instance': ctx.proof.source_instance,
            'target_doctype': 'Address', 'target_name': address.name, 'sync_status': 'Synchronized'},
            fields=['name', 'source_system', 'source_type', 'source_id', 'source_instance', 'target_doctype', 'target_name',
                    'sync_status', 'mapping_version', 'source_field_hashes', 'target_field_hashes'], limit_page_length=2)
        if len(rows) == 1:
            return rows[0]
    raise MappedSendError('The mapped relation has no unique synchronized baseline link.')


def _selected_related_identities(ctx, paths):
    """Only related identities whose nested input is actually sent (after the
    missing, null and condition rules) are resolved; an omitted optional
    relation must not block an independent field."""
    sent = [tuple(path) for path in paths]
    selected = {}
    for path, config in ctx.content['send_options'].get('related_identities', {}).items():
        prefix = tuple(path.split('.')[:-1])
        if any(len(item) > len(prefix) and item[:len(prefix)] == prefix for item in sent):
            selected[path] = config
    return selected


def _related_identity_values(ctx, remote, selected=None):
    values = {}
    if selected is None:
        selected = ctx.content['send_options'].get('related_identities', {})
    for path, config in selected.items():
        document = _source_document(ctx, config['doctype'])
        frappe.has_permission(document.doctype, 'write', doc=document, throw=True)
        if document.doctype == 'Address':
            from hero_integration.services.migration import _require_exclusive_customer_address
            _require_exclusive_customer_address(document, ctx.document.name)
        rows = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
            'source_type': config['source_type'], 'source_instance': ctx.proof.source_instance,
            'target_doctype': document.doctype, 'target_name': document.name, 'sync_status': 'Synchronized'},
            pluck='source_id', limit_page_length=2)
        if (len(rows) != 1 or not str(rows[0]).isdigit() or int(rows[0]) <= 0
                or str(_at(remote, config['read_path'], None)) != str(rows[0])):
            raise MappedSendError('The approved readback does not prove the related HERO identity.')
        values[path] = int(rows[0])
    return values


def _prepare(ctx, client, phase_id=None):
    from hero_integration.services.change_detection import decode
    from hero_integration.services.graphql_contract import validate_mutation
    from hero_integration.services.mapping_contract_v1 import MutationAdapter, MutationDraft, SnapshotAdapter, StoredSnapshot
    ctx.condition_values = []
    creating = ctx.content['send_options']['mode'] == 'CREATE'
    remote = {} if creating else _read_record(ctx, client)
    lifecycle = getattr(_query_plan(ctx), 'source_deleted', None)
    if not creating and callable(lifecycle) and lifecycle(remote):
        raise MappedSendError('The approved Query lifecycle marks this HERO record as deleted.')
    changes, conflicts, values, paths, all_values = [], [], {}, [], []
    options = ctx.content['send_options']
    phases = []
    for identity, item in assignments(ctx.content):
        from hero_integration.services.project_naming import is_local_project_name
        if is_local_project_name(item['source'], item['target']['field']):
            raise MappedSendError('Project naming is local to ERPNext and cannot be sent to HERO.')
        setting = options['assignments'][identity]
        value = _resolve(ctx, item, setting, remote, client, phase_id)
        if setting['resolver'] == 'PHASE':
            phases = [{'id': k, 'name': label} for k, label in _lookup(ctx, setting, remote, client)]
        if value is _MISSING:
            continue
        observed = _at(remote, setting['read_path'])
        if observed is _MISSING and not creating:
            raise MappedSendError('The approved query omitted a configured Send readback field.')
        all_values.append((identity, None if creating else observed, value))
        if not creating and _equal_target(setting, observed, value):
            continue
        source = item['source']
        document = _source_document(ctx, source['doctype'])
        baseline_link = None if creating else _baseline_link(ctx, item)
        source_base = {} if creating else decode(baseline_link.source_field_hashes) or {}
        target_base = {} if creating else decode(baseline_link.target_field_hashes) or {}
        baseline_key = ('ToDo.' if source['doctype'] == 'ToDo' else '') + source['field']
        ctx.baseline_links[identity] = baseline_link
        canonical_remote = None if creating else _baseline_remote(ctx, item, setting, observed, record=remote)
        if creating:
            _matching_query_rule(ctx, item, setting)
        change = {'id': identity, 'field': item['target']['field'], 'source_doctype': source['doctype'],
            'source_field': source['field'], 'label': getattr(frappe.get_meta(source['doctype']).get_field(source['field']), 'label', source['field']),
            'hero': None if creating else observed, 'new': value, 'value': value, 'baseline_key': baseline_key,
            'local': document.get(source['field']), 'canonical_remote': canonical_remote}
        if not creating and (baseline_key not in source_base or baseline_key not in target_base
                or source_base[baseline_key] != _hash(canonical_remote)):
            conflicts.append(change)
        else:
            changes.append(change)
            path = '.'.join((item['target']['argument'], item['target']['field']))
            paths.append(tuple(path.split('.')))
            values[path] = value
    if creating:
        title = values.get('task.title')
        if not isinstance(title, str) or not title.strip() or not values.get('task.target_project_match_id'):
            raise MappedSendError('Task creation requires a nonempty mapped title and verified Project.')
    else:
        identity_path = (options['identity_argument'], options['identity_field'])
        paths.append(identity_path)
        values['.'.join(identity_path)] = ctx.hero_id
    ctx.related_selection = _selected_related_identities(ctx, paths)
    for path, value in _related_identity_values(ctx, remote, ctx.related_selection).items():
        paths.append(tuple(path.split('.')))
        values[path] = value
    validated = SnapshotAdapter().adapt(StoredSnapshot(ctx.snapshot.name, ctx.snapshot.snapshot_json,
        ctx.snapshot.schema_fingerprint, ctx.proof.source_instance, ctx.snapshot.analysis_status))
    compiled = MutationAdapter().construct_preview_values(validated, MutationDraft(ctx.mapping.operation_name,
        tuple(paths), ctx.proof.source_instance, int(ctx.mapping.mapping_revision)), values, temporal=True)
    # The approved service may bind GraphQL arguments to differently named variables.
    variables = {ctx.parsed.argument_variables[name]: value for name, value in compiled['variables'].items()
                 if name in ctx.parsed.argument_variables}
    if len(variables) != len(compiled['variables']):
        raise MappedSendError('The approved mutation service does not bind every configured input argument.')
    variables = validate_mutation(json.loads(ctx.snapshot.snapshot_json), ctx.parsed, variables)
    canonical = [ctx.document.doctype, ctx.document.name, str(ctx.document.modified), ctx.mapping.name,
        int(ctx.mapping.mapping_revision), ctx.content, ctx.contract.contract_fingerprint,
        int(ctx.contract.contract_revision), ctx.read.service.query_document, ctx.read.contract.__dict__,
        ctx.proof.fingerprint, ctx.hero_id, all_values, variables, ctx.condition_values]
    return remote, changes, conflicts, variables, sha256(_json(canonical).encode()).hexdigest(), phases


def preview(doctype, name, *, phase_id=None, client=None):
    from hero_integration.api import _mutation_execution_state
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.services.writeback_source import validate_source
    ctx = _load_context(doctype, name)
    if ctx.content.get('usage') == 'MAPPED_UPDATE_V2':
        if phase_id not in (None, '', 0, '0'):
            raise MappedSendError('The selected phase identity is invalid.')
        from hero_integration.services.mapped_update_v2 import preview as preview_update
        result = preview_update(ctx.mapping.name, doctype, name, client=client)
        return {**result, doctype.lower(): name, 'phases': [], 'hero': {'phase_id': None}}
    config = validate_source(ctx.proof)
    client = client or HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=0)
    remote, changes, conflicts, _variables, fingerprint, phases = _prepare(ctx, client, phase_id)
    enabled = bool(_mutation_execution_state()['mutation_execution_enabled'])
    phase_id = next((_at(remote, ctx.content['send_options']['assignments'][identity]['read_path'], None)
        for identity, _item in assignments(ctx.content)
        if ctx.content['send_options']['assignments'][identity]['resolver'] == 'PHASE'), None)
    return {doctype.lower(): name, 'hero_id': '' if ctx.hero_id is None else str(ctx.hero_id), 'create': ctx.content['send_options']['mode'] == 'CREATE', 'mapping': ctx.mapping.name,
        'changes': changes, 'conflicts': conflicts, 'fingerprint': fingerprint, 'phases': phases,
        'hero': {'phase_id': phase_id},
        'can_send': enabled and bool(changes) and not conflicts, 'sending_enabled': enabled}


def _check_conditions_current(ctx):
    values = getattr(ctx, 'condition_values', [])
    documents = {}
    for condition in sorted(values, key=lambda value: (value['doctype'], value['name'], value['field'])):
        key = (condition['doctype'], condition['name'])
        if key not in documents:
            documents[key] = frappe.get_doc(*key, for_update=True)
        document = documents[key]
        frappe.has_permission(document.doctype, 'write' if document.doctype == ctx.document.doctype else 'read',
                              doc=document, throw=True)
        if (str(document.modified) != condition['modified']
                or _json(document.get(condition['field'], _MISSING)) != _json(condition['value'])):
            raise MappedSendError('The local Send condition changed during execution; reconcile the remote result.')


def _apply_phase_source(ctx, changes, after):
    from hero_integration.services.change_detection import managed_target_update
    phases = [(change, dict(assignments(ctx.content))[change['id']]) for change in changes
              if ctx.content['send_options']['assignments'][change['id']]['resolver'] == 'PHASE']
    if not phases:
        return
    current = frappe.get_doc(ctx.document.doctype, ctx.document.name, for_update=True)
    frappe.has_permission(current.doctype, 'write', doc=current, throw=True)
    if str(current.modified) != str(ctx.document.modified) or any(
            _hash(current.get(item['source']['field'])) != _hash(change['local']) for change, item in phases):
        raise MappedSendError('The local phase source changed during Send; the remote result needs reconciliation.')
    for change, item in phases:
        setting = ctx.content['send_options']['assignments'][change['id']]
        current.set(item['source']['field'], _baseline_remote(ctx, item, setting, _at(after, setting['read_path']), record=after))
    with managed_target_update(current.doctype, current.name, ctx.link.name):
        current.save()
    ctx.project_stamp_receipt = {'before': str(ctx.document.modified), 'after': str(current.modified)}
    for change, item in phases:
        change['local'] = current.get(item['source']['field'])



def _acknowledge_created_task(ctx, changes, after):
    from hero_integration.services.change_detection import encode, field_hashes
    from hero_integration.services.migration import _digest
    current = frappe.get_doc('Task', ctx.document.name, for_update=True)
    frappe.has_permission('Task', 'write', doc=current, throw=True)
    if str(current.modified) != str(ctx.document.modified):
        raise MappedSendError('The local Task changed during creation; reconcile the external result.')
    _assert_task_unbound(current.name)
    source, target = {}, {}
    for change in changes:
        item = dict(assignments(ctx.content))[change['id']]
        setting = ctx.content['send_options']['assignments'][change['id']]
        document = current if item['source']['doctype'] == 'Task' else frappe.get_doc(
            item['source']['doctype'], _source_document(ctx, item['source']['doctype']).name, for_update=True)
        frappe.has_permission(document.doctype, 'read', doc=document, throw=True)
        if _hash(document.get(item['source']['field'])) != _hash(change['local']):
            raise MappedSendError('A mapped local source changed during creation; reconcile the external result.')
        source[change['baseline_key']] = _baseline_remote(ctx, item, setting, _at(after, setting['read_path']), record=after)
        target[change['baseline_key']] = document.get(item['source']['field'])
    plan = _query_plan(ctx)
    if not isinstance(plan.mapping_version, str) or not plan.mapping_version:
        raise MappedSendError('The approved Query has no canonical mapping version for the new Task link.')
    ctx.link = frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO',
        'source_type': 'Task', 'source_id': str(ctx.hero_id), 'source_instance': ctx.proof.source_instance,
        'target_doctype': 'Task', 'target_name': current.name, 'sync_status': 'Synchronized', 'conflict_detail': '',
        'source_payload_hash': _digest(after), 'target_payload_hash': _digest(target),
        'source_field_hashes': encode(field_hashes(source)), 'target_field_hashes': encode(field_hashes(target)),
        'mapping_version': plan.mapping_version, 'last_synced_on': frappe.utils.now_datetime()}).insert(ignore_permissions=True)


def send(doctype, name, *, fingerprint='', confirmed=0, phase_id=None, client=None, mutation_client=None):
    from hero_integration.api import _assert_mutation_execution_released, _reserve_mutation, _finalize_mutation
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.graphql_mutation import HeroMutationClient
    from hero_integration.services.change_detection import decode, encode
    from hero_integration.services.writeback_source import validate_source
    _assert_mutation_execution_released()
    ctx = _load_context(doctype, name)
    if ctx.content.get('usage') == 'MAPPED_UPDATE_V2':
        if phase_id not in (None, '', 0, '0'):
            raise MappedSendError('The selected phase identity is invalid.')
        from hero_integration.services.mapped_update_v2 import send as send_update
        return send_update(ctx.mapping.name, doctype, name, fingerprint=fingerprint, confirmed=confirmed,
                           client=client, mutation_client=mutation_client)
    config = validate_source(ctx.proof)
    client = client or HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=0)
    before, changes, conflicts, variables, current, _phases = _prepare(ctx, client, phase_id)
    if confirmed not in (True, 1, '1', 'true') or not fingerprint or fingerprint != current:
        raise MappedSendError('The mapped Send preview changed; review and confirm it again.')
    if conflicts or not changes:
        raise MappedSendError('Send requires mapped local changes without remote baseline conflicts.')
    creating = ctx.content['send_options']['mode'] == 'CREATE'
    if creating:
        _assert_create_available(ctx)
    audit = _reserve_mutation(operation_name=ctx.mapping.operation_name,
        schema_fingerprint=ctx.snapshot.schema_fingerprint, mapping_revision=int(ctx.mapping.mapping_revision),
        request_fingerprint=_create_scope(ctx) if creating else current)
    attempted = False
    try:
        if creating:
            _assert_create_available(ctx, exclude=audit.correlation_id)
            _assert_task_unbound(ctx.document.name)
            fresh = frappe.get_doc('Task', ctx.document.name)
            if str(fresh.modified) != str(ctx.document.modified):
                raise MappedSendError('The local Task changed after its creation preview.')
        config = validate_source(ctx.proof)
        transport = mutation_client or HeroMutationClient(config.endpoint, lambda: config.api_token, config.timeout_seconds)
        attempted = True
        returned = transport.execute_once(ctx.service.query_document, variables)
        record = returned.get(ctx.mapping.operation_name) if isinstance(returned, dict) else None
        if creating and isinstance(record, dict):
            identity = record.get('id')
            if type(identity) not in (int, str) or not str(identity).isdigit() or int(identity) <= 0:
                raise MappedSendError('HERO did not return a valid created Task identity.')
            ctx.hero_id = int(identity)
        if (not isinstance(returned, dict) or returned.get('errors')
                or not isinstance(record, dict) or str(record.get('id')) != str(ctx.hero_id)):
            raise MappedSendError('HERO did not confirm the mapped bound record identity.')
        validate_source(ctx.proof)
        after = _read_record(ctx, client)
        lifecycle = getattr(_query_plan(ctx), 'source_deleted', None)
        if callable(lifecycle) and lifecycle(after):
            raise MappedSendError('The approved Query marks the resulting HERO record as deleted.')
        selected = ctx.related_selection  # the same relations the prepared mutation sent
        if _related_identity_values(ctx, after, selected) != _related_identity_values(ctx, before, selected):
            raise MappedSendError('HERO changed a mapped related record identity during Send.')
        for change in changes:
            setting = ctx.content['send_options']['assignments'][change['id']]
            observed = _at(after, setting['read_path'])
            if observed is _MISSING or not _equal_target(setting, observed, change['value']):
                raise MappedSendError('HERO did not store the configured mapped field value.')
        validate_source(ctx.proof)
        _check_conditions_current(ctx)
        if creating:
            _acknowledge_created_task(ctx, changes, after)
        else:
            _acknowledge_updated_fields(ctx, changes, after)
    except Exception:
        # Reservation already committed; this action owns all later writes and callbacks.
        frappe.db.rollback()
        detail = (_CREATE_PRETRANSPORT_FAILURE if creating and not attempted else
                  'Mapped Send was not verified; no automatic retry. Check HERO before another submission.')
        _finalize_mutation(audit, 'Failed', detail)
        raise MappedSendError('Sending to HERO could not be confirmed; no automatic retry was performed.') from None
    _finalize_mutation(audit, 'Succeeded', 'Mapped fields were sent once and verified by approved Query readback.')
    return {'status': 'SENT', 'hero_id': ctx.hero_id, 'changes': [c['label'] for c in changes],
        'correlation_id': audit.correlation_id,
        **({'project_stamp_receipt': ctx.project_stamp_receipt} if hasattr(ctx, 'project_stamp_receipt') else {})}


def _acknowledge_updated_fields(ctx, changes, after):
    from hero_integration.services.change_detection import decode, encode
    baselines = {}
    for change in changes:
        original = ctx.baseline_links[change['id']]
        if original.name not in baselines:
            current_link = frappe.get_doc('HERO Integration Link', original.name, for_update=True)
            identity_fields = ('source_system', 'source_type', 'source_id', 'source_instance', 'target_doctype', 'target_name', 'mapping_version')
            if (getattr(current_link, 'sync_status', None) != 'Synchronized'
                    or any(getattr(current_link, key, None) != getattr(original, key, None) for key in identity_fields)
                    or getattr(current_link, 'source_instance', None) != ctx.proof.source_instance):
                raise MappedSendError('The mapped binding identity or status changed during Send; reconcile the remote result.')
            baselines[original.name] = (decode(current_link.source_field_hashes) or {},
                                        decode(current_link.target_field_hashes) or {})
        current_source, current_target = baselines[original.name]
        key = change['baseline_key']
        if (current_source.get(key) != (decode(original.source_field_hashes) or {}).get(key)
                or current_target.get(key) != (decode(original.target_field_hashes) or {}).get(key)):
            raise MappedSendError('The mapped baseline changed during Send; reconcile the remote result.')
    _apply_phase_source(ctx, changes, after)
    for change in changes:
        item = dict(assignments(ctx.content))[change['id']]
        setting = ctx.content['send_options']['assignments'][change['id']]
        link = ctx.baseline_links[change['id']]
        source, target = baselines[link.name]
        source[change['baseline_key']] = _hash(_baseline_remote(ctx, item, setting,
            _at(after, setting['read_path']), record=after))
        target[change['baseline_key']] = _hash(change['local'])
    for link_name, (source, target) in baselines.items():
        frappe.db.set_value('HERO Integration Link', link_name,
            {'source_field_hashes': encode(source), 'target_field_hashes': encode(target)}, update_modified=False)
