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

    def setUp(self):
        self.run = NS(name='RUN', mode='Preview', mapping_revision=1)
        self.record = {'id': 7, 'target_user_id': 8, 'author_user_id': None}
        self.recipient = NS(name='USER-BINDING', target_name='user@example.invalid')
        self.policy = NS(name='POLICY', policy_revision=1)
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
            if doctype == 'HERO Task Assignment State': return self.state
            if doctype == 'ToDo': return next(x for x in self.todos if x.name == name)
            if doctype == 'HERO Object Binding': return NS(target_name='old@example.invalid', binding_revision=1, lifecycle='VALID')
            raise AssertionError(doctype)
        db = NS(sql=sql, exists=lambda *args: True, get_value=lambda *args: self.enabled)
        self.fake = NS(db=db, local=NS(site='private.test'), session=NS(user='actor@example.invalid'),
                       get_doc=get_doc, has_permission=lambda *args, **kwargs: self.permission,
                       get_system_settings=lambda *args: self.sharing_disabled)
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
        plan = NS(definitions=[NS(source_path='target_user_id')])
        mapping = NS(mapping_json=json.dumps({'native_task_onboarding': {
            'confirmed': True, 'source_instance': 'source', 'source_type': 'Task'}}))
        original_get_doc = self.fake.get_doc
        def get_doc(doctype, name, **kwargs):
            if doctype == 'HERO GraphQL Service': return NS(schema_snapshot='SNAP')
            if doctype == 'HERO GraphQL Mapping': return mapping
            if doctype == 'HERO GraphQL Schema Snapshot': return NS()
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

if __name__ == '__main__': unittest.main()
