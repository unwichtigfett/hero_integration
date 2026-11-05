"""Portable, locally approved update contracts; never execution authority.

Only the registered update and Project-logbook profiles and their mapping identities
travel with configuration. Endpoints, credentials, gates, audit records and
runtime variables cannot occur in this closed format. Callers own the enclosing
configuration transaction; this module never commits or contacts HERO.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from uuid import UUID

import frappe
from graphql import build_client_schema, parse, print_ast, validate as validate_graphql
from graphql.language.ast import FieldNode, OperationDefinitionNode, VariableNode

from hero_integration.services.graphql_contract import parse_mutation
from hero_integration.services.graphql_graph import analyze_snapshot, normalize_schema
from hero_integration.services.localized_error import LocalizedError
from hero_integration.services.mapped_update_v2 import AREAS
from hero_integration.services.personal_mapping_content import configured_mutation_helpers


class MutationContractExchangeError(LocalizedError):
    pass


LOGBOOK_OPERATION = 'add_logbook_entry'
OPERATIONS = frozenset(spec[3] for spec in AREAS.values()) | {LOGBOOK_OPERATION}
KEYS = {'operation_name', 'service_name', 'query', 'mapping', 'status'}
SCOPE_KEYS = {'mode', 'resolver', 'areas', 'mapping_definition_ids'}


def _fail(message):
    raise MutationContractExchangeError(message)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail('Mutation contract mapping identities must be canonical UUIDs.')
    return value


def _scope(value, operation):
    logbook = operation == LOGBOOK_OPERATION
    resolver = 'PROJECT_LOGBOOK_V1' if logbook else 'MAPPED_UPDATE_V2'
    if (not isinstance(value, dict) or set(value) != SCOPE_KEYS
            or value.get('mode') != 'VALIDATED_RESOLVER' or value.get('resolver') != resolver):
        _fail('Mutation contract scope must explicitly select its registered resolver.')
    areas, ids = value.get('areas'), value.get('mapping_definition_ids')
    if (not isinstance(areas, list) or not areas or len(areas) > 9
            or any(not isinstance(area, str) or area not in AREAS or (area != 'HeroProjectMatches' if logbook else AREAS[area][3] != operation) for area in areas)
            or len(set(areas)) != len(areas) or not isinstance(ids, list) or len(ids) != len(areas)):
        _fail('Mutation contract areas do not match their registered operation.')
    identities = [_uuid(identity) for identity in ids]
    if len(set(identities)) != len(identities):
        _fail('Mutation contract mapping identities must be unique.')
    return {'mode': 'VALIDATED_RESOLVER', 'resolver': resolver,
            'areas': sorted(areas), 'mapping_definition_ids': sorted(identities)}


def _query(raw, operation, schema):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 20000:
        _fail('Mutation contract query must be bounded GraphQL text.')
    try:
        tree = parse(raw)
        if len(tree.definitions) != 1 or not isinstance(tree.definitions[0], OperationDefinitionNode):
            raise ValueError
        node = tree.definitions[0]
        roots = node.selection_set.selections
        if (node.operation.value != 'mutation' or node.directives or len(roots) != 1
                or not isinstance(roots[0], FieldNode) or roots[0].name.value != operation
                or roots[0].alias or roots[0].directives):
            raise ValueError
        root = roots[0]
        selections = root.selection_set.selections if root.selection_set else ()
        if (len(selections) != 1 or not isinstance(selections[0], FieldNode)
                or selections[0].name.value != 'id' or selections[0].alias
                or selections[0].arguments or selections[0].directives or selections[0].selection_set
                or any(v.default_value is not None or v.directives for v in node.variable_definitions)
                or any(not isinstance(a.value, VariableNode) for a in root.arguments)):
            raise ValueError
        if validate_graphql(schema, tree):
            raise ValueError
        # print_ast discards comments (which may contain private notes), and
        # canonicalizes whitespace without importing executable literals.
        canonical = print_ast(tree)
        parsed = parse_mutation(canonical)
        if len(set(parsed.argument_variables.values())) != len(parsed.argument_variables):
            raise ValueError
    except Exception as error:
        raise MutationContractExchangeError('Mutation contract must be one schema-valid variable-only update selecting its id.') from error
    return canonical, parsed


def _mapping_scopes(mappings, raw_schema):
    if isinstance(mappings, dict):
        mappings = mappings.get('mappings', mappings.get('mutation_mappings'))
    if not isinstance(mappings, list) or len(mappings) > 10:
        _fail('Mutation contract validation requires the accompanying update mappings.')
    result = {}
    seen = set()
    seen_ids = set()
    for group in mappings:
        if not isinstance(group, dict):
            _fail('Mutation contract validation requires the accompanying update mappings.')
        original = group.get('content')
        if not isinstance(original, dict):
            _fail('Mutation contract validation requires the accompanying update mappings.')
        normalise_mapping, _ = configured_mutation_helpers(original.get('usage'))
        content = normalise_mapping(original, raw_schema)
        operation, area = content['target']['operation'], content['area']
        identity = _uuid(group.get('definition_id'))
        profile = (area, content['usage'])
        if (profile in seen or identity in seen_ids or group.get('operation_name') != operation
                or group.get('direction') != 'FRAPPE_TO_MUTATION' or group.get('operation_kind') != 'MUTATION'):
            _fail('Mutation contract mappings have duplicate areas or incompatible operation scopes.')
        seen.add(profile)
        seen_ids.add(identity)
        scope = result.setdefault(operation, {'areas': [], 'mapping_definition_ids': [], 'arguments': set()})
        scope['areas'].append(area)
        scope['mapping_definition_ids'].append(identity)
        paths = ([content['identity']['input_path']] if content['usage'] == 'MAPPED_UPDATE_V2' else [])
        paths += [item['input_path'] for item in content['assignments'] + content.get('required_readback', [])]
        scope['arguments'].update(path.split('.')[0] for path in paths)
    return result


def validate(items, raw_schema, mutation_mappings=None):
    """Pure validation before DDL; optionally prove the complete mapping scope."""
    if not isinstance(items, list) or len(items) > len(OPERATIONS):
        _fail('Configuration mutation contracts must be a list of at most nine registered operations.')
    schema = build_client_schema({'__schema': normalize_schema(raw_schema)})
    scopes = _mapping_scopes(mutation_mappings, raw_schema) if mutation_mappings is not None else None
    result, seen = [], set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != KEYS
                or not isinstance(item.get('operation_name'), str) or item['operation_name'] not in OPERATIONS
                or item['operation_name'] in seen or item.get('status') not in ('Draft', 'Active')
                or not isinstance(item.get('service_name'), str) or not item['service_name'].strip()
                or len(item['service_name']) > 140 or any(ord(c) < 32 for c in item['service_name'])):
            _fail('Invalid or duplicate portable mutation contract.')
        operation = item['operation_name']
        seen.add(operation)
        scope = _scope(item['mapping'], operation)
        query, parsed = _query(item['query'], operation, schema)
        if scopes is not None:
            expected = scopes.get(operation)
            if (not expected or scope['areas'] != sorted(expected['areas'])
                    or scope['mapping_definition_ids'] != sorted(expected['mapping_definition_ids'])
                    or set(parsed.argument_variables) != expected['arguments']):
                _fail('Mutation contract does not exactly cover its accompanying mapping identities and arguments.')
        result.append({'operation_name': operation, 'service_name': item['service_name'],
                       'query': query, 'mapping': scope, 'status': item['status']})
    return sorted(result, key=lambda item: item['operation_name'])


def _snapshot(snapshot, source, *, lock=False):
    frappe.only_for('System Manager')
    if isinstance(snapshot, str):
        snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', snapshot, for_update=lock)
    elif lock:
        snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', snapshot.name, for_update=True)
    snapshot.check_permission('read')
    raw = json.loads(snapshot.snapshot_json)
    if (snapshot.source_instance != source or snapshot.analysis_status != 'Validated'
            or getattr(snapshot, 'execution_contract_status', '') == 'Blocked'
            or analyze_snapshot(raw).fingerprint != snapshot.schema_fingerprint):
        _fail('Mutation contract snapshot must match the validated target source and schema.')
    return snapshot, raw


def _documents(snapshot, operation, *, lock=False):
    result = []
    for doctype in ('HERO GraphQL Service', 'HERO Mutation Contract'):
        names = frappe.get_all(doctype, filters={'schema_snapshot': snapshot.name, 'operation_name': operation}, pluck='name', limit_page_length=2)
        result.append([frappe.get_doc(doctype, name, for_update=lock) for name in names])
    return result


def _inspect(snapshot, item, schema, *, lock=False):
    services, contracts = _documents(snapshot, item['operation_name'], lock=lock)
    if len(services) > 1 or len(contracts) > 1:
        return {'state': 'Conflict', 'reason': 'Several mutation services or contracts require explicit reconciliation.'}
    service = services[0] if services else None
    contract = contracts[0] if contracts else None
    if contract and not service:
        return {'state': 'Conflict', 'reason': 'An existing mutation contract has no matching service.'}
    if service:
        try:
            canonical, _parsed = _query(service.query_document, item['operation_name'], schema)
            matches = (canonical == item['query'] and service.service_name == item['service_name']
                       and service.schema_fingerprint == snapshot.schema_fingerprint and not service.read_mapping
                       and service.status in ('Draft', 'Validated'))
        except ValueError:
            matches = False
        if not matches:
            return {'state': 'Conflict', 'reason': 'The existing mutation service differs from the imported definition.'}
    if contract:
        try:
            scope = _scope(json.loads(contract.mapping_json), item['operation_name'])
            matches = (scope == item['mapping'] and contract.graphql_service == service.name
                       and contract.schema_fingerprint == snapshot.schema_fingerprint
                       and contract.contract_fingerprint == parse_mutation(service.query_document).fingerprint)
        except ValueError:
            matches = False
        if not matches or contract.status not in ('Draft', 'Active'):
            return {'state': 'Conflict', 'reason': 'The existing mutation contract differs from the imported definition.'}
    if item['status'] == 'Draft' and (contract and contract.status == 'Active' or service and service.status == 'Validated'):
        return {'state': 'Conflict', 'reason': 'Import cannot silently downgrade an approved mutation contract.'}
    state = ('New' if not contract else 'Approve' if item['status'] == 'Active'
             and (contract.status != 'Active' or service.status != 'Validated') else 'Unchanged')
    return {'state': state, 'service': service, 'contract': contract}


def _report(items, plans):
    rows = [{'operation_name': item['operation_name'], 'areas': item['mapping']['areas'], 'status': item['status'],
             **{key: value for key, value in plan.items() if key not in {'service', 'contract'}},
             'service': getattr(plan.get('service'), 'name', None),
             'contract': getattr(plan.get('contract'), 'name', None)} for item, plan in zip(items, plans)]
    counts = {key: 0 for key in ('New', 'Approve', 'Unchanged', 'Conflict')}
    counts.update(Counter(row['state'] for row in rows))
    return {'counts': counts, 'contracts': rows, 'can_apply': not counts['Conflict'], 'execution': False}


def preview(snapshot, items, source, *, mutation_mappings=None):
    snapshot, raw = _snapshot(snapshot, source)
    items = validate(items, raw, mutation_mappings)
    schema = build_client_schema({'__schema': normalize_schema(raw)})
    return _report(items, [_inspect(snapshot, item, schema) for item in items])


def planned_preview(items, raw_schema, *, mutation_mappings):
    """Validate a package whose target snapshot has not been created yet."""
    canonical = validate(items, raw_schema, mutation_mappings)
    return _report(canonical, [{'state': 'New'} for item in canonical])


def _approve(snapshot, service, contract, item, schema):
    service.reload()
    contract.reload()
    query, _parsed = _query(service.query_document, item['operation_name'], schema)
    if (query != item['query'] or service.schema_snapshot != snapshot.name
            or service.schema_fingerprint != snapshot.schema_fingerprint or service.read_mapping
            or contract.status != 'Active' or contract.graphql_service != service.name
            or contract.schema_fingerprint != snapshot.schema_fingerprint
            or _scope(json.loads(contract.mapping_json), item['operation_name']) != item['mapping']
            or contract.contract_fingerprint != parse_mutation(service.query_document).fingerprint):
        _fail('Mutation service or saved contract changed before explicit local approval.')
    if service.status != 'Validated':
        frappe.db.set_value('HERO GraphQL Service', service.name, {'status': 'Validated',
            'validation_summary': 'Locally schema-validated update contract explicitly approved during configuration import. No HERO operation was executed.'})
        service.add_comment('Info', 'Mutation contract explicitly approved during configuration import after local schema validation. No HERO operation was executed.')


def apply(snapshot, items, source, *, confirmed=False, mutation_mappings=None):
    """Use native saves and the explicit reviewed local-approval transition only."""
    if confirmed is not True:
        _fail('Explicit local mutation contract import confirmation is required.')
    snapshot, raw = _snapshot(snapshot, source, lock=True)
    items = validate(items, raw, mutation_mappings)
    schema = build_client_schema({'__schema': normalize_schema(raw)})
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    for item in items:
        _lock_active_target('mapped-update-contract:' + snapshot.name, 'Mutation', item['operation_name'])
    plans = [_inspect(snapshot, item, schema, lock=True) for item in items]
    if any(plan['state'] == 'Conflict' for plan in plans):
        _fail('Mutation contract import has unresolved conflicts; no contract was written.')
    savepoint = 'hero_config_mutation_contracts'
    frappe.db.savepoint(savepoint)
    try:
        for item, plan in zip(items, plans):
            if plan['state'] == 'Unchanged':
                continue
            service, contract = plan.get('service'), plan.get('contract')
            if service is None:
                service = frappe.get_doc({'doctype': 'HERO GraphQL Service', 'service_name': item['service_name'],
                    'operation_name': item['operation_name'], 'query_document': item['query'],
                    'schema_snapshot': snapshot.name, 'provenance': 'Explicit configuration contract import', 'status': 'Draft'}).insert()
            if contract is None:
                contract = frappe.get_doc({'doctype': 'HERO Mutation Contract', 'graphql_service': service.name,
                    'mapping_json': json.dumps(item['mapping'], ensure_ascii=False, sort_keys=True), 'status': item['status']}).insert()
            elif contract.status != item['status']:
                contract.status = item['status']
                contract.save()
            if item['status'] == 'Active':
                _approve(snapshot, service, contract, item, schema)
            plan.update(service=service, contract=contract)
        return _report(items, plans)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def export(source_instance, snapshot_name, *, mutation_mappings=None):
    """Export only contracts referenced by the selected portable update mappings."""
    snapshot, raw = _snapshot(snapshot_name, source_instance)
    if mutation_mappings is None:
        from hero_integration.services.config_mutation_drafts import export as export_mappings
        mutation_mappings = export_mappings(source_instance, snapshot_name)
    scopes = _mapping_scopes(mutation_mappings, raw)
    output = []
    for operation in sorted(scopes):
        services, contracts = _documents(snapshot, operation)
        if not contracts:
            continue
        if len(contracts) != 1:
            _fail('Several mutation services or contracts require explicit reconciliation.')
        contract = contracts[0]
        if contract.status == 'Disabled':
            continue
        service = next((doc for doc in services if doc.name == contract.graphql_service), None)
        if not service:
            _fail('An existing mutation contract has no matching service.')
        output.append({'operation_name': operation, 'service_name': service.service_name,
                       'query': service.query_document, 'mapping': json.loads(contract.mapping_json), 'status': contract.status})
    canonical = validate(output, raw, mutation_mappings)
    schema = build_client_schema({'__schema': normalize_schema(raw)})
    if any(_inspect(snapshot, item, schema)['state'] not in ('Unchanged', 'Approve') for item in canonical):
        _fail('Existing mutation contracts cannot be exported without reconciliation.')
    return deepcopy(canonical)
