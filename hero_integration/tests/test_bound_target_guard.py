"""A HERO-bound ERPNext record is deleted only by the purge or an administrator rejection.

Deleting it elsewhere would leave a binding without target, and every later run
would report a conflict for it (hardening 2026-11-01).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import guards


class _Throw(Exception):
    pass


def _fake(bound):
    flags = SimpleNamespace()
    return SimpleNamespace(
        flags=flags,
        db=SimpleNamespace(exists=lambda doctype, filters: bound if doctype == "HERO Integration Link" else None),
        throw=lambda message, exc=None: (_ for _ in ()).throw(_Throw(message)),
        PermissionError=PermissionError,
    )


class TestBoundTargetGuard(unittest.TestCase):
    def test_bound_target_cannot_be_deleted(self):
        with patch.object(guards, "frappe", _fake("LINK-1")):
            with self.assertRaises(_Throw):
                guards.protect_hero_bound_target(SimpleNamespace(doctype="Project", name="PV-1"))

    def test_unbound_and_hero_owned_documents_are_not_checked(self):
        fake = _fake(None)
        with patch.object(guards, "frappe", fake):
            guards.protect_hero_bound_target(SimpleNamespace(doctype="Project", name="PV-2"))
        fake = _fake("LINK-1")
        with patch.object(guards, "frappe", fake):
            guards.protect_hero_bound_target(SimpleNamespace(doctype="HERO Integration Link", name="LINK-1"))

    def test_purge_and_rejection_may_delete_inside_the_allowance(self):
        fake = _fake("LINK-1")
        with patch.object(guards, "frappe", fake):
            with guards.bound_target_deletion():
                guards.protect_hero_bound_target(SimpleNamespace(doctype="Customer", name="KD-1"))
            with self.assertRaises(_Throw):
                guards.protect_hero_bound_target(SimpleNamespace(doctype="Customer", name="KD-1"))


class TestTargetLookupIndex(unittest.TestCase):
    def test_binding_lookup_by_target_is_indexed(self):
        import frappe
        from hero_integration.hero_integration.doctype.hero_integration_link import hero_integration_link

        calls = []
        fake_db = SimpleNamespace(add_index=lambda doctype, fields, index_name=None: calls.append((doctype, fields, index_name)))
        with patch.object(frappe, "db", fake_db):
            hero_integration_link.on_doctype_update()
        self.assertEqual(calls, [("HERO Integration Link", ["target_doctype", "target_name"], "target_lookup")])


class TestHeroProjectIds(unittest.TestCase):
    def _fake(self):
        return SimpleNamespace(flags=SimpleNamespace(),
                               throw=lambda message, exc=None: (_ for _ in ()).throw(_Throw(message)))

    def _project(self, name):
        return SimpleNamespace(name=name, is_new=lambda: True)

    def test_erpnext_cannot_create_or_rename_to_a_hero_id(self):
        with patch.object(guards, "frappe", self._fake()):
            with self.assertRaises(_Throw):
                guards.protect_hero_project_ids(self._project("PV-3333-H"))
            with self.assertRaises(_Throw):
                guards.protect_hero_project_rename(self._project("PROJ-1"), None, "PROJ-1", "pv-3333-h")
            guards.protect_hero_project_ids(self._project("PV-3333"))
            guards.protect_hero_project_rename(self._project("PROJ-1"), None, "PROJ-1", "PV-3333")

    def test_hero_import_and_rename_tool_may_use_the_suffix(self):
        with patch.object(guards, "frappe", self._fake()):
            with guards.hero_project_naming():
                guards.protect_hero_project_ids(self._project("PV-3333-H"))
                guards.protect_hero_project_rename(self._project("PROJ-1"), None, "PROJ-1", "PV-3333-H")


if __name__ == "__main__":
    unittest.main()
