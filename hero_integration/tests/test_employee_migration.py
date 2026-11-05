"""Employee import defaults, bindings, conflicts, atomic retries and Query scope."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from hero_integration.services import employee_migration as em
from hero_integration.tests import test_employee_mapping as mapping_tests


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
        self.assertEqual(employee.user_id, 'login@example.invalid')
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
        self.assertEqual(self.new_docs[0].user_id, self.user.name)
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
        self.assertEqual(employee.user_id, user.name)
        self.assertEqual(user.custom_hero_role, 'partner')
        self.assertNotIn({'role':'partner'}, user.roles)

    def test_account_email_is_canonicalized_before_linking(self):
        self.row['user']['email'] = '  NEW-LOGIN@EXAMPLE.INVALID '
        self.assertEqual(self.run_import(), 'added')
        user = next(doc for doc in self.new_docs if doc.doctype == 'User')
        employee = next(doc for doc in self.new_docs if doc.doctype == 'Employee')
        self.assertEqual(user.email, 'new-login@example.invalid')
        self.assertEqual(employee.user_id, user.name)

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
    def test_nested_and_direct_lists_and_empty_list(self):
        self.assertEqual(em._records({'partners':[{'id':2},{'id':1}]}, 'partners'), [{'id':1},{'id':2}])
        self.assertEqual(em._records({'company':{'partners':[]}}, 'company.partners'), [])

    def test_missing_duplicate_invalid_or_null_list_blocks(self):
        for value in [None, {}, [{'id':1},{'id':1}], [{'id':False}], [{'id':'1'}]]:
            with self.subTest(value=value), self.assertRaises(em.EmployeeMigrationBlocked):
                em._records({'partners': value}, 'partners')

    def test_query_validates_supported_shape_without_inventing_pagination(self):
        from graphql import build_schema, introspection_from_schema
        user = 'type User { id:Int email:String role:String locale:String last_login:String created:String modified:String }'
        partner = 'type Partner { id:Int user_id:Int first_name:String last_name:String full_name:String email:String phone:String mobile:String role:String status:String account_type:String user:User }'
        for root, path in [('type Query { partners:[Partner] }','partners'),
                           ('type Query { company:Company } type Company { partners:[Partner] }','company.partners')]:
            schema = introspection_from_schema(build_schema(f'{root} {partner} {user}'))
            query, result = em.read_query(schema)
            self.assertEqual(result, path)
            self.assertIn('user { id email role locale last_login created modified }', query)
            self.assertNotIn('first:', query)


class TestEmployeeResume(unittest.TestCase):
    def setUp(self):
        self.stored = {}
        self.fake = SimpleNamespace(only_for=Mock(), get_all=lambda *a, **k: ['de'],
            db=SimpleNamespace(get_default=lambda key:self.stored.get(key),
                               set_default=lambda key,value:self.stored.__setitem__(key,value),
                               commit=Mock(), rollback=Mock()))
        self.client = SimpleNamespace(query=lambda *a: {'partners':[{'id':1},{'id':2}]})
        self.addCleanup(patch.stopall)
        patch.object(em, 'frappe', self.fake).start()
        patch.object(em.runtime, 'resolve_read', return_value=em.runtime.builtin_read('HeroEmployees')).start()
        patch.object(em, 'preflight', return_value=('Company','snapshot','query','partners')).start()

    def run_import(self, **kwargs):
        return em.sync_employees(self.client, source_instance='source', run_name='run-1', **kwargs)

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
