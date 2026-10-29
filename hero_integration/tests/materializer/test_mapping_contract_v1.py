from __future__ import annotations

import json
import copy
import sys
from types import SimpleNamespace

import pytest

from hero_integration.graphql_protocol.graphql_result_decoder import (
    decode_graphql_result,
)
from hero_integration.graphql_protocol.transport import Response
from hero_integration.services.graphql_graph import analyze_snapshot
from hero_integration.services.mapping_contract_v1 import (
    MappingContractError,
    adapt_read_contract,
    MutationAdapter,
    MutationDraft,
    NullMode,
    QueryAdapter,
    QueryDraft,
    RelationMode,
    SnapshotAdapter,
    StoredSnapshot,
)
from hero_integration.services.read_contract import ReadContract, ReadContractError, from_documents
from hero_integration.sources.hero import CUSTOMER_CONTRACT_V1, customer_query


def test_personal_task_fields_materialize_with_identity_and_lifecycle_selection():
    raw = copy.deepcopy(RAW_SCHEMA)
    query = next(t for t in raw['__schema']['types'] if t['name'] == 'Query')
    scalar = lambda value: {'kind': 'SCALAR', 'name': value}
    query['fields'].append({'name': 'tasks', 'args': [
        {'name': 'first', 'type': scalar('Int')}, {'name': 'offset', 'type': scalar('Int')},
        {'name': 'orderBy', 'type': scalar('String')}],
        'type': {'kind': 'LIST', 'ofType': {'kind': 'OBJECT', 'name': 'Task'}}})
    raw['__schema']['types'].append({'kind': 'OBJECT', 'name': 'Task', 'fields': [
        {'name': 'id', 'args': [], 'type': scalar('ID')},
        {'name': 'title', 'args': [], 'type': scalar('String')},
        {'name': 'target_project_match_id', 'args': [], 'type': scalar('Int')},
        {'name': 'target_user_id', 'args': [], 'type': scalar('Int')},
        {'name': 'author_user_id', 'args': [], 'type': scalar('Int')},
        {'name': 'is_deleted', 'args': [], 'type': scalar('Boolean')}]})
    stored = StoredSnapshot('tasks-snapshot', raw, analyze_snapshot(raw).fingerprint, 'hero-primary')
    validated = SnapshotAdapter().adapt(stored)
    content = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id',
        'variables': {'first': '$page_size', 'offset': '$offset'}, 'page_size': 25,
        'native_task_onboarding': {'confirmed': True, 'source_instance': 'hero-primary', 'source_type': 'Task'},
        'mapping_definition_ids': ['task-id', 'task-title', 'task-project'],
        'field_rules': {'task-id': {'source_path': 'id'}, 'task-title': {'source_path': 'title'},
                        'task-project': {'source_path': 'target_project_match_id'}}}
    request = QueryAdapter().materialize_personal_task(validated, content)
    assert {binding.selection_path for binding in request.result_bindings} == {
        ('id',), ('title',), ('target_project_match_id',), ('is_deleted',)}
    assert set(request.variables.values()) == {0, 25, 'id'}
    assert request.mutation_safety_request is None
    assert request == QueryAdapter().materialize_personal_task(validated, content)
    assignment = {**content, 'mapping_definition_ids': [*content['mapping_definition_ids'], 'task-user'],
                  'field_rules': {**content['field_rules'], 'task-user': {'source_path': 'target_user_id'}}}
    assigned_request = QueryAdapter().materialize_personal_task(validated, assignment)
    assert ('author_user_id',) in {binding.selection_path for binding in assigned_request.result_bindings}
    with pytest.raises(MappingContractError, match='pagination'):
        QueryAdapter().materialize_personal_task(validated, {**content, 'variables': {'first': 100}})
    with pytest.raises(MappingContractError, match='scope'):
        QueryAdapter().materialize_personal_task(validated, {**content, 'native_task_onboarding': {
            'confirmed': True, 'source_instance': 'other', 'source_type': 'Task'}})
def named(kind, name):
    return {"kind": kind, "name": name}


def non_null(value):
    return {"kind": "NON_NULL", "ofType": value}


def listed(value):
    return {"kind": "LIST", "ofType": value}


