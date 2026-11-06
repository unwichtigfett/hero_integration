"""Offline admission checks; these do not establish Frappe persistence."""
import json
import importlib
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from hero_integration.services import task_assignment_policy, task_assignment_state


class TestTaskAssignmentPlanning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global migration
        cls.package = importlib.import_module('hero_integration.services')
        cls._missing = object()
        cls._saved_alias = getattr(cls.package, 'migration', cls._missing)
        names = ('frappe', 'frappe.utils', 'hero_integration.services.migration')
        cls.saved_modules = {name: sys.modules.get(name) for name in names}
        if cls.saved_modules['frappe'] is None:
            frappe = types.ModuleType('frappe')
            utils = types.ModuleType('frappe.utils')
            utils.now_datetime = lambda: None
            utils.get_datetime = lambda value: value
            sys.modules['frappe'] = frappe
            sys.modules['frappe.utils'] = utils
        sys.modules.pop('hero_integration.services.migration', None)
        migration = importlib.import_module('hero_integration.services.migration')

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.saved_modules.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if cls._saved_alias is cls._missing:
            if getattr(cls.package, 'migration', None) is migration:
                delattr(cls.package, 'migration')
        else:
            cls.package.migration = cls._saved_alias

    def setUp(self):
        self.run = NS(name='RUN', mode='Preview', mapping_revision=1)
        self.record = {'id': 7, 'target_user_id': 8, 'author_user_id': None}
        self.recipient = NS(name='USER-BINDING', target_name='user@example.invalid')
        self.policy = NS(name='POLICY', policy_revision=1, policy_json='{}')
        self.task = NS(name='TASK', status='Open')
        self.state = None
        self.todos = []
        self.enabled = True
        self.permission = True
        self.sharing_disabled = True
        def sql(query, *args, **kwargs):
            if 'tabHERO Object Binding' in query:
                return [self.recipient] if self.recipient else []
            if 'tabHERO Task Assignment State' in query:
                return [NS(name='STATE')] if self.state else []
            if 'tabToDo' in query:
                return [NS(name=x.name) for x in self.todos]
            raise AssertionError(query)
        def get_doc(doctype, name, **kwargs):
            if doctype == 'Task': return self.task
            if doctype == 'HERO Task Assignment Policy': return self.policy
            if doctype == 'HERO Task Assignment State': return self.state
            if doctype == 'ToDo': return next(x for x in self.todos if x.name == name)
            if doctype == 'HERO Object Binding': return NS(target_name='old@example.invalid', binding_revision=1, lifecycle='VALID')
            raise AssertionError(doctype)
        db = NS(sql=sql, exists=lambda *args: True, savepoint=lambda name: None,
                get_value=lambda doctype, name, field: self.enabled if field == 'enabled' else False)
        self.fake = NS(db=db, local=NS(site='private.test'), session=NS(user='actor@example.invalid'),
                       get_doc=get_doc, has_permission=lambda *args, **kwargs: self.permission,
                       get_system_settings=lambda *args: self.sharing_disabled,
                       get_cached_value=lambda *args: False)
        for obj, name, value in [
            (migration, 'frappe', self.fake),
            (migration, '_link', lambda *args: NS(name='LINK', target_doctype='Task', target_name='TASK')),
            (task_assignment_policy, 'approved_initial_policy', lambda *args: self.policy),
            (task_assignment_policy, 'task_has_policy_company', lambda *args: True),
            (task_assignment_state, 'assignment_boundary_available', lambda: True),
        ]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)

    def plan(self):
        return migration._plan_task_assignment(self.run, self.record, 'TASK', 'source')

    def managed(self, recipient='old@example.invalid', state='SYSTEM', revision=1):
        self.todos = [NS(name='TODO', status='Open', modified='v1', allocated_to=recipient,
                         assigned_by='actor@example.invalid')]
        self.state = NS(state=state, task_name='TASK', policy='POLICY', policy_revision=1,
                        mapping_revision=1, todo='TODO', todo_modified='v1', user_binding='OLD',
                        user_binding_revision=revision)

    def test_initial_is_planned_but_effects_require_policy(self):
        plan = self.plan()
        self.assertEqual((plan.action, plan.proposed, plan.recipient), ('BLOCKED', 'CREATE', self.recipient.target_name))
        self.assertIn('POLICY_REQUIRED', plan.detail)

    def test_source_author_does_not_block_an_approved_initial_assignment(self):
        self.policy.policy_json = json.dumps({'native_effects': {
            'date_rule': 'LOCAL_TODAY', 'allow_notifications': True, 'allow_follow': True}})
        self.record['author_user_id'] = 123
        self.assertEqual(self.plan().action, 'CREATE')
        del self.record['author_user_id']
        self.assertEqual(self.plan().action, 'CREATE')

    def test_opt_in_native_effects_and_guarded_follow(self):
        effects = {'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False}
        self.policy.policy_json = json.dumps({'native_effects': effects})
        self.assertIn('notification', self.plan().detail)
        effects['allow_notifications'] = True
        self.policy.policy_json = json.dumps({'native_effects': effects})
        self.assertEqual((self.plan().action, self.plan().date_rule), ('CREATE', 'LOCAL_TODAY'))
        self.fake.get_cached_value = lambda *args: True
        self.assertIn('following', self.plan().detail)
        effects['allow_follow'] = True
        self.policy.policy_json = json.dumps({'native_effects': effects})
        self.assertEqual(self.plan().action, 'CREATE')

    def test_silent_initial_assignment_does_not_require_sharing_or_project(self):
        self.policy.policy_json = json.dumps({'silent_initial': True, 'native_effects': {
            'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False}})
        self.sharing_disabled = False
        self.fake.get_cached_value = lambda *args: True
        with patch.object(task_assignment_policy, 'task_has_policy_company', return_value=False):
            self.assertEqual(self.plan().action, 'CREATE')
        self.permission = False
        self.assertIn('BLOCKED_PERMISSION', self.plan().detail)

    def test_silent_initial_preserves_local_assignments(self):
        self.policy.policy_json = json.dumps({'silent_initial': True, 'native_effects': {
            'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False}})
        self.todos = [NS(name='LOCAL', status='Open', allocated_to='other@example.invalid')]
        self.assertEqual(self.plan().action, 'BLOCKED')
        self.todos.append(NS(name='LOCAL2', status='Open', allocated_to=self.recipient.target_name))
        self.assertEqual(self.plan().action, 'KEEP')

    def test_silent_completed_assignment_is_closed_and_preserves_history(self):
        self.policy.policy_json = json.dumps({'silent_initial': True, 'native_effects': {
            'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False}})
        self.task.status = 'Completed'
        original = migration._link
        with patch.object(migration, '_link', side_effect=lambda kind, *args: None if kind == 'task_completed_assignment' else original(kind, *args)):
            self.assertEqual(self.plan().action, 'CREATE_COMPLETED')
            self.todos = [NS(name='LOCAL', status='Closed', allocated_to=self.recipient.target_name)]
            self.assertEqual(self.plan().action, 'KEEP')
            self.todos = [NS(name='LOCAL', status='Closed', allocated_to='other@example.invalid')]
            self.assertEqual(self.plan().action, 'BLOCKED')

    def test_missing_null_invalid_and_unknown_users(self):
        for record, expected in [({'id': 7}, 'NO_OP'), ({'id': 7, 'target_user_id': None}, 'NO_OP'),
                                 ({'id': 7, 'target_user_id': True}, 'PENDING_IDENTITY')]:
            self.record = record; self.assertEqual(self.plan().action, expected)
        self.record = {'id': 7, 'target_user_id': 8, 'author_user_id': None}
        self.enabled = False; self.assertEqual(self.plan().action, 'PENDING_IDENTITY')
        self.enabled = True; self.recipient = None; self.assertEqual(self.plan().action, 'PENDING_IDENTITY')

    def test_local_ownership_stale_revision_and_unordered_transfer(self):
        self.managed(state='LOCAL'); self.assertEqual(self.plan().action, 'CONFLICT')
        self.managed(revision=2); self.assertEqual(self.plan().action, 'CONFLICT')
        self.managed(); self.assertIn('PENDING_SOURCE_ORDER', self.plan().detail)
        self.state.user_binding = 'USER-BINDING'
        self.assertEqual(self.plan().action, 'KEEP')

    def test_conflicts_lifecycle_and_sharing(self):
        self.todos = [NS(name='A', status='Open'), NS(name='B', status='Open')]
        self.assertEqual(self.plan().action, 'CONFLICT')
        self.todos = []
        for status in ('Completed', 'Cancelled'):
            self.task.status = status; self.assertEqual(self.plan().action, 'BLOCKED')
        self.task.status = 'Open'; self.permission = False
        self.assertIn('BLOCKED_PERMISSION', self.plan().detail)
        self.permission = True; self.sharing_disabled = False
        self.assertIn('BLOCKED_PERMISSION', self.plan().detail)

    def test_preview_and_execute_share_read_only_decision(self):
        outputs = []
        with patch.object(migration, 'record_output', side_effect=lambda **kw: outputs.append(kw)):
            for mode in ('Preview', 'Execute'):
                self.run.mode = mode
                migration._record_task_assignment_pending(self.run, self.record, 'TASK', 'source')
        first, second = (json.loads(item['detail']) for item in outputs)
        self.assertEqual(first, second)
        self.assertEqual(first['decision'], 'BLOCKED')
        self.assertEqual(first['local_date'], 'POLICY_REQUIRED')

    def test_projection_invokes_same_admission_for_both_modes(self):
        from hero_integration.services import read_contract
        run = NS(name='RUN', mode='Preview', mapping_revision=1, service_revision=1,
                 graphql_service='SERVICE', graphql_mapping='MAP', source_instance='source',
                 resume_offset=0, resume_record_index=0, save=lambda: None)
        source = NS(source_instance='source', contract=NS(target_operation='HeroTasks'),
                    projection=lambda offset: NS(records=[self.record], next_offset=None))
        plan = NS(definitions=[NS(source_path='target_user_id')], target_operation='HeroTasks', operation='HeroTasks')
        mapping = NS(mapping_json=json.dumps({'native_task_onboarding': {
            'confirmed': True, 'source_instance': 'source', 'source_type': 'Task'}}))
        original_get_doc = self.fake.get_doc
        def get_doc(doctype, name, **kwargs):
            if doctype == 'HERO GraphQL Service': return NS(schema_snapshot='SNAP')
            if doctype == 'HERO GraphQL Mapping': return mapping
            if doctype == 'HERO GraphQL Schema Snapshot': return NS(source_instance='source')
            return original_get_doc(doctype, name, **kwargs)
        self.fake.get_doc = get_doc
        output = []
        with patch.object(migration, '_mapping_plan_for_run', return_value=plan), \
             patch.object(migration, '_cancel_if_requested'), \
             patch.object(migration, '_migrate_native_task', return_value=migration.Result('PREVIEW_OK', '', 'Task', 'TASK')), \
             patch.object(migration, '_record_result'), \
             patch.object(migration, 'record_output', side_effect=lambda **kw: output.append(kw)), \
             patch.object(read_contract, 'from_documents', return_value=NS(target_operation='HeroTasks', service_revision=1, mapping_revision=1)):
            for mode in ('Preview', 'Execute'):
                run.mode = mode
                run.resume_offset = run.resume_record_index = 0
                migration._process_projection(run, source)
        self.assertEqual(len(output), 2)
        self.assertEqual(json.loads(output[0]['detail']), json.loads(output[1]['detail']))
        self.assertEqual(json.loads(output[0]['detail'])['proposed'], 'CREATE')

    def test_authorized_execution_calls_native_adapter_and_records_provenance(self):
        self.policy.policy_json = json.dumps({'native_effects': {'date_rule': 'LOCAL_TODAY',
            'allow_notifications': True, 'allow_follow': False}})
        original_get_doc = self.fake.get_doc
        inserted, calls = [], []
        def get_doc(doctype, name=None, **kwargs):
            if isinstance(doctype, dict):
                return NS(insert=lambda: inserted.append(doctype))
            if doctype == 'HERO Object Binding':
                return NS(name='USER-BINDING', binding_revision=1)
            if doctype == 'HERO Task Assignment Policy': return self.policy
            return original_get_doc(doctype, name, **kwargs)
        self.fake.get_doc = get_doc
        self.fake.get_all = lambda *args, **kwargs: [NS(name=x.name, allocated_to=x.allocated_to) for x in self.todos]
        self.fake.db.rollback = lambda: self.fail('unexpected rollback')
        def add(args):
            calls.append(args)
            self.todos = [NS(name='TODO', status='Open', modified='v1', allocated_to=args['assign_to'][0],
                assigned_by='actor@example.invalid', reference_type='Task', reference_name='TASK', date=args['date'])]
        form = types.ModuleType('frappe.desk.form'); form.assign_to = NS(add=add)
        desk = types.ModuleType('frappe.desk'); desk.form = form
        utils = sys.modules['frappe.utils']
        with patch.dict(sys.modules, {'frappe.desk': desk, 'frappe.desk.form': form}), \
             patch.object(utils, 'nowdate', return_value='2026-01-02', create=True):
            self.run.mode = 'Execute'
            plan = self.plan()
            self.assertEqual(migration._execute_initial_task_assignment(self.run, self.record, 'TASK', 'source', plan), 'TODO')
        self.assertEqual(calls, [{'doctype': 'Task', 'name': 'TASK', 'assign_to': [self.recipient.target_name], 'date': '2026-01-02'}])
        self.assertEqual((inserted[0]['todo'], inserted[0]['state'], inserted[0]['user_binding']), ('TODO', 'SYSTEM', 'USER-BINDING'))

    def test_native_failure_rolls_back_entire_transaction(self):
        self.policy.policy_json = json.dumps({'native_effects': {'date_rule': 'LOCAL_TODAY',
            'allow_notifications': True, 'allow_follow': False}})
        rolled_back = []
        self.fake.db.rollback = lambda: rolled_back.append(True)
        def fail(args): raise RuntimeError('native failure')
        form = types.ModuleType('frappe.desk.form'); form.assign_to = NS(add=fail)
        desk = types.ModuleType('frappe.desk'); desk.form = form
        with patch.dict(sys.modules, {'frappe.desk': desk, 'frappe.desk.form': form}), \
             patch.object(sys.modules['frappe.utils'], 'nowdate', return_value='2026-01-02', create=True):
            with self.assertRaisesRegex(RuntimeError, 'native failure'):
                migration._execute_initial_task_assignment(self.run, self.record, 'TASK', 'source', self.plan())
        self.assertEqual(rolled_back, [True])

if __name__ == '__main__': unittest.main()
