from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services import migration
from hero_integration.services.binding_registry import BindingRegistryService, state_fingerprint
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import BindingContract, plan_from_definitions
from hero_integration.services.sync_engine import FrappeBoundIndex, FrappeBoundProcessor


class TestMigrationBindingFrappe(IntegrationTestCase):
    def setUp(self):
        self.source = "migration-" + uuid4().hex[:12]
        self.number = "C-" + uuid4().hex[:12]
        self.row = dict(id=27, nr=self.number, full_name=self.number, category="A", type="commercial",
                        is_deleted=False, is_contact_person=False, _hero_source_instance=self.source)
        def rule(path, doctype, field, purpose, transform, source_type):
            return MappingSpec("HeroCustomers", path, doctype, field, source_type, purpose, transform,
                               definition_id="test-" + path)
        self.plan = plan_from_definitions("HeroCustomers", [
            rule("id", "HERO Integration Link", "source_id", "Source Identity", "Positive Int To Text", "int"),
            rule("full_name", "Customer", "customer_name", "Record Name", "Trim", "string"),
            rule("type", "Customer", "customer_type", "Customer Type", "Customer Type From HERO Type", "string"),
            rule("category", "Customer", "customer_details", "Field", "Trim", "string"),
        ], adapter_options={"version": 2})
        self.plan = replace(self.plan, binding_contract=BindingContract(self.source, "Contact", "test-map", 1))
        self.run = SimpleNamespace(name=self.source, source_instance=self.source, mode="Execute")

    def migrate(self):
        return migration._migrate_with_binding(self.row, self.run, self.plan, "customer",
            lambda: migration._migrate_customer(self.row, self.run, plan=self.plan))

    def test_migrate_poll_migrate_uses_latest_ack_and_preserves_local_changes(self):
        result = self.migrate()
        self.assertEqual(result.code, "CREATED", result.detail)
        fields = FrappeBoundIndex(object_type="Contact").fields(frappe.local.site, self.source)
        self.assertEqual([field.binding.target_field for field in fields], ["customer_details"])
        processor = FrappeBoundProcessor(1, ("test-category",), run_id=self.source, object_type="Contact",
                                        mapping_name="test-map")
        # Same previously admitted rules and transaction, synthetic provider.
        with patch.object(processor, "_approved_plan", return_value=self.plan):
            self.assertTrue(processor.process_event(dict(self.row, category="B"), fields, "v2", 2))
        self.row["category"] = "C"
        result = self.migrate()
        self.assertEqual(result.code, "UPDATED", result.detail)
        target = frappe.get_doc("Customer", self.number)
        self.assertEqual(target.customer_details, "C")
        target.customer_details = "local"
        target.save()
        self.assertEqual(self.migrate().code, "OBSERVED")
        self.row["category"] = "D"
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.assertEqual(frappe.db.get_value("Customer", self.number, "customer_details"), "local")
        self.row["category"] = " local "
        self.assertEqual(self.migrate().code, "CONVERGED")
        field, = FrappeBoundIndex(object_type="Contact").fields(frappe.local.site, self.source)
        self.assertEqual(field.binding.acknowledged_source_fingerprint, state_fingerprint("local", present=True))
        self.assertEqual(self.migrate().code, "SKIPPED")
        self.assertEqual(frappe.db.count("HERO Object Binding", {"source_instance": self.source}), 1)

    def test_independent_fields_and_partial_response_keep_the_correct_baselines(self):
        from hero_integration.services.sync_engine import SyncStatus

        extra = MappingSpec("HeroCustomers", "company_name", "Customer", "tax_id", "string", "Field", "Trim",
                            definition_id="test-tax")
        self.plan = replace(self.plan, definitions=(*self.plan.definitions, extra))
        self.row["company_name"] = "tax-initial"
        self.assertEqual(self.migrate().code, "CREATED")
        target = frappe.get_doc("Customer", self.number)
        target.tax_id = "tax-local"
        target.save()
        fields = FrappeBoundIndex(object_type="Contact").fields(frappe.local.site, self.source)
        self.assertEqual({field.binding.target_field for field in fields}, {"tax_id", "customer_details"})
        processor = FrappeBoundProcessor(1, ("test-category", "test-tax"), run_id=self.source,
                                         object_type="Contact", mapping_name="test-map")
        with patch.object(processor, "_approved_plan", return_value=self.plan):
            self.assertIs(processor.process_event(dict(self.row, category="B"), fields, "v2", 2), True)
            self.row.pop("company_name")
            result = processor.process_event(dict(self.row, category="C"), fields, "v3", 3)
            self.assertEqual(result, SyncStatus.PARTIAL)
            self.assertFalse(processor.accepted("27", "v3", fields))
            self.assertEqual((target.reload().tax_id, target.customer_details), ("tax-local", "C"))
            self.assertEqual(processor.process_event(dict(self.row, category="C", company_name="tax-remote"),
                             fields, "v4", 4), SyncStatus.CONFLICT)
        self.assertEqual(target.reload().tax_id, "tax-local")
        latest = {f.binding.target_field: f.binding for f in FrappeBoundIndex().fields(frappe.local.site, self.source)}
        self.assertEqual(latest["tax_id"].acknowledged_target_fingerprint, state_fingerprint("tax-initial", present=True))
        self.assertEqual(latest["customer_details"].acknowledged_target_fingerprint, state_fingerprint("C", present=True))

    def test_explicit_remote_tombstone_holds_bound_scalar_and_ack(self):
        from hero_integration.services.sync_engine import SyncStatus
        lifecycle = MappingSpec("HeroCustomers", "is_deleted", "HERO Integration Link", "sync_status", "boolean",
                                "Lifecycle Flag", "Identity", definition_id="test-lifecycle")
        self.plan = replace(self.plan, definitions=(*self.plan.definitions, lifecycle))
        self.assertEqual(self.migrate().code, "CREATED")
        index = FrappeBoundIndex(object_type="Contact")
        fields = index.fields(frappe.local.site, self.source)
        processor = FrappeBoundProcessor(1, ("test-category",), run_id=self.source, object_type="Contact",
                                         mapping_name="test-map")
        with patch.object(processor, "_approved_plan", return_value=self.plan):
            status = processor.process_event(dict(self.row, category="deleted content", is_deleted=True),
                                             fields, "removed", 4)
        self.assertEqual(status, SyncStatus.CONFLICT)
        self.assertEqual(frappe.db.get_value("Customer", self.number, "customer_details"), "A")
        self.assertEqual(index.fields(frappe.local.site, self.source), fields)

    def test_bound_poll_respects_pending_native_link_review(self):
        from hero_integration.services.sync_engine import SyncConflict
        self.assertEqual(self.migrate().code, "CREATED")
        link = frappe.db.get_value("HERO Integration Link", {"source_instance": self.source}, "name")
        frappe.db.set_value("HERO Integration Link", link, "sync_status", "Pending Review")
        fields = FrappeBoundIndex(object_type="Contact").fields(frappe.local.site, self.source)
        processor = FrappeBoundProcessor(1, ("test-category",), run_id=self.source, object_type="Contact",
                                         mapping_name="test-map")
        with patch.object(processor, "_approved_plan", return_value=self.plan), self.assertRaises(SyncConflict):
            processor.process_event(dict(self.row, category="remote"), fields, "v2", 2)
        self.assertEqual(frappe.db.get_value("Customer", self.number, "customer_details"), "A")

    def test_binding_failure_rolls_back_initial_target_and_link(self):
        with patch.object(BindingRegistryService, "record_verified_acknowledgment",
                          side_effect=frappe.ValidationError("synthetic rejection")):
            self.assertEqual(self.migrate().code, "BLOCKED_RECORD")
        self.assertFalse(frappe.db.exists("Customer", self.number))
        self.assertFalse(frappe.db.exists("HERO Integration Link", {"source_instance": self.source}))
        self.assertFalse(frappe.db.exists("HERO Object Binding", {"source_instance": self.source}))

    def test_parallel_claims_cannot_assign_one_source_to_two_targets(self):
        from threading import Barrier, Thread
        from hero_integration.services.binding_registry import BindingConflict, FrappeBindingStorage

        site, sites_path = frappe.local.site, frappe.local.sites_path
        frappe.db.rollback()  # Release test-runner locks before the two independent transactions.
        ready = Barrier(2)
        results = []
        def claim(target):
            try:
                frappe.init(site=site, sites_path=sites_path)
                frappe.connect()
                frappe.set_user("Administrator")
                frappe.db.sql("SET SESSION innodb_lock_wait_timeout=5")
                ready.wait(timeout=8)
                BindingRegistryService(FrappeBindingStorage()).create_or_resolve_object_binding(
                    site_scope=site, source_instance=self.source, object_type="Contact", external_object_id="27",
                    target_app="erpnext", target_doctype="Customer", target_name=target,
                    mapping_definition_id="test-map", mapping_revision=1)
                frappe.db.commit()
                results.append("created")
            except BindingConflict:
                frappe.db.rollback()
                results.append("conflict")
            except Exception as error:
                frappe.db.rollback()
                results.append(f"{type(error).__name__}: {error}")
            finally:
                frappe.destroy()

        workers = [Thread(target=claim, args=(self.number + suffix,)) for suffix in ("-A", "-B")]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(15)
            self.assertFalse(any(worker.is_alive() for worker in workers), results)
            self.assertCountEqual(results, ["created", "conflict"])
            frappe.db.rollback()
            self.assertEqual(frappe.db.count("HERO Object Binding", {"source_instance": self.source}), 1)
        finally:
            frappe.db.delete("HERO Object Binding", {"source_instance": self.source})
            frappe.db.commit()

    def test_stale_mapping_and_missing_target_leave_existing_claim_intact(self):
        result = self.migrate()
        self.assertEqual(result.code, "CREATED", result.detail)
        original = self.plan
        self.plan = replace(original, binding_contract=replace(original.binding_contract, revision=2))
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.plan = original
        frappe.db.delete("Customer", self.number)
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.assertFalse(frappe.db.exists("Customer", self.number))
        self.assertEqual(frappe.db.count("HERO Object Binding", {"source_instance": self.source}), 1)
