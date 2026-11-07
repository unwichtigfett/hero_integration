"""Employee import defaults, bindings, conflicts, atomic retries and Query scope."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from hero_integration.services import employee_migration as em
from hero_integration.tests import test_employee_mapping as mapping_tests
from hero_integration.tests.saved_reader_fixture import isolate_direct_materialization


class Doc(dict):
    def __getattr__(self, name):
        return self.get(name)

    def save(self, **kwargs):
        return self

    def insert(self, **kwargs):
        self['name'] = self.get('name') or self.get('email') or 'new-employee'
        return self


class TestEmployeeWrites(unittest.TestCase):
    def setUp(self):
        fixture = mapping_tests.TestEmployeeMapping(); fixture.setUp()
        self.row = fixture.record
        self.user = Doc(doctype='User', name='login@example.invalid', email='login@example.invalid', enabled=1)
        self.docs = {('User', self.user.name): self.user}
        self.links = {}
        self.new_docs = []
        self.db = SimpleNamespace(exists=lambda dt, name: (dt, name) in self.docs)
        self.fake = SimpleNamespace(db=self.db, get_all=self.get_all, get_doc=self.get_doc,
                                    utils=SimpleNamespace(now_datetime=lambda: 'now'))
        self.addCleanup(patch.stopall)
        patch.object(em, 'frappe', self.fake).start()
        patch.object(em.runtime, 'resolve_read', return_value=em.runtime.builtin_read('HeroEmployees')).start()
        patch.object(em, '_link', side_effect=lambda instance, kind, *args: self.links.get(kind)).start()
        self.write_link = patch.object(em, '_write_link').start()
        self.find_user = patch.object(em, '_existing_user', return_value=None).start()

    def get_all(self, dt, **kwargs):
        if dt == 'Employee':
            return [name for (kind, name) in self.docs if kind == 'Employee']
        return []

    def get_doc(self, dt, name=None, **kwargs):
        if isinstance(dt, dict):
            doc = Doc(dt); self.new_docs.append(doc)
            return doc
        return self.docs[(dt, name)]

    def run_import(self):
        return em.upsert(self.row, 'source-a', 'Synthetic Company', {'de'})

    def test_insert_reuses_native_fields_and_explicit_company_joining_date(self):
        self.assertEqual(self.run_import(), 'added')
        employee = self.new_docs[0]
        self.assertEqual(employee.company, 'Synthetic Company')
        self.assertEqual(employee.date_of_joining, '2026-01-01')
        self.assertIsNone(employee.user_id)
        self.assertTrue(employee.employee_number.startswith('HERO-'))
        self.assertEqual((employee.create_user_permission, employee.create_user_automatically), (0, 0))
        self.assertNotIn('date_of_birth', employee)
        self.assertEqual(self.user.phone, self.row['phone'])
        self.assertEqual(self.user.custom_hero_last_login, self.row['user']['last_login'])
        self.assertNotIn('roles', self.user)
        self.assertNotIn('last_login', self.user)
        self.assertEqual(self.write_link.call_count, 2)

    def test_partner_id_preserves_identity_when_source_email_changes(self):
        self.find_user.return_value = self.user.name
        self.row['user']['email'] = 'changed@example.invalid'
        self.assertEqual(self.run_import(), 'added')
        self.assertIsNone(self.new_docs[0].user_id)
        self.assertEqual(self.user.email, 'login@example.invalid')
        self.assertEqual(self.user.custom_hero_partner_id, str(self.row['id']))

    def test_different_stored_partner_id_blocks_adoption(self):
        self.user['custom_hero_partner_id'] = '999'
        with self.assertRaisesRegex(em.EmployeeMigrationBlocked, 'identities differ'):
            self.run_import()

    def test_missing_login_creates_active_user_with_only_employee_role(self):
        self.row['user']['email'] = 'new-login@example.invalid'
        self.assertEqual(self.run_import(), 'added')
        user = next(doc for doc in self.new_docs if doc.doctype == 'User')
        employee = next(doc for doc in self.new_docs if doc.doctype == 'Employee')
        self.assertEqual(user.enabled, 1)
        self.assertEqual(user.roles, [{'role':'Employee'}])
        self.assertEqual(user.send_welcome_email, 0)
        self.assertEqual(user.user_type, 'System User')
        self.assertIsNone(employee.user_id)
        self.assertEqual(user.custom_hero_role, 'partner')
        self.assertNotIn({'role':'partner'}, user.roles)

    def test_account_email_is_canonicalized_before_linking(self):
        self.row['user']['email'] = '  NEW-LOGIN@EXAMPLE.INVALID '
        self.assertEqual(self.run_import(), 'added')
        user = next(doc for doc in self.new_docs if doc.doctype == 'User')
        employee = next(doc for doc in self.new_docs if doc.doctype == 'Employee')
        self.assertEqual(user.email, 'new-login@example.invalid')
        self.assertIsNone(employee.user_id)

    def test_missing_login_email_blocks_account_creation(self):
        self.row['user']['email'] = None
        with self.assertRaisesRegex(em.EmployeeMigrationBlocked, 'login email'):
            self.run_import()
        self.assertEqual(self.new_docs, [])

    def test_preexisting_user_phone_is_not_overwritten_on_adoption(self):
        self.user['phone'] = 'local-phone'
        with self.assertRaisesRegex(em.EmployeeMigrationBlocked, 'values differ'):
            self.run_import()
        self.assertEqual(self.new_docs, [])
        self.assertEqual(self.user['phone'], 'local-phone')

    def test_inactive_hero_cannot_disable_a_local_login(self):
        self.row['status'] = 'inactive'
        with self.assertRaisesRegex(em.EmployeeMigrationBlocked, 'login access'):
            self.run_import()
        self.assertEqual(self.user.enabled, 1)
        self.assertEqual(self.new_docs, [])

    def test_employee_link_changed_locally_is_a_conflict_before_any_write(self):
        employee = Doc(doctype='Employee', name='employee-1', user_id=self.user.name, first_name='Locally changed')
        self.docs[('Employee', employee.name)] = employee
        self.links['partner'] = Doc(target_name=employee.name, target_field_hashes=json.dumps({'first_name':'Synthetic'}),
                                    target_payload_hash=em._digest({'first_name':'Synthetic'}))
        with self.assertRaisesRegex(em.EmployeeMigrationBlocked, 'changed in ERPNext'):
            self.run_import()
        self.write_link.assert_not_called()
        self.assertNotIn('phone', self.user)

    def test_admin_account_cannot_be_adopted(self):
        self.row['user']['email'] = 'Administrator'
        with self.assertRaises(em.EmployeeMigrationBlocked):
            self.run_import()
        self.assertEqual(self.new_docs, [])


class TestEmployeeRecordList(unittest.TestCase):
    def test_historical_queries_materialize_root_and_nested_arguments(self):
        from graphql import build_schema, introspection_from_schema, parse, validate
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
        for root, path in [('partners(show_deleted:Boolean):[Partner]', 'partners'),
                           ('company:Company', 'company.partners')]:
            schema = build_schema('type Query { ' + root + ' } type Company { partners(show_deleted:Boolean):[Partner] } '
                'type Partner { id:Int status:String user_id:Int user:User } type User { id:Int }')
            raw = introspection_from_schema(schema)
            validated = SnapshotAdapter().adapt(StoredSnapshot('snapshot', raw, analyze_snapshot(raw).fingerprint, 'source-a'))
            with self.subTest(path=path), patch.object(em.runtime, 'validated_snapshot', return_value=validated):
                request = em._historical_request(object(), 'source-a', path)
                self.assertEqual(validate(schema, parse(request.document)), [])
                self.assertIn(True, request.variables.values())
                self.assertIn('show_deleted: $', request.document)
                self.assertNotIn('show_deleted: true', request.document)

    def test_project_run_revalidates_employee_reader_before_and_after_historical_lookup(self):
        from hero_integration.services import migration
        from hero_integration.services import direct_runtime
        read = SimpleNamespace(operation='HeroEmployees', origin='service', mapping='map', mapping_version='v1',
                               document='query', records_path='company.partners', variables={}, page_size=1)
        client = SimpleNamespace(query=Mock(return_value={'company': {'partners': [
            {'id': 1, 'status': 'deleted', 'user_id': 101, 'user': {'id': 101}},
            {'id': 2, 'status': 'active', 'user_id': 102, 'user': {'id': 102}}]}}))
        contract = object()
        source = SimpleNamespace(source_instance='source-a', client=client, contract=contract,
                                 revalidate=Mock(return_value=contract))
        with patch.object(direct_runtime, 'approved_read', return_value=read), \
             patch.object(em, '_historical_request', return_value=SimpleNamespace(document='query', variables={})):
            result = migration._verified_historical_users(SimpleNamespace(action='Project Migration'), source)
        self.assertEqual(result, ['101'])
        self.assertEqual(source.revalidate.call_count, 2)
    def test_historical_ids_require_consistent_deleted_source_records(self):
        rows = [
            {'id': 1, 'status': 'deleted', 'user_id': 101, 'user': {'id': 101}},
            {'id': 2, 'status': 'deleted', 'user_id': 102, 'user': {'id': 103}},
            {'id': 3, 'status': 'deleted', 'user_id': 104, 'user': {'id': 104}},
            {'id': 4, 'status': 'active', 'user_id': 104, 'user': {'id': 104}},
        ]
        client = SimpleNamespace(query=Mock(return_value={'company': {'partners': rows}}))
        with patch.object(em, '_historical_request', return_value=SimpleNamespace(document='query', variables={'v1': True})):
            users, partners = em._historical_identities(client, 'company.partners',
                {'101', '102', '103', '104', '105'}, {'1', '2'}, read=object(), source_instance='source-a')
        self.assertEqual(users, ['101'])
        self.assertEqual(partners, ['1'])
        self.assertEqual(client.query.call_args.args[1], {'v1': True})

    def test_nested_and_direct_lists_and_empty_list(self):
        self.assertEqual(em._records({'partners':[{'id':2},{'id':1}]}, 'partners'), [{'id':1},{'id':2}])
        self.assertEqual(em._records({'company':{'partners':[]}}, 'company.partners'), [])

    def test_missing_duplicate_invalid_or_null_list_blocks(self):
        for value in [None, {}, [{'id':1},{'id':1}], [{'id':False}], [{'id':'1'}]]:
            with self.subTest(value=value), self.assertRaises(em.EmployeeMigrationBlocked):
                em._records({'partners': value}, 'partners')

class TestEmployeeResume(unittest.TestCase):
    def setUp(self):
        isolate_direct_materialization(self)
        self.stored = {}
        self.fake = SimpleNamespace(only_for=Mock(), get_all=lambda *a, **k: ['de'],
            db=SimpleNamespace(get_default=lambda key:None,
                               get_value=lambda dt, filters, field:self.stored.get(filters['defkey']),
                               set_default=lambda key,value:self.stored.__setitem__(key,value),
                               commit=Mock(), rollback=Mock()))
        self.client = SimpleNamespace(query=lambda *a: {'partners':[{'id':1},{'id':2}]})
        self.addCleanup(patch.stopall)
        patch.object(em, 'frappe', self.fake).start()
        patch.object(em.runtime, 'resolve_read', return_value=em.runtime.builtin_read('HeroEmployees')).start()
        patch.object(em, 'preflight', return_value=('Company','snapshot','query','partners')).start()
        patch('hero_integration.services.deferred_references.require_storage').start()
        patch.object(em, 'remember_relationships').start()
        patch.object(em, '_historical_request', return_value=SimpleNamespace(document='query', variables={'v1': True})).start()

    def run_import(self, **kwargs):
        return em.sync_employees(self.client, source_instance='source', run_name='run-1', **kwargs)

    def test_deleted_related_users_are_reported_without_provisioning(self):
        self.client.query = Mock(side_effect=[
            {'partners': [{'id': 1, 'user_id': 10, 'user': {'id': 10}}]},
            {'partners': [{'id': 1, 'status': 'active', 'user_id': 10, 'user': {'id': 10}},
                          {'id': 2, 'status': 'deleted', 'user_id': 20, 'user': {'id': 20}}]},
        ])
        with patch.object(em, 'upsert', return_value='updated') as write:
            state = self.run_import(record_limit=1, cohort_user_ids=['10', '20', '30'], cohort_partner_ids=[])
        self.assertEqual(state['testing_historical_user_ids'], ['20'])
        self.assertEqual(state['testing_missing_user_ids'], ['30'])
        self.assertEqual(write.call_count, 1)
        self.assertEqual(write.call_args.args[0]['id'], 1)

    def test_unavailable_historical_lookup_keeps_users_unresolved(self):
        from hero_integration.client.graphql import HeroClientError
        self.client.query = Mock(side_effect=[{'partners': [{'id': 1}]}, HeroClientError('unavailable')])
        with patch.object(em, 'upsert', return_value='updated'):
            state = self.run_import(record_limit=1, cohort_user_ids=['20'], cohort_partner_ids=[])
        self.assertEqual(state['testing_missing_user_ids'], ['20'])
        self.assertTrue(state['testing_historical_audit_unavailable'])

    def test_failure_rolls_back_and_is_retryable_without_recounting_successes(self):
        with patch.object(em, 'upsert', side_effect=['added',em.EmployeeMigrationBlocked('Review')]) as write:
            first = self.run_import()
        self.assertFalse(first['done'])
        self.assertEqual(first['done_ids'], [1])
        self.fake.db.rollback.assert_called_once()
        with patch.object(em, 'upsert', return_value='added') as write:
            final = self.run_import()
        self.assertTrue(final['done'])
        self.assertEqual(write.call_count, 1)
        self.assertEqual(final['counts']['added'], 2)
        self.assertEqual(final['counts']['conflict'], 0)
        self.assertNotIn('last_error', final)

    def test_progress_uses_run_longtext_and_keeps_legacy_checkpoint(self):
        legacy = {'counts': {'added': 1}, 'done_ids': [1], 'outcomes': {'1': 'added'}}
        self.stored[em.STATE_KEY.format('run-1')] = json.dumps(legacy)
        run_fields = {}
        self.fake.db.has_column = lambda doctype, field: field == 'employee_progress_json'
        self.fake.db.get_value = lambda doctype, name, field: (
            self.stored.get(name['defkey']) if doctype == 'DefaultValue' else run_fields.get(field))
        self.fake.db.set_value = lambda doctype, name, field, value, **kwargs: run_fields.__setitem__(field, value)
        with patch.object(em, 'upsert', return_value='added') as upsert:
            state = self.run_import()
        self.assertEqual(upsert.call_count, 1)
        self.assertEqual(state['done_ids'], [1, 2])
        self.assertEqual(json.loads(run_fields['employee_progress_json'])['done_ids'], [1, 2])
        self.assertEqual(json.loads(self.stored[em.STATE_KEY.format('run-1')]), legacy)

    def test_cancelled_slice_performs_no_writes(self):
        with patch.object(em, 'upsert') as write:
            state = self.run_import(should_stop=lambda: True)
        self.assertTrue(state['stopped'])
        write.assert_not_called()

    def test_source_binding_is_rechecked_before_each_record(self):
        verify = Mock(side_effect=em.EmployeeMigrationBlocked('Changed source'))
        with patch.object(em, 'upsert') as write, self.assertRaises(em.EmployeeMigrationBlocked):
            self.run_import(verify_source=verify)
        write.assert_not_called()

    def test_changed_inventory_blocks_resume(self):
        with patch.object(em, 'upsert', return_value='added'):
            self.run_import()
        self.client.query = lambda *a: {'partners':[{'id':1}]}
        with patch.object(em, 'upsert') as write, self.assertRaisesRegex(em.EmployeeMigrationBlocked, 'list changed'):
            self.run_import()
        write.assert_not_called()


class TestEmployeeAssignmentIdentity(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        self.read = replace(em.runtime.builtin_read('HeroEmployees'), mapping='approved-employees')
        self.mapping = SimpleNamespace(source_instance='source-a', status='Active', definition_id='employees', mapping_revision=2)
        self.rows = []
        self.fake = SimpleNamespace(local=SimpleNamespace(site='site.example'),
            get_all=Mock(side_effect=lambda *a, **kw:self.rows), get_doc=Mock(return_value=self.mapping))
        self.registry = Mock()
        for p in (patch.object(em, 'frappe', self.fake),
                  patch('hero_integration.services.binding_registry.BindingRegistryService', return_value=self.registry),
                  patch('hero_integration.services.binding_registry.FrappeBindingStorage'),
                  patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target')):
            p.start(); self.addCleanup(p.stop)

    def test_employee_produces_the_source_scoped_identity_used_by_task_assignment(self):
        self.assertTrue(em._ensure_user_object_binding(self.read, 'source-a', 22, 'employee@example.invalid'))
        self.registry.create_or_resolve_object_binding.assert_called_once_with(
            site_scope='site.example', source_instance='source-a', object_type='User', external_object_id='22',
            target_app='frappe', target_doctype='User', target_name='employee@example.invalid',
            mapping_definition_id='employees', mapping_revision=2)
        self.assertEqual(self.fake.get_all.call_args.kwargs['filters']['source_instance'], 'source-a')

    def test_unchanged_identity_is_reused_even_after_mapping_revision_changes(self):
        self.rows = [SimpleNamespace(name='binding', target_name='employee@example.invalid')]
        self.mapping.mapping_revision = 8
        self.assertFalse(em._ensure_user_object_binding(self.read, 'source-a', 22, 'employee@example.invalid'))
        self.registry.create_or_resolve_object_binding.assert_not_called()

    def test_ambiguous_or_different_binding_never_reassigns_a_person(self):
        for rows in ([SimpleNamespace(target_name='other@example.invalid')],
                     [SimpleNamespace(target_name='employee@example.invalid')]*2):
            self.rows = rows
            with self.assertRaises(em.EmployeeMigrationBlocked):
                em._ensure_user_object_binding(self.read, 'source-a', 22, 'employee@example.invalid')
        self.registry.create_or_resolve_object_binding.assert_not_called()
