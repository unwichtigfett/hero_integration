from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.tests._frappe_test_import import load_api

api = load_api()


SCHEMA = {"__schema": {"mutationType": {"name": "Mutation"}, "types": [
    {"name": "Mutation", "fields": [{"name": "changeCase", "type": {"kind": "OBJECT", "name": "Case"}}]},
    {"name": "Case", "fields": []},
]}}
PAYLOAD = {
    "name": "Change case draft",
    "operation_name": "ChangeCase",
    "operation_kind": "mutation",
    "query": "mutation ChangeCase { changeCase { id } }",
    "variables": {"api_token": "must-not-be-stored"},
}


class _Service:
    def __init__(self, name="HERO-GQL-00001", revision=1):
        self.name = name
        self.schema_snapshot = "SNAPSHOT"
        self.service_name = ""
        self.operation_name = ""
        self.query_document = ""
        self.service_revision = revision
        self.status = "Draft"
        self.validation_summary = ""
        self.save_calls = 0

    def save(self):
        self.save_calls += 1

    def as_dict(self):
        return self.__dict__.copy()


class _FrappeMock:
    def __init__(self, service=None, permission_error=None):
        self.service = service
        self.permission_error = permission_error
        self.only_for_calls = []
        self.new_doc_calls = []
        self.get_all_calls = []

    def only_for(self, role):
        self.only_for_calls.append(role)
        if self.permission_error:
            raise self.permission_error

    def throw(self, message):
        raise ValueError(message)

    def new_doc(self, doctype):
        self.new_doc_calls.append(doctype)
        self.service = _Service()
        return self.service

    def get_doc(self, doctype, name):
        if doctype != "HERO GraphQL Service" or name != self.service.name:
            raise AssertionError((doctype, name))
        return self.service

    def get_single(self, doctype):
        # The service list reports the backend-owned mutation gate (OFF by default).
        assert doctype == "HERO Integration Settings", doctype
        return SimpleNamespace(allow_graphql_mutation_execution=0)

    def get_all(self, doctype, **_kwargs):
        self.get_all_calls.append(doctype)
        if doctype != "HERO GraphQL Service":
            raise AssertionError(doctype)
        return [self.service.as_dict()] if self.service else []


