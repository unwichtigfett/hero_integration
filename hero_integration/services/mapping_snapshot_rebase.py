"""Retarget unchanged, locally approved mapping graphs to an identical snapshot.

This is a reference-only owning transition, not a mapping publication or an
approval grant. Native saves would invalidate existing approvals and immutable
rule versions. Therefore this service locks and checks the complete graph,
changes only snapshot references with compare-and-set updates, records Versions,
and revalidates the graph before returning. It never commits, runs DDL or calls
HERO. Callers may wrap apply in another savepoint for a nonpersistent preview.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
from uuid import UUID, uuid4

import frappe

from hero_integration.services.graphql_graph import analyze_snapshot, normalize_schema
from hero_integration.services.localized_error import LocalizedError


class SnapshotRebaseError(LocalizedError):
    pass


MAPPING = 'HERO GraphQL Mapping'
RULE = 'HERO Frappe Mapping'
SERVICE = 'HERO GraphQL Service'
CONTRACT = 'HERO Mutation Contract'
DRAFT = 'HERO Mapping Draft'
SNAPSHOT = 'HERO GraphQL Schema Snapshot'
TABLES = {MAPPING, RULE, SERVICE, CONTRACT, DRAFT}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def _fail(message):
    raise SnapshotRebaseError(message)


def _snapshot(name, source=None, *, lock=False, require_compatible=True):
    doc = frappe.get_doc(SNAPSHOT, name, for_update=lock)
    doc.check_permission('read')
    raw = json.loads(doc.snapshot_json)
    actual = analyze_snapshot(raw).fingerprint
    if (doc.analysis_status != 'Validated' or actual != doc.schema_fingerprint
            or require_compatible and doc.get('execution_contract_status') == 'Blocked'
            or source is not None and doc.source_instance != source):
        _fail('Snapshot rebinding requires intact validated schemas of the same HERO source.')
    return doc, normalize_schema(raw), actual


def snapshots_equivalent(old_name, new_name, source_instance=None):
    """Read-only proof for historical lineage; no approval or runtime exemption."""
    try:
        old, before, old_hash = _snapshot(old_name, source_instance)
        new, after, new_hash = _snapshot(new_name, source_instance)
        return bool(old.source_instance and old.source_instance == new.source_instance
                    and old.get('endpoint_fingerprint')
                    and old.endpoint_fingerprint == new.get('endpoint_fingerprint')
                    and old_hash == new_hash and before == after)
    except (ValueError, TypeError, KeyError):
        return False


def _documents(doctype, filters):
    # Membership itself must be a current read. A get_all() name list can be
    # stale under REPEATABLE READ even when every returned document is locked.
    allowed = {'source_instance', 'schema_snapshot', 'status', 'execution_status', 'site_scope', 'state'}
    if doctype not in TABLES or not filters or set(filters) - allowed:
        _fail('Unsupported snapshot binding document.')
    clauses, values = [], []
    for field, value in sorted(filters.items()):
        if isinstance(value, (list, tuple)):
            if len(value) != 2 or value[0] != 'in' or not isinstance(value[1], (list, tuple)) or not value[1]:
                _fail('Unsupported snapshot binding document.')
            clauses.append(f'`{field}` in ({", ".join(["%s"] * len(value[1]))})')
            values.extend(value[1])
        else:
            clauses.append(f'`{field}`=%s')
            values.append(value)
    rows = frappe.db.sql(f'select name from `tab{doctype}` where ' + ' and '.join(clauses)
                         + ' order by name for update', tuple(values), as_dict=True)
    return [frappe.get_doc(doctype, row['name'], for_update=True) for row in rows]


def _binding(doc, target, *, context=None):
    fields = ('status', 'schema_snapshot', 'schema_fingerprint', 'mapping_revision', 'service_revision',
              'contract_revision', 'content_hash', 'mapping_json', 'query_document', 'read_mapping',
              'graphql_service', 'execution_status', 'approved_service_revision', 'approved_mapping_revision',
              'definition_id', 'source_instance', 'contract_fingerprint', 'modified',
              'content_revision', 'generation', 'state', 'new_definition_json',
              'source_path', 'source_type', 'target_doctype', 'target_field', 'purpose',
              'transformation', 'relation', 'priority', 'schema_manifest', 'schema_dependencies',
              'personal_graphql_mapping', 'portable_definition_id', 'portable_lineage_json')
    values = {key: doc.get(key) for key in fields}
    return {'doctype': doc.doctype, 'name': doc.name,
            'from_snapshot': context['snapshot_name'] if context else doc.schema_snapshot,
            'to_snapshot': target.name, 'state_hash': _hash(values)}


def _validate_group(mappings, readers, contracts, services, snapshots):
    """Use existing seal, revision, query and mutation-scope validators."""
    from hero_integration.services.read_contract import from_documents
    from hero_integration.services.personal_mapping_content import configured_mutation_helpers
    from hero_integration.services.config_mutation_contracts import _query, _scope
    from hero_integration.services.mutation_binding_contract import parse_mutation
    from hero_integration.services.mapped_update_v2 import _validate_contract_scope
    from graphql import build_client_schema, parse, validate

    by_name = {row.name: row for row in mappings}
    service_by_name = {row.name: row for row in services}
    read_counts, scopes, read_contracts = {}, set(), {}
    legacy_contracts, configured_contracts = set(), set()
    for service in readers:
        mapping = by_name.get(service.read_mapping)
        if mapping is None:
            _fail('Snapshot rebinding found a reader outside the selected mapping graph.')
        snapshot = snapshots[service.schema_snapshot]
        contract = from_documents(service, mapping, snapshot)
        read_contracts[service.name] = contract
        read_counts[mapping.name] = read_counts.get(mapping.name, 0) + 1
        key = ('QUERY_TO_FRAPPE', contract.target_operation)
        if key in scopes:
            _fail('Snapshot rebinding found ambiguous active mapping or service scopes.')
        scopes.add(key)
    for mapping in mappings:
        if mapping.direction == 'QUERY_TO_FRAPPE':
            if read_counts.get(mapping.name) != 1:
                _fail('Snapshot rebinding requires exactly one approved reader per active Query mapping.')
            continue
        content = json.loads(mapping.mapping_json)
        usage = content.get('usage')
        if usage not in {'MAPPED_UPDATE_V2', 'PROJECT_LOGBOOK_V1'}:
            from hero_integration.services.frappe_mapping_catalog import (
                FrappePickerMetadata, validate_send_mapping_sources, validate_draft_writeback_source,
            )
            schema = json.loads(snapshots[mapping.schema_snapshot].snapshot_json)
            if usage == 'SEND_TO_HERO':
                from hero_integration.services.mapped_send import normalise_send_mapping
                content = normalise_send_mapping(content, schema)
                validate_send_mapping_sources(content, FrappePickerMetadata())
                options = content['send_options']
                key = (mapping.direction, usage, content['source']['doctype'], options['mode'])
                reader = service_by_name.get(options['read_service'])
                readback = read_contracts.get(options['read_service'])
                area = {'Customer': 'HeroCustomers', 'Project': 'HeroProjectMatches', 'Task': 'HeroTasks'}[content['source']['doctype']]
                if (reader is None or reader.schema_snapshot != mapping.schema_snapshot or readback is None
                        or readback.target_operation != area or readback.identity_path != options['read_identity_path']):
                    _fail('Snapshot rebinding found a mutation outside its approved readback graph.')
            else:
                from hero_integration.services.frappe_writeback import normalise_mapping
                content = normalise_mapping(content, schema)
                source = content['source']
                validate_draft_writeback_source(source['doctype'], source['field'], FrappePickerMetadata())
                key = (mapping.direction, 'FIELD_WRITEBACK', source['doctype'], source['field'])
            if (mapping.direction != 'FRAPPE_TO_MUTATION' or mapping.operation_kind != 'MUTATION'
                    or mapping.operation_name != content['target']['operation']
                    or mapping.schema_fingerprint != snapshots[mapping.schema_snapshot].schema_fingerprint):
                _fail('Snapshot rebinding found an invalid mutation service or contract.')
            if key in scopes:
                _fail('Snapshot rebinding found ambiguous active mapping or service scopes.')
            scopes.add(key)
            matches = [row for row in contracts if row.operation_name == mapping.operation_name
                       and row.schema_snapshot == mapping.schema_snapshot]
            if len(matches) != 1:
                _fail('Snapshot rebinding requires one existing active contract per mutation operation.')
            legacy_contracts.add(matches[0].name)
            continue
        normalise, validate_sources = configured_mutation_helpers(content.get('usage'))
        content = normalise(content, json.loads(snapshots[mapping.schema_snapshot].snapshot_json))
        validate_sources(content)
        key = (mapping.direction, content['area'], content['usage'])
        if key in scopes:
            _fail('Snapshot rebinding found ambiguous active mapping or service scopes.')
        scopes.add(key)
        reader = service_by_name.get(content['read_service'])
        readback = read_contracts.get(content['read_service'])
        expected_identity = 'id' if content['usage'] == 'PROJECT_LOGBOOK_V1' else content['identity']['read_path']
        if (reader is None or reader.schema_snapshot != mapping.schema_snapshot or readback is None
                or readback.target_operation != content['area'] or readback.identity_path != expected_identity):
            _fail('Snapshot rebinding found a mutation outside its approved readback graph.')
        matches = [row for row in contracts if row.operation_name == mapping.operation_name
                   and row.schema_snapshot == mapping.schema_snapshot]
        if len(matches) != 1:
            _fail('Snapshot rebinding requires one existing active contract per mutation operation.')
        contract = matches[0]
        configured_contracts.add(contract.name)
        if content['usage'] == 'MAPPED_UPDATE_V2':
            _validate_contract_scope(mapping, content, contract)
        else:
            scope = _scope(json.loads(contract.mapping_json), mapping.operation_name)
            identity = mapping.get('portable_definition_id') or str(UUID(mapping.definition_id))
            if scope['areas'] != [content['area']] or scope['mapping_definition_ids'] != [identity]:
                _fail('Snapshot rebinding found an incompatible Project logbook contract scope.')
    for contract in contracts:
        service = service_by_name.get(contract.graphql_service)
        snapshot = snapshots[contract.schema_snapshot]
        if (service is None or service.status != 'Validated' or service.schema_snapshot != snapshot.name
                or contract.schema_fingerprint != snapshot.schema_fingerprint
                or service.schema_fingerprint != snapshot.schema_fingerprint):
            _fail('Snapshot rebinding found an invalid mutation service or contract.')
        schema = build_client_schema({'__schema': normalize_schema(json.loads(snapshot.snapshot_json))})
        if contract.name in legacy_contracts and contract.name not in configured_contracts:
            # Older approved contracts do not declare the newer resolver scope.
            # Retain their own parser and validate the entire saved query against the schema.
            if not isinstance(json.loads(contract.mapping_json), dict) or validate(schema, parse(service.query_document)):
                _fail('Snapshot rebinding found an invalid mutation service or contract.')
        else:
            _scope(json.loads(contract.mapping_json), contract.operation_name)
            _query(service.query_document, contract.operation_name, schema)
        parsed = parse_mutation(service.query_document)
        if contract.contract_fingerprint != parsed.fingerprint or parsed.root_field != contract.operation_name:
            _fail('Snapshot rebinding found a changed mutation contract fingerprint.')


def _plan(snapshot_name, source_instance):
    from hero_integration.services.writeback_source import capture_source
    from hero_integration.services.mapping_draft_exchange import _lock_context

    preliminary = frappe.get_all(MAPPING, filters={'source_instance': source_instance, 'status': 'Active'},
                                 fields=['name', 'schema_snapshot'], limit_page_length=0)
    old_names = sorted({row.schema_snapshot for row in preliminary} - {snapshot_name})
    target, target_schema, fingerprint = _snapshot(snapshot_name, source_instance, lock=True,
                                                  require_compatible=bool(old_names))
    if not old_names:
        for direction in ('FRAPPE_TO_MUTATION', 'QUERY_TO_FRAPPE'):
            _lock_context(source_instance, snapshot_name, direction)
        current = _documents(MAPPING, {'source_instance': source_instance, 'status': 'Active'})
        if {(row.name, row.schema_snapshot) for row in current} != {(row.name, row.schema_snapshot) for row in preliminary}:
            _fail('Mapping snapshot bindings changed; preview the import again.')
        return {'snapshot_name': target.name, 'canonical_snapshot': target.name, 'schema_fingerprint': fingerprint,
                'source_instance': source_instance, 'source_proof': '',
                'needs_rebase': False, 'bindings': []}, []
    source_proof = capture_source(target, source_instance)
    snapshots = {target.name: target}
    for name in old_names:
        old, schema, actual = _snapshot(name, source_instance, lock=True)
        if old.get('endpoint_fingerprint') != target.get('endpoint_fingerprint'):
            _fail('Snapshot rebinding cannot cross different HERO endpoints.')
        if actual != fingerprint or schema != target_schema:
            _fail('The saved HERO schemas differ. Review the schema changes before importing mappings.')
        snapshots[name] = old
    for name in sorted(snapshots):
        for direction in ('FRAPPE_TO_MUTATION', 'QUERY_TO_FRAPPE'):
            _lock_context(source_instance, name, direction)
    mappings = _documents(MAPPING, {'source_instance': source_instance, 'status': 'Active'})
    if {(row.name, row.schema_snapshot) for row in mappings} != {(row.name, row.schema_snapshot) for row in preliminary}:
        _fail('Mapping snapshot bindings changed; preview the import again.')
    readers = _documents(SERVICE, {'schema_snapshot': ['in', list(snapshots)],
                                  'execution_status': 'Approved', 'status': 'Validated'})
    readers = [row for row in readers if row.get('read_mapping')]
    contracts = _documents(CONTRACT, {'schema_snapshot': ['in', list(snapshots)], 'status': 'Active'})
    services = {row.name: row for row in readers}
    for contract in contracts:
        service = frappe.get_doc(SERVICE, contract.graphql_service, for_update=True)
        services[service.name] = service
    rules = _documents(RULE, {'schema_snapshot': ['in', old_names], 'status': 'Active'})
    # Unselected templates share the same verified source/schema scope. Their
    # content, status and revision remain unchanged just like selected rules.
    if any(row.schema_fingerprint != snapshots[row.schema_snapshot].schema_fingerprint for row in rules):
        _fail('Snapshot rebinding requires intact validated schemas of the same HERO source.')
    _validate_group(mappings, readers, contracts, list(services.values()), snapshots)

    # Private draft contents and immutable revision history are never rewritten.
    # Current parentless Mutation context is metadata; its unchanged content hash
    # and baseline fingerprint remain valid after this exact-schema transition.
    draft_changes = []
    mapping_names = {row.name for row in mappings if row.schema_snapshot in old_names}
    for draft in _documents(DRAFT, {'site_scope': frappe.local.site, 'state': 'OPEN'}):
        context = json.loads(draft.new_definition_json) if draft.get('new_definition_json') else None
        linked = draft.get('base_active_mapping') in mapping_names
        parentless = context and context.get('source_instance') == source_instance and context.get('snapshot_name') in old_names
        content = json.loads(draft.content_json)
        bound_rules = any(isinstance(base, dict) and base.get('schema_snapshot') in old_names
                          for base in (content.get('field_rule_bases') or {}).values())
        if not linked and not parentless and not bound_rules:
            continue
        if content.get('field_rule_bases') or context and context.get('direction') == 'QUERY_TO_FRAPPE':
            _fail('Open Query drafts contain snapshot-bound rule baselines. Reconcile these drafts before rebinding snapshots.')
        if parentless:
            if draft.owner_user != frappe.session.user:
                _fail('A private parentless draft must be rebound by its owner before switching snapshots.')
            if context.get('schema_fingerprint') != fingerprint or content.get('usage') not in {'MAPPED_UPDATE_V2', 'PROJECT_LOGBOOK_V1'}:
                _fail('An open draft has an incompatible snapshot context.')
            draft_changes.append((draft, context))

    documents = [(row, None) for row in [*mappings, *rules, *services.values(), *contracts]
                 if row.schema_snapshot != target.name] + draft_changes
    bindings = sorted((_binding(doc, target, context=context) for doc, context in documents),
                      key=lambda row: (row['doctype'], row['name']))
    plan = {'snapshot_name': target.name, 'canonical_snapshot': old_names[0] if len(old_names) == 1 else target.name,
            'schema_fingerprint': fingerprint, 'source_instance': source_instance,
            'source_proof': source_proof.fingerprint, 'needs_rebase': bool(bindings), 'bindings': bindings}
    return plan, documents


def _audit(doc, field, before, after):
    frappe.get_doc({'doctype': 'Version', 'ref_doctype': doc.doctype, 'docname': doc.name,
                    'data': _json({'changed': [[field, before, after]],
                                   'snapshot_rebase': {'identical_schema': True, 'actor': frappe.session.user}})}).insert(ignore_permissions=True)


def _move(doc, context, target):
    if doc.doctype not in TABLES:
        _fail('Unsupported snapshot binding document.')
    doc.check_permission('write')
    if context is None:
        field, before, after = 'schema_snapshot', doc.schema_snapshot, target
    else:
        field, before = 'new_definition_json', doc.new_definition_json
        after = _json({**context, 'snapshot_name': target})
    # The table/column identifiers are closed server-owned constants. All data
    # is parameterized; locked content, revisions and approvals are untouched.
    frappe.db.sql(f'update `tab{doc.doctype}` set `{field}`=%s where name=%s and `{field}`=%s and modified=%s',
                  (after, doc.name, before, doc.modified))
    current = frappe.db.sql(f'select `{field}` from `tab{doc.doctype}` where name=%s for update', (doc.name,), as_dict=True)
    if len(current) != 1 or current[0][field] != after:
        _fail('Mapping snapshot bindings changed; preview the import again.')
    _audit(doc, field, before, after)
    frappe.clear_document_cache(doc.doctype, doc.name)


def prepare(snapshot_name, source_instance, *, apply=False, expected_fingerprint=None):
    """Plan or atomically apply an identical-schema rebind; caller owns commit.

    apply requires the fingerprint returned by a fresh read-only prepare. On
    failure the service rolls back only its own savepoint, preserving caller DML.
    Returned fingerprints describe the pre-transition graph, not mutable caches.
    """
    frappe.only_for('System Manager')
    savepoint = 'hero_snapshot_rebase_' + uuid4().hex
    if apply:
        frappe.db.savepoint(savepoint)
    try:
        plan, documents = _plan(snapshot_name, source_instance)
        plan['fingerprint'] = _hash(plan)
        if apply and expected_fingerprint != plan['fingerprint']:
            _fail('Snapshot rebinding changed since preview; preview the import again.')
        if apply and documents:
            for doc, context in documents:
                _move(doc, context, snapshot_name)
            # A fresh complete plan also re-reads snapshots and current source.
            # Validate the relocated graph with native seals before succeeding.
            target, _, _ = _snapshot(snapshot_name, source_instance, lock=True)
            mappings = _documents(MAPPING, {'source_instance': source_instance, 'status': 'Active'})
            readers = _documents(SERVICE, {'schema_snapshot': snapshot_name, 'execution_status': 'Approved', 'status': 'Validated'})
            readers = [row for row in readers if row.get('read_mapping')]
            contracts = _documents(CONTRACT, {'schema_snapshot': snapshot_name, 'status': 'Active'})
            services = {row.name: row for row in readers}
            for contract in contracts:
                services[contract.graphql_service] = frappe.get_doc(SERVICE, contract.graphql_service, for_update=True)
            _validate_group(mappings, readers, contracts, list(services.values()), {target.name: target})
            from hero_integration.services.writeback_source import capture_source
            if capture_source(target, source_instance).fingerprint != plan['source_proof']:
                _fail('The confirmed HERO source changed during snapshot rebinding.')
        return {**plan, 'applied': bool(apply and documents)}
    except Exception:
        if apply:
            frappe.db.rollback(save_point=savepoint)
            for doc, _context in locals().get('documents', []):
                frappe.clear_document_cache(doc.doctype, doc.name)
        raise


@contextmanager
def preview_target(snapshot_name, source_instance):
    """Temporarily expose the exact target graph, then restore caller state."""
    plan = prepare(snapshot_name, source_instance)
    if not plan['needs_rebase']:
        yield plan
        return
    savepoint = 'hero_snapshot_preview_' + uuid4().hex
    frappe.db.savepoint(savepoint)
    try:
        prepare(snapshot_name, source_instance, apply=True, expected_fingerprint=plan['fingerprint'])
        yield plan
    finally:
        frappe.db.rollback(save_point=savepoint)
        for binding in plan['bindings']:
            frappe.clear_document_cache(binding['doctype'], binding['name'])
