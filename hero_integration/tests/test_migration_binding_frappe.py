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
            self.assertEqual(processor.process_event(dict(self.row, category="D", company_name="tax-remote"),
                             fields, "v4", 4), SyncStatus.CONFLICT)
        self.assertEqual(processor.accepted("27", "v4", fields), SyncStatus.CONFLICT)
        self.assertEqual(target.reload().tax_id, "tax-local")
        latest = {f.binding.target_field: f.binding for f in FrappeBoundIndex().fields(frappe.local.site, self.source)}
        self.assertEqual(latest["tax_id"].acknowledged_target_fingerprint, state_fingerprint("tax-initial", present=True))
        self.assertEqual(latest["customer_details"].acknowledged_target_fingerprint, state_fingerprint("D", present=True))

    def test_replayed_receipt_reports_review_only_while_its_field_is_open(self):
        from hero_integration.services.sync_engine import SyncStatus

        self.assertEqual(self.migrate().code, "CREATED")
        customer = frappe.get_doc("Customer", self.number)
        customer.customer_details = "local"
        customer.save()
        fields = FrappeBoundIndex(object_type="Contact").fields(frappe.local.site, self.source)
        processor = FrappeBoundProcessor(1, ("test-category",), run_id=self.source,
                                         object_type="Contact", mapping_name="test-map")
        with patch.object(processor, "_approved_plan", return_value=self.plan):
            self.assertEqual(processor.process_event(dict(self.row, category="other"), fields, "v2", 2),
                             SyncStatus.CONFLICT)
            self.assertEqual(processor.accepted("27", "v2", fields), SyncStatus.CONFLICT)
            self.assertIs(processor.process_event(dict(self.row, category="local"), fields, "v3", 3), True)
        self.assertIs(processor.accepted("27", "v2", fields), True)
        self.assertEqual(customer.reload().customer_details, "local")

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
        self.assertEqual(index.fields(frappe.local.site, self.source), ())
        from hero_integration.services.binding_registry import FrappeBindingStorage
        old = fields[0].binding
        storage = FrappeBindingStorage()
        self.assertEqual(storage.field(frappe.local.site, old.identity_key), old)
        self.assertEqual(storage.object(frappe.local.site, old.object_binding_key).lifecycle, "LEGACY")
        self.row["is_deleted"] = True
        self.assertEqual(self.migrate().code, "SKIPPED_SOURCE_DELETED")
        self.row["is_deleted"] = False
        self.assertEqual(self.migrate().code, "CONFLICT")

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
        self._competing_claims(shared_target=False)

    def test_two_workers_from_different_sources_cannot_claim_one_target(self):
        self._competing_claims(shared_target=True)

    def _competing_claims(self, *, shared_target):
        from threading import Barrier, Thread
        from hero_integration.services.binding_registry import BindingConflict, FrappeBindingStorage

        site, sites_path = frappe.local.site, frappe.local.sites_path
        frappe.db.rollback()  # Release test-runner locks before the two independent transactions.
        targets = [frappe.get_doc({"doctype": "Customer", "customer_name": self.number + suffix,
                    "customer_type": "Company"}).insert().name for suffix in ("-A", "-B")]
        frappe.db.commit()
        ready = Barrier(2)
        results = []
        sources = (self.source, self.source + "-other" if shared_target else self.source)
        def claim(target, source):
            try:
                frappe.init(site=site, sites_path=sites_path)
                frappe.connect()
                frappe.set_user("Administrator")
                frappe.db.sql("SET SESSION innodb_lock_wait_timeout=5")
                ready.wait(timeout=8)
                BindingRegistryService(FrappeBindingStorage()).create_or_resolve_object_binding(
                    site_scope=site, source_instance=source, object_type="Contact", external_object_id="27",
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

        workers = [Thread(target=claim, args=(targets[0] if shared_target else target, source))
                   for target, source in zip(targets, sources)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(15)
            self.assertFalse(any(worker.is_alive() for worker in workers), results)
            self.assertCountEqual(results, ["created", "conflict"])
            frappe.db.rollback()
            self.assertEqual(frappe.db.count("HERO Object Binding", {"source_instance": ["in", sources]}), 1)
        finally:
            frappe.db.delete("HERO Object Binding", {"source_instance": ["in", sources]})
            for target in targets:
                frappe.delete_doc("Customer", target)
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

    def test_historical_link_alone_cannot_authorize_the_next_migration(self):
        from hero_integration.services.binding_registry import FrappeBindingStorage
        self.assertEqual(self.migrate().code, "CREATED")
        bound, = FrappeBoundIndex().fields(frappe.local.site, self.source)
        frappe.db.delete(FrappeBindingStorage.FIELD, {"object_binding": bound.binding.object_binding_key})
        frappe.db.delete(FrappeBindingStorage.OBJECT, {"name": bound.binding.object_binding_key})
        self.row["category"] = "remote update"
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.assertEqual(frappe.db.get_value("Customer", self.number, "customer_details"), "A")

    def test_same_name_new_incarnation_never_adopts_old_binding(self):
        from hero_integration.services.binding_registry import FrappeBindingStorage, verify_incarnation, BindingConflict
        self.assertEqual(self.migrate().code, "CREATED")
        storage = FrappeBindingStorage()
        old, = FrappeBoundIndex().fields(frappe.local.site, self.source)
        parent = storage.object(frappe.local.site, old.binding.object_binding_key)
        with self.assertRaises(BindingConflict):
            frappe.rename_doc("Customer", self.number, self.number + "-renamed", force=True)
        self.assertTrue(frappe.db.exists("Customer", self.number))
        frappe.db.savepoint("before_native_delete")
        frappe.delete_doc("Customer", self.number)
        self.assertEqual(storage.object(frappe.local.site, parent.identity_key).lifecycle, "LEGACY")
        frappe.db.rollback(save_point="before_native_delete")
        self.assertEqual(storage.object(frappe.local.site, parent.identity_key), parent)
        frappe.delete_doc("Customer", self.number)
        fresh = frappe.get_doc({"doctype": "Customer", "customer_name": self.number,
                                "customer_type": "Company", "customer_details": "new incarnation"}).insert()
        self.assertEqual(fresh.name, self.number)
        with self.assertRaises(BindingConflict):
            verify_incarnation(parent, fresh)
        self.row["category"] = "stale source"
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.assertEqual(fresh.reload().customer_details, "new incarnation")
        self.assertEqual(storage.field(frappe.local.site, old.binding.identity_key), old.binding)
        self.assertEqual(FrappeBoundIndex().fields(frappe.local.site, self.source), ())

    def test_reclassification_and_cross_scope_claim_protect_native_customer(self):
        from hero_integration.services.binding_registry import BindingConflict, FrappeBindingStorage
        self.assertEqual(self.migrate().code, "CREATED")
        store = FrappeBindingStorage()
        with self.assertRaises(BindingConflict):
            BindingRegistryService(store).create_or_resolve_object_binding(site_scope=frappe.local.site,
                source_instance="other-"+self.source, object_type="Other", external_object_id="999",
                target_app="erpnext", target_doctype="Customer", target_name=self.number,
                mapping_definition_id="test-map", mapping_revision=1)
        self.row.update(is_contact_person=True, category="forbidden")
        self.assertEqual(self.migrate().code, "SKIPPED_CONTACT_PERSON")
        self.assertEqual(frappe.db.get_value("Customer", self.number, "customer_details"), "A")
        self.assertEqual(FrappeBoundIndex().fields(frappe.local.site, self.source), ())
        self.row["is_contact_person"] = False
        self.assertEqual(self.migrate().code, "CONFLICT")

    def test_mixed_migration_and_field_review_isolate_acknowledgments(self):
        from hero_integration.services import conflict_report
        from hero_integration.services.binding_registry import FrappeBindingStorage
        self.plan = replace(self.plan, definitions=(*self.plan.definitions,
            MappingSpec("HeroCustomers", "company_name", "Customer", "tax_id", "string", "Field", "Trim",
                        definition_id="test-tax")))
        self.row["company_name"] = "initial"
        self.assertEqual(self.migrate().code, "CREATED")
        target = frappe.get_doc("Customer", self.number)
        target.tax_id = "local"
        target.save()
        self.row.update(company_name="remote", category="independent")
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.assertEqual((target.reload().tax_id, target.customer_details), ("local", "independent"))
        store = FrappeBindingStorage()
        fields = {f.binding.target_field: f.binding for f in FrappeBoundIndex().fields(frappe.local.site, self.source)}
        self.assertEqual(fields["tax_id"].acknowledged_value_json, '"initial"')
        self.assertEqual(fields["customer_details"].acknowledged_value_json, '"independent"')
        review = frappe.get_doc("HERO Sync Change Event", {"open_field": fields["tax_id"].identity_key})
        self.assertEqual(self.migrate().code, "CONFLICT")
        self.assertEqual(review.reload().outcome, "FIELD_CONFLICT")
        version = str(review.modified)
        try:
            frappe.set_user("Guest")
            with self.assertRaises(frappe.PermissionError):
                conflict_report.resolve_field(review.name, version, "HERO")
        finally:
            frappe.set_user("Administrator")
        with patch.object(conflict_report, "_read_reviewed_record", return_value=(self.row, self.plan, 1, "id")):
            target.tax_id = "new local"
            target.save()
            with self.assertRaises(frappe.ValidationError):
                conflict_report.resolve_field(review.name, version, "HERO")
            target.tax_id = "local"
            target.save()
            preview = conflict_report.preview_field(review.name, version, "HERO")
            conflict_report.resolve_field(review.name, version, "HERO", confirmed=True,
                confirmation_fingerprint=preview["confirmation_fingerprint"])
            with self.assertRaises(frappe.ValidationError):
                conflict_report.resolve_field(review.name, version, "HERO")
        self.assertEqual((target.reload().tax_id, target.customer_details), ("remote", "independent"))
        self.assertEqual(review.reload().outcome, "RESOLVED")
        self.assertEqual(store.field(frappe.local.site, fields["tax_id"].identity_key).acknowledged_value_json, '"remote"')

    def test_real_projects_continue_other_fields_and_other_objects(self):
        from hero_integration.services.binding_registry import FrappeBindingStorage, native_fingerprint
        from hero_integration.services.mapping_registry_model import MappingDirection
        from hero_integration.services.sync_engine import SyncStatus

        operation = "HeroProjectMatches"
        rules = tuple(MappingSpec(operation, field, "Project", field, "string", "Field", "Identity",
                                  definition_id="test-" + field) for field in ("priority", "is_active"))
        plan = replace(self.plan, operation=operation, target_operation=operation, definitions=rules, adapter_options={},
                       binding_contract=BindingContract(self.source, "ProjectMatch", "test-projects", 1))
        store = FrappeBindingStorage()
        registry = BindingRegistryService(store)
        index = FrappeBoundIndex(object_type="ProjectMatch")
        docs = []
        for external in ("11", "12"):
            doc = frappe.get_doc({"doctype": "Project", "project_name": self.number + "-" + external,
                                  "company": frappe.db.get_value("Company", {}, "name"),
                                  "priority": "Medium", "is_active": "Yes"}).insert()
            docs.append(doc)
            parent = registry.create_or_resolve_object_binding(site_scope=frappe.local.site, source_instance=self.source,
                object_type="ProjectMatch", external_object_id=external, target_app="erpnext", target_doctype=doc.doctype,
                target_name=doc.name, mapping_definition_id="test-projects", mapping_revision=1)
            for rule in rules:
                binding = registry.create_or_resolve_field_binding(parent, source_field=rule.source_path,
                    target_field=rule.target_field, mapping_definition_id=rule.definition_id, mapping_revision=1,
                    direction=MappingDirection.HERO_TO_FRAPPE)
                fp = native_fingerprint(doc.get(rule.target_field), "Select")
                registry.record_verified_acknowledgment(binding, expected_revision=binding.revision,
                    mapping_definition_id=rule.definition_id, mapping_revision=1, source_fingerprint=fp,
                    target_fingerprint=fp, evidence_id=self.source, version=1)
        docs[0].priority = "High"
        docs[0].save()
        processor = FrappeBoundProcessor(1, [r.definition_id for r in rules], run_id=self.source,
                                         object_type="ProjectMatch", mapping_name="test-projects")
        with patch.object(processor, "_approved_plan", return_value=plan):
            for _ in range(2):
                results = []
                for external in index.identities(frappe.local.site, self.source):
                    fields = index.fields_for_ids(frappe.local.site, self.source, (external,))
                    results.append(processor.process_event({"id": external, "priority": "Low", "is_active": "No"},
                                                           fields, str(_), _))
                self.assertEqual(results, [SyncStatus.CONFLICT, True])
        self.assertEqual(list(index.identities(frappe.local.site, self.source, after="11")), ["12"])
        self.assertEqual([(d.reload().priority, d.is_active) for d in docs], [("High", "No"), ("Low", "No")])
        fields = {(f.object_id, f.binding.target_field): f.binding for f in index.fields(frappe.local.site, self.source)}
        self.assertEqual(fields["11","priority"].acknowledged_version, 1)
        self.assertEqual(fields["11","is_active"].acknowledged_version, 2)
        self.assertEqual(fields["12","priority"].acknowledged_version, 2)
        from hero_integration.services import conflict_report
        review = frappe.get_doc("HERO Sync Change Event", {"open_field": fields["11", "priority"].identity_key})
        version = str(review.modified)
        for observed in ({"id": 11}, {"id": 12, "is_deleted": False}, {"id": 11, "is_deleted": True},
                         {"id": 11, "is_deleted": 0}, {"id": 11, "is_deleted": False}):
            row = dict(observed, priority="Low")
            with self.subTest(observed=observed), patch.object(conflict_report, "_read_reviewed_record",
                                                              return_value=(row, plan, 1, "id")):
                if observed.get("is_deleted") is False and observed["id"] == 11:
                    self.assertEqual(conflict_report.preview_field(review.name, version, "HERO")["field"], "priority")
                else:
                    with self.assertRaises(frappe.ValidationError):
                        conflict_report.preview_field(review.name, version, "HERO")

        self.assertEqual(frappe.db.count("HERO Sync Change Event", {"source_instance": self.source,
                                                                    "outcome": "FIELD_CONFLICT"}), 1)
