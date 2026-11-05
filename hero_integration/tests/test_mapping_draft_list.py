"""Both kinds of personal drafts are discoverable without widening owner access."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration import api


class TestMappingDraftList(unittest.TestCase):
    def setUp(self):
        self.parent = SimpleNamespace(name="ACTIVE", definition_id="definition", source_instance="source",
            schema_snapshot="snapshot", schema_fingerprint="fingerprint", direction="QUERY_TO_FRAPPE",
            operation_kind="QUERY", operation_name="contacts", check_permission=Mock())
        self.draft = SimpleNamespace(name="DRAFT", definition_id="definition", site_scope="site",
            owner_user="Administrator", state="OPEN", new_definition_json="", base_active_mapping="ACTIVE",
            content_json=json.dumps({"target_operation": "HeroCustomers"}), content_revision=4,
            check_permission=Mock())
        self.filters = None
        self.fake = SimpleNamespace(local=SimpleNamespace(site="site"), session=SimpleNamespace(user="Administrator"),
            only_for=Mock(), get_all=self.get_all, get_doc=self.get_doc)

    def get_all(self, doctype, filters, **kwargs):
        self.filters = filters
        return [{"name": self.draft.name}]

    def get_doc(self, doctype, name):
        return self.parent if doctype == "HERO GraphQL Mapping" else self.draft

    def read(self):
        with patch.object(api, "frappe", self.fake), patch.object(api, "_mapping_request_fields"):
            return api.mapping_draft_list("QUERY_TO_FRAPPE")

    def test_current_definition_draft_is_listed_with_its_own_revision_and_title(self):
        result = self.read()
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["active_mapping"], "ACTIVE")
        self.assertEqual(item["draft_revision"], 4)
        self.assertEqual(item["target_operation"], "HeroCustomers")
        self.parent.check_permission.assert_called_once_with("read")
        self.fake.only_for.assert_called_once_with("System Manager")

    def test_foreign_owner_returned_by_a_stale_list_is_not_exposed(self):
        self.draft.owner_user = "another-admin"
        self.assertEqual(self.read()["items"], [])
        self.assertEqual(self.filters, {"site_scope": "site", "owner_user": "Administrator", "state": "OPEN"})
        self.parent.check_permission.assert_not_called()

    def test_parent_identity_mismatch_cannot_open_another_mapping(self):
        self.parent.definition_id = "another-definition"
        self.assertEqual(self.read()["items"], [])

    def test_parentless_draft_still_lists_without_an_active_mapping(self):
        self.draft.new_definition_json = json.dumps({
            "site_scope": "site", "definition_id": "definition", "source_instance": "source",
            "snapshot_name": "snapshot", "schema_fingerprint": "fingerprint", "direction": "QUERY_TO_FRAPPE",
            "operation_kind": "QUERY", "operation_name": "contacts"})
        self.draft.base_active_mapping = ""
        item = self.read()["items"][0]
        self.assertNotIn("active_mapping", item)
        self.assertEqual(item["draft_revision"], 4)
        self.parent.check_permission.assert_not_called()


if __name__ == "__main__":
    unittest.main()
