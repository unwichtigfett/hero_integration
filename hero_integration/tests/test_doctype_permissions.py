"""Write permission for mapping targets that are child tables (Contact Phone)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
