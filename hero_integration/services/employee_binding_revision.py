"""Carry verified employee bindings across additive changes to an approved reader."""
from dataclasses import replace
from hashlib import sha256
import json

import frappe
from graphql import parse
from graphql.language import FieldNode, OperationDefinitionNode

from hero_integration.services.binding_registry import (
    BindingConflict, BindingState, FrappeBindingStorage, MappingDirection, incarnation, verify_incarnation,
)


def _require(condition):
    if not condition:
        raise BindingConflict("Employee binding revision requires review: additive compatibility is unproven.")


def _selection(document):
    """Only plain, unfiltered employee projections can extend an earlier selection."""
    nodes = parse(document).definitions
    _require(len(nodes) == 1 and isinstance(nodes[0], OperationDefinitionNode))
    op = nodes[0]
    _require(op.operation.value == "query" and not op.directives and not op.variable_definitions)
    paths = set()

    def visit(selection, prefix):
        names = set()
        for field in selection.selections:
            _require(isinstance(field, FieldNode) and not field.alias and not field.directives and not field.arguments)
            name = field.name.value
            _require(name not in names)
            names.add(name)
            path = (*prefix, name)
            if field.selection_set:
                visit(field.selection_set, path)
            else:
                paths.add(path)

    visit(op.selection_set, ())
    return paths


def compatible_rules(plan, old, current, old_query, query):
    """Reconstruct the *old* sealed plan with the current rules. No baseline is inferred."""
    prior, seal = old['query_adapter_seal'], current['query_adapter_seal']
    _require(all(prior[k] == seal[k] for k in ('version', 'schema_fingerprint', 'source_instance',
                                               'variables_sha256', 'read_shape_sha256')))
    omit = {'query_adapter_seal', 'mapping_definition_ids', 'query_adapter'}
    _require({k:v for k,v in old.items() if k not in omit} == {k:v for k,v in current.items() if k not in omit})
    _require(current['target_operation'] == 'HeroEmployees' and plan.mapping_version == seal['mapping_version'])
    _require(sha256(query.encode()).hexdigest() == seal['document_sha256'])
    _require(sha256(old_query.encode()).hexdigest() == prior['document_sha256'])
    if old_query != query:
        old_paths, new_paths = _selection(old_query), _selection(query)
        prefix = tuple(current['records_path'].split('.'))
        _require(old_paths <= new_paths and all(p[:len(prefix)] == prefix for p in new_paths - old_paths))
    ids = set(old['mapping_definition_ids'])
    selected = {s.definition_id for s in plan.definitions}
    _require(ids and ids <= selected and selected == set(current['mapping_definition_ids']))
    added = [s for s in plan.definitions if s.definition_id not in ids]
    _require(all(s.target_doctype == 'Employee' and s.purpose == 'Field'
                 and s.target_field.startswith('custom_hero_') for s in added))
    options = dict(current['query_adapter'])
    if 'source_history' not in old['query_adapter'] and options.get('source_history') == 'partner_absences':
        options.pop('source_history')
    if 'transform_options' in options:
        options['transform_options'] = {k:v for k,v in options['transform_options'].items() if k in ids}
    _require(options == old['query_adapter'])
    previous_plan = replace(plan, definitions=tuple(s for s in plan.definitions if s.definition_id in ids),
                            adapter_options=options)
    _require(previous_plan.mapping_version == prior['mapping_version'])
    return {s.definition_id:s for s in previous_plan.definitions}


def _changes(doctype, name):
    for text in frappe.get_all('Version', filters={'ref_doctype':doctype, 'docname':name},
                               pluck='data', order_by='creation desc'):
        yield {key:(a,b) for key,a,b in json.loads(text).get('changed', [])}