RAW_SCHEMA = {
    "__schema": {
        "queryType": {"name": "Query"},
        "mutationType": {"name": "Mutation"},
        "types": [
            {
                "kind": "OBJECT",
                "name": "Query",
                "fields": [
                    {
                        "name": "searchCases",
                        "args": [
                            {
                                "name": "filter",
                                "defaultValue": None,
                                "type": non_null(named("INPUT_OBJECT", "CaseFilter")),
                            },
                            {
                                "name": "offset",
                                "defaultValue": None,
                                "type": non_null(named("SCALAR", "Int")),
                            },
                            {
                                "name": "orderBy",
                                "defaultValue": None,
                                "type": named("ENUM", "CaseOrder"),
                            },
                            {
                                "name": "includeDeleted",
                                "defaultValue": None,
                                "type": named("SCALAR", "Boolean"),
                            },
                        ],
                        "type": non_null(listed(non_null(named("OBJECT", "Case")))),
                    }
                ],
            },
            {
                "kind": "OBJECT",
                "name": "Mutation",
                "fields": [
                    {
                        "name": "updateCase",
                        "args": [
                            {
                                "name": "input",
                                "defaultValue": None,
                                "type": non_null(named("INPUT_OBJECT", "CaseUpdateInput")),
                            }
                        ],
                        "type": named("OBJECT", "Case"),
                    }
                ],
            },
            {
                "kind": "OBJECT",
                "name": "Case",
                "fields": [
                    {"name": "id", "args": [], "type": non_null(named("SCALAR", "ID"))},
                    {"name": "title", "args": [], "type": named("SCALAR", "String")},
                    {"name": "owner", "args": [], "type": named("OBJECT", "Owner")},
                    {
                        "name": "requiresArgument",
                        "args": [
                            {
                                "name": "id",
                                "defaultValue": None,
                                "type": non_null(named("SCALAR", "ID")),
                            }
                        ],
                        "type": named("OBJECT", "Owner"),
                    },
                ],
            },
            {
                "kind": "OBJECT",
                "name": "Owner",
                "fields": [
                    {"name": "name", "args": [], "type": named("SCALAR", "String")}
                ],
            },
            {
                "kind": "INPUT_OBJECT",
                "name": "CaseFilter",
                "inputFields": [
                    {
                        "name": "state",
                        "defaultValue": None,
                        "type": non_null(named("ENUM", "CaseState")),
                    },
                    {
                        "name": "ids",
                        "defaultValue": None,
                        "type": non_null(listed(non_null(named("SCALAR", "ID")))),
                    },
                    {
                        "name": "criteria",
                        "defaultValue": None,
                        "type": named("INPUT_OBJECT", "CaseCriteria"),
                    },
                ],
            },
            {
                "kind": "INPUT_OBJECT",
                "name": "CaseCriteria",
                "inputFields": [
                    {
                        "name": "labels",
                        "defaultValue": None,
                        "type": non_null(listed(non_null(named("SCALAR", "String")))),
                    },
                    {"name": "enabled", "defaultValue": None, "type": named("SCALAR", "Boolean")},
                    {"name": "rank", "defaultValue": None, "type": named("SCALAR", "Int")},
                    {"name": "note", "defaultValue": None, "type": named("SCALAR", "String")},
                ],
            },
            {
                "kind": "INPUT_OBJECT",
                "name": "CaseUpdateInput",
                "inputFields": [
                    {"name": "title", "defaultValue": None, "type": named("SCALAR", "String")},
                    {"name": "state", "defaultValue": None, "type": named("ENUM", "CaseState")},
                ],
            },
            {
                "kind": "ENUM",
                "name": "CaseState",
                "enumValues": [{"name": "ACTIVE"}, {"name": "INACTIVE"}],
            },
            {
                "kind": "ENUM",
                "name": "CaseOrder",
                "enumValues": [{"name": "ID_ASC"}, {"name": "ID_DESC"}],
            },
            *(
                {"kind": "SCALAR", "name": scalar}
                for scalar in ("Boolean", "ID", "Int", "String")
            ),
        ],
    }
}


def snapshot():
    fingerprint = analyze_snapshot(RAW_SCHEMA).fingerprint
    return SnapshotAdapter().adapt(
        StoredSnapshot("CASE-SNAPSHOT", RAW_SCHEMA, fingerprint, "hero-primary")
    )


