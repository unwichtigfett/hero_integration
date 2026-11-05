"""Integration target lookup and reserved project naming contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import guards


class _Throw(Exception):
    pass


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
        return SimpleNamespace(flags=SimpleNamespace(), _=lambda message: message,
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
