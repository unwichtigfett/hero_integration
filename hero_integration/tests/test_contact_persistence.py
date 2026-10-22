from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from hero_integration.integrations.erpnext.contact import (
    HomePhoneConflict,
    home_phone_for_customer,
    primary_contact_values,
    synchronize_primary_contact,
    synchronize_home_phone,
)


class TestContactPersistence(unittest.TestCase):
    def test_home_phone_is_added_once_to_an_exclusive_customer_contact(self):
        customer = _Document(name="CUST-001", customer_primary_contact="CONT-001")
        contact = _Document(
            name="CONT-001",
            links=[_Document(link_doctype="Customer", link_name="CUST-001")],
            phone_nos=[_Document(phone="+49 555 101", is_primary_mobile_no=1)],
        )

        with _fake_frappe(contact):
            synchronize_home_phone(customer, " +49 555 102 ")
            synchronize_home_phone(customer, "+49 555 102")
            stored = home_phone_for_customer(customer)

        self.assertEqual([row["phone"] for row in contact["phone_nos"]], ["+49 555 101", "+49 555 102"])
        self.assertEqual(contact.save_calls, 1)
        self.assertEqual(stored, "+49 555 102")

    def test_shared_contact_is_never_mutated(self):
        customer = _Document(name="CUST-001", customer_primary_contact="CONT-001")
        contact = _Document(
            name="CONT-001",
            links=[
                _Document(link_doctype="Customer", link_name="CUST-001"),
                _Document(link_doctype="Customer", link_name="CUST-OTHER"),
            ],
            phone_nos=[],
        )

        with _fake_frappe(contact):
            with self.assertRaises(HomePhoneConflict):
                synchronize_home_phone(customer, "+49 555 102")

        self.assertEqual(contact["phone_nos"], [])
        self.assertEqual(contact.save_calls, 0)

    def test_primary_contact_uses_native_email_and_mobile_children_without_duplicates(self):
        customer = _Document(name="CUST-001", customer_primary_contact="CONT-001")
        contact = _Document(
            name="CONT-001", first_name="Old", last_name="Name",
            links=[_Document(link_doctype="Customer", link_name="CUST-001")],
            email_ids=[_Document(email_id="old@example.invalid", is_primary=1)],
            phone_nos=[_Document(phone="+49 555 100", is_primary_mobile_no=1)],
        )

        with _fake_frappe(contact):
            values = {
                "first_name": "Ada", "last_name": "Lovelace",
                "email_id": "ada@example.invalid", "mobile_no": "+49 555 101",
            }
            synchronize_primary_contact(customer, values)
            synchronize_primary_contact(customer, values)
            snapshot = primary_contact_values(customer, set(values))

        self.assertEqual(contact["first_name"], "Ada")
        self.assertEqual(contact["last_name"], "Lovelace")
        self.assertEqual([row["email_id"] for row in contact["email_ids"]], ["ada@example.invalid"])
        self.assertEqual([row["phone"] for row in contact["phone_nos"]], ["+49 555 101"])
        self.assertEqual(snapshot, values)
        self.assertEqual(contact.save_calls, 1)

    def test_missing_optional_contact_values_do_not_overwrite_native_values(self):
        customer = _Document(name="CUST-001", customer_primary_contact="CONT-001")
        contact = _Document(
            name="CONT-001", first_name="Ada", last_name="Lovelace",
            links=[_Document(link_doctype="Customer", link_name="CUST-001")],
            email_ids=[_Document(email_id="ada@example.invalid", is_primary=1)],
            phone_nos=[_Document(phone="+49 555 101", is_primary_mobile_no=1)],
        )

        with _fake_frappe(contact):
            synchronize_primary_contact(customer, {"first_name": None, "email_id": ""})

        self.assertEqual(contact["first_name"], "Ada")
        self.assertEqual(contact["email_ids"][0]["email_id"], "ada@example.invalid")
        self.assertEqual(contact.save_calls, 0)


class _Document(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.__dict__.update(values)
        self.save_calls = 0

    def append(self, fieldname, values):
        row = _Document(**values)
        self[fieldname].append(row)
        return row

    def save(self):
        self.save_calls += 1


class _fake_frappe:
    def __init__(self, contact):
        self.contact = contact
        self.modules = {"frappe": types.SimpleNamespace(get_doc=lambda doctype, name: contact)}

    def __enter__(self):
        self.patch = mock.patch.dict(sys.modules, self.modules)
        self.patch.__enter__()
        return self

    def __exit__(self, *args):
        return self.patch.__exit__(*args)
