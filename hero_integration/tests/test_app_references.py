"""Helpdesk and CRM keep references to ERPNext records, never copies."""

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
        self.meta = SimpleNamespace(has_field=lambda field: field in {
            refs.CUSTOMER_FIELD, refs.PROJECT_FIELD, refs.HELPDESK_CUSTOMER_FIELD})

    def set(self, field, value):
        self[field] = value


def _fake(projects=None, contact_emails=None, contact_customers=None, helpdesk_customers=None):
    projects, contact_emails, contact_customers = projects or {}, contact_emails or {}, contact_customers or {}
    helpdesk_customers = helpdesk_customers or {}

    def get_all(doctype, filters=None, pluck=None, distinct=False, **kwargs):
        if doctype == "HD Customer":
            return [name for name in [helpdesk_customers.get(filters["erpnext_customer"])] if name]
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
    def test_only_installed_app_interfaces_are_configured(self):
        for apps, helpdesk_calls, crm_calls in (
            ([], 0, 0),
            (["helpdesk"], 1, 0),
            (["crm"], 0, 2),
            (["helpdesk", "crm"], 1, 2),
        ):
            with self.subTest(apps=apps), patch.object(refs, "READ_ROLES", {}), \
                    patch.object(refs.frappe, "get_installed_apps", return_value=apps), \
                    patch.object(refs, "ensure_reference_fields"), \
                    patch.object(refs, "_show_in_helpdesk") as helpdesk, \
                    patch.object(refs, "_show_in_crm_side_panel") as crm:
                refs.setup_reference_access()
                self.assertEqual(helpdesk.call_count, helpdesk_calls)
                self.assertEqual(crm.call_count, crm_calls)

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


class TestHelpdeskCustomerLink(unittest.TestCase):
    def _fill(self, doc, **fake):
        with patch.object(refs, "frappe", _fake(**fake)):
            refs.fill_references(doc)
        return doc

    def test_ticket_shows_the_hd_customer_linked_to_its_erpnext_customer(self):
        doc = self._fill(_Doc("HD Ticket", erp_project="PV-1"), projects={"PV-1": "C-1"},
                         helpdesk_customers={"C-1": "HD C-1"})
        self.assertEqual((doc["erp_customer"], doc["customer"]), ("C-1", "HD C-1"))
        corrected = self._fill(_Doc("HD Ticket", erp_customer="C-1", customer="Other"),
                               helpdesk_customers={"C-1": "HD C-1"})
        self.assertEqual(corrected["customer"], "HD C-1")

    def test_unlinked_customers_and_crm_documents_are_left_alone(self):
        self.assertNotIn("customer", self._fill(_Doc("HD Ticket", erp_customer="C-1")))
        self.assertNotIn("customer", self._fill(_Doc("CRM Deal", erp_customer="C-1"),
                                                helpdesk_customers={"C-1": "HD C-1"}))

    def test_existing_tickets_are_linked_once(self):
        written = []
        tickets = [{"name": "1", "erp_customer": "C-1", "customer": None},
                   {"name": "2", "erp_customer": "C-1", "customer": "HD C-1"}]
        fake = SimpleNamespace(
            db=SimpleNamespace(exists=lambda *args: True,
                               set_value=lambda *args, **kwargs: written.append(args)),
            get_meta=lambda doctype: SimpleNamespace(has_field=lambda field: True),
            get_all=lambda doctype, **kwargs: [["C-1", "HD C-1"]] if doctype == "HD Customer" else tickets)
        with patch.object(refs, "frappe", fake):
            self.assertEqual(refs.link_existing_helpdesk_tickets(), 1)
        self.assertEqual(written, [("HD Ticket", "1", "customer", "HD C-1")])


class TestErpnextOwnsCustomers(unittest.TestCase):
    def _check(self, method, enabled=1, new=False, changed=(), cascade=False, from_erpnext=False, **values):
        doc = SimpleNamespace(doctype="HD Customer", get=values.get, is_new=lambda: new,
                              has_value_changed=lambda field: field in changed,
                              flags={"ignore_erpnext_sync": True} if from_erpnext else {})
        fake = SimpleNamespace(
            throw=lambda message: (_ for _ in ()).throw(_Throw(message)), _=lambda text: text,
            db=SimpleNamespace(get_single_value=lambda doctype, field: enabled),
            flags={refs.HELPDESK_CASCADE_FLAG: True} if cascade else {})
        with patch.object(refs, "frappe", fake):
            refs.protect_erpnext_customer(doc, method, "old", "new", False)

    def test_helpdesk_cannot_create_change_rename_or_delete_a_listed_customer(self):
        for method, case in (("validate", {"new": True}), ("validate", {"changed": ("customer_type",)}),
                             ("validate", {"changed": ("image",)}), ("before_rename", {}),
                             ("on_trash", {"erpnext_customer": "C-1"})):
            with self.subTest(method=method, case=case), self.assertRaises(_Throw):
                self._check(method, **case)

    def test_erpnext_changes_helpdesk_only_fields_and_a_disabled_listing_pass(self):
        self._check("validate", new=True, from_erpnext=True)
        self._check("validate", new=True, erpnext_customer="C-1")
        self._check("validate", changed=("domain", "contacts"))
        self._check("before_rename", cascade=True)
        self._check("on_trash", cascade=True, erpnext_customer="C-1")
        self._check("on_trash")  # ERPNext already removed the link while deleting its Customer
        for method in ("validate", "before_rename", "on_trash"):
            self._check(method, enabled=0, new=True, changed=("image",), erpnext_customer="C-1")


class TestRecordMirrors(unittest.TestCase):
    def _check(self, doctype, **values):
        doc = SimpleNamespace(doctype=doctype, get=values.get)
        fake = SimpleNamespace(throw=lambda message: (_ for _ in ()).throw(_Throw(message)), _=lambda text: text)
        with patch.object(refs, "frappe", fake):
            refs.refuse_record_mirrors(doc)

    def test_crm_must_not_create_customers(self):
        with self.assertRaises(_Throw):
            self._check("ERPNext CRM Settings", enabled=1, create_customer_on_status_change=1)

    def test_harmless_settings_are_allowed(self):
        self._check("ERPNext HD Settings", enabled=1)
        self._check("ERPNext CRM Settings", enabled=1, create_customer_on_status_change=0)
        self._check("ERPNext CRM Settings", enabled=0, create_customer_on_status_change=1)


if __name__ == "__main__":
    unittest.main()
