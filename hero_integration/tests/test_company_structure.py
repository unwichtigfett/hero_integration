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
            set_value=lambda doctype, name, values, update_modified=True: self.updates.append((name, values)),
            commit=lambda: None,
        )

    def only_for(self, role):
        pass

    def throw(self, message):
        raise RuntimeError(message)

    def get_doc(self, values):
        return _Doc(self.inserted, values)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, limit_page_length=None):
        if doctype == "HERO Integration Link":
            return list(getattr(self, "bound", []))
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

    def test_backfill_touches_only_hero_projects_and_only_empty_fields(self):
        from hero_integration.integrations import project_form
        projects = [{"name": "PV-1-H", "project_name": "PV-1 | Photovoltaik", "company": "Firma", "department": None, "project_type": None},
                    {"name": "ELEK-2-H", "project_name": "ELEK-2 | Elektro", "company": "Firma", "department": "IT - RSE", "project_type": None},
                    {"name": "SONS-3-H", "project_name": "SONS-3 | Sonstiges", "company": "Firma", "department": None, "project_type": "Sonstiges"},
                    {"name": "PV-0001", "project_name": "PV-0001 | created in ERPNext", "company": "Firma", "department": None, "project_type": None}]
        fake = _Frappe(existing={("Department", "Photovoltaik - RSE"), ("Department", "Elektro - RSE")}, projects=projects)
        fake.bound = ["PV-1-H", "ELEK-2-H", "SONS-3-H"]
        types = {"PV": "Photovoltaik", "ELEK": "Elektrohandwerk", "SONS": "Sonstiges"}
        with patch.object(structure, "frappe", fake), \
                patch.object(project_form, "project_type_for_trade", side_effect=lambda code: types.get(code)):
            report = structure.backfill_project_departments()
            self.assertEqual(fake.updates, [])
            self.assertEqual(report["projects"]["not_from_hero"], 1)
            structure.backfill_project_departments(dry_run=0)
        self.assertEqual(fake.updates, [
            ("PV-1-H", {"department": "Photovoltaik - RSE", "project_type": "Photovoltaik"}),
            ("ELEK-2-H", {"project_type": "Elektrohandwerk"}),
        ])
        fake.updates.clear()
        with patch.object(structure, "frappe", fake), \
                patch.object(project_form, "project_type_for_trade", side_effect=lambda code: types.get(code)):
            structure.backfill_project_departments(dry_run=0, fields="project_type")
        self.assertEqual(fake.updates[0], ("PV-1-H", {"project_type": "Photovoltaik"}))


if __name__ == "__main__":
    unittest.main()
