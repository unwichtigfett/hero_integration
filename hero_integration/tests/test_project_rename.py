"""Existing HERO projects are renamed to <trade>-<display_id>-H only when safe (2026-11-01)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import project_rename as pr


class _Client:
    def __init__(self):
        self.documents = []

    def query(self, document, variables):
        self.documents.append(document)
        return {"data": {"p1": {"id": 1, "measure": {"short": "PV"}},
                         "p2": {"id": 2, "measure": {"short": "X9", "name": "Elektrohandwerk"}},
                         "p3": {"id": 3, "measure": None}}}


class _Frappe:
    _ = staticmethod(lambda message: message)

    def __init__(self, active=()):
        self.renamed, self.active = [], list(active)
        self.flags = SimpleNamespace()
        self.db = SimpleNamespace(savepoint=lambda name: None, rollback=lambda **k: None, commit=lambda: None)

    def only_for(self, role):
        pass

    def throw(self, message):
        raise RuntimeError(message)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, limit_page_length=None):
        if doctype == "HERO Integration Run":
            return self.active
        return [SimpleNamespace(source_id=str(i), source_instance="default") for i in (1, 2, 3)]

    def rename_doc(self, doctype, old, new, **kwargs):
        assert getattr(self.flags, "hero_project_naming", False), "rename must run inside hero_project_naming"
        self.renamed.append((old, new))


ROWS = [
    {"doctype": "Project", "current_name": "PROJ-0001", "proposed_name": "PV-1-H", "safe_rename": True, "reason": ""},
    {"doctype": "Project", "current_name": "PV-2-H", "proposed_name": "PV-2-H", "safe_rename": True, "reason": ""},
    {"doctype": "Project", "current_name": "PROJ-0003", "proposed_name": "", "safe_rename": False,
     "reason": "reviewed source-scoped trade configuration is required"},
    {"doctype": "Customer", "current_name": "KD-1", "proposed_name": "2222", "safe_rename": True, "reason": ""},
]


class TestProjectRename(unittest.TestCase):
    def test_trades_come_from_hero_measure_never_from_titles(self):
        links = [SimpleNamespace(source_id=str(i), source_instance="default") for i in (1, 2, 3)]
        client = _Client()
        self.assertEqual(pr.hero_project_trades(client, links), {"default": {"1": "PV", "2": "ELEK"}})
        self.assertTrue(all(doc.startswith("query ") for doc in client.documents))

    def _run(self, fake, **kwargs):
        import hero_integration.services.guards as guards
        import hero_integration.services.business_number_preview as preview
        with patch.object(pr, "frappe", fake), patch.object(guards, "frappe", fake), \
                patch.object(preview, "preview_rows", return_value=ROWS):
            return pr.rename_hero_projects(client=_Client(), **kwargs)

    def test_dry_run_counts_and_renames_nothing(self):
        fake = _Frappe()
        report = self._run(fake)
        self.assertEqual((report["to_rename"], report["already_named"], report["projects"]), (1, 1, 3))
        self.assertEqual(fake.renamed, [])

    def test_real_run_renames_only_safe_projects_inside_the_hero_naming_allowance(self):
        fake = _Frappe()
        report = self._run(fake, dry_run=0, confirm=pr.CONFIRMATION)
        self.assertEqual(fake.renamed, [("PROJ-0001", "PV-1-H")])
        self.assertEqual((report["renamed"], report["failed"]), (1, 0))

    def test_real_run_needs_the_phrase_and_no_active_run(self):
        for fake, kwargs in ((_Frappe(), {"dry_run": 0}),
                             (_Frappe(active=["HERO-RUN-1"]), {"dry_run": 0, "confirm": pr.CONFIRMATION})):
            with self.assertRaises(RuntimeError):
                self._run(fake, **kwargs)
            self.assertEqual(fake.renamed, [])


if __name__ == "__main__":
    unittest.main()
