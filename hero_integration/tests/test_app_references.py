"""Helpdesk and CRM keep references to ERPNext records, never copies (option A, 2026-11-01)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.integrations import app_references as refs


class _Throw(Exception):
    pass


class _Doc(dict):
    def __init__(self, doctype, **values):
        super().__init__(values)
        self.doctype = doctype
        self.meta = SimpleNamespace(has_field=lambda field: field in {refs.CUSTOMER_FIELD, refs.PROJECT_FIELD})

    def set(self, field, value):
        self[field] = value


def _fake(projects=None, contact_emails=None, contact_customers=None):
    projects, contact_emails, contact_customers = projects or {}, contact_emails or {}, contact_customers or {}

    def get_all(doctype, filters=None, pluck=None, distinct=False, **kwargs):
        if doctype == "Contact Email":
            return contact_emails.get(filters["email_id"], [])
        if doctype == "Dynamic Link":
            return [c for contact in filters["parent"][1] for c in contact_customers.get(contact, [])]
        raise AssertionError(doctype)

    return SimpleNamespace(
        db=SimpleNamespace(get_value=lambda doctype, name, field: projects.get(name)),
        get_all=get_all, throw=lambda message: (_ for _ in ()).throw(_Throw(message)), _=lambda text: text,
    )


class TestFillReferences(unittest.TestCase):
    def _fill(self, doc, **fake):
        with patch.object(refs, "frappe", _fake(**fake)):
            refs.fill_references(doc)
        return doc

    def test_project_sets_its_customer_and_a_mismatch_is_refused(self):
        doc = self._fill(_Doc("HD Ticket", erp_project="PV-1"), projects={"PV-1": "C-1"})
        self.assertEqual(doc["erp_customer"], "C-1")
        with self.assertRaises(_Throw):
            self._fill(_Doc("CRM Deal", erp_project="PV-1", erp_customer="C-2"), projects={"PV-1": "C-1"})

    def test_customer_comes_from_the_contact_or_the_email_when_unambiguous(self):
        self.assertEqual(self._fill(_Doc("HD Ticket", contact="CON-1"),
                                    contact_customers={"CON-1": ["C-1"]})["erp_customer"], "C-1")
        self.assertEqual(self._fill(_Doc("CRM Lead", email="a@b.invalid"), contact_emails={"a@b.invalid": ["CON-2"]},
                                    contact_customers={"CON-2": ["C-2"]})["erp_customer"], "C-2")
        ambiguous = self._fill(_Doc("HD Ticket", raised_by="x@y.invalid"), contact_emails={"x@y.invalid": ["A", "B"]},
                               contact_customers={"A": ["C-1"], "B": ["C-2"]})
        self.assertNotIn("erp_customer", ambiguous)

    def test_a_chosen_customer_is_never_replaced(self):
        doc = self._fill(_Doc("HD Ticket", erp_customer="C-9", contact="CON-1"), contact_customers={"CON-1": ["C-1"]})
        self.assertEqual(doc["erp_customer"], "C-9")


class TestSetup(unittest.TestCase):
    def test_crm_side_panel_gains_one_erpnext_section(self):
        layout = SimpleNamespace(layout=json.dumps([{"label": "Details", "columns": [{"fields": ["organization"]}]}]),
                                 save=lambda **kwargs: None)
        fake = SimpleNamespace(db=SimpleNamespace(get_value=lambda *args: "CRM Deal-Side Panel"),
                               get_doc=lambda *args: layout)
        with patch.object(refs, "frappe", fake):
            refs._show_in_crm_side_panel("CRM Deal")
            refs._show_in_crm_side_panel("CRM Deal")
        sections = json.loads(layout.layout)
        self.assertEqual([section["label"] for section in sections], ["ERPNext", "Details"])
        self.assertEqual(sections[0]["columns"][0]["fields"], ["erp_customer", "erp_project"])

    def test_fields_are_created_only_for_installed_apps_and_only_when_missing(self):
        created = {}
        fake = SimpleNamespace(db=SimpleNamespace(
            exists=lambda doctype, value: value == "HD Ticket" if doctype == "DocType" else False))
        with patch.object(refs, "frappe", fake), patch(
                "frappe.custom.doctype.custom_field.custom_field.create_custom_fields",
                side_effect=lambda fields, update: created.update(fields) or self.assertFalse(update)):
            refs.ensure_reference_fields()
        self.assertEqual(list(created), ["HD Ticket"])
        self.assertEqual([f["options"] for f in created["HD Ticket"]], ["Customer", "Project"])


class TestRecordMirrors(unittest.TestCase):
    def _check(self, doctype, **values):
        doc = SimpleNamespace(doctype=doctype, get=values.get)
        fake = SimpleNamespace(throw=lambda message: (_ for _ in ()).throw(_Throw(message)), _=lambda text: text)
        with patch.object(refs, "frappe", fake):
            refs.refuse_record_mirrors(doc)

    def test_mirrors_that_copy_or_create_records_are_refused(self):
        with self.assertRaises(_Throw):
            self._check("ERPNext HD Settings", enabled=1)
        with self.assertRaises(_Throw):
            self._check("ERPNext CRM Settings", enabled=1, create_customer_on_status_change=1)

    def test_harmless_settings_are_allowed(self):
        self._check("ERPNext HD Settings", enabled=0)
        self._check("ERPNext CRM Settings", enabled=1, create_customer_on_status_change=0)
        self._check("ERPNext CRM Settings", enabled=0, create_customer_on_status_change=1)


if __name__ == "__main__":
    unittest.main()
