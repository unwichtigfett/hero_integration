"""Write permission for mapping targets that are child tables (Contact Phone)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import doctype_permissions as perms


class _PermissionError(Exception):
    pass


def fake(istable, allowed, parents=("Contact",)):
    checked = []

    def has_permission(doctype, ptype, throw=False):
        checked.append(doctype)
        if doctype in allowed:
            return True
        if throw:
            raise _PermissionError(doctype)
        return False

    def throw(message, exc=None):
        raise (exc or ValueError)(message)

    return SimpleNamespace(
        get_meta=lambda doctype: SimpleNamespace(istable=istable),
        has_permission=has_permission,
        get_all=lambda doctype, **kwargs: list(parents) if doctype == "DocField" else [],
        db=SimpleNamespace(exists=lambda doctype, name: True),
        throw=throw, _=lambda text: text, PermissionError=_PermissionError), checked


class TestDoctypePermissions(unittest.TestCase):
    def test_a_child_table_is_checked_on_its_parent(self):
        frappe, checked = fake(istable=1, allowed={"Contact"})
        perms.ensure_doctype_write("Contact Phone", frappe)
        self.assertEqual(checked, ["Contact"])

    def test_a_child_table_without_writable_parent_is_refused(self):
        frappe, _ = fake(istable=1, allowed=set())
        with self.assertRaises(_PermissionError):
            perms.ensure_doctype_write("Contact Phone", frappe)

    def test_a_normal_doctype_is_checked_directly(self):
        frappe, checked = fake(istable=0, allowed={"Customer"})
        perms.ensure_doctype_write("Customer", frappe)
        self.assertEqual(checked, ["Customer"])

    def test_catalog_probe_is_silent_but_enforcement_still_raises(self):
        calls = []
        frappe, _ = fake(istable=0, allowed=set())
        def denied(doctype, ptype, throw=False):
            calls.append((doctype, ptype, throw))
            if throw:
                raise _PermissionError(doctype)
            return False
        frappe.has_permission = denied
        self.assertFalse(perms.can_doctype_write("GL Entry", frappe))
        self.assertEqual(calls, [("GL Entry", "write", False)])
        with self.assertRaises(_PermissionError):
            perms.ensure_doctype_write("GL Entry", frappe)
        self.assertEqual(calls[-1], ("GL Entry", "write", True))

    def test_silent_probe_respects_child_parent_permissions(self):
        allowed, _ = fake(istable=1, allowed={"Contact"})
        denied, _ = fake(istable=1, allowed=set())
        self.assertTrue(perms.can_doctype_write("Contact Phone", allowed))
        self.assertFalse(perms.can_doctype_write("Contact Phone", denied))

    def test_frappe_does_not_emit_role_messages_for_a_silent_probe(self):
        try:
            import frappe
            import frappe.permissions
            import frappe.share
        except ModuleNotFoundError:
            self.skipTest("Frappe is not installed")
        meta = SimpleNamespace(istable=0, issingle=0, is_virtual=0)
        with patch.object(frappe, "session", frappe._dict(user="user@example.invalid")), \
             patch.object(frappe, "flags", frappe._dict()), \
             patch.object(frappe, "is_table", return_value=False), \
             patch.object(frappe, "get_meta", return_value=meta), \
             patch.object(frappe.permissions, "get_role_permissions", return_value={}), \
             patch.object(frappe.permissions, "get_doctype_ptype_map", return_value={}), \
             patch.object(frappe.share, "get_shared", return_value=[]), \
             patch.object(frappe.permissions, "_", side_effect=lambda text: text), \
             patch.object(frappe, "_", side_effect=lambda text: text), \
             patch.object(frappe, "bold", side_effect=lambda text: text), \
             patch.object(frappe.permissions, "msgprint") as messages:
            self.assertFalse(perms.can_doctype_write("GL Entry", frappe))
            messages.assert_not_called()
            with self.assertRaises(frappe.PermissionError):
                perms.ensure_doctype_write("GL Entry", frappe)
            self.assertEqual(messages.call_count, 1)


if __name__ == "__main__":
    unittest.main()