def draft(**overrides):
    values = {
        "operation_name": "SearchCases",
        "alias": "cases",
        "field": "searchCases",
        "selected_paths": ("id", "owner.name"),
        "variables": {
            "filter": {
                "state": "ACTIVE",
                "ids": ["case-1"],
                "criteria": {"labels": [], "enabled": False, "rank": 0, "note": None},
            },
            "offset": 0,
            "orderBy": "ID_ASC",
            "includeDeleted": False,
        },
        "pagination": "offset",
        "ordering": "orderBy",
    }
    values.update(overrides)
    return QueryDraft(**values)


def variables_by_argument(request):
    return {
        binding.argument_name: request.variables[binding.variable_name]
        for binding in request.argument_bindings
    }


def read_documents():
    snapshot_document = SimpleNamespace(name="READ-SNAPSHOT", schema_fingerprint="read-fingerprint", source_instance='hero-primary')
    mapping = SimpleNamespace(
        name="READ-MAPPING",
        status="Active",
        direction='QUERY_TO_FRAPPE', operation_kind='QUERY', source_instance='hero-primary',
        schema_snapshot=snapshot_document.name,
        schema_fingerprint=snapshot_document.schema_fingerprint,
        mapping_revision=3,
        mapping_json=json.dumps(
            {
                "target_operation": "HeroCustomers",
                "records_path": "contacts",
                "identity_path": "id",
                "mapping_definition_ids": ["customer-id"],
                "variables": {
                    "first": "$page_size",
                    "offset": "$offset",
                    "orderBy": "id",
                    "showDeleted": True,
                },
                "page_size": 2,
                "customer_contract_version": CUSTOMER_CONTRACT_V1,
            }
        ),
    )
    service = SimpleNamespace(
        name="READ-SERVICE",
        status="Validated",
        schema_snapshot=snapshot_document.name,
        schema_fingerprint=snapshot_document.schema_fingerprint,
        service_revision=7,
        read_mapping=mapping.name,
        execution_status="Approved",
        approved_service_revision=7,
        approved_mapping_revision=3,
        query_document=customer_query(CUSTOMER_CONTRACT_V1),
    )
    return service, mapping, snapshot_document


def test_snapshot_adapter_rejects_invalid_or_stale_fingerprints():
    with pytest.raises(MappingContractError, match="Unvalidated"):
        SnapshotAdapter().adapt(StoredSnapshot("S", RAW_SCHEMA, "not-a-fingerprint", "hero-primary"))

    with pytest.raises(MappingContractError, match="Stale"):
        SnapshotAdapter().adapt(StoredSnapshot("S", RAW_SCHEMA, "0" * 64, "hero-primary"))


def test_query_adapter_validates_recursive_types_and_preserves_distinct_values():
    request = QueryAdapter().materialize(snapshot(), draft())
    values = variables_by_argument(request)

    assert values["filter"] == {
        "state": "ACTIVE",
        "ids": ["case-1"],
        "criteria": {"labels": [], "enabled": False, "rank": 0, "note": None},
    }
    assert values["offset"] == 0
    assert values["includeDeleted"] is False

    missing = draft(variables={"filter": {"state": "ACTIVE", "ids": ["case-1"]}, "offset": 0, "orderBy": "ID_ASC"})
    missing_values = variables_by_argument(QueryAdapter().materialize(snapshot(), missing))
    assert "includeDeleted" not in missing_values
    assert "criteria" not in missing_values["filter"]

    for filter_value in (
        {"state": "UNKNOWN", "ids": ["case-1"]},
        {"state": "ACTIVE", "ids": ["case-1", None]},
        {"state": "ACTIVE", "ids": ["case-1"], "criteria": {"labels": None}},
    ):
        with pytest.raises(MappingContractError):
            QueryAdapter().materialize(
                snapshot(),
                draft(variables={"filter": filter_value, "offset": 0, "orderBy": "ID_ASC"}),
            )


def test_query_adapter_seals_nested_selection_and_decodes_bound_results():
    request = QueryAdapter().materialize(snapshot(), draft())
    decoded = decode_graphql_result(
        request,
        Response(
            True,
            json.dumps({"data": {"cases": [{"id": "case-1", "owner": {"name": "Ada"}}, {"id": "case-2", "owner": {"name": "Bea"}}]}}),
            True,
        ),
    )
    assert decoded.value_for(("owner", "name")) == ("Ada", "Bea")
    assert decoded.value_for(("id",)) == ("case-1", "case-2")
    with pytest.raises(MappingContractError, match="requires unbound arguments"):
        QueryAdapter().materialize(snapshot(), draft(selected_paths=("requiresArgument.name",)))


