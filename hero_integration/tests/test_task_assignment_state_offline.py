"""Offline boundaries for native assignment hooks; no Frappe site is used."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import task_assignment_state as state


class TestAssignmentHookScope(unittest.TestCase):
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
