"""ERPNext naming tests require a connected site; rollback preserves records and naming counters."""
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

    def item_masters(self):
        """Create isolated item masters even when the setup wizard has not run."""
        root = frappe.db.get_value('Item Group', {'is_group': 1, 'parent_item_group': ('is', 'not set')}, 'name')
        if not root:
            root = frappe.get_doc({'doctype': 'Item Group', 'item_group_name': 'Synthetic Root ' + self.digits,
                                   'is_group': 1}).insert().name
        group = frappe.get_doc({'doctype': 'Item Group', 'item_group_name': 'Synthetic Group ' + self.digits,
                                'parent_item_group': root, 'is_group': 0}).insert().name
        uom = frappe.get_doc({'doctype': 'UOM', 'uom_name': 'Synthetic Unit ' + self.digits}).insert().name
        return group, uom

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

    def test_equivalent_project_notes_clear_conflict_without_overwriting(self):
        company = frappe.db.get_single_value('Global Defaults', 'default_company') or frappe.get_all(
            'Company', pluck='name', limit=1)[0]
        reference = f'PV-0{self.digits}'
        original = {'project_name': f'Synthetic {reference}', 'company': company, 'notes': '<p>Earlier text</p>'}
        result = migration._upsert('project', self.source_id, {'id': self.source_id}, original,
                                   'Project', 'project_name', 'old-mapping', EXECUTE, document_name=reference)
        self.assertEqual(result.code, 'CREATED')
        current = '<p>First line<br>Second line</p>'
        frappe.db.set_value('Project', reference, 'notes', current)
        record = {'id': self.source_id, 'partner_notes': 'First line\nSecond line'}
        values = {**original, 'notes': record['partner_notes']}
        result = migration._upsert('project', self.source_id, record, values, 'Project', 'project_name',
                                   'new-mapping', EXECUTE, document_name=reference)
        self.assertEqual(result.code, 'CONVERGED')
        self.assertEqual(frappe.db.get_value('Project', reference, 'notes'), current)
        self.assertEqual(frappe.db.get_value('HERO Integration Link',
                                            {'source_type': 'project', 'source_id': self.source_id}, 'sync_status'),
                         'Synchronized')
        repeated = migration._upsert('project', self.source_id, record, values, 'Project', 'project_name',
                                     'new-mapping', EXECUTE, document_name=reference)
        self.assertEqual(repeated.code, 'SKIPPED')

    def test_new_task_can_import_hero_deadline_but_local_edits_remain_blocked(self):
        values = {'subject': 'Synthetic deadline ' + self.digits, 'custom_hero_due_date': '2026-04-03 12:00:00'}
        result = migration._upsert('Task', self.source_id, {'id': self.source_id}, values,
                                   'Task', 'subject', 'deadline-test', EXECUTE)
        self.assertEqual(result.code, 'CREATED')
        task = frappe.get_doc('Task', result.target_name)
        self.assertEqual(str(task.custom_hero_due_date), '2026-04-03 12:00:00')
        task.custom_hero_due_date = '2026-04-04 12:00:00'
        with self.assertRaises(frappe.PermissionError):
            task.save()

    def test_product_collision_records_a_real_conflict_without_fake_baselines(self):
        from hero_integration.services import product_migration
        group, uom = self.item_masters()
        item = frappe.get_doc({'doctype': 'Item', 'item_code': 'TEST-' + self.digits,
                               'item_name': 'Synthetic collision', 'item_group': group,
                               'stock_uom': uom, 'is_stock_item': 0}).insert()
        result = product_migration._code_taken(None, {'product_id': self.source_id}, item.name,
                                               'default', SimpleNamespace(mapping_version='test'))
        self.assertEqual(result, 'conflict')
        link = frappe.get_doc('HERO Integration Link', {'source_type': 'supply_product', 'source_id': self.source_id})
        self.assertEqual(link.sync_status, 'Conflict')
        self.assertFalse(link.source_payload_hash or link.target_payload_hash)
        link.sync_status = 'Synchronized'
        with self.assertRaises(frappe.ValidationError):
            link.save()

    def test_product_with_service_number_gets_a_separate_stable_item(self):
        from hero_integration.services import product_migration
        group, uom = self.item_masters()
        code = 'TEST-SERVICE-' + self.digits
        service = frappe.get_doc({'doctype': 'Item', 'item_code': code, 'item_name': 'Synthetic service',
                                  'item_group': group, 'stock_uom': uom, 'is_stock_item': 0}).insert()
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_instance': 'default',
            'source_type': 'supply_service', 'source_id': self.source_id, 'target_doctype': 'Item',
            'target_name': service.name, 'sync_status': 'Synchronized', 'mapping_version': 'test',
            'source_payload_hash': 'synthetic-source', 'target_payload_hash': 'synthetic-target'}).insert()
        values = {'item_code': code, 'item_name': 'Synthetic article', 'item_group': group,
                  'stock_uom': uom, 'owned': ('item_name', 'item_group', 'stock_uom'),
                  'barcode': None, 'supplier': None, 'buying': None, 'selling': None}
        row = {'product_id': self.source_id}
        read = SimpleNamespace(mapping_version='test', plan=SimpleNamespace(adapter_options={}))
        masters = SimpleNamespace(for_item=lambda fields: None)
        with mock.patch.object(product_migration, 'item_values', return_value=values.copy()), \
             mock.patch.object(product_migration, 'plan_price', return_value=None):
            self.assertEqual(product_migration.upsert(row, None, masters, 'default', read=read), 'added')
        target = product_migration._collision_code(code, 'default', self.source_id)
        self.assertEqual(frappe.db.get_value('Item', target, 'custom_hero_original_article_number'), code)
        self.assertEqual(frappe.db.get_value('Item', service.name, 'item_name'), 'Synthetic service')
        self.assertEqual(frappe.db.get_value('HERO Integration Link',
            {'source_type': 'supply_product', 'source_id': self.source_id}, 'target_name'), target)

    def test_legacy_customer_id_is_reused_without_rename(self):
        legacy = f"KD-TEST-{self.digits}"
        values = {"customer_name": f"BN Test {self.digits}", "customer_type": "Individual",
                  "custom_hero_customer_number": self.digits}
        first = migration._upsert("customer", self.source_id, {"id": self.source_id}, values,
                                  "Customer", "customer_name", "bn-test", EXECUTE, document_name=legacy)
        repeated = migration._upsert("customer", self.source_id, {"id": self.source_id}, values,
                                     "Customer", "customer_name", "bn-test", EXECUTE, document_name=self.digits)
        self.assertEqual((first.code, repeated.code, repeated.target_name), ("CREATED", "SKIPPED", legacy))
        self.assertFalse(frappe.db.exists("Customer", self.digits))

    def test_invalid_number_keeps_raw_value_and_uses_an_internal_id(self):
        raw = f" 0{self.digits}/22 "
        name, note = migration._document_name(migration.customer_document_name, {"nr": raw},
                                              "customer number", internal_prefix="HERO-CUSTOMER")
        result = migration._upsert("customer", self.source_id, {"id": self.source_id},
            {"customer_name": f"BN Test {self.digits}", "customer_type": "Individual",
             "custom_hero_customer_number": raw}, "Customer", "customer_name", "bn-test", EXECUTE,
            document_name=name)
        self.assertEqual(result.code, "CREATED")
        self.assertTrue(name.startswith("HERO-CUSTOMER-"))
        self.assertIn("internal ID", note)
        self.assertEqual(frappe.db.get_value("Customer", name, "custom_hero_customer_number"), raw)

    def test_business_number_changes_and_explicit_clears_require_review(self):
        self.upsert_customer(self.digits)
        for incoming in (self.digits + "1", "", None):
            with self.subTest(incoming=incoming):
                result = migration._business_number_drift("customer", self.source_id, "default", "Customer",
                    "custom_hero_customer_number", incoming, "customer number", SimpleNamespace(mode="Preview"))
                self.assertEqual(result.code, "CONFLICT")
                self.assertEqual(frappe.db.get_value("Customer", self.digits, "custom_hero_customer_number"), self.digits)

    def test_source_instances_never_cross_link_on_identical_numbers(self):
        for instance, expected in (("bn-account-a", "CREATED"), ("bn-account-b", "CONFLICT")):
            result = migration._upsert("customer", self.source_id, {"id": self.source_id},
                {"customer_name": f"BN Test {instance} {self.digits}", "customer_type": "Individual",
                 "custom_hero_customer_number": self.digits}, "Customer", "customer_name", "bn-test", EXECUTE,
                document_name=self.digits, source_instance=instance)
            self.assertEqual(result.code, expected)
        self.assertFalse(frappe.db.exists("HERO Integration Link", {
            "source_instance": "bn-account-b", "source_type": "customer", "source_id": self.source_id}))

    def test_identity_link_uniqueness_race_rolls_back_the_unlinked_customer(self):
        self.assertEqual(self.upsert_customer(self.digits).code, "CREATED")
        other_name = self.digits + "RACE"
        with mock.patch.object(migration, "_link", return_value=None):
            result = migration._upsert("customer", self.source_id, {"id": self.source_id},
                {"customer_name": f"BN Race {self.digits}", "customer_type": "Individual"},
                "Customer", "customer_name", "bn-test", EXECUTE, document_name=other_name)
        self.assertEqual(result.code, "CONFLICT")
        self.assertFalse(frappe.db.exists("Customer", other_name))
        self.assertEqual(self.link_target(), self.digits)

    def test_read_only_preview_lists_identity_and_referencing_records(self):
        from hero_integration.services import business_number_preview as preview
        self.upsert_customer(self.digits)
        rows = preview.preview_rows()
        row = next(row for row in rows if row["doctype"] == "Customer" and row["current_name"] == self.digits)
        self.assertEqual(row["hero_number"], self.digits)
        self.assertEqual(row["hero_source_id"], self.source_id)
        self.assertEqual(row["reason"], "already named by its HERO number")
        self.assertIn("HERO Integration Link", row["referencing_records"])
        self.assertEqual(self.link_target(), self.digits)


if __name__ == "__main__":
    unittest.main()
