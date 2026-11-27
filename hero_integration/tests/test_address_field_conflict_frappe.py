"""Primary Address reconciliation with actual Frappe saves, row locks and persisted field reviews."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services import conflict_report, migration
from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.mapping_runtime import BindingContract, MappingPlan
from hero_integration.services.query_mapping import address_values


class TestAddressFieldConflict(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.instance = "address-" + uuid4().hex[:12]
        self.storage = FrappeBindingStorage()
        if not frappe.db.exists("Address Template", "Germany"):
            frappe.get_doc({"doctype": "Address Template", "country": "Germany",
                            "template": "{{ city }} {{ pincode }}"}).insert()
        self.customer = frappe.get_doc({"doctype": "Customer", "customer_name": self.instance,
                                       "customer_type": "Company"}).insert()
        rules = [MappingSpec("HeroCustomers", "id", "HERO Integration Link", "source_id", "int",
            "Source Identity", "Positive Int To Text", definition_id="source-id")]
        for path, field in (("street", "address_line1"), ("city", "city"), ("zipcode", "pincode"),
                            ("country", "country")):
            rules.append(MappingSpec("HeroCustomers", "address." + path, "Address", field, "string",
                "Field", "Trim", "Customer Primary Address", definition_id="address-" + field))
        self.plan = MappingPlan("HeroCustomers", tuple(rules), "HeroCustomers", {"version": 2},
                                BindingContract(self.instance, "Contact", "customer-mapping", 1))
        self.row = {"id": 7, "is_deleted": False, "is_contact_person": False, "address": {
            "id": 21, "street": "Teststraße 1", "country": "Germany", "city": "Berlin", "zipcode": "10115"}}
        # Match the parent evidence established by the completed Customer import.
        BindingRegistryService(self.storage).create_or_resolve_object_binding(
            site_scope=frappe.local.site, source_instance=self.instance, object_type="Contact",
            external_object_id="7", target_app="erpnext", target_doctype="Customer", target_name=self.customer.name,
            mapping_definition_id="customer-mapping", mapping_revision=1)
        frappe.get_doc({"doctype": "HERO Integration Link", "source_type": "customer", "source_id": "7",
            "source_instance": self.instance, "target_doctype": "Customer", "target_name": self.customer.name,
            "source_payload_hash": "a"*64, "target_payload_hash": "a"*64,
            "mapping_version": self.plan.mapping_version}).insert()

    def sync_address(self):
        return migration._synchronize_customer_relations(self.customer, {}, None, address_values(self.plan, self.row),
            self.instance, self.plan.mapping_version, self.plan, self.row, "fixture")

    def fields(self, source_id):
        parent = self.storage.lock_source_object(frappe.local.site, self.instance, "customer_address", "Address", source_id)
        return {f.target_field: f for f in self.storage.fields_for_object(frappe.local.site, parent.identity_key)}

    def test_empty_postcode_creates_no_field_baseline_and_preserves_verified_native_postcode(self):
        self.row['address']['zipcode'] = ''
        self.sync_address()
        self.sync_address()
        link = migration._link('customer_address', '21', self.instance)
        self.assertFalse(frappe.db.get_value('Address', link.target_name, 'pincode'))
        self.assertNotIn('pincode', self.fields('21'))
        self.assertNotIn('pincode', migration.decode_hashes(link.source_field_hashes))
        self.row['address']['zipcode'] = '10115'
        self.sync_address()
        target = frappe.get_doc('Address', link.target_name)
        self.assertEqual(target.pincode, '10115')
        field = self.fields('21')['pincode']
        target.pincode = '10117'
        target.save()
        self.row['address'].update(zipcode='', city='Bonn')
        self.sync_address()
        target.reload()
        self.assertEqual((target.pincode, target.city), ('10117', 'Bonn'))
        self.assertEqual(self.fields('21')['pincode'], field)
        link.reload()
        self.assertEqual(migration.decode_hashes(link.source_field_hashes)['pincode'],
                         migration.field_hashes({'pincode': '10115'})['pincode'])

    def test_city_conflict_preserves_baseline_and_allows_independent_postcode_and_next_address(self):
        self.sync_address()
        link = migration._link("customer_address", "21", self.instance)
        target = frappe.get_doc("Address", link.target_name)
        initial = self.fields("21")
        target.city = "Kiel"
        target.save()
        self.row["address"].update(city="Bonn", zipcode="10117")
        self.assertEqual(self.sync_address(), "conflict")
        target.reload()
        self.assertEqual((target.city, target.pincode), ("Kiel", "10117"))
        fields = self.fields("21")
        self.assertEqual(fields["city"], initial["city"])
        self.assertEqual(fields["pincode"].acknowledged_version, initial["pincode"].acknowledged_version + 1)
        self.assertEqual(json.loads(fields["pincode"].acknowledged_value_json), "10117")
        link.reload()
        hashes = migration.decode_hashes(link.target_field_hashes)
        self.assertEqual(hashes["city"], migration.field_hashes({"city":"Berlin"})["city"])
        self.assertEqual(hashes["pincode"], migration.field_hashes({"pincode":"10117"})["pincode"])
        event = frappe.get_doc("HERO Sync Change Event", {"open_field": fields["city"].identity_key})
        proof = json.loads(event.field_changes)
        self.assertEqual((proof["baseline"], proof["local"], proof["source"]), ("Berlin", "Kiel", "Bonn"))
        self.assertEqual((event.external_id, proof["reader"]["external_id"]), ("21", "7"))
        self.assertEqual(self.sync_address(), "conflict")
        self.assertEqual(self.fields("21"), fields)
        self.assertEqual(frappe.db.count("HERO Sync Change Event", {"object_binding": event.object_binding}), 1)
        # Only the approved network read is substituted. Binding, ownership, permission and value checks are native.
        with patch.object(conflict_report, "_read_reviewed_record", return_value=(self.row, self.plan, 1, "id")):
            self.assertEqual(conflict_report._review_field(event.name, str(event.modified))[5], "Bonn")
            self.row["address"]["id"] = 22
            with self.assertRaises(frappe.ValidationError):
                conflict_report._review_field(event.name, str(event.modified))
        self.row["address"].update(city="Hamburg", zipcode="20095")
        self.sync_address()
        other = self.fields("22")
        self.assertEqual(json.loads(other["pincode"].acknowledged_value_json), "20095")
        self.assertEqual(frappe.db.get_value("Address", target.name, "city"), "Kiel")
        self.assertEqual(frappe.db.get_value("HERO Sync Change Event", event.name, "outcome"), "FIELD_CONFLICT")

    def test_customer_import_and_completed_relationship_pass_preserve_partial_field_ack(self):
        from hero_integration.services import deferred_references
        plan = self.customer_plan()
        row = dict(self.row, id=8, display=self.instance + "-owner", kind="Company", notes="first",
                   _hero_source_instance=self.instance)
        run = SimpleNamespace(mode="Execute", state="Running", name="fixture")

        def import_record(*, relations=False):
            run.state = "Completed" if relations else "Running"
            token = deferred_references._replaying.set(relations)
            try:
                return migration._migrate_with_binding(row, run, plan, "customer",
                    lambda: migration._migrate_customer(row, run, plan=plan), relationships_only=relations)
            finally:
                deferred_references._replaying.reset(token)

        initial = import_record()
        self.assertEqual(initial.code, "CREATED", initial.detail)
        linked = import_record(relations=True)
        self.assertIn(linked.code, {"UPDATED", "SKIPPED", "CONVERGED"}, linked.detail)
        link = migration._link("customer_address", "21", self.instance)
        target = frappe.get_doc("Address", link.target_name)
        target.city = "Kiel"
        target.save()
        row["address"].update(city="Bonn", zipcode="10117")
        row["notes"] = "second"
        changed = import_record()
        self.assertEqual(changed.code, "UPDATED", changed.detail)
        conflict = import_record(relations=True)
        target.reload()
        self.assertEqual((target.city, target.pincode), ("Kiel", "10117"), conflict.detail)
        self.assertEqual(conflict.code, "CONFLICT", conflict.detail)
        self.assertEqual(frappe.db.get_value("Customer", initial.target_name, "customer_details"), "second")
        fields = self.fields("21")
        event = frappe.get_doc("HERO Sync Change Event", {"open_field": fields["city"].identity_key})
        with patch.object(conflict_report, "_read_reviewed_record", return_value=(row, plan, 1, "id")):
            version = str(event.modified)
            preview = conflict_report.preview_field(event.name, version, "HERO")
            conflict_report.resolve_field(event.name, version, "HERO",
                confirmation_fingerprint=preview["confirmation_fingerprint"], confirmed=True)
        self.assertEqual(frappe.db.get_value("Address", target.name, "city"), "Bonn")
        reconciled = import_record(relations=True)
        self.assertIn(reconciled.code, {"SKIPPED", "CONVERGED", "OBSERVED", "UPDATED"}, reconciled.detail)
        self.assertFalse(frappe.db.exists("HERO Sync Change Event", {"open_field": fields["city"].identity_key}))
        self.assertEqual(json.loads(self.fields("21")["city"].acknowledged_value_json), "Bonn")
        link.reload()
        self.assertEqual(migration.decode_hashes(link.target_field_hashes)["city"],
                         migration.field_hashes({"city": "Bonn"})["city"])

    def test_historical_or_unwitnessed_child_cannot_write_a_same_named_address(self):
        self.sync_address()
        link = migration._link("customer_address", "21", self.instance)
        before = frappe.db.get_value("Address", link.target_name, "pincode")
        self.row["address"]["zipcode"] = "10117"
        parent = self.storage.lock_source_object(frappe.local.site, self.instance, "customer_address", "Address", "21")
        frappe.db.set_value("HERO Object Binding", parent.identity_key, "lifecycle", "LEGACY")
        from hero_integration.services.binding_registry import BindingConflict
        with self.assertRaises(BindingConflict):
            self.sync_address()
        # Pre-incarnation installations may have only field hashes, or a previous target creation timestamp.
        address = migration._customer_primary_address(dict(address={
            "id":21, "street":"Teststraße 1", "city":"Berlin", "zipcode":"10117"}))
        for witness in ("", "2000-01-01T00:00:00.000000"):
            frappe.db.set_value("HERO Integration Link", link.name, "target_incarnation", witness)
            with self.assertRaises(migration.CustomerAddressError):
                migration._synchronize_customer_relations(self.customer, {}, None, address,
                                                          self.instance, self.plan.mapping_version)
        self.assertEqual(frappe.db.get_value("Address", link.target_name, "pincode"), before)

    def customer_plan(self):
        rules = [MappingSpec("HeroCustomers", path, "Customer", field, "string", purpose, "Trim",
                             definition_id=field) for path, field, purpose in (
            ("display", "customer_name", "Record Name"), ("kind", "customer_type", "Customer Type"),
            ("notes", "customer_details", "Field"))]
        return replace(self.plan, definitions=self.plan.definitions + tuple(rules))

    def test_persisted_parent_field_review_admits_address_through_deferred_queue(self):
        from hero_integration.services import deferred_references as deferred
        from hero_integration.client import settings
        plan = self.customer_plan()
        row = dict(self.row, id=9, display=self.instance + "-queued", kind="Company", notes="original",
                   _hero_source_instance=self.instance)
        run = frappe.get_doc({"doctype":"HERO Integration Run", "action":"Customer Migration",
            "mode":"Execute", "state":"Running", "source_instance":self.instance, "source_binding_revision":1}).insert()
        pending = deferred.observation_key(self.instance, "HeroCustomers", "9", run.name)
        configuration = SimpleNamespace(source_instance=self.instance, source_binding_confirmed=True,
                                        source_binding_revision=1)
        other = dict(row, id=10, display=self.instance + "-independent", address=dict(row["address"], id=22))
        other_pending = deferred.observation_key(self.instance, "HeroCustomers", "10", run.name)

        def import_and_remember(record=row):
            result = migration._migrate_with_binding(record, run, plan, "customer",
                lambda: migration._migrate_customer(record, run, plan=plan))
            deferred.remember(run, "customer", record, result)
            return result
        with patch.object(migration, "_mapping_plan_for_run", return_value=plan), \
             patch.object(migration, "validate_run_mappings"), \
             patch.object(migration, "_source_for_run", return_value=configuration), \
             patch.object(settings, "get_configuration", return_value=configuration):
            created = import_and_remember()
            self.assertEqual(created.code, "CREATED", created.detail)
            self.assertEqual(import_and_remember(other).code, "CREATED")
            frappe.db.set_value(run.doctype, run.name, "state", "Completed")
            with patch.object(migration, "_migrate_customer", wraps=migration._migrate_customer) as full_writer:
                deferred.resolve_one(other_pending)
                full_writer.assert_called_once()  # Ordinary success still runs the complete Customer relationship adapter.
            deferred.resolve_one(pending)
            self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, "status"), "Resolved")
            customer = frappe.get_doc("Customer", created.target_name)
            customer.customer_details = "edited in ERPNext"
            customer.save()
            row["notes"] = "edited in HERO"
            row["address"]["zipcode"] = "10117"
            frappe.db.set_value(run.doctype, run.name, "state", "Running")
            other["address"]["zipcode"] = "20095"
            independent = import_and_remember(other)
            self.assertIn(independent.code, {"SKIPPED", "UPDATED", "OBSERVED", "CONVERGED"}, independent.detail)
            conflict = import_and_remember()
            self.assertEqual(conflict.code, "CONFLICT", conflict.detail)
            self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, "status"), "Waiting")
            address = migration._link("customer_address", "21", self.instance).target_name
            deferred.resolve_one(pending)
            self.assertEqual(frappe.db.get_value("Address", address, "pincode"), "10115")
            frappe.db.set_value(run.doctype, run.name, "state", "Completed With Conflicts")
            with patch.object(frappe, "enqueue") as enqueue:
                deferred.notify_completed_run(run.name)
                enqueue.assert_called_once()
            deferred.resolve_one(pending)
            self.assertEqual(frappe.db.get_value("Address", address, "pincode"), "10117")
            self.assertEqual(frappe.db.get_value(deferred.MAIN, pending, "status"), "Resolved")
            self.assertEqual(json.loads(self.fields("21")["pincode"].acknowledged_value_json), "10117")
            self.assertEqual(frappe.db.get_value("Customer", customer.name, "customer_details"), "edited in ERPNext")
            event = frappe.get_doc("HERO Sync Change Event", {"source_instance":self.instance,
                "outcome":"FIELD_CONFLICT", "external_id":"9"})
            self.assertEqual(json.loads(event.field_changes)["baseline"], "original")
            deferred.resolve_one(other_pending)
            other_target = migration._link("customer_address", "22", self.instance).target_name
            self.assertEqual(frappe.db.get_value("Address", other_target, "pincode"), "20095")
            self.assertEqual(frappe.db.get_value(deferred.MAIN, other_pending, "status"), "Resolved")
            self.assertEqual(json.loads(self.fields("22")["pincode"].acknowledged_value_json), "20095")
