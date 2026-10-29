import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import adoption, mapping_exchange, schema_provisioning
from hero_integration.tests.test_schema_provisioning import _Frappe, package


class TestAdoptionRecordSecurity(unittest.TestCase):
    def setUp(self):
        self.f = _Frappe()
        self.f.snapshot.source_instance = "source-a"
        self.f.snapshot.endpoint_fingerprint = "endpoint-a"
        self.f.snapshot.schema_version = 1
        self.f.snapshot.modified = "2026-10-26 10:00:00"
        for module in (adoption, mapping_exchange, schema_provisioning):
            p = patch.object(module, "frappe", self.f)
            p.start()
            self.addCleanup(p.stop)
        self.payload = json.dumps(package())

    def test_historical_queued_record_remains_unchanged_and_requires_new_preview(self):
        row = {"name": "legacy", "doctype": "HERO Mapping Adoption", "owner": self.f.session.user,
               "schema_snapshot": "SYNTHETIC", "package_json": self.payload, "selected_ids_json": "[]", "status": "Queued"}
        self.f.adoptions["legacy"] = row
        before = copy.deepcopy(row)
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Legacy adoption requires a new preview"):
            adoption.execute("legacy")
        self.assertEqual(row, before)
        self.assertEqual(self.f.mutations, [])

    def test_foreign_record_cannot_be_resumed(self):
        row = {"name": "foreign", "doctype": "HERO Mapping Adoption", "owner": "another-user",
               "schema_snapshot": "SYNTHETIC", "package_json": self.payload, "selected_ids_json": "[]", "status": "Queued"}
        self.f.adoptions["foreign"] = row
        before = copy.deepcopy(row)
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Only the adoption owner"):
            adoption.execute("foreign")
        self.assertEqual(row, before)
        self.assertEqual(self.f.mutations, [])

    def test_missing_site_key_fails_before_record_creation(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", self.payload)
        self.f.local.conf.clear()
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "encryption key"):
            adoption.start("SYNTHETIC", self.payload, ["field:customer-schema-value"], preview["preview_revision"])
        self.assertEqual(self.f.mutations, [])

    def test_forged_record_binding_rejected_without_mutation(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", self.payload)
        selection = {"ids": [], "preview": preview, "binding": "0" * 64}
        row = {"name": "forged", "doctype": "HERO Mapping Adoption", "owner": self.f.session.user,
               "schema_snapshot": "SYNTHETIC", "package_json": self.payload,
               "selected_ids_json": json.dumps(selection), "status": "Queued"}
        self.f.adoptions["forged"] = row
        before = copy.deepcopy(row)
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "confirmation is invalid"):
            adoption.execute("forged")
        self.assertEqual(row, before)
        self.assertEqual(self.f.mutations, [])

    def test_source_or_snapshot_revision_change_invalidates_preview(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", self.payload)
        self.f.snapshot.source_instance = "source-b"
        selected = [item["id"] for item in preview["items"] if item["selectable"]]
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "stale"):
            schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, preview["preview_revision"])
        self.assertEqual(self.f.mutations, [])

    def test_copied_binding_cannot_authorize_another_record(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", self.payload)
        selection = {"ids": ["field:customer-schema-value"], "preview": preview}
        binding = adoption._binding("SYNTHETIC", self.payload, selection, "original")
        row = {"name": "copy", "doctype": "HERO Mapping Adoption", "owner": self.f.session.user,
               "schema_snapshot": "SYNTHETIC", "package_json": self.payload,
               "selected_ids_json": json.dumps({**selection, "binding": binding}), "status": "Queued"}
        self.f.adoptions["copy"] = row
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "confirmation is invalid"):
            adoption.execute("copy")
        self.assertEqual(self.f.mutations, [])

    def test_resume_rejects_changed_source_context_before_ddl(self):
        preview = schema_provisioning.preview_package("SYNTHETIC", self.payload)
        selection = {"ids": ["field:customer-schema-value"], "preview": preview}
        binding = adoption._binding("SYNTHETIC", self.payload, selection, "pending")
        row = {"name": "pending", "doctype": "HERO Mapping Adoption", "owner": self.f.session.user,
               "schema_snapshot": "SYNTHETIC", "package_json": self.payload,
               "selected_ids_json": json.dumps({**selection, "binding": binding}), "status": "Queued"}
        self.f.adoptions["pending"] = row
        self.f.snapshot.endpoint_fingerprint = "endpoint-b"
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Snapshot context changed"):
            adoption.execute("pending")
        self.assertEqual(self.f.mutations, [])
        self.assertEqual(self.f.mappings, {})

    def test_source_context_change_during_ddl_is_rejected_before_mapping_save(self):
        data = package()
        data["mappings"] = []
        payload = json.dumps(data)
        preview = schema_provisioning.preview_package("SYNTHETIC", payload)
        original = schema_provisioning._create_custom_field
        def competing_update(definition):
            original(definition)
            self.f.snapshot.endpoint_fingerprint = "endpoint-b"
        with patch.object(schema_provisioning, "_create_custom_field", side_effect=competing_update):
            with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Snapshot context changed"):
                schema_provisioning.adopt_package("SYNTHETIC", payload, ["field:customer-schema-value"], True, preview["preview_revision"])
        self.assertEqual(self.f.mappings, {})

    def test_valid_schema_only_selection(self):
        data = package()
        data["mappings"] = []
        payload = json.dumps(data)
        preview = schema_provisioning.preview_package("SYNTHETIC", payload)
        result = adoption.start("SYNTHETIC", payload, ["field:customer-schema-value"], preview["preview_revision"])
        self.assertEqual(result["created"]["custom_fields"], ["Customer.custom_hero_schema_value"])
        self.assertEqual(result["created"]["mappings"], [])

    def test_controller_rejects_direct_record_creation_or_update(self):
        from hero_integration.hero_integration.doctype.hero_mapping_adoption import hero_mapping_adoption as controller
        doc = SimpleNamespace(package_json="{}")
        fake = SimpleNamespace(only_for=lambda role: None, throw=lambda message: (_ for _ in ()).throw(PermissionError(message)))
        with patch.object(controller, "frappe", fake):
            with self.assertRaises(PermissionError):
                controller.HEROMappingAdoption.validate(doc)
            with controller.service_write_context():
                controller.HEROMappingAdoption.validate(doc)
            with self.assertRaises(PermissionError):
                controller.HEROMappingAdoption.validate(doc)
