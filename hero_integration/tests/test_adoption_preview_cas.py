import json
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import adoption, mapping_exchange, schema_provisioning
from hero_integration.tests.test_schema_provisioning import _Frappe, package
from types import SimpleNamespace


class TestAdoptionPreviewCAS(TestCase):
    def setUp(self):
        self.f = _Frappe()
        self.f.session = SimpleNamespace(user="alice")
        for module in (adoption, mapping_exchange, schema_provisioning):
            p = patch.object(module, "frappe", self.f)
            p.start()
            self.addCleanup(p.stop)
        # This in-memory fixture has no site or database lock service. CAS
        # behavior is exercised here; real lock semantics need a site test.
        for target in (
            "hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping.lock_exchange_context",
            "hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target",
        ):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        self.payload = json.dumps(package())

    def preview(self):
        p = schema_provisioning.preview_package("SYNTHETIC", self.payload)
        return p, [item["id"] for item in p["items"] if item["selectable"]]

    def test_valid_preview_is_accepted_and_missing_revision_rejected(self):
        p, selected = self.preview()
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "stale or missing"):
            schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True)
        result = schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, p["preview_revision"])
        self.assertEqual(result["created"]["mappings"], ["customer-schema-value"])

    def test_stale_preview_rejected_before_schema_write(self):
        p, selected = self.preview()
        self.f.doctypes["HERO External Detail"] = {"custom": 0, "module": "Other", "istable": 0, "fields": {}}
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "stale"):
            schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, p["preview_revision"])
        self.assertEqual(self.f.mutations, [])

    def test_direct_adoption_requires_system_manager(self):
        p, selected = self.preview()
        with patch.object(self.f, "only_for", side_effect=PermissionError("role")):
            with self.assertRaises(PermissionError):
                schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, p["preview_revision"])
        self.assertEqual(self.f.mutations, [])

    def test_preview_cannot_be_reused_by_another_user(self):
        p, selected = self.preview()
        self.f.session.user = "bob"
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "stale"):
            schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, p["preview_revision"])
        self.assertEqual(self.f.mutations, [])

    def test_existing_foreign_draft_is_rejected_before_ddl(self):
        row = dict(package()["mappings"][0], name="customer-schema-value", status="Draft", owner="bob", schema_dependencies="[]", source_path="old")
        self.f.mappings[row["definition_id"]] = row
        p, selected = self.preview()
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "owned by another"):
            schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, p["preview_revision"])
        self.assertEqual(self.f.mutations, [])

    def test_tampered_selection_record_cannot_resume(self):
        p, selected = self.preview()
        self.f.fail_mapping_insert = True
        with self.assertRaises(RuntimeError):
            adoption.start("SYNTHETIC", self.payload, selected, p["preview_revision"])
        name = next(iter(self.f.adoptions))
        row = self.f.adoptions[name]
        selection = json.loads(row["selected_ids_json"])
        selection["ids"] = ["mapping:customer-schema-value"]
        row["selected_ids_json"] = json.dumps(selection)
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "confirmation is invalid"):
            adoption.execute(name)
        self.assertNotIn("customer-schema-value", self.f.mappings)

    def test_direct_adoption_record_writes_are_blocked(self):
        from hero_integration.hero_integration.doctype.hero_mapping_adoption import hero_mapping_adoption as controller
        document = SimpleNamespace(package_json="{}")
        facade = SimpleNamespace(only_for=lambda role: None, throw=lambda msg: (_ for _ in ()).throw(PermissionError(msg)))
        with patch.object(controller, "frappe", facade):
            with self.assertRaisesRegex(PermissionError, "managed by the adoption service"):
                controller.HEROMappingAdoption.validate(document)
            with controller.service_write_context():
                controller.HEROMappingAdoption.validate(document)
            self.assertTrue(document.package_fingerprint)
            with self.assertRaises(PermissionError):
                controller.HEROMappingAdoption.validate(document)

    def test_resume_rejects_mapping_created_after_failed_ddl(self):
        p, selected = self.preview()
        self.f.fail_mapping_insert = True
        with self.assertRaises(RuntimeError):
            adoption.start("SYNTHETIC", self.payload, selected, p["preview_revision"])
        name = next(iter(self.f.adoptions))
        row = dict(package()["mappings"][0], name="customer-schema-value", status="Draft", owner="bob", schema_dependencies="[]")
        self.f.mappings[row["definition_id"]] = row
        self.f.fail_mapping_insert = False
        with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "Mapping changed"):
            adoption.execute(name)
        self.assertEqual(self.f.mappings[row["definition_id"]]["status"], "Draft")

    def test_another_user_draft_appearing_during_ddl_is_not_overwritten(self):
        p, selected = self.preview()
        original = schema_provisioning._create_custom_field
        def competing_write(definition):
            original(definition)
            row = dict(package()["mappings"][0], name="customer-schema-value", status="Draft", owner="bob", schema_dependencies="[]")
            self.f.mappings[row["definition_id"]] = row
        with patch.object(schema_provisioning, "_create_custom_field", side_effect=competing_write):
            with self.assertRaisesRegex(schema_provisioning.SchemaProvisioningError, "changed during"):
                schema_provisioning.adopt_package("SYNTHETIC", self.payload, selected, True, p["preview_revision"])
        self.assertEqual(self.f.mappings["customer-schema-value"]["owner"], "bob")
        self.assertEqual(self.f.mappings["customer-schema-value"]["status"], "Draft")
