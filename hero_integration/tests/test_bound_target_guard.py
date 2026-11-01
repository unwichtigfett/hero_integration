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


if __name__ == "__main__":
    unittest.main()
