"""Restartable scheduled inventory on the real Frappe state and receipt tables."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from graphql import build_schema, introspection_from_schema

from hero_integration.client.graphql import HeroGraphQLClient
from hero_integration.services.sync_engine import FrappeSyncLedger
from hero_integration.services.sync_runtime_adapter import run_scheduled_auto_sync
from hero_integration.tests.test_sync_native_events import NativeCustomerSyncFixture


class TestScheduledInventory(NativeCustomerSyncFixture, IntegrationTestCase):
    read_arguments = {"first": 2, "offset": 0, "orderBy": "id"}
    read_variables = {"first": "$page_size", "offset": "$offset", "orderBy": "id"}
    read_page_size = 2

    def source_schema(self):
        return introspection_from_schema(build_schema("""
            type Query { contacts(first:Int!, offset:Int!, orderBy:String!, ids:[Int!]): [Contact] }
            type Contact { id:ID category:String is_contact_person:Boolean is_deleted:Boolean }
        """))

    def test_budgeted_inventory_restarts_after_failure_and_keeps_unbound_evidence(self):
        _, approved, _, config = self.install_runtime_plan("AUTO_SYNC")
        dt = "HERO Integration Settings"
        options = {"polling_full_inventory": 1, "polling_page_size": 2, "polling_page_overlap": 0,
                   "polling_allow_backfill": 0, "allow_graphql_mutation_execution": 0}
        saved = {key: frappe.db.get_single_value(dt, key) for key in options}
        ids = [str(int(self.identity) + i) for i in range(3)]
        def cleanup():
            frappe.db.rollback()
            frappe.db.set_single_value(dt, saved)
            frappe.db.delete("HERO Sync Change Event", {"external_id": ["in", ids], "mapping_name": self.mapping.name})
            frappe.db.commit()
        self.addCleanup(cleanup)
        frappe.db.set_single_value(dt, options)
        frappe.db.commit()
        pages = []
        def read(query, variables):
            from graphql import parse
            root = parse(query).definitions[0].selection_set.selections[0]
            args = {arg.name.value: variables[arg.value.name.value] for arg in root.arguments}
            pages.append(args["offset"])
            return {"contacts": [{**self.payload, "id": key} for key in ids[args["offset"]:][:args["first"]]]}
        with patch("hero_integration.client.settings.get_configuration", return_value=config), \
                patch.object(HeroGraphQLClient, "query", side_effect=read):
            original = FrappeSyncLedger.save_state
            def interrupted(ledger, state, expected):
                if state.inventory_checkpoint:
                    raise RuntimeError("synthetic interruption before checkpoint")
                return original(ledger, state, expected)
            with patch.object(FrappeSyncLedger, "save_state", interrupted):
                self.assertEqual(run_scheduled_auto_sync().status, "FAILED")
            self.assertIsNone(self.ledger.state(self.site).inventory_checkpoint)
            self.assertEqual(self.doc.reload().customer_details, self.identity)
            self.assertEqual(run_scheduled_auto_sync().status, "PARTIAL")
            self.assertEqual(self.doc.reload().customer_details, "revised")
            self.assertEqual(FrappeSyncLedger().state(self.site).inventory_checkpoint["next_offset"], 2)
            self.assertEqual(run_scheduled_auto_sync().status, "PARTIAL")
            self.assertEqual(run_scheduled_auto_sync().status, "PARTIAL")  # End of a pass with unbound rows.
        state = self.ledger.state(self.site)
        self.assertEqual(pages, [0, 0, 2, 3])
        self.assertIsNone(state.inventory_checkpoint)
        self.assertIsNone(state.cursor)
        self.assertEqual(frappe.db.count("HERO Sync Change Event", {"mapping_name": self.mapping.name,
                         "external_id": ["in", ids[1:]], "outcome": "DEFERRED"}), 2)
        self.assertEqual(frappe.db.count("HERO Sync Run Ledger", {"plan_key": approved.plan_key, "execution_scope": "INVENTORY"}), 4)

    def test_absence_from_partial_and_finished_inventory_does_not_retire_bound_customer(self):
        _, _, _, config = self.install_runtime_plan("AUTO_SYNC")
        settings = "HERO Integration Settings"
        options = {"polling_full_inventory": 1, "polling_page_size": 2, "polling_page_overlap": 0,
                   "polling_allow_backfill": 0, "allow_graphql_mutation_execution": 0}
        prior = {key: frappe.db.get_single_value(settings, key) for key in options}
        absent = self.identity
        present = [str(int(absent) + delta) for delta in (1, 2)]
        def cleanup():
            frappe.db.rollback()
            frappe.db.set_single_value(settings, prior)
            frappe.db.delete("HERO Sync Change Event", {"mapping_name": self.mapping.name, "external_id": ["in", present]})
            frappe.db.commit()
        self.addCleanup(cleanup)
        frappe.db.set_single_value(settings, options)
        frappe.db.commit()
        bound_name = self.parent.identity_key
        before = frappe.db.get_value("HERO Object Binding", bound_name, ["lifecycle", "binding_revision", "target_name"])
        self.assertEqual(before[0], "VALID")
        remote = [{"contacts": [dict(self.payload, id=key) for key in present]}, {"contacts": []}]
        with patch("hero_integration.client.settings.get_configuration", return_value=config), \
                patch.object(HeroGraphQLClient, "query", side_effect=remote):
            for more in (True, False):
                self.assertEqual(run_scheduled_auto_sync().status, "PARTIAL")
                self.assertEqual(bool(self.ledger.state(self.site).inventory_checkpoint), more)
                self.assertEqual(self.doc.reload().customer_details, absent)
                self.assertEqual(frappe.db.get_value("HERO Object Binding", bound_name,
                                 ["lifecycle", "binding_revision", "target_name"]), before)

    def test_multiple_small_reader_pages_share_a_tick_budget_and_resume_after_network_failure(self):
        from graphql import parse
        _, _, _, config = self.install_runtime_plan("AUTO_SYNC")
        settings = "HERO Integration Settings"
        options = {"polling_full_inventory":1, "polling_page_size":6, "polling_page_overlap":0,
                   "polling_allow_backfill":0, "allow_graphql_mutation_execution":0}
        saved = {key:frappe.db.get_single_value(settings,key) for key in options}
        ids = [str(int(self.identity)+i) for i in range(5)]
        def cleanup():
            frappe.db.rollback()
            frappe.db.set_single_value(settings,saved)
            frappe.db.delete("HERO Sync Change Event", {"external_id":["in",ids],"mapping_name":self.mapping.name})
            frappe.db.commit()
        self.addCleanup(cleanup)
        frappe.db.set_single_value(settings,options)
        frappe.db.commit()
        offsets = []
        def transport(document, variables):
            root = parse(document).definitions[0].selection_set.selections[0]
            args = {arg.name.value:variables[arg.value.name.value] for arg in root.arguments}
            start = args["offset"]
            offsets.append(start)
            if len(offsets) == 2:
                raise TimeoutError("Synthetic interrupted second page")
            return {"contacts":[{**self.payload,"id":key} for key in ids[start:start+args["first"]]]}
        with patch("hero_integration.client.settings.get_configuration",return_value=config), patch.object(HeroGraphQLClient,"query",side_effect=transport):
            self.assertEqual(run_scheduled_auto_sync().status, "FAILED")
            self.assertEqual(self.ledger.state(self.site).inventory_checkpoint["next_offset"], 2)
            self.assertEqual(self.doc.reload().customer_details, "revised")
            self.assertEqual(run_scheduled_auto_sync().status, "PARTIAL")
        self.assertEqual(offsets, [0,2,2,4,5])
        self.assertIsNone(self.ledger.state(self.site).inventory_checkpoint)
        self.assertIsNone(self.ledger.state(self.site).cursor)

    def test_stale_field_revision_defers_only_its_own_record_in_the_inventory_page(self):
        from hero_integration.services.mapping_registry_model import MappingDirection
        from graphql import parse

        _, _, _, config = self.install_runtime_plan("AUTO_SYNC")
        identity = str(int(self.identity) + 1)
        customer = frappe.get_doc({"doctype":"Customer", "customer_name":identity, "customer_type":"Company",
                                   "customer_details":"unchanged"}).insert()
        parent = self.registry.create_or_resolve_object_binding(site_scope=self.site, source_instance="hero",
            object_type=self.object_type, external_object_id=identity, target_app="erpnext", target_doctype="Customer",
            target_name=customer.name, mapping_definition_id=self.rule.definition_id, mapping_revision=2)
        field = self.registry.create_or_resolve_field_binding(parent, source_field=self.source_field,
            target_field=self.native_field, mapping_definition_id=self.rule.definition_id, mapping_revision=2,
            direction=MappingDirection.HERO_TO_FRAPPE)
        settings = "HERO Integration Settings"
        options = {"polling_full_inventory":1, "polling_page_size":4, "polling_page_overlap":0,
                   "polling_allow_backfill":0, "allow_graphql_mutation_execution":0}
        saved = {key:frappe.db.get_single_value(settings,key) for key in options}
        def cleanup():
            frappe.db.rollback()
            frappe.db.set_single_value(settings,saved)
            frappe.db.delete("HERO Sync Change Event", {"mapping_name":self.mapping.name,"external_id":identity})
            frappe.db.delete("HERO Field Binding",field.identity_key)
            frappe.db.delete("HERO Object Binding",parent.identity_key)
            frappe.db.delete("Customer",customer.name)
            frappe.db.commit()
        self.addCleanup(cleanup)
        frappe.db.set_single_value(settings,options)
        frappe.db.commit()
        def read(doc, values):
            root = parse(doc).definitions[0].selection_set.selections[0]
            offset = next(values[arg.value.name.value] for arg in root.arguments if arg.name.value == "offset")
            return {"contacts":[self.payload, dict(self.payload, id=identity)][offset:]}
        with patch("hero_integration.client.settings.get_configuration", return_value=config), patch.object(HeroGraphQLClient, "query", side_effect=read):
            self.assertEqual(run_scheduled_auto_sync().status, "PARTIAL")
        self.assertEqual(self.doc.reload().customer_details, "revised")
        self.assertEqual(customer.reload().customer_details, "unchanged")
        self.assertEqual(frappe.db.get_value("HERO Field Binding",field.identity_key,"mapping_revision"), 2)
        self.assertEqual(frappe.db.count("HERO Sync Change Event", {"mapping_name":self.mapping.name,
                         "external_id":identity, "outcome":"DEFERRED"}), 1)
        self.assertIsNone(self.ledger.state(self.site).inventory_checkpoint)

    def test_primary_address_save_records_only_the_owners_mapped_field_fingerprint(self):
        import json
        from pathlib import Path
        from hero_integration.services.binding_registry import native_fingerprint
        from hero_integration.services.change_detection import related_document_key

        cfg = json.loads((Path(__file__).parents[1] / "config/migration_baseline.json").read_text())
        mapping = next(m["content"] for m in cfg["mutation_mappings"] if m["content"].get("area") == "HeroCustomers")
        if not frappe.db.exists("Address Template", "Germany"):
            frappe.get_doc({"doctype": "Address Template", "country": "Germany", "template": "{{ address_line1 }}"}).insert()
            self.addCleanup(frappe.db.delete, "Address Template", "Germany")
        address = frappe.get_doc({"doctype":"Address", "address_title":self.identity,
            "address_type":"Billing", "address_line1":"Synthetic street", "city":"Synthetic city",
            "country":"Germany",
            "links":[{"link_doctype":"Customer", "link_name":self.doc.name}]}).insert()
        self.addCleanup(frappe.db.delete, "Address", address.name)
        self.addCleanup(frappe.db.delete, "Dynamic Link", {"parenttype":"Address", "parent":address.name})
        frappe.db.set_value("Customer", self.doc.name, "customer_primary_address", address.name)
        get_all = frappe.get_all
        def mappings(dt, *args, **kwargs):
            if dt == "HERO GraphQL Mapping" and kwargs.get("pluck") == "mapping_json":
                return [json.dumps(mapping)]
            return get_all(dt, *args, **kwargs)
        with patch.object(frappe, "get_all", side_effect=mappings):
            address.city = "Changed local city"
            address.save()
        events = frappe.get_all("HERO Integration Record", filters={"correlation_id":self.parent.identity_key,
            "technical_action":"frappe_related_change_observed"}, fields=["target_name", "changed_fields", "native_state_json"])
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].target_name, events[0].changed_fields), (self.doc.name, "Address.city"))
        self.assertEqual(json.loads(events[0].native_state_json), {"_related_document":related_document_key(address),
            "Address.city":native_fingerprint(address.city, address.meta.get_field("city").fieldtype)})

        # Direct SQL writes bypass the Document hook and may preserve modified.
        from types import SimpleNamespace
        from hero_integration.services.change_detection import recover_related_changes
        from hero_integration.services.offset_pagination import fingerprint
        scan_key = fingerprint(["outbound-related-scan", self.parent.site_scope, self.parent.identity_key,
                                self.mapping.name, self.mapping.mapping_revision])
        self.addCleanup(frappe.db.delete, "HERO Sync Change Event", scan_key)
        ctx = SimpleNamespace(parent=self.parent, document=self.doc, mapping=self.mapping, source_documents={"city":address})
        difference = SimpleNamespace(changes=[{"id":"city","source_field":"city"}])
        def count():
            return frappe.db.count("HERO Integration Record", {"correlation_id":self.parent.identity_key,
                                   "technical_action":"frappe_related_change_observed"})
        recover_related_changes(ctx, difference, "synthetic")
        self.assertEqual(count(), 1)  # native hook already observed the same state
        stamp = frappe.db.get_value("Address", address.name, "modified")
        frappe.db.set_value("Address", address.name, "city", "SQL change", update_modified=False)
        address.reload()
        self.assertEqual(address.modified, stamp)
        for _ in range(2):
            recover_related_changes(ctx, difference, "synthetic")
        self.assertEqual(count(), 2)
        self.assertNotIn("SQL change", frappe.db.get_value("HERO Sync Change Event",scan_key,"field_changes"))
        recover_related_changes(ctx, SimpleNamespace(changes=[]), "synthetic")
        self.assertEqual(count(), 2)
