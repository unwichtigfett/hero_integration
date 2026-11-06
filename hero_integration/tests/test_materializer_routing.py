from dataclasses import replace
import json

import pytest

from hero_integration.graphql_protocol.graphql_result_decoder import decode_graphql_result
from hero_integration.graphql_protocol.transport import Response
from hero_integration.services.frappe_field_registry import FrappeFieldReference
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import StoredSnapshot
from hero_integration.services.mapping_registry_model import (
    GraphQLReference, MappingDefinition, MappingDirection, MissingSemantics, MissingValuePolicy, TransformPolicy,
)
from hero_integration.services.mapping_registry_json import serialize_mapping_registry_v3
from hero_integration.services.mapping_registry_service import MappingRegistryOperationError, MappingRegistryService
from hero_integration.services.mapping_registry_storage import InMemoryMappingRegistryStorage
from hero_integration.services.materializer_routing import (
    MaterializerRouter,
    MaterializerRoutingError,
    QueryRoutingRequest,
)
from hero_integration.services.schema_registry import build_schema_registry
from hero_integration.tests.mapping_registry.test_mapping_registry_validation import fields
from hero_integration.tests.materializer.test_mapping_contract_v1 import RAW_SCHEMA
from hero_integration.tests.test_schema_registry import _stored


def test_compile_typed_query_plan_from_cataloged_references():
    snapshot = _stored()
    registry = build_schema_registry(snapshot)

    plan = MaterializerRouter().compile_query(
        snapshot,
        registry,
        QueryRoutingRequest(
            operation="case",
            output_references=(
                "hero.query.case.id",
                "hero.query.case.title",
            ),
            arguments={"id": "case-1"},
            identity_reference="hero.query.case.id",
            revision=7,
        ),
    )

    assert plan.direction == "QUERY_OUTPUT"
    assert plan.execution_authority == "NONE"
    assert plan.snapshot_revision == 7
    assert plan.selected_response_paths == (
        ("data", "case", "id"),
        ("data", "case", "title"),
    )
    assert "query case" in plan.request.document
    assert plan.request.mutation_safety_request is None


def test_nested_field_arguments_are_validated_and_parameterized():
    snapshot = StoredSnapshot('nested', RAW_SCHEMA, analyze_snapshot(RAW_SCHEMA).fingerprint, 'hero-primary')
    registry = build_schema_registry(snapshot)
    route = QueryRoutingRequest(
        operation='searchCases', output_references=('hero.query.searchCases.requiresArgument.name',),
        arguments={'filter': {'state': 'ACTIVE', 'ids': []}, 'offset': 0},
        nested_arguments={'requiresArgument': {'id': 'owner-1'}},
    )
    router = MaterializerRouter()
    plan = router.compile_query(snapshot, registry, route)
    assert 'requiresArgument(id: $v3)' in plan.request.document
    assert plan.request.variables['v3'] == 'owner-1'
    assert router.compile_query(snapshot, registry, route) == plan
    assert 'owner-1' not in plan.request.document
    for nested in ({}, {'requiresArgument': {'id': None}},
                   {'requiresArgument': {'id': 'x', 'unknown': 1}}, {'owner': {'id': 'x'}}):
        with pytest.raises(MaterializerRoutingError):
            router.compile_query(snapshot, registry, replace(route, nested_arguments=nested))


def test_explicit_recursive_output_paths_are_resolved_beyond_discovery_limit():
    snapshot = _stored()
    registry = build_schema_registry(snapshot)
    route = QueryRoutingRequest(operation='case', arguments={'id': '1'},
        output_references=('hero.query.case.children.children.children.children.id',))
    plan = MaterializerRouter().compile_query(snapshot, registry, route)
    assert plan.request.document.count('children {') == 4
    assert plan.selected_response_paths == (('data', 'case', 'children', 'children', 'children', 'children', 'id'),)
    with pytest.raises(MaterializerRoutingError):
        MaterializerRouter().compile_query(snapshot, registry,
            replace(route, output_references=('hero.query.case.children.unknown.id',)))


