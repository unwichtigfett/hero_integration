"""Final transport gate with synthetic provider schema, isolated receipts and a fake HTTPS opener."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from graphql import build_schema, introspection_from_schema
from hero_integration.client import mutation_guard as guard
from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient, validate_endpoint
from hero_integration.client.graphql_mutation import HeroMutationClient
from hero_integration.services import automatic_writeback
from hero_integration.services.binding_registry import FrappeBindingStorage
from hero_integration.services.graphql_schema_cache import compiled_schema
from hero_integration.tests.test_graphql_client_budget import _Opener


class TestMutationTransportGuard(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.endpoint = validate_endpoint('https://synthetic.invalid/graphql', {'synthetic.invalid'})
        self.client = HeroMutationClient(self.endpoint, lambda: self.fail('stale caller credential was used'))
        self.opener = _Opener({'data': {'example': {'id': 1}}})
        self.enterContext(patch('hero_integration.client.graphql.urllib.request.build_opener', return_value=self.opener))
        schema = build_schema('''
            type Query { ok:String }
            type Contact { id:Int }
            type Project { id:Int }
            type ProjectMatch { id:Int }
            input Input { id:Int!, notes:String, other_id:Int, related:Input, tags:[String!] }
            type Mutation {
                changeContact(record:Input!):Contact changeProject(record:Input!):Project
                changeMatch(record:Input!):ProjectMatch bulk(records:[Input!]!):[Contact]
            }
        ''')
        raw = json.dumps(introspection_from_schema(schema))
        self.hash = compiled_schema(raw).fingerprint
        self.snapshot = NS(snapshot_json=raw, schema_fingerprint=self.hash, source_instance='sandbox-a',
                           endpoint_fingerprint='endpoint-a', analysis_status='Validated', execution_contract_status='')
        self.audit = NS(correlation_id='synthetic-correlation')
        self.binding = NS(source_instance='sandbox-a', binding_revision=3, endpoint_fingerprint='endpoint-a')
        self.enterContext(patch('hero_integration.services.writeback_source._current',
                                return_value=(NS(endpoint=self.endpoint, api_token='safe-token'), self.binding)))
        self.env = NS(require_environment=Mock(return_value='sandbox-uuid'), environment_generation=Mock(return_value='epoch-a'))
        self.db = NS(exists=Mock(return_value=True))
        self.frappe = NS(request=NS(method='POST', path='/api/method/hero_integration.api.execute_graphql_service'),
                         session=NS(user='operator'), local=NS(site='sandbox-a.test'), only_for=Mock(), db=self.db)
        self.enterContext(patch.dict(sys.modules, {'frappe': self.frappe, 'hero_integration.services.test_environment': self.env}))
        self.policy = json.loads(guard._POLICY.read_text())
        self.assertEqual(self.policy['targets'], {'Contact':7966175, 'Project':11955928})
        for profile in self.policy['reviewed_operations']:
            self.assertNotEqual(profile['schema'], self.hash)
        self.source = dict(environment='sandbox-uuid', generation='epoch-a', site='sandbox-a.test', source_instance='sandbox-a',
                           binding_revision=3, endpoint_fingerprint='endpoint-a', schema=self.hash)
        self.source_file = dict(self.source, targets=dict(self.policy['targets']))
        self.permit = {}
        self.enterContext(patch.object(guard, '_config', side_effect=lambda path: self.source_file if path == guard._SOURCE else self.permit))
        self.enterContext(patch.object(guard, '_RECEIPTS', self.directory))
        self.mounted = self.enterContext(patch.object(guard, '_mounted_receipts', return_value=True))
        self.enterContext(patch.object(guard.time, 'time', return_value=1000))
        self.policy_file = self.directory / 'policy.json'
        self.enterContext(patch.object(guard, '_POLICY', self.policy_file))
        self.policy_file.write_text(json.dumps(self.policy))

    def request(self, target='Contact', *, register=True):
        root = 'change'+target
        document = 'mutation Example($record:Input!) {' + root + '(record:$record){id}}'
        identity = self.policy['targets'].get(target, 11955928)
        variables = {'record': {'id': identity, 'notes': 'synthetic sample'}}
        self.permit = dict(self.source, actor='operator', target=target, id=str(identity), operation=root,
                           wire_sha256=guard.wire_digest(document, variables), issued_at=998, expires_at=1100, permit_id=uuid4().hex)
        if register:
            self.policy['reviewed_operations'].append(dict(schema=self.hash, target=target, root=root, argument='record',
                                                           input='Input', field='notes'))
            self.policy_file.write_text(json.dumps(self.policy))
        return document, variables

    def send(self, document, variables, *, confirmed=True):
        scope = guard.confirmed_service(self.audit, self.snapshot) if confirmed else nullcontext()
        with scope:
            return self.client.execute_once(document, variables)

    def test_default_candidate_blocks_both_allowed_ids_before_http(self):
        for target in ('Contact','Project'):
            with self.assertRaises(HeroClientError):
                self.send(*self.request(target, register=False))
        self.assertEqual(self.opener.calls, 0)

    def test_synthetic_reviewed_contacts_and_projects_each_require_the_exact_one_use_grant(self):
        self.opener.outcomes.append({'data':{'example':{'id':1}}})
        for target in ('Contact','Project'):
            document, values = self.request(target)
            with self.assertRaises(HeroClientError):
                self.send(document, values, confirmed=False)
            with self.assertRaises(HeroClientError):
                self.send(document, {'record': dict(values['record'], notes='other')})
            self.send(document, values)
            with self.assertRaises(HeroClientError):
                self.send(document, values)
        self.assertEqual(self.opener.calls, 2)

    def test_invalid_targets_mixed_ids_indirect_fields_and_batch_cannot_be_authorized(self):
        doc, values = self.request()
        cases = [(doc, {'record': p}) for p in (
            {'id': 123, 'notes':'x'}, {'id':11955928, 'notes':'x'},
            {'id':True, 'notes':'x'}, {'notes':'x'},
            {'id':7966175, 'other_id':11955928}, {'id':7966175, 'related':{'id':11955928}},
            {'id':7966175, 'tags':['other']}, {'id':7966175, 'notes':'x', 'other_id':11955928})]
        cases += [('mutation M($record:Input!){changeContact(record:$record){id} changeProject(record:$record){id}}', values),
                  ('mutation M($record:Input!){other:changeContact(record:$record){id}}', values),
                  ('mutation M($records:[Input!]!){bulk(records:$records){id}}', {'records':[values['record']]}),
                  ('mutation M{changeContact(record:{id:7966175,notes:"x"}){id}}', {}),
                  ('mutation M($record:Input!){...F} fragment F on Mutation { changeContact(record:$record){id} }', values),
                  ('mutation M($record:Input!){changeContact(record:$record){id}} mutation B{changeProject(record:{id:1}){id}}', values),
                  ('mutation M($record:Input!){changeMatch(record:$record){id}}', {'record':{'id':11955928,'notes':'x'}})]
        for document, variables in cases:
            # A matching payload hash by itself must never authorize the operation.
            self.permit['wire_sha256'] = guard.wire_digest(document, variables)
            with self.subTest(document=document, variables=variables), self.assertRaises(HeroClientError):
                self.send(document, variables)
        self.assertEqual(self.opener.calls, 0)

    def test_stale_source_generation_and_schema_remain_blocked(self):
        doc, variables = self.request()
        for key, value in (('environment','other'), ('generation','before-restore'), ('site','other-site'),
                           ('schema','stale'), ('binding_revision',2), ('source_instance','source-b'),
                           ('endpoint_fingerprint','wrong-endpoint'), ('targets', {'Contact': 2,'Project':11955928})):
            with self.subTest(key=key), patch.dict(self.source_file, {key:value}), self.assertRaises(HeroClientError):
                self.send(doc, variables)
        for key, value in (('generation','old'),('actor','other'),('expires_at',999),('target','Project'),('operation','other')):
            with self.subTest(key=key), patch.dict(self.permit, {key:value}), self.assertRaises(HeroClientError):
                self.send(doc, variables)
        self.env.environment_generation.return_value='after-restore'
        with self.assertRaises(HeroClientError):
            self.send(doc, variables)
        self.db.exists.return_value=False
        self.env.environment_generation.return_value='epoch-a'
        with self.assertRaises(HeroClientError):
            self.send(doc, variables)
        self.assertEqual(self.opener.calls, 0)

    def test_reset_and_other_rpc_handlers_cannot_spend_even_a_matching_grant(self):
        doc, values = self.request()
        self.frappe.request.path = "/api/method/hero_integration.api.start_migration"
        with self.assertRaises(HeroClientError):
            self.send(doc, values)
        self.frappe.request.path = "/api/method/hero_integration.api.execute_graphql_service"
        self.env.require_environment.side_effect = ValueError("Reset pending")
        with self.assertRaises(HeroClientError):
            self.send(doc, values)
        self.assertEqual(self.opener.calls, 0)
        self.assertFalse((self.directory / self.permit["permit_id"]).exists())

    def worker_case(self):
        from hero_integration.services.writeback_source import SourceContext
        import hero_integration

        site = self.frappe.local.site
        self.frappe.session.user = 'Administrator'
        self.frappe.request = None
        self.frappe.conf = {'allow_tests': True}
        self.frappe.get_single = lambda _: NS(testing_enabled=1)
        self.env.STATE = self.directory / 'state'
        source = SourceContext('sandbox-a', 3, 'f' * 64)
        config = NS(endpoint=self.endpoint, api_token='test-token')
        self.enterContext(patch('hero_integration.services.writeback_source._current', return_value=(config, source)))
        selection = NS(site_scope=site, source_instance=source.source_instance,
                       schema_fingerprint=self.hash, schema_snapshot='snapshot')
        plan = NS(selection=selection, generation=2)
        parent = NS(identity_key='parent', site_scope=site, source_instance=source.source_instance,
                    target_doctype='Task', target_name='TASK-test', external_object_id='123',
                    lifecycle='VALID', reconciliation='')
        field = NS(identity_key='field', object_binding_key='parent', state='LOCAL_CHANGED', acknowledged_version=2,
                   acknowledged_source_fingerprint='before', acknowledged_target_fingerprint='before', reconciliation='')
        event = NS(name='event', correlation_id='parent', target_name='TASK-test', target_doctype='Task',
                   technical_action='frappe_change_observed')
        self.enterContext(patch.object(automatic_writeback, 'current_plan', return_value=plan))
        self.enterContext(patch.object(FrappeBindingStorage, 'object', return_value=parent))
        self.enterContext(patch.object(FrappeBindingStorage, 'field', return_value=field))
        self.enterContext(patch.object(hero_integration, 'api', NS(_event_action_key=lambda *_: 'action'), create=True))
        for var, value in ((automatic_writeback._automatic_plan, plan),
                           (automatic_writeback._dispatch_event, (event, 'mapping'))):
            token = var.set(value)
            self.addCleanup(var.reset, token)
        self.audit.operation_name = 'update_task'
        self.audit.bound_object_key = 'object'
        self.audit.intent_json = json.dumps({'version':2, 'mapping':'mapping', 'mapping_revision':4,
                                            'object':'object', 'snapshot':self.hash})
        self.source.update(endpoint_fingerprint=source.endpoint_fingerprint)
        self.source_file = dict(self.source, targets={'Task':123})
        request = NS(document='mutation M($task:TaskInput){update_task(task:$task){id}}',
                     variables={'task':{'id':123,'comment':'updated'}})
        self.permit = dict(self.source, actor='Administrator', target='Task', id='123', operation='update_task',
                           wire_sha256=guard.wire_digest(request.document, request.variables),
                           issued_at=998, expires_at=1100, permit_id=uuid4().hex)
        return request, source, parent, field

    def test_regular_worker_spends_campaign_permit_for_bound_update_without_http_rpc(self):
        request, source, parent, field = self.worker_case()
        with guard.worker_update(request, self.audit, source, lambda: (parent, [field])):
            self.client.execute_once(request.document, request.variables)
            with self.assertRaises(HeroClientError):
                self.client.execute_once(request.document, request.variables)
        self.assertEqual(self.opener.calls, 1)

    def test_bound_worker_outside_test_campaign_uses_runtime_authority(self):
        request, source, parent, field = self.worker_case()
        self.frappe.conf.clear()
        self.frappe.get_single = lambda _: NS(testing_enabled=0)
        with patch.object(guard, '_SOURCE', self.directory / 'absent-source'), \
                patch.object(guard, '_PERMIT', self.directory / 'absent-permit'), \
                patch.object(guard, '_config', side_effect=AssertionError('Unexpected test campaign')), \
                guard.worker_update(request, self.audit, source, lambda: (parent, [field])):
            self.client.execute_once(request.document, request.variables)
        self.assertEqual(self.opener.calls, 1)

    def test_worker_rejects_missing_binding_stale_authority_and_changed_payload(self):
        request, source, parent, field = self.worker_case()
        def attempt(bindings, variables):
            with self.assertRaises(HeroClientError), guard.worker_update(
                    request, self.audit, source, lambda: bindings):
                self.client.execute_once(request.document, variables)
        attempt((None, [field]), request.variables)
        attempt((parent, [None]), request.variables)
        attempt((parent, [field]), {'task':{'id':123,'comment':'unreviewed'}})
        original = json.loads(self.audit.intent_json)
        with patch.object(self.audit, 'intent_json', json.dumps({**original, 'version':3, 'mode':'CREATE'})):
            attempt((parent, [field]), request.variables)
        with patch.dict(self.permit, operation='create_task'):
            attempt((parent, [field]), request.variables)
        with patch('hero_integration.services.automatic_writeback.current_plan', return_value=None):
            attempt((parent, [field]), request.variables)
        self.db.exists.side_effect = lambda dt, filters: not (dt == 'HERO GraphQL Mapping'
                                                               and filters['mapping_revision'] == 4)
        attempt((parent, [field]), request.variables)
        self.assertEqual(self.opener.calls, 0)
        self.assertFalse((self.directory / self.permit['permit_id']).exists())

    def test_parallel_senders_can_spend_one_permit_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        doc, values = self.request()
        def attempt():
            barrier.wait(timeout=3)
            try:
                self.send(doc, values)
                return "sent"
            except HeroClientError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual([future.result() for future in (pool.submit(attempt), pool.submit(attempt))],
                                  ["sent", "MUTATION_DENIED"])
        self.assertEqual(self.opener.calls, 1)

    def test_missing_persistent_spent_permit_store_blocks_an_approved_request(self):
        doc, values = self.request()
        self.mounted.return_value = False
        with self.assertRaises(HeroClientError):
            self.send(doc, values)
        self.assertEqual(self.opener.calls, 0)

    def test_unknown_transport_outcome_consumes_the_grant(self):
        doc, values = self.request()
        self.opener.outcomes = [TimeoutError('synthetic timeout'), {'data': {}}]
        with self.assertRaises(HeroClientError) as error:
            self.send(doc, values)
        self.assertEqual(error.exception.code, 'TRANSPORT')
        with self.assertRaises(HeroClientError) as replay:
            self.send(doc, values)
        self.assertEqual(replay.exception.code, 'MUTATION_DENIED')
        self.assertEqual(self.opener.calls, 1)

    def test_migration_read_transport_and_raw_post_have_no_mutation_authority(self):
        doc, values = self.request()
        client = HeroGraphQLClient(self.endpoint, Mock(side_effect=AssertionError('read token requested')))
        for attempt in (lambda: client.query(doc, values), lambda: client._post(doc, values),
                        lambda: client._post(doc, values, mutation=True), lambda: self.send(doc, values, confirmed=False)):
            with self.assertRaises(HeroClientError):
                attempt()
        self.assertEqual(self.opener.calls, 0)
