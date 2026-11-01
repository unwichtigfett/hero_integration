"""Offline boundaries for native assignment hooks; no Frappe site is used."""
import asyncio
import sys
import types
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import task_assignment_state as state


class TestAssignmentHookScope(unittest.TestCase):
    def test_nested_managed_scope_restores_prior_state_after_failure(self):
        self.assertFalse(state.managed_assignment_active())
        with state.managed_assignment():
            self.assertTrue(state.managed_assignment_active())
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                with state.managed_assignment():
                    self.assertTrue(state.managed_assignment_active())
                    raise RuntimeError("synthetic failure")
            self.assertTrue(state.managed_assignment_active())
        self.assertFalse(state.managed_assignment_active())

    def test_managed_scope_is_isolated_between_concurrent_tasks(self):
        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()

            async def managed():
                with state.managed_assignment():
                    entered.set()
                    await release.wait()
                    return state.managed_assignment_active()

            async def unrelated():
                await entered.wait()
                active = state.managed_assignment_active()
                release.set()
                return active

            return await asyncio.gather(managed(), unrelated())

        self.assertEqual(asyncio.run(exercise()), [True, False])
        self.assertFalse(state.managed_assignment_active())

    def test_framework_version_and_every_hook_are_required(self):
        hook_names = {
            "assignment_intent_before": "hero_integration.services.task_assignment_state.before_native_assignment",
            "assignment_intent_after": "hero_integration.services.task_assignment_state.after_native_assignment",
            "assignment_intent_guard": "hero_integration.services.task_assignment_state.guard_native_assignment",
        }
        frappe = types.ModuleType("frappe")
        frappe.__path__ = []
        desk = types.ModuleType("frappe.desk")
        desk.__path__ = []
        form = types.ModuleType("frappe.desk.form")
        form.__path__ = []
        form.assign_to = types.SimpleNamespace(ASSIGNMENT_INTENT_HOOK_VERSION=2)
        present = {key: [value] for key, value in hook_names.items()}
        frappe.get_hooks = lambda name: present.get(name, [])
        modules = {"frappe": frappe, "frappe.desk": desk, "frappe.desk.form": form}
        with patch.dict(sys.modules, modules):
            self.assertTrue(state.assignment_boundary_available())
            form.assign_to.ASSIGNMENT_INTENT_HOOK_VERSION = 1
            self.assertFalse(state.assignment_boundary_available())
            form.assign_to.ASSIGNMENT_INTENT_HOOK_VERSION = 2
            for key, value in hook_names.items():
                with self.subTest(missing=key):
                    present[key] = []
                    self.assertFalse(state.assignment_boundary_available())
                    present[key] = [value]

    def test_unrelated_and_managed_assignments_skip_database_lookup(self):
        frappe = types.ModuleType("frappe")
        frappe.db = types.SimpleNamespace(exists=Mock(), sql=Mock())
        with patch.dict(sys.modules, {"frappe": frappe}):
            self.assertFalse(state.guard_native_assignment({"doctype": "Project", "name": "synthetic"}))
            state.before_native_assignment({"doctype": "Project", "name": "synthetic"}, "recipient")
            state.after_native_assignment({"doctype": "Project", "name": "synthetic"}, "recipient")
            with state.managed_assignment():
                self.assertFalse(state.guard_native_assignment({"doctype": "Task", "name": "synthetic"}))
                state.before_native_assignment({"doctype": "Task", "name": "synthetic"}, "recipient")
                state.after_native_assignment({"doctype": "Task", "name": "synthetic"}, "recipient")
        frappe.db.exists.assert_not_called()
        frappe.db.sql.assert_not_called()
        self.assertFalse(state.managed_assignment_active())

    def test_todo_observer_skips_managed_and_migration_writes(self):
        frappe = types.ModuleType("frappe")
        frappe.db = types.SimpleNamespace(exists=Mock(), sql=Mock())
        frappe.flags = types.SimpleNamespace(in_migrate=True)
        frappe.get_doc = Mock()
        todo = types.SimpleNamespace(name="TODO-SYNTHETIC", reference_type="Task", reference_name="TASK-SYNTHETIC")
        with patch.dict(sys.modules, {"frappe": frappe}):
            state.observe_local_todo(todo)
            frappe.flags.in_migrate = False
            with state.managed_assignment():
                state.observe_local_todo(todo)
        frappe.db.exists.assert_not_called()
        frappe.db.sql.assert_not_called()
        frappe.get_doc.assert_not_called()
        self.assertFalse(state.managed_assignment_active())