def test_router_accepts_schema_proven_union_fragment_and_rejects_other_types():
    snapshot = _stored()
    registry = build_schema_registry(snapshot)
    route = QueryRoutingRequest(operation='search', arguments={'filter': {}},
        output_references=('hero.query.search.@Case.id', 'hero.query.search.@Project.id'))
    plan = MaterializerRouter().compile_query(snapshot, registry, route)
    assert '... on Case' in plan.request.document
    assert '... on Project' in plan.request.document
    with pytest.raises(MaterializerRoutingError):
        MaterializerRouter().compile_query(snapshot, registry,
            replace(route, output_references=('hero.query.search.@Query.case',)))


def _mapping_context(reference, direction):
    snapshot = StoredSnapshot('mapping-snapshot', RAW_SCHEMA, analyze_snapshot(RAW_SCHEMA).fingerprint, 'hero-primary')
    registry = build_schema_registry(snapshot)
    graph = GraphQLReference.parse(reference)
    operation = registry.operation(graph.operation_kind.value, graph.operation)
    candidates = operation.output_fields if direction == MappingDirection.HERO_TO_FRAPPE else operation.input_fields
    field = next(item for item in candidates if item.reference_id == reference)
    target = fields()
    definition = MappingDefinition('mapped-field', snapshot.source_instance, direction, graph, field.type_ref,
        FrappeFieldReference('example', 'Sample', 'value'), 'Data', TransformPolicy.IDENTITY,
        MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.OMIT), snapshot.name, snapshot.fingerprint, 1)
    return snapshot, registry, target, definition


def test_mapping_query_generates_typed_document_and_decodes_nested_results_without_authority():
    snapshot, registry, target, mapping = _mapping_context('hero.query.searchCases.owner.name', MappingDirection.HERO_TO_FRAPPE)
    arguments = {'filter': {'state': 'ACTIVE', 'ids': ['case-1'],
                 'criteria': {'labels': [], 'enabled': False, 'rank': 0, 'note': None}},
                 'offset': 0, 'orderBy': 'ID_ASC', 'includeDeleted': False}
    plan = MaterializerRouter().compile_query_mappings(snapshot, registry, target, (mapping,),
        operation='searchCases', arguments=arguments, identity_reference='hero.query.searchCases.id',
        pagination_argument='offset', revision=2)
    assert plan.execution_authority == 'NONE'
    assert plan.selected_reference_ids == ('hero.query.searchCases.id', 'hero.query.searchCases.owner.name')
    assert plan.request.variables[plan.pagination_variable] == 0
    result = decode_graphql_result(plan.request, Response(True, json.dumps({'data': {'searchCases': [
        {'id': 'case-1', 'owner': {'name': 'Ada'}}, {'id': 'case-2', 'owner': {'name': 'Sam'}}]}}), True))
    assert result.value_for(('id',)) == ('case-1', 'case-2')
    assert result.value_for(('owner', 'name')) == ('Ada', 'Sam')
    assert MaterializerRouter().compile_query_mappings(snapshot, registry, target, (mapping,),
        operation='searchCases', arguments=arguments, identity_reference='hero.query.searchCases.id',
        pagination_argument='offset', revision=2) == plan


def test_mapping_mutation_compiles_typed_input_without_granting_execution():
    snapshot, registry, target, mapping = _mapping_context('hero.mutation.updateCase.input.title', MappingDirection.FRAPPE_TO_HERO)
    plan = MaterializerRouter().compile_mutation_mappings(snapshot, registry, target, (mapping,),
        operation='updateCase', arguments={'input': {'title': 'A'}},
        output_references=('hero.mutation.updateCase.result.id',))
    assert plan.execution_authority == 'NONE'
    assert plan.request.mutation_safety_request is not None
    with pytest.raises(MaterializerRoutingError):
        MaterializerRouter().compile_mutation_mappings(snapshot, registry, target, (mapping,),
            operation='updateCase', arguments={'input': {'title': 123}},
            output_references=('hero.mutation.updateCase.result.id',))


