"""Actual Frappe dispatcher with stubbed identity and storage; no site or database."""
import unittest
import logging
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import frappe
with patch.object(frappe, "logger", return_value=logging.getLogger("draft-test")):
    import frappe.handler
from hero_integration import api


class TestDraftDispatcherOffline(unittest.TestCase):
    def dispatch(self, name, method, role):
        def only_for(required):
            if required != role:
                raise PermissionError('not authorized')
        def denied(*args, **kwargs):
            raise PermissionError('method not allowed')
        form = frappe._dict({'definition_id': 'definition'})
        with ExitStack() as stack:
            for obj, attribute, value in (
                (frappe, 'local', SimpleNamespace(request=SimpleNamespace(method=method))),
                (frappe, 'flags', frappe._dict(in_safe_exec=False)),
                (frappe, 'form_dict', form),
                (frappe, 'session', frappe._dict(user='employee' if role == 'Employee' else 'admin')),
                (frappe, 'only_for', only_for),
                (frappe, 'override_whitelisted_method', lambda cmd: cmd),
                (frappe, 'throw_permission_error', denied),
                (frappe, 'db', SimpleNamespace(exists=lambda *a, **kw: False)),
                (frappe.handler, 'get_server_script_map', lambda: {}),
                (frappe.handler, 'get_attr', lambda cmd: getattr(api, cmd.rsplit('.', 1)[-1])),
            ):
                stack.enter_context(patch.object(obj, attribute, value))
            frappe.local.site = 'offline'
            return frappe.handler.execute_cmd('hero_integration.api.' + name)

    def test_admin_get_read_dispatches_without_creating_draft(self):
        self.assertEqual(self.dispatch('mapping_draft_read', 'GET', 'System Manager'), {'draft': None})

    def test_employee_is_denied_personal_draft_read(self):
        with self.assertRaises(PermissionError):
            self.dispatch('mapping_draft_read', 'GET', 'Employee')

    def test_post_only_create_cannot_be_dispatched_by_get(self):
        with self.assertRaises(PermissionError):
            self.dispatch('mapping_draft_create', 'GET', 'System Manager')