def test_query_adapter_requires_bound_pagination_and_ordering_values():
    incomplete = {"filter": {"state": "ACTIVE", "ids": []}, "offset": 0}
    with pytest.raises(MappingContractError, match="pagination/ordering"):
        QueryAdapter().materialize(snapshot(), draft(variables=incomplete))
    with pytest.raises(MappingContractError, match="pagination/ordering"):
        QueryAdapter().materialize(snapshot(), draft(pagination="cursor"))


def test_read_contract_rejects_stale_revisions_and_identity_or_ordering_drift(monkeypatch):
    service, mapping, read_snapshot = read_documents()
    rule = {'definition_id':'customer-id', 'status':'Active', 'operation':'HeroCustomers',
            'schema_snapshot':mapping.schema_snapshot, 'schema_fingerprint':mapping.schema_fingerprint,
            'personal_graphql_mapping':'', 'source_path':'id'}
    fake_frappe = SimpleNamespace(db=SimpleNamespace(has_column=lambda *_: True),
        get_all=lambda _doctype, filters, **_kwargs: [rule] if filters.get('definition_id') == 'customer-id' else [])
    monkeypatch.setitem(sys.modules, 'frappe', fake_frappe)
    service.approved_service_revision = 6
    with pytest.raises(ReadContractError, match="service approval is stale"):
        from_documents(service, mapping, read_snapshot)
    service.approved_service_revision = 7
    service.approved_mapping_revision = 2
    with pytest.raises(ReadContractError, match="mapping approval is stale"):
        from_documents(service, mapping, read_snapshot)
    service.approved_mapping_revision = 3
    config = json.loads(mapping.mapping_json)
    config["identity_path"] = "external_id"
    mapping.mapping_json = json.dumps(config)
    with pytest.raises(ReadContractError, match="exact reviewed"):
        from_documents(service, mapping, read_snapshot)
    config["identity_path"] = "id"
    config["variables"]["orderBy"] = "name"
    mapping.mapping_json = json.dumps(config)
    with pytest.raises(ReadContractError, match="exact reviewed"):
        from_documents(service, mapping, read_snapshot)


def test_mutation_draft_is_never_executable_and_blocks_partial_or_unapproved_collections():
    adapted = MutationAdapter().materialize(snapshot(), MutationDraft("updateCase", (("input", "title"),), "hero-primary", 4))
    assert adapted["executable"] is False
    assert adapted["source_instance"] == "hero-primary"
    for draft_value in (
        MutationDraft("updateCase", (("input", "title"),), "hero-primary", 4, null_mode=NullMode.EXPLICIT_NULL),
        MutationDraft("updateCase", (("input", "title"),), "hero-primary", 4, relation_mode=RelationMode.COLLECTION),
        MutationDraft("updateCase", (("input", "missing"),), "hero-primary", 4),
    ):
        with pytest.raises(MappingContractError):
            MutationAdapter().materialize(snapshot(), draft_value)


def test_mutation_plan_rejects_wrong_source_and_ambiguous_or_collection_paths():
    raw = copy.deepcopy(RAW_SCHEMA)
    input_type = next(t for t in raw['__schema']['types'] if t['name'] == 'CaseUpdateInput')
    input_type['inputFields'].append({'name':'children', 'defaultValue':None,
        'type':listed(named('INPUT_OBJECT', 'CaseChild'))})
    input_type['inputFields'].append({'name':'details', 'defaultValue':None,
        'type':named('INPUT_OBJECT', 'CaseChild')})
    raw['__schema']['types'].append({'kind':'INPUT_OBJECT', 'name':'CaseChild',
        'inputFields':[{'name':'title', 'defaultValue':None, 'type':non_null(named('SCALAR','String'))}]})
    adapted = SnapshotAdapter().adapt(StoredSnapshot('snapshot', raw, analyze_snapshot(raw).fingerprint, 'hero-primary'))
    plan = MutationAdapter().materialize(adapted, MutationDraft('updateCase', (('input','title'),), 'hero-primary', 1))
    assert plan['input_types'] == [{'path':['input','title'], 'type':'String', 'nullable':True}]
    assert plan['null_mode'] == 'missing_is_unchanged'
    assert plan['executable'] is False
    nested = MutationAdapter().materialize(adapted, MutationDraft('updateCase', (('input','details','title'),), 'hero-primary', 1))
    assert nested['input_types'] == [{'path':['input','details','title'], 'type':'String!', 'nullable':False}]
    collection = MutationAdapter().materialize(adapted, MutationDraft('updateCase', (('input','children'),),
        'hero-primary', 1, relation_mode=RelationMode.COLLECTION, resolver_approved=True))
    assert collection['input_types'][0]['type'] == '[CaseChild]'
    for value in (
        MutationDraft('updateCase', (('input','title'),), 'another-source', 1),
        MutationDraft('updateCase', (('input','title'),), 'hero-primary', True),
        MutationDraft('updateCase', (('input','title'), ('input','title')), 'hero-primary', 1),
        MutationDraft('updateCase', (('input',), ('input','title')), 'hero-primary', 1),
        MutationDraft('updateCase', (('input','children'),), 'hero-primary', 1),
        MutationDraft('updateCase', (('input','children','title'),), 'hero-primary', 1),
        MutationDraft('updateCase', (('input','children','title'),), 'hero-primary', 1,
                      relation_mode=RelationMode.COLLECTION, resolver_approved=True),
    ):
        with pytest.raises(MappingContractError):
            MutationAdapter().materialize(adapted, value)