def test_mapping_compilation_rejects_missing_target_stale_scope_and_wrong_operation():
    snapshot, registry, target, mapping = _mapping_context('hero.query.searchCases.owner.name', MappingDirection.HERO_TO_FRAPPE)
    router = MaterializerRouter()
    kwargs = dict(operation='searchCases', arguments={}, identity_reference='hero.query.searchCases.id')
    with pytest.raises(MaterializerRoutingError, match='available target'):
        router.compile_query_mappings(snapshot, registry, replace(target, fields=()), (mapping,), **kwargs)
    with pytest.raises(MaterializerRoutingError, match='invalid'):
        router.compile_query_mappings(snapshot, registry, target, (replace(mapping, source_instance='other'),), **kwargs)
    with pytest.raises(MaterializerRoutingError, match='operation binding'):
        router.compile_query_mappings(snapshot, registry, target, (mapping,), **{**kwargs, 'operation': 'case'})
    with pytest.raises(MaterializerRoutingError, match='identity'):
        router.compile_query_mappings(snapshot, registry, target, (mapping,), **{**kwargs, 'identity_reference': ''})


def test_active_registry_round_trip_compiles_only_exact_revision_and_selected_definitions():
    snapshot, registry, target, mapping = _mapping_context('hero.query.searchCases.owner.name', MappingDirection.HERO_TO_FRAPPE)
    storage = InMemoryMappingRegistryStorage()
    service = MappingRegistryService(storage, registry, target)
    package = serialize_mapping_registry_v3(site_scope=target.site, registry_revision=0,
        provenance={'source_instance': snapshot.source_instance, 'snapshot_name': snapshot.name,
                    'schema_fingerprint': snapshot.fingerprint}, definitions=(mapping,))
    imported = service.import_complete_json(package, expected_revision=None)
    arguments = {'filter': {'state': 'ACTIVE', 'ids': ['case-1'],
                 'criteria': {'labels': [], 'enabled': False, 'rank': 0, 'note': None}},
                 'offset': 0, 'orderBy': 'ID_ASC', 'includeDeleted': False}
    kwargs = dict(definition_ids=('mapped-field',), operation='searchCases', arguments=arguments,
                  identity_reference='hero.query.searchCases.id', pagination_argument='offset')
    plan = service.compile_active_query(snapshot, expected_revision=imported.revision, **kwargs)
    assert plan.execution_authority == 'NONE'
    assert plan.mapping_registry_revision == imported.revision
    exported = service.export_active_json(target.site)
    second = MappingRegistryService(InMemoryMappingRegistryStorage(), registry, target)
    second.import_complete_json(exported, expected_revision=None)
    assert second.compile_active_query(snapshot, expected_revision=1, **kwargs).request == plan.request
    with pytest.raises(MappingRegistryOperationError, match='revision conflicts'):
        service.compile_active_query(snapshot, expected_revision=2, **kwargs)
    with pytest.raises(MappingRegistryOperationError, match='positive expected revision'):
        service.compile_active_query(snapshot, expected_revision=True, **kwargs)
    with pytest.raises(MappingRegistryOperationError, match='absent'):
        service.compile_active_query(snapshot, expected_revision=1, **{**kwargs, 'definition_ids': ('unknown',)})


def test_active_mutation_round_trip_is_nonexecuting_and_revision_guarded():
    snapshot, registry, target, mapping = _mapping_context('hero.mutation.updateCase.input.title', MappingDirection.FRAPPE_TO_HERO)
    service = MappingRegistryService(InMemoryMappingRegistryStorage(), registry, target)
    package = serialize_mapping_registry_v3(site_scope=target.site, registry_revision=0,
        provenance={'source_instance': snapshot.source_instance, 'snapshot_name': snapshot.name,
                    'schema_fingerprint': snapshot.fingerprint}, definitions=(mapping,))
    service.import_complete_json(package, expected_revision=None)
    kwargs = dict(definition_ids=('mapped-field',), operation='updateCase', arguments={'input': {'title': 'A'}},
                  output_references=('hero.mutation.updateCase.result.id',))
    plan = service.compile_active_mutation(snapshot, expected_revision=1, **kwargs)
    assert plan.execution_authority == 'NONE'
    other = MappingRegistryService(InMemoryMappingRegistryStorage(), registry, target)
    other.import_complete_json(service.export_active_json(target.site), expected_revision=None)
    assert other.compile_active_mutation(snapshot, expected_revision=1, **kwargs).request == plan.request
    with pytest.raises(MappingRegistryOperationError, match='revision conflicts'):
        service.compile_active_mutation(snapshot, expected_revision=2, **kwargs)
