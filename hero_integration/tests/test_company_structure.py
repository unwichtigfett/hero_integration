"""Company structure setup is create-only and maps HERO trades to departments."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.integrations import company_structure as structure


class _Doc(dict):
    def __init__(self, store, values):
        super().__init__(values)
        self.store = store

    def append(self, field, row):
        self.setdefault(field, []).append(row)

    def insert(self):
        self.store.append(dict(self))
        return self


class _Frappe:
    def __init__(self, existing=(), projects=()):
        self.existing = set(existing)
        self.inserted, self.updates = [], []
        self.projects = list(projects)
        self.db = SimpleNamespace(
            exists=lambda doctype, name=None: (doctype, name) in self.existing or doctype == "Role"
                    or (doctype == "DocType" and name in {"HD Team", "HD Ticket Type"}),
            get_value=lambda doctype, name, field: "RSE" if doctype == "Company" else None,
            get_single_value=lambda doctype, field: "Firma",
            set_value=lambda doctype, name, field, value, update_modified=True: self.updates.append((name, value)),
            commit=lambda: None,
        )

    def only_for(self, role):
        pass

    def throw(self, message):
        raise RuntimeError(message)

    def get_doc(self, values):
        return _Doc(self.inserted, values)

    def get_all(self, doctype, filters=None, fields=None, limit_page_length=None):
        return [SimpleNamespace(**p) for p in self.projects]


class TestCompanyStructure(unittest.TestCase):
    def test_setup_creates_only_missing_objects_without_people(self):
        fake = _Frappe(existing={("Department", "Verwaltung - RSE"), ("Department", "Rechnungswesen - RSE"),
                                 ("Department", "All Departments"), ("Designation", "Elektriker")})
        with patch.object(structure, "frappe", fake):
            created = structure.setup_company_structure()
        self.assertNotIn("Verwaltung", created["Department"])
        self.assertIn("Photovoltaik", created["Department"])
        self.assertNotIn("Elektriker", created["Designation"])
        self.assertEqual(set(created["Role Profile"]), set(structure.ROLE_PROFILES))
        self.assertEqual(created["HD Ticket Type"], ["Netzanmeldung"])
        self.assertFalse(any(doc["doctype"] in {"Employee", "User", "HD Team"} for doc in fake.inserted))
        monteur = next(doc for doc in fake.inserted if doc.get("role_profile") == "Monteur")
        self.assertEqual([row["role"] for row in monteur["roles"]], ["Employee Self Service"])

    def test_trade_departments(self):
        fake = _Frappe(existing={("Department", "Photovoltaik - RSE"), ("Department", "Elektro - RSE")})
        with patch.object(structure, "frappe", fake):
            self.assertEqual(structure.department_for_trade("PV", "Firma"), "Photovoltaik - RSE")
            self.assertEqual(structure.department_for_trade("ds", "Firma"), "Photovoltaik - RSE")
            self.assertEqual(structure.department_for_trade("ELEK", "Firma"), "Elektro - RSE")
            self.assertIsNone(structure.department_for_trade("KLIM", "Firma"))  # awaits a decision
            self.assertIsNone(structure.department_for_trade(None, "Firma"))

    def test_backfill_fills_only_empty_departments_and_dry_run_writes_nothing(self):
        projects = [{"name": "PV-1", "project_name": "PV-1 | Photovoltaik", "company": "Firma"},
                    {"name": "ELEK-2", "project_name": "ELEK-2 | Elektro", "company": "Firma"},
                    {"name": "PROJ-0003", "project_name": "SONS-3 | Sonstiges", "company": "Firma"},
                    {"name": "PROJ-0004", "project_name": "PV-4 | Photovoltaik", "company": "Firma"}]
        fake = _Frappe(existing={("Department", "Photovoltaik - RSE"), ("Department", "Elektro - RSE")}, projects=projects)
        with patch.object(structure, "frappe", fake):
            report = structure.backfill_project_departments()
            self.assertEqual(fake.updates, [])
            self.assertEqual(report["projects"], {"Photovoltaik - RSE": 2, "Elektro - RSE": 1, "unmapped": 1})
            structure.backfill_project_departments(dry_run=0)
        self.assertEqual(fake.updates, [("PV-1", "Photovoltaik - RSE"), ("ELEK-2", "Elektro - RSE"), ("PROJ-0004", "Photovoltaik - RSE")])


if __name__ == "__main__":
    unittest.main()