READ_QUERY = """query SearchCases($v1: CaseFilter!, $v2: Boolean, $v3: Int!, $v4: CaseOrder) {
  cases: searchCases(filter: $v1, includeDeleted: $v2, offset: $v3, orderBy: $v4) {
  id
  owner {
    name
  }
}
}"""

def approved_read_contract():
 values = dict(draft().variables)
 values["offset"] = "$offset"
 return ReadContract(
  service_name="READ-SERVICE", service_revision=7, mapping_name="READ-MAPPING",
  mapping_revision=3, target_operation="HeroCustomers", records_path="cases",
  identity_path="id", definition_ids=("customer-id",), variables=values, page_size=2,
 )

def test_read_adapter_seals_validated_contract_v1_request_without_changing_transport_query():
 adapted = adapt_read_contract(
  snapshot(), approved_read_contract(), draft(), READ_QUERY, offset=0,
  service_revision=7, mapping_revision=3, snapshot_fingerprint=snapshot().fingerprint,
 )
 assert adapted.transport_document == READ_QUERY
 assert adapted.request.document == READ_QUERY
 assert adapted.request.variables["v3"] == 0
 assert adapted.selected_paths == ("id", "owner.name")
 assert tuple(binding.response_path for binding in adapted.result_bindings) == (
  ("data", "cases", "id"), ("data", "cases", "owner", "name"),
 )

 assert adapted == adapt_read_contract(
  snapshot(), approved_read_contract(), draft(), READ_QUERY, offset=0,
  service_revision=7, mapping_revision=3, snapshot_fingerprint=snapshot().fingerprint,
 )
def test_read_adapter_rejects_saved_query_variable_revision_snapshot_and_binding_mismatches():
 contract = approved_read_contract()
 with pytest.raises(MappingContractError, match="saved reviewed query"):
  adapt_read_contract(snapshot(), contract, draft(), READ_QUERY + "\n", offset=0, service_revision=7, mapping_revision=3, snapshot_fingerprint=snapshot().fingerprint)
 with pytest.raises(MappingContractError, match="variable"):
  adapt_read_contract(snapshot(), contract, draft(variables={**draft().variables, "offset": 1}), READ_QUERY, offset=0, service_revision=7, mapping_revision=3, snapshot_fingerprint=snapshot().fingerprint)
 with pytest.raises(MappingContractError, match="mapping revision"):
  adapt_read_contract(snapshot(), contract, draft(), READ_QUERY, offset=0, service_revision=7, mapping_revision=4, snapshot_fingerprint=snapshot().fingerprint)
 with pytest.raises(MappingContractError, match="snapshot fingerprint"):
  adapt_read_contract(snapshot(), contract, draft(), READ_QUERY, offset=0, service_revision=7, mapping_revision=3, snapshot_fingerprint="0" * 64)
 with pytest.raises(MappingContractError, match="result bindings"):
  adapt_read_contract(snapshot(), contract, draft(selected_paths=()), READ_QUERY, offset=0, service_revision=7, mapping_revision=3, snapshot_fingerprint=snapshot().fingerprint)
 with pytest.raises(MappingContractError, match="read-only"):
  adapt_read_contract(snapshot(), contract, draft(), "mutation Write { updateCase { id } }", offset=0, service_revision=7, mapping_revision=3, snapshot_fingerprint=snapshot().fingerprint)
