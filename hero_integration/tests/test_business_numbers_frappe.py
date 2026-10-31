"""Real ERPNext v16: HERO business numbers name new Customers and Projects.

Needs a connected Frappe site with ERPNext. Every test rolls back, so the site
keeps no records and no naming-series counter moves.
"""
from __future__ import annotations

import random
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from hero_integration.services import migration

EXECUTE = SimpleNamespace(mode="Execute")


class TestBusinessNumbersOnERPNext(unittest.TestCase):
    def setUp(self):
        self.user = frappe.session.user
        frappe.set_user("Administrator")
        frappe.db.savepoint("hero_business_numbers_test")
        self.digits = str(random.randint(10**8, 10**9 - 1))
        self.source_id = f"bn-test-{self.digits}"

    def tearDown(self):
        frappe.db.rollback(save_point="hero_business_numbers_test")
        frappe.clear_messages()
        frappe.set_user(self.user)

    def upsert_customer(self, name, source_id=None):
        values = {"customer_name": f"BN Test {self.digits}", "customer_type": "Individual",
                  "custom_hero_customer_number": name}
        return migration._upsert(
            "customer", source_id or self.source_id, {"id": source_id or self.source_id}, values, "Customer",
            "customer_name", "bn-test", EXECUTE, document_name=name,
        )

    def link_target(self, source_type="customer"):
        return frappe.db.get_value(
            "HERO Integration Link", {"source_type": source_type, "source_id": self.source_id}, "target_name",
        )

    def test_the_hero_number_names_a_new_customer_despite_the_naming_series(self):
        for number in (self.digits, "0" + self.digits):
            with self.subTest(number=number):
                result = migration._upsert(
                    "customer", f"{self.source_id}-{number}", {"id": number},
                    {"customer_name": f"BN Test {number}", "customer_type": "Individual"},
                    "Customer", "customer_name", "bn-test", EXECUTE, document_name=number,
                )
                self.assertEqual((result.code, result.target_name), ("CREATED", number))
                self.assertEqual(frappe.db.get_value("Customer", number, "customer_name"), f"BN Test {number}")

    def test_a_customer_created_by_hand_still_uses_the_site_naming(self):
        customer = frappe.get_doc({"doctype": "Customer", "customer_name": f"BN Manual {self.digits}",
                                   "customer_type": "Individual"}).insert()
        self.assertNotEqual(customer.name, self.digits)
        if frappe.defaults.get_global_default("cust_master_name") == "Naming Series":
            series = (frappe.get_meta("Customer").get_field("naming_series").options or "").split("\n")[0]
            self.assertTrue(customer.name.startswith(series.split(".")[0]), customer.name)

    def test_a_repeated_import_reuses_the_linked_customer(self):
        self.assertEqual(self.upsert_customer(self.digits).code, "CREATED")
        again = self.upsert_customer(self.digits)
        self.assertEqual((again.code, again.target_name), ("SKIPPED", self.digits))
        self.assertEqual(frappe.db.count("Customer", {"customer_name": f"BN Test {self.digits}"}), 1)
        self.assertEqual(self.link_target(), self.digits)

    def test_a_name_held_by_a_manual_customer_is_a_conflict(self):
        frappe.get_doc({"doctype": "Customer", "customer_name": "Someone else", "customer_type": "Individual"}).insert(
            set_name=self.digits)
        result = self.upsert_customer(self.digits)
        self.assertEqual(result.code, "CONFLICT")
        self.assertEqual(frappe.db.get_value("Customer", self.digits, "customer_name"), "Someone else")
        self.assertIsNone(self.link_target())

    def test_a_concurrent_insert_of_the_same_name_is_a_conflict_not_a_failure(self):
        frappe.get_doc({"doctype": "Customer", "customer_name": "Concurrent", "customer_type": "Individual"}).insert(
            set_name=self.digits)
        real_exists = frappe.db.exists

        def miss_the_new_name(doctype, filters=None, *args, **kwargs):
            # Simulates the other import committing between check and insert.
            if doctype == "Customer" and filters == self.digits:
                return None
            return real_exists(doctype, filters, *args, **kwargs)

        with mock.patch.object(frappe.db, "exists", side_effect=miss_the_new_name):
            result = self.upsert_customer(self.digits)
        self.assertEqual(result.code, "CONFLICT")
        self.assertIsNone(self.link_target())
        self.assertEqual(frappe.db.get_value("Customer", self.digits, "customer_name"), "Concurrent")

    def test_the_trade_reference_names_a_new_project(self):
        company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.get_all(
            "Company", pluck="name", limit=1)[0]
        reference = f"PV-0{self.digits}"
        result = migration._upsert(
            "project", self.source_id, {"id": self.source_id},
            {"project_name": f"{reference} | Photovoltaik, BN Test", "company": company,
             "custom_hero_display_id": f"0{self.digits}"},
            "Project", "project_name", "bn-test", EXECUTE, document_name=reference,
        )
        self.assertEqual((result.code, result.target_name), ("CREATED", reference))
        self.assertEqual(self.link_target("project"), reference)
        self.assertEqual(frappe.db.get_value("Project", reference, "custom_hero_display_id"), f"0{self.digits}")


if __name__ == "__main__":
    unittest.main()
