from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from hero_integration.integrations.erpnext.contact import (
    HomePhoneConflict,
    home_phone_for_customer,
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
