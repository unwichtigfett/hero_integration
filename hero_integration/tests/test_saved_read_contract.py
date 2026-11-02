from __future__ import annotations

import json
import sys
from dataclasses import asdict
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services.read_contract import ReadContractError, from_documents
from hero_integration.sources.hero import CUSTOMER_CONTRACT_V1, customer_query, GraphQLServiceSource


class TestSavedReadContract(unittest.TestCase):
    def setUp(self):
        # Synthetic metadata boundary: ordinary legacy rules are unscoped.
        fake = SimpleNamespace(db=SimpleNamespace(has_column=lambda *_: False),
            get_all=lambda _doctype, filters, **_kwargs: [{'definition_id':filters['definition_id'], 'personal_graphql_mapping':'',
                'status':'Active', 'operation':'HeroCustomers', 'schema_snapshot':'SNAP', 'schema_fingerprint':'fp'}])
        patcher = patch.dict(sys.modules, {'frappe':fake})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.snapshot = SimpleNamespace(name="SNAP", schema_fingerprint="fp", source_instance="synthetic")
        self.mapping = SimpleNamespace(
            name="MAP", status="Active", direction="QUERY_TO_FRAPPE", operation_kind="QUERY", source_instance="synthetic", schema_snapshot="SNAP", schema_fingerprint="fp", mapping_revision=3,
            mapping_json=json.dumps({"target_operation": "HeroCustomers", "records_path": "contacts", "identity_path": "id", "mapping_definition_ids": ["customer-id"], "variables": {"first": "$page_size", "offset": "$offset", "orderBy": "id", "showDeleted": True}, "page_size": 2, "customer_contract_version": CUSTOMER_CONTRACT_V1}),
        )
        self.service = SimpleNamespace(
            name="SERVICE", status="Validated", schema_snapshot="SNAP", schema_fingerprint="fp", service_revision=7,
            read_mapping="MAP", execution_status="Approved", approved_service_revision=7, approved_mapping_revision=3,
            query_document=customer_query(CUSTOMER_CONTRACT_V1),
        )

    def test_selected_service_document_and_variables_reach_the_transport(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({"contacts": [{"id": 41, "full_name": "Synthetic Ada"}]})
        page = GraphQLServiceSource(client, self.service, contract, "synthetic").customers()
        self.assertEqual(client.calls, [(self.service.query_document, {"first": 2, "offset": 0, "orderBy": "id", "showDeleted": True})])
        self.assertEqual(page.records[0]["_hero_source_instance"], "synthetic")
        # A short page may be a server page cap; only an empty page ends the scan.
        self.assertEqual(page.next_offset, 1)

    def test_saved_reader_overwrites_response_source_annotation(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        for instance in ("default", "synthetic"):
            with self.subTest(instance=instance):
                client = _Client({"contacts": [{"id": 41, "_hero_source_instance": "other-source"}]})
                page = GraphQLServiceSource(client, self.service, contract, instance).customers()
                self.assertEqual(page.records[0]["_hero_source_instance"], instance)

    def test_non_read_only_service_is_rejected_with_translatable_contract_error(self):
        self.service.query_document = "mutation Change { change { id } }"
        with self.assertRaises(ReadContractError) as caught:
            from_documents(self.service, self.mapping, self.snapshot)
        source = "Only one HERO GraphQL query operation is allowed."
        self.assertEqual(str(caught.exception), source)
        self.assertEqual(caught.exception.translated(lambda text: "Nur eine Query ist erlaubt." if text == source else text),
                         "Nur eine Query ist erlaubt.")

    def test_unapproved_or_stale_contract_is_rejected_before_transport(self):
        self.service.execution_status = "Pending Approval"
        with self.assertRaisesRegex(ReadContractError, "explicit execution approval"):
            from_documents(self.service, self.mapping, self.snapshot)
        self.service.execution_status = "Approved"
        self.service.approved_mapping_revision = 2
        with self.assertRaisesRegex(ReadContractError, "mapping approval is stale"):
            from_documents(self.service, self.mapping, self.snapshot)

    def test_mapping_approval_revoked_during_read_discards_returned_page(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        calls = []

        def query(document, variables):
            calls.append((document, variables))
            self.service.approved_mapping_revision = 2
            return {"contacts": [{"id": 41, "full_name": "Synthetic Ada"}]}

        source = GraphQLServiceSource(SimpleNamespace(query=query), self.service, contract, "synthetic",
                                      revalidate=lambda: from_documents(self.service, self.mapping, self.snapshot))
        with self.assertRaisesRegex(ReadContractError, "mapping approval is stale"):
            source.customers()
        self.assertEqual(len(calls), 1)

    def test_mapping_approval_revoked_before_read_blocks_transport(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        self.service.approved_mapping_revision = 2
        client = _Client({"contacts": [{"id": 41}]})
        source = GraphQLServiceSource(client, self.service, contract, "synthetic",
                                      revalidate=lambda: from_documents(self.service, self.mapping, self.snapshot))
        with self.assertRaisesRegex(ReadContractError, "mapping approval is stale"):
            source.customers()
        self.assertEqual(client.calls, [])

    def test_other_source_is_rejected_before_transport(self):
        self.mapping.source_instance = 'other-source'
        with self.assertRaisesRegex(ReadContractError, 'snapshot source'):
            from_documents(self.service, self.mapping, self.snapshot)
        self.mapping.source_instance = ''
        self.snapshot.source_instance = ''
        with self.assertRaisesRegex(ReadContractError, 'snapshot source'):
            from_documents(self.service, self.mapping, self.snapshot)

    def test_missing_selected_identity_path_is_rejected_before_effect(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({"contacts": [{"full_name": "Synthetic Ada"}]})
        with self.assertRaisesRegex(ReadContractError, "absent"):
            GraphQLServiceSource(client, self.service, contract, "synthetic").customers()

    def test_unreviewed_synthetic_query_remains_rejected(self):
        self.service.query_document = "query SyntheticContacts { synthetic_contacts { id } }"
        with self.assertRaisesRegex(ReadContractError, "exact reviewed"):
            from_documents(self.service, self.mapping, self.snapshot)


class TestGeneratedTaskRead(unittest.TestCase):
    def setUp(self):
        from hero_integration import api  # Load with the real framework before replacing the DB boundary.
        from hero_integration.services.direct_mapping import MappingSpec
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.services.mapping_runtime import plan_from_definitions
        from hero_integration.services.read_contract import task_materialization
        scalar = lambda name: {'kind': 'SCALAR', 'name': name}
        schema = {'__schema': {'queryType': {'name': 'Query'}, 'types': [
            {'kind': 'OBJECT', 'name': 'Query', 'fields': [{'name': 'tasks', 'args': [
                {'name': 'first', 'type': scalar('Int')}, {'name': 'offset', 'type': scalar('Int')},
                {'name': 'orderBy', 'type': scalar('String')}],
                'type': {'kind': 'LIST', 'ofType': {'kind': 'OBJECT', 'name': 'Task'}}}]},
            {'kind': 'OBJECT', 'name': 'Task', 'fields': [
                {'name': 'id', 'args': [], 'type': scalar('Int')},
                {'name': 'title', 'args': [], 'type': scalar('String')},
                {'name': 'target_user_id', 'args': [], 'type': scalar('Int')},
                {'name': 'author_user_id', 'args': [], 'type': scalar('Int')},
                {'name': 'target_project_match_id', 'args': [], 'type': scalar('Int')},
                {'name': 'due_date', 'args': [], 'type': scalar('String')},
                {'name': 'is_deleted', 'args': [], 'type': scalar('Boolean')}]},
            *({'kind': 'SCALAR', 'name': name} for name in ('Int', 'String', 'Boolean'))]}}
        fingerprint = analyze_snapshot(schema).fingerprint
        self.snapshot = SimpleNamespace(name='SNAP', schema_fingerprint=fingerprint,
            snapshot_json=json.dumps(schema), source_instance='synthetic', analysis_status='Validated')
        specs = [MappingSpec('HeroTasks', 'id', 'HERO Integration Link', 'source_id', 'int',
                             'Source Identity', 'Positive Int To Text', definition_id='id'),
                 MappingSpec('HeroTasks', 'title', 'Task', 'subject', 'string|null',
                             'Record Name', 'Trim', definition_id='title'),
                 MappingSpec('HeroTasks', 'target_user_id', 'ToDo', 'allocated_to', 'int|null',
                             'Native Binding', 'Native Adapter', definition_id='user'),
                 MappingSpec('HeroTasks', 'target_project_match_id', 'Task', 'project', 'int|null',
                             'Native Binding', 'Native Adapter', definition_id='project'),
                 MappingSpec('HeroTasks', 'due_date', 'Task', 'custom_hero_due_date', 'string|null',
                             'Native Binding', 'Native Adapter', definition_id='due')]
        self.plan = plan_from_definitions('tasks', specs, target_operation='HeroTasks', native_task=True)
        config = {'target_operation': 'HeroTasks', 'records_path': 'tasks', 'identity_path': 'id',
                  'mapping_definition_ids': ['id', 'title', 'user', 'project', 'due'], 'variables': {'first': '$page_size', 'offset': '$offset'},
                  'page_size': 2, 'native_task_onboarding': {'confirmed': True, 'source_instance': 'synthetic', 'source_type': 'Task'}}
        request, config['generated_task_seal'] = task_materialization(self.snapshot, config, self.plan)
        self.mapping = SimpleNamespace(name='MAP', status='Active', schema_snapshot='SNAP', schema_fingerprint=fingerprint,
            mapping_revision=3, mapping_json=json.dumps(config), operation_name='tasks', operation_kind='QUERY',
            direction='QUERY_TO_FRAPPE', source_instance='synthetic')
        self.service = SimpleNamespace(name='SERVICE', operation_name='tasks', status='Validated', schema_snapshot='SNAP',
            schema_fingerprint=fingerprint, service_revision=7, read_mapping='MAP', execution_status='Approved',
            approved_service_revision=7, approved_mapping_revision=3, query_document=request.document)
        from hero_integration.services import read_contract
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
        rows = {spec.definition_id: {**asdict(spec), 'personal_graphql_mapping': 'MAP',
                    'schema_snapshot': 'SNAP', 'schema_fingerprint': fingerprint, 'status': 'Active'} for spec in specs}
        db = SimpleNamespace(sql=lambda query, params, **kwargs: [rows[params[0]]] if params[0] in rows else [])
        fake_frappe = SimpleNamespace(db=db, get_doc=lambda *args, **kwargs: self.mapping,
            get_all=lambda doctype, **kw: [{key: row.get(key) for key in kw.get('fields', row)}
                                          for row in rows.values() if row['status'] == 'Active'])
        self.rows, self.fake_frappe = rows, fake_frappe
        metadata = SimpleNamespace(ensure_write=lambda doctype: None, field=lambda doctype, field: SimpleNamespace(
            fieldtype='Datetime' if field == 'custom_hero_due_date' else 'Link' if field in {'allocated_to', 'project'} else 'Data',
            options='User' if field == 'allocated_to' else 'Project' if field == 'project' else '',
            read_only=1 if field == 'custom_hero_due_date' else 0))
        self.fingerprint = patch.dict(read_contract.PROJECTIONS['HeroTasks'], {'fingerprint': fingerprint})
        self.frappe_patch = patch.dict(sys.modules, {'frappe': fake_frappe})
        self.metadata_patch = patch.object(hero_frappe_mapping, '_FrappeMetadata', return_value=metadata)
        self.fingerprint.start(); self.frappe_patch.start(); self.metadata_patch.start()
        self.addCleanup(self.fingerprint.stop); self.addCleanup(self.frappe_patch.stop); self.addCleanup(self.metadata_patch.stop)

    def test_approved_generated_read_uses_client_and_real_decoder(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({'tasks': [{'id': 41, 'title': 'Synthetic Task', 'is_deleted': False,
                                    'target_user_id': 99, 'author_user_id': None,
                                    'target_project_match_id': None, 'due_date': None}]})
        source = GraphQLServiceSource(client, self.service, contract, 'synthetic',
            revalidate=lambda: from_documents(self.service, self.mapping, self.snapshot))
        page = source.projection(0)
        self.assertEqual(page.records[0]['title'], 'Synthetic Task')
        self.assertEqual(client.calls[0][0], contract.materialized_request.document)
        self.assertIn(2, client.calls[0][1].values())
        self.service.approved_mapping_revision = 2
        with self.assertRaisesRegex(ReadContractError, 'mapping approval is stale'):
            source.projection(0)
        self.assertEqual(len(client.calls), 1)

    def test_saved_personal_rules_generate_the_same_document_as_published_active_rules(self):
        from hero_integration.services.mapping_contract_v1 import QueryAdapter, SnapshotAdapter, StoredSnapshot
        content = json.loads(self.mapping.mapping_json)
        content['field_rules'] = {}
        for spec in self.plan.definitions:
            rule = asdict(spec)
            rule.pop('operation'); rule.pop('definition_id')
            content['field_rules'][spec.definition_id] = rule
        snapshot = SnapshotAdapter().adapt(StoredSnapshot(self.snapshot.name, self.snapshot.snapshot_json,
            self.snapshot.schema_fingerprint, self.snapshot.source_instance, self.snapshot.analysis_status))
        personal_request = QueryAdapter().materialize_personal_task(snapshot, content)
        active_request = from_documents(self.service, self.mapping, self.snapshot).materialized_request
        self.assertEqual(personal_request.document, active_request.document)
        self.assertEqual(personal_request.request_fingerprint, active_request.request_fingerprint)

    def test_generated_read_fails_on_tampering_and_missing_decoded_field(self):
        from hero_integration.graphql_protocol.graphql_result_decoder import GraphQLResultDecodeError
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({'tasks': [{'id': 41, 'is_deleted': False, 'target_user_id': 99, 'author_user_id': None,
                                    'target_project_match_id': None, 'due_date': None}]})
        source = GraphQLServiceSource(client, self.service, contract, 'synthetic',
            revalidate=lambda: from_documents(self.service, self.mapping, self.snapshot))
        with self.assertRaises(GraphQLResultDecodeError):
            source.projection(0)
        self.service.query_document += ' '
        with self.assertRaisesRegex(ReadContractError, 'plan or approved document changed'):
            from_documents(self.service, self.mapping, self.snapshot)

    def test_generated_read_rejects_revoked_approval_and_changed_rule(self):
        self.service.execution_status = 'Pending Approval'
        with self.assertRaisesRegex(ReadContractError, 'explicit execution approval'):
            from_documents(self.service, self.mapping, self.snapshot)
        self.service.execution_status = 'Approved'
        self.mapping.source_instance = 'other'
        with self.assertRaisesRegex(ReadContractError, 'snapshot source'):
            from_documents(self.service, self.mapping, self.snapshot)
        self.mapping.source_instance = 'synthetic'
        self.rows['title']['transformation'] = 'Identity'
        with self.assertRaisesRegex(ReadContractError, 'cannot be reconstructed'):
            from_documents(self.service, self.mapping, self.snapshot)

    def test_generated_read_without_immediate_revalidation_cannot_call_client(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({'tasks': []})
        with self.assertRaisesRegex(ReadContractError, 'approval changed'):
            GraphQLServiceSource(client, self.service, contract, 'synthetic').projection(0)
        self.assertEqual(client.calls, [])

    def test_existing_approval_api_requires_manager_and_approves_generated_mapping(self):
        from hero_integration import api
        self.service.read_mapping = ''
        self.service.execution_status = 'Pending Approval'
        self.service.approved_service_revision = 0
        self.service.approved_mapping_revision = 0
        saves = []
        def save():
            saves.append(self.service.read_mapping)
            if len(saves) == 1:
                self.service.execution_status = 'Pending Approval'
                self.service.approved_service_revision = self.service.approved_mapping_revision = 0
        self.service.save = save
        self.fake_frappe.only_for = lambda role: None
        self.fake_frappe.get_doc = lambda doctype, name, **kwargs: {
            'HERO GraphQL Service': self.service, 'HERO GraphQL Mapping': self.mapping,
            'HERO GraphQL Schema Snapshot': self.snapshot}[doctype]
        self.fake_frappe.get_all = lambda *args, **kwargs: [
            {key: value for key, value in row.items() if key != 'status'} for row in self.rows.values()]
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping
        with patch.object(api, 'frappe', self.fake_frappe), patch.object(hero_graphql_mapping, 'lock_exchange_context'):
            result = api.approve_read_service('SERVICE', 'MAP', 7, 3)
            self.assertEqual(result['status'], 'Approved')
            self.assertEqual(len(saves), 2)
            self.assertIsNotNone(from_documents(self.service, self.mapping, self.snapshot).materialized_request)
            self.fake_frappe.only_for = lambda role: (_ for _ in ()).throw(PermissionError('denied'))
            with self.assertRaises(PermissionError):
                api.approve_read_service('SERVICE', 'MAP', 7, 3)

    def test_real_executor_decoder_mapping_and_task_adapter_replay_offline(self):
        from hero_integration.services import migration
        from hero_integration.client.graphql import HeroEndpoint, HeroGraphQLClient
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping

        records = {'task': None, 'link': None}
        class Document:
            def __init__(self, data):
                self.__dict__.update(data)
            def get(self, field): return getattr(self, field, None)
            def set(self, field, value): setattr(self, field, value)
            def insert(self):
                if self.doctype == 'Task':
                    self.name = 'TASK-1'; records['task'] = self
                else:
                    self.name = 'LINK-1'; records['link'] = self
                return self
            def save(self): return self
        class FakeDB:
            def sql(_, query, values=(), **kwargs):
                if 'tabHERO Frappe Mapping' in query:
                    return [self.rows[identity] for identity in values if identity in self.rows]
                if 'tabHERO Integration Link' in query:
                    return [SimpleNamespace(name='LINK-1')] if records['link'] else []
                return []
            def exists(_, doctype, name):
                if doctype == 'HERO Integration Link' and isinstance(name, dict) and name.get('source_type') == 'project':
                    return 'PROJECT-LINK'
                return doctype == 'Project' or (doctype == 'Task' and records['task'] is not None)
            def get_single_value(_, doctype, field): return 'Europe/Berlin'
            def savepoint(_, name): pass
            def rollback(_, **kwargs): pass
        def get_doc(doctype, name=None, **kwargs):
            if isinstance(doctype, dict): return Document(doctype)
            if doctype == 'HERO Integration Link' and name == 'PROJECT-LINK':
                return SimpleNamespace(target_doctype='Project', target_name='PROJECT-1')
            return {'HERO GraphQL Service': self.service, 'HERO GraphQL Mapping': self.mapping,
                    'HERO GraphQL Schema Snapshot': self.snapshot,
                    'HERO Integration Link': records['link'], 'Task': records['task'], 'Project': SimpleNamespace(name='PROJECT-1')}[doctype]
        self.fake_frappe.db = FakeDB()
        self.fake_frappe.get_doc = get_doc
        self.fake_frappe.conf = SimpleNamespace(db_name='isolated-fake')
        self.fake_frappe.local = SimpleNamespace(site='test-site')
        self.fake_frappe.session = SimpleNamespace(user='Administrator')
        self.fake_frappe.only_for = lambda role: None
        self.fake_frappe.has_permission = lambda *args, **kwargs: True
        run = SimpleNamespace(graphql_service='SERVICE', graphql_mapping='MAP', service_revision=7,
            mapping_revision=3, source_instance='synthetic', mode='Execute', name='RUN', state='Running',
            resume_offset=0, resume_record_index=0, processed=0, created_records=0, conflict_records=0,
            skipped_records=0, save=lambda: None, reload=lambda: None)
        config = SimpleNamespace(endpoint=HeroEndpoint('https://example.invalid/graphql', 'example.invalid'),
                                 api_token='synthetic', timeout_seconds=5, source_instance='synthetic',
                                 source_binding_revision=1, source_binding_confirmed=True)
        run.source_binding_revision = 1
        from hero_integration.services.introspection import endpoint_fingerprint
        self.snapshot.endpoint_fingerprint = endpoint_fingerprint(config.endpoint.url, config.source_instance)
        self.fake_frappe.get_single = lambda _: SimpleNamespace(polling_rate_limit=10000)
        response = {'tasks': [{'id': 41, 'title': 'Synthetic Task', 'is_deleted': False,
                               'target_user_id': 99, 'author_user_id': None, 'target_project_match_id': 101,
                               'due_date': '2026-10-29T12:30:00+00:00'}]}
        with patch.object(migration, 'frappe', self.fake_frappe), \
             patch.object(migration, 'get_configuration', return_value=config), \
             patch.object(hero_frappe_mapping, '_lock_active_target'), \
             patch.object(hero_graphql_mapping, 'lock_exchange_context'), \
             patch.object(migration, 'now_datetime', return_value='synthetic-time'), \
             patch.object(HeroGraphQLClient, '_post', side_effect=[response, {'tasks': []}, response, {'tasks': []}]) as transport, \
             patch.object(migration, 'record_output') as output:
            migration.bind_run_checkpoint(run)
            source = migration._source_for_run(run, config)
            migration._process_projection(run, source)
            run.resume_offset = run.resume_record_index = 0
            migration._process_projection(run, source)
        events = [call.kwargs for call in output.call_args_list]
        self.assertEqual([event['result'] for event in events if event['action'] == 'hero_to_frappe'], ['CREATED', 'SKIPPED'])
        self.assertEqual(run.created_records, 1)
        self.assertEqual(records['task'].subject, 'Synthetic Task')
        self.assertEqual(records['task'].project, 'PROJECT-1')
        self.assertEqual(str(records['task'].custom_hero_due_date), '2026-10-29 13:30:00')
        self.assertTrue(any('PENDING_IDENTITY' in event.get('detail', '') for event in events))
        self.assertEqual(transport.call_count, 4)


class _Client:
    def __init__(self, data): self.data, self.calls = data, []
    def query(self, document, variables):
        self.calls.append((document, variables)); return self.data
