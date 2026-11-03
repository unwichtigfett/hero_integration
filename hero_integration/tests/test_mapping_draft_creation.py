"""New picker drafts retain the ordinary validator and server-owned identity boundary."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from graphql import build_schema, introspection_from_schema

from hero_integration import api
from hero_integration.services import mapping_draft_creation as creation
from hero_integration.services.mapping_draft_service import MappingDraftError


SCHEMA = introspection_from_schema(build_schema('''
    type Contact { id: ID!, first_name: String }
    type File { id: ID!, name: String }
    type Company { partners: [Contact!]! }
    input ContactInput { first_name: String }
    type Query { contacts(first: Int, offset: Int): [Contact!]!, FileUpload_FileUploads: [File!]!,
                 company: Company, unknown: String }
    type Mutation { update_contact(id: ID!, contact: ContactInput!): Contact }
'''))
QUERY = "QUERY_TO_FRAPPE"
MUTATION = "FRAPPE_TO_MUTATION"
RULE = "cf2d270b-d3ce-43fe-888f-4548b873f2db"


class TestMappingDraftCreation(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(name="SNAPSHOT", source_instance="configured", schema_fingerprint="a" * 64)
        self.storage = Mock()

    def save(self, content, direction=QUERY, operation="contacts"):
        return creation.create(self.snapshot, SCHEMA, direction, operation, content,
                               actor="admin@example.invalid", site_scope="site", storage=self.storage)

    def query_content(self):
        content = creation.seed(SCHEMA, QUERY, "contacts")["content"]
        content["mapping_definition_ids"] = [RULE]
        content["field_rules"] = {RULE: {"source_path": "first_name"}}
        return content

    def test_query_seed_has_no_selected_mapping_or_saved_reader(self):
        result = creation.seed(SCHEMA, QUERY, "contacts")
        self.assertEqual(result["query_targets"], ["HeroCustomers"])
        self.assertEqual(result["content"]["mapping_definition_ids"], [])
        self.assertEqual(result["content"]["query_adapter"], {"version": 2})
        self.assertEqual(result["content"]["records_path"], "contacts")

    def test_shared_file_root_requires_area_choice(self):
        result = creation.seed(SCHEMA, QUERY, "FileUpload_FileUploads")
        self.assertIsNone(result["content"])
        self.assertEqual(set(result["query_targets"]), {"HeroDocuments", "HeroPictures"})
        chosen = creation.seed(SCHEMA, QUERY, "FileUpload_FileUploads", "HeroPictures")
        self.assertEqual(chosen["content"]["target_operation"], "HeroPictures")

    def test_employee_nested_root_is_explicit_unpaged(self):
        content = creation.seed(SCHEMA, QUERY, "company")["content"]
        self.assertEqual(content["records_path"], "company.partners")
        self.assertEqual(content["variables"], {})
        self.assertEqual(content["target_operation"], "HeroEmployees")

    def test_operation_direction_and_adapter_are_closed(self):
        for direction, operation, target in [("QUERY", "contacts", ""),
                (QUERY, "missing", ""), (MUTATION, "contacts", ""),
                (QUERY, "unknown", ""), (QUERY, "contacts", "HeroTasks"),
                (MUTATION, "update_contact", "HeroCustomers")]:
            with self.subTest(direction=direction, operation=operation, target=target):
                with self.assertRaises(MappingDraftError):
                    creation.seed(SCHEMA, direction, operation, target)

    def test_mutation_seed_is_unsaved_first_assignment_form(self):
        result = creation.seed(SCHEMA, MUTATION, "update_contact")
        self.assertEqual(result["content"], {"source": {}, "target": {"operation": "update_contact"},
                                             "transformation": "Identity"})
        self.storage.service.assert_not_called()

    def test_empty_query_cannot_be_first_saved_draft(self):
        with self.assertRaises(MappingDraftError):
            self.save(creation.seed(SCHEMA, QUERY, "contacts")["content"])
        self.storage.service.assert_not_called()

    def test_create_uses_server_context_fresh_uuid_and_ordinary_validator(self):
        content = self.query_content()
        result = self.save(content)
        definition, actor, persisted = self.storage.service.return_value.open_new_draft.call_args.args
        self.assertEqual(str(UUID(definition["definition_id"])), definition["definition_id"])
        self.assertEqual(definition["source_instance"], "configured")
        self.assertEqual(definition["site_scope"], "site")
        self.assertEqual(definition["snapshot_name"], "SNAPSHOT")
        self.assertEqual(definition["schema_fingerprint"], "a" * 64)
        self.assertEqual(actor, "admin@example.invalid")
        self.assertEqual(persisted, content)
        self.assertIsNot(persisted, content)
        self.assertIs(result, self.storage.service.return_value.open_new_draft.return_value)

    def test_transform_options_must_reference_selected_rules(self):
        content = self.query_content()
        content["query_adapter"]["transform_options"] = {RULE: {"default": "Fallback"}}
        self.save(content)
        content["query_adapter"]["transform_options"]["other"] = {"default": "Wrong"}
        with self.assertRaises(MappingDraftError):
            self.save(content)
        self.assertEqual(self.storage.service.return_value.open_new_draft.call_count, 1)

    def test_first_save_completes_metadata_types_before_ordinary_persistence(self):
        content = self.query_content()
        def complete(value):
            value["query_adapter"]["transform_options"] = {RULE: {"target_type": "Data"}}
        self.storage._complete_query_target_types.side_effect = complete
        self.save(content)
        persisted = self.storage.service.return_value.open_new_draft.call_args.args[-1]
        self.assertEqual(persisted["query_adapter"]["transform_options"], {RULE: {"target_type": "Data"}})
        self.assertNotIn("transform_options", content["query_adapter"])
        self.storage._complete_query_target_types.assert_called_once()

    def test_storage_validation_failure_propagates_without_fallback(self):
        self.storage.service.return_value.open_new_draft.side_effect = MappingDraftError("Invalid source field")
        with self.assertRaisesRegex(MappingDraftError, "Invalid source field"):
            self.save({"source": {}, "target": {"operation": "update_contact"}}, MUTATION, "update_contact")


class TestMappingDraftCreationAPI(unittest.TestCase):
    def setUp(self):
        def fail(message, *args):
            raise ValueError(message)
        self.snapshot = SimpleNamespace(name="SNAPSHOT", source_instance="configured", schema_fingerprint="a" * 64,
                                        check_permission=Mock())
        self.frappe = SimpleNamespace(only_for=Mock(), form_dict={}, throw=fail, _=lambda text: text,
                                     session=SimpleNamespace(user="actor"), local=SimpleNamespace(site="site"))
        self.patches = [patch.object(api, "frappe", self.frappe),
                        patch.object(api, "_validated_snapshot", return_value=(self.snapshot, SCHEMA)),
                        patch.object(api, "_configured_source_instance", return_value="configured")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_seed_is_read_permission_checked_and_returns_schema_context(self):
        result = api.mapping_draft_new_seed("SNAPSHOT", QUERY, "contacts")
        self.assertEqual(result["schema_fingerprint"], "a" * 64)
        self.assertEqual(result["source_instance"], "configured")
        self.snapshot.check_permission.assert_called_once_with("read")
        self.frappe.only_for.assert_called_once_with("System Manager")

    def test_snapshot_from_another_configured_source_is_rejected(self):
        self.snapshot.source_instance = "foreign"
        with self.assertRaisesRegex(ValueError, "another configured HERO source"):
            api.mapping_draft_new_seed("SNAPSHOT", QUERY, "contacts")

    def test_request_field_spoofing_fails_before_snapshot_lookup(self):
        for field in ("actor", "site_scope", "source_instance", "definition_id", "confirmed"):
            self.frappe.form_dict = {field: "spoofed"}
            for method, args in ((api.mapping_draft_new_seed, ("SNAPSHOT", QUERY, "contacts")),
                                 (api.mapping_draft_new, ("SNAPSHOT", QUERY, "contacts", "{}"))):
                with self.subTest(field=field, method=method.__name__):
                    with self.assertRaisesRegex(ValueError, "Unsupported request field"):
                        method(*args)
        api._validated_snapshot.assert_not_called()

    def test_save_uses_session_actor_and_site_and_returns_existing_draft_shape(self):
        content = {"source": {"doctype": "Customer", "field": "customer_name"},
                   "target": {"operation": "update_contact", "path": "contact.first_name"}}
        with patch.object(creation, "create", return_value="draft") as create, \
                patch.object(api, "_personal_draft_result", return_value={"definition_id": RULE}), \
                patch("hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage"):
            result = api.mapping_draft_new("SNAPSHOT", MUTATION, "update_contact", json.dumps(content))
        self.assertEqual(create.call_args.kwargs["actor"], "actor")
        self.assertEqual(create.call_args.kwargs["site_scope"], "site")
        self.assertEqual(create.call_args.args[-1], content)
        self.assertEqual(result, {"draft": {"definition_id": RULE}})

    def test_duplicate_json_keys_rejected_before_draft_creation(self):
        with patch.object(creation, "create") as create:
            with self.assertRaises(ValueError):
                api.mapping_draft_new("SNAPSHOT", QUERY, "contacts", '{"source":{},"source":{}}')
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