def previous_contract(read, revision):
    contract = read.plan.binding_contract
    _require(contract.revision == revision + 1)
    mapping = frappe.get_doc('HERO GraphQL Mapping', read.mapping, for_update=True)
    current = json.loads(mapping.mapping_json)
    _require(mapping.status == 'Active' and mapping.definition_id == contract.definition_id
             and mapping.source_instance == contract.source_instance and mapping.mapping_revision == contract.revision)
    revisions = [changes for changes in _changes(mapping.doctype, mapping.name)
                 if tuple(map(str, changes.get('mapping_revision', ()))) == (str(revision), str(contract.revision))]
    _require(len(revisions) == 1 and 'mapping_json' in revisions[0])
    old, new = map(json.loads, revisions[0]['mapping_json'])
    _require(new == current)
    unchanged = {'definition_id','source_instance','schema_snapshot','schema_fingerprint','operation_name','direction','operation_kind','status'}
    _require(not unchanged.intersection(revisions[0]))
    digest = old['query_adapter_seal']['document_sha256']
    old_query = read.document
    if sha256(old_query.encode()).hexdigest() != digest:
        old_query = next((q for changes in _changes('HERO GraphQL Service', read.origin)
                          for q in changes.get('query_document', ()) if q and sha256(q.encode()).hexdigest() == digest), None)
    _require(old_query)
    return old['query_adapter_seal']['mapping_version'], compatible_rules(read.plan, old, current, old_query, read.document)


def carry_employee_bindings(read, record):
    """Called inside the checked migration record transaction. A rejected record rolls back both claims."""
    contract = read.plan.binding_contract
    storage = FrappeBindingStorage(frappe)
    site = frappe.local.site
    from hero_integration.services.execution_runtime import locked_provenance, open_field_reviews
    pending = []
    partners = ((contract.object_type, 'partner', 'Employee', record['id']),
                ('User', 'User', 'User', record.get('user_id') or (record.get('user') or {}).get('id')))
    for kind, source_type, dt, identity in partners:
        if identity is None:
            continue
        claim = storage.lock_source_object(site, contract.source_instance, kind, dt, str(identity))
        if claim is None or claim.mapping_revision == contract.revision:
            continue
        _require(claim.mapping_definition_id == contract.definition_id and not claim.reconciliation)
        link = locked_provenance(contract.source_instance, source_type, str(identity), dt)
        _require(link and link.target_name == claim.target_name and link.sync_status == 'Synchronized')
        native = frappe.get_doc(dt, claim.target_name, for_update=True)
        verify_incarnation(claim, native)
        _require(incarnation(native.creation) <= incarnation(link.creation))
        _require(not open_field_reviews(claim))
        pending.append((claim, link))
    if not pending:
        return
    previous, rules = previous_contract(read, pending[0][0].mapping_revision)
    writes = []
    for claim, link in pending:
        proof = frappe.db.get_value('HERO Integration Link', link.name, ['mapping_version','target_incarnation'],
                                    as_dict=True, for_update=True)
        _require(claim.mapping_revision + 1 == contract.revision and proof.mapping_version == previous
                 and (proof.target_incarnation == claim.target_incarnation or
                      (claim.target_doctype == 'Employee' and not proof.target_incarnation)))
        writes.append((storage.OBJECT, claim))
        for field in storage.fields_for_object(site, claim.identity_key, for_update=True):
            spec = rules.get(field.mapping_definition_id)
            _require(field.direction == MappingDirection.HERO_TO_FRAPPE and spec
                     and field.mapping_revision == claim.mapping_revision
                     and (field.source_instance, field.target_app, field.target_doctype, field.target_name)
                         == (claim.source_instance, claim.target_app, claim.target_doctype, claim.target_name)
                     and (spec.source_path, spec.target_doctype, spec.target_field)
                         == (field.source_field, field.target_doctype, field.target_field)
                     and field.state not in {BindingState.MAPPING_CHANGED, BindingState.RECONCILIATION_REQUIRED}
                     and not field.reconciliation)
            writes.append((storage.FIELD, field))
    for dt, binding in writes:
        birth = binding.identity_mapping_revision or binding.mapping_revision
        _require(binding.identity_key == binding.canonical_key(birth))
        storage._write(frappe, dt)
        frappe.db.set_value(dt, binding.identity_key, {'identity_mapping_revision':birth,
                            'mapping_revision':contract.revision, 'binding_revision':binding.revision + 1})