class TestWorkspaceMutationDraftContract(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(name="SNAPSHOT", schema_fingerprint="synthetic")
        self.frappe = _FrappeMock()
        self.patch_frappe = patch.object(api, "frappe", self.frappe)
        self.patch_snapshot = patch.object(api, "_validated_snapshot", return_value=(self.snapshot, SCHEMA))
        self.patch_frappe.start()
        self.patch_snapshot.start()
        self.addCleanup(self.patch_frappe.stop)
        self.addCleanup(self.patch_snapshot.stop)

    def test_mutation_draft_can_be_saved_reloaded_and_edited_without_variables(self):
        created = api.workspace_service_save("SNAPSHOT", json.dumps(PAYLOAD))
        service = self.frappe.service

        self.assertEqual(created["query"]["service_kind"], "mutation")
        self.assertEqual(created["query"]["status"], "Draft")
        self.assertEqual(service.save_calls, 1)
        self.assertFalse(hasattr(service, "variables"))
        listed = api.workspace_service_list("SNAPSHOT", "mutation")
        self.assertEqual(listed["rows"], [created["query"]])
        self.assertEqual(listed["mutation_execution"]["mutation_execution_status"], "DISABLED")
        self.assertEqual(api.workspace_service_detail(service.name)["query"], created["query"])

        edited_payload = {**PAYLOAD, "name": "Edited case draft", "query": "mutation ChangeCase { changeCase { id } }"}
        edited = api.workspace_service_save("SNAPSHOT", json.dumps(edited_payload), service.name, expected_revision=1)
        self.assertEqual(edited["query"]["name"], "Edited case draft")
        self.assertEqual(service.status, "Draft")
        self.assertEqual(service.save_calls, 2)

    def test_mutation_validation_is_local_and_never_executable(self):
        result = api.workspace_service_validate("SNAPSHOT", json.dumps(PAYLOAD))

        self.assertTrue(result["valid"])
        self.assertFalse(result["can_execute"])
        self.assertEqual(result["mutation"]["return_type"], "Case")
        self.assertIn("disabled", result["reason"])

    def test_saved_mutation_validation_never_activates_or_resaves_the_draft(self):
        api.workspace_service_save("SNAPSHOT", json.dumps(PAYLOAD))
        service = self.frappe.service

        result = api.workspace_service_validate("SNAPSHOT", query_id=service.name, expected_revision=1)

        self.assertTrue(result["valid"])
        self.assertFalse(result["can_execute"])
        self.assertEqual(service.status, "Draft")
        self.assertEqual(service.save_calls, 1)

    def test_mismatched_and_mixed_documents_are_rejected_before_save(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            api.workspace_service_save("SNAPSHOT", json.dumps({**PAYLOAD, "operation_kind": "query"}))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            api.workspace_service_save(
                "SNAPSHOT", json.dumps({**PAYLOAD, "query": PAYLOAD["query"] + " query { changeCase { id } }"}),
            )
        self.assertIsNone(self.frappe.service)

    def test_every_available_mutation_execute_facade_fails_closed(self):
        with patch.object(api, "_assert_mutation_execution_released", side_effect=ValueError(api.MUTATION_EXECUTION_DISABLED)):
            with self.assertRaisesRegex(ValueError, api.MUTATION_EXECUTION_DISABLED):
                api.execute_graphql_service("HERO-GQL-00001")

    def test_missing_permission_is_denied_before_draft_persistence(self):
        blocked = _FrappeMock(permission_error=PermissionError("denied"))
        with patch.object(api, "frappe", blocked), self.assertRaisesRegex(PermissionError, "denied"):
            api.workspace_service_save("SNAPSHOT", json.dumps(PAYLOAD))

        self.assertEqual(blocked.new_doc_calls, [])

    def test_generated_query_is_saved_as_unapproved_draft_without_manual_document(self):
        payload = {'name': 'Generated', 'operation_kind': 'query', 'mapping_generation': {'registry_revision': 1}}
        plan = SimpleNamespace(request=SimpleNamespace(document='query Search { cases { id } }', operation_name='Search'))
        with patch.object(api, '_workspace_generated_query', return_value=plan) as generate:
            result = api.workspace_service_save('SNAPSHOT', json.dumps(payload))
        self.assertEqual(self.frappe.only_for_calls, ['System Manager'])
        generate.assert_called_once_with(self.snapshot, payload['mapping_generation'])
        self.assertEqual(result['query']['query'], plan.request.document)
        self.assertEqual((self.frappe.service.status, self.frappe.service.save_calls), ('Draft', 1))
        self.assertFalse(hasattr(self.frappe.service, 'execution_status'))

    def test_generated_query_rejects_conflicting_document_and_mutation_before_write(self):
        payload = {'name': 'Generated', 'operation_kind': 'query', 'mapping_generation': {},
                   'query': 'query Unsafe { cases { id } }'}
        with patch.object(api, '_workspace_generated_query') as generate:
            with self.assertRaisesRegex(ValueError, 'no supplied document'):
                api.workspace_service_save('SNAPSHOT', json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'query direction'):
                api.workspace_service_save('SNAPSHOT', json.dumps({**payload, 'query': '', 'operation_kind': 'mutation'}))
        generate.assert_not_called()
        self.assertEqual(self.frappe.new_doc_calls, [])

    def test_personal_task_query_is_generated_from_current_actor_and_revision(self):
        self.frappe.local = SimpleNamespace(site='test.local')
        self.frappe.session = SimpleNamespace(user='owner@example.invalid')
        payload = {'name': 'Task draft service', 'operation_kind': 'query', 'personal_mapping_generation': {
            'definition_id': 'task-def', 'generation': 1, 'draft_revision': 2,
            'active_revision': 3, 'reconciliation_revision': 0}}
        request = SimpleNamespace(document='query tasks { tasks { id title is_deleted } }', operation_name='tasks')
        storage = SimpleNamespace(materialize_native_task=unittest.mock.Mock(return_value=request))
        with patch('hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage', return_value=storage):
            result = api.workspace_service_save('SNAPSHOT', json.dumps(payload))
        storage.materialize_native_task.assert_called_once_with('task-def', 'owner@example.invalid', 1, 2, 3, 0,
                                                                snapshot_name='SNAPSHOT')
        self.assertEqual(result['query']['query'], request.document)
        self.assertEqual(self.frappe.service.status, 'Draft')

    def test_generated_query_uses_active_registry_and_materializer_before_draft_save(self):
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.services.mapping_contract_v1 import StoredSnapshot
        from hero_integration.services.mapping_registry_json import serialize_mapping_registry_v3
        from hero_integration.services.mapping_registry_model import (
            GraphQLReference, MappingDefinition, MappingDirection, MissingSemantics, MissingValuePolicy, TransformPolicy,
        )
        from hero_integration.services.mapping_registry_service import MappingRegistryService, MappingRegistryOperationError
        from hero_integration.services.mapping_registry_storage import InMemoryMappingRegistryStorage
        from hero_integration.services.schema_registry import build_schema_registry
        from hero_integration.services.frappe_field_registry import FrappeFieldReference
        from hero_integration.tests.mapping_registry.test_mapping_registry_validation import fields

        scalar = lambda name: {'kind': 'SCALAR', 'name': name}
        schema = {'__schema': {'queryType': {'name': 'Query'}, 'mutationType': {'name': 'Mutation'}, 'types': [
            {'kind': 'OBJECT', 'name': 'Query', 'fields': [{'name': 'thing', 'args': [], 'type': {'kind': 'OBJECT', 'name': 'Thing'}}]},
            {'kind': 'OBJECT', 'name': 'Mutation', 'fields': []},
            {'kind': 'OBJECT', 'name': 'Thing', 'fields': [{'name': 'id', 'args': [], 'type': scalar('ID')},
                                                          {'name': 'title', 'args': [], 'type': scalar('String')}]},
            {'kind': 'SCALAR', 'name': 'ID'}, {'kind': 'SCALAR', 'name': 'String'}]}}
        fingerprint = analyze_snapshot(schema).fingerprint
        snapshot = SimpleNamespace(name='SNAPSHOT', snapshot_json=json.dumps(schema), schema_fingerprint=fingerprint,
                                   source_instance='source', analysis_status='Validated')
        stored = StoredSnapshot(snapshot.name, snapshot.snapshot_json, fingerprint, snapshot.source_instance)
        registry, target = build_schema_registry(stored), fields()
        ref = GraphQLReference.parse('hero.query.thing.title')
        field = next(item for item in registry.operation('QUERY', 'thing').output_fields if item.reference_id == ref.identity)
        definition = MappingDefinition('field-1', 'source', MappingDirection.HERO_TO_FRAPPE, ref, field.type_ref,
            FrappeFieldReference('example', 'Sample', 'value'), 'Data', TransformPolicy.IDENTITY,
            MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.OMIT), snapshot.name, fingerprint, 1)
        storage = InMemoryMappingRegistryStorage()
        service = MappingRegistryService(storage, registry, target)
        package = serialize_mapping_registry_v3(site_scope=target.site, registry_revision=0,
            provenance={'source_instance': 'source', 'snapshot_name': snapshot.name, 'schema_fingerprint': fingerprint},
            definitions=(definition,))
        service.import_complete_json(package, expected_revision=None)
        self.frappe.local = SimpleNamespace(site=target.site)
        payload = {'name': 'Generated', 'operation_kind': 'query', 'mapping_generation': {
            'registry_revision': 1, 'definition_ids': ['field-1'], 'operation': 'thing',
            'arguments': {}, 'identity_reference': 'hero.query.thing.id'}}
        with patch.object(api, '_validated_snapshot', return_value=(snapshot, schema)), \
             patch('hero_integration.services.frappe_field_registry.build_frappe_field_registry', return_value=target), \
             patch('hero_integration.services.mapping_registry_storage.FrappeMappingRegistryStorage', return_value=storage):
            result = api.workspace_service_save('SNAPSHOT', json.dumps(payload))
            self.assertIn('title', result['query']['query'])
            self.assertIn('id', result['query']['query'])
            self.assertEqual(self.frappe.service.status, 'Draft')
            payload['mapping_generation']['registry_revision'] = 2
            with self.assertRaises(MappingRegistryOperationError):
                api.workspace_service_save('SNAPSHOT', json.dumps(payload))
            self.assertEqual(self.frappe.service.save_calls, 1)
