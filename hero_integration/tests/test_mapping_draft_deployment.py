"""Deployment imports drafts only, retaining target ownership and user edits."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_draft_deployment as deployment
from hero_integration.services.mapping_draft_service import draft_key


class Environment:
    def __init__(self, package):
        self.package = package
        self.local = SimpleNamespace(site=package["target_site"])
        self.session = SimpleNamespace(user="migration-user")
        self.drafts, self.active, self.created, self.validated = {}, set(), [], []
        self.snapshot = "TARGET-SNAPSHOT"
        self.services = ["TARGET-READER"]
        self.fields, self.field_creates = {}, []
        for definition in package["required_custom_fields"]:
            self.fields[definition["dt"], definition["fieldname"]] = SimpleNamespace(**definition)
        self.fail_at = None
        self.db = SimpleNamespace(exists=self.exists, savepoint=self.savepoint, rollback=self.rollback)

    def set_user(self, user):
        self.session.user = user

    def exists(self, doctype, key):
        if doctype == "HERO Mapping Draft":
            return key in self.drafts
        if doctype == "HERO GraphQL Mapping":
            return key["definition_id"] in self.active
        raise AssertionError(doctype)

    def get_all(self, doctype, filters=None, **kwargs):
        if doctype == "HERO GraphQL Schema Snapshot":
            assert filters["source_instance"] == self.package["source_instance"]
            assert filters["schema_fingerprint"] == self.package["schema_fingerprint"]
            return [self.snapshot] if self.snapshot else []
        if doctype == "HERO GraphQL Service":
            assert "source_instance" not in filters
            assert "operation_kind" not in filters
            assert filters["schema_snapshot"] == self.snapshot
            return self.services
        raise AssertionError(doctype)

    def get_meta(self, doctype):
        return SimpleNamespace(get_field=lambda name: self.fields.get((doctype, name)))

    def savepoint(self, name):
        self.before = deepcopy(self.drafts), list(self.created)

    def rollback(self, **kwargs):
        self.drafts, self.created = self.before

    def storage(self):
        return SimpleNamespace(validate_new_definition=self.validate, service=lambda: SimpleNamespace(
            open_new_draft=self.create))

    def validate(self, definition, content):
        assert definition["snapshot_name"] == self.snapshot
        assert definition["site_scope"] == self.local.site
        assert self.session.user == "Administrator"
        self.validated.append((definition, content))

    def create(self, definition, actor, content):
        assert len(self.validated) == len(self.package["mappings"])
        if self.fail_at == len(self.created):
            raise ValueError("Simulated storage failure")
        key = draft_key(self.local.site, definition["definition_id"], actor)
        self.drafts[key] = deepcopy(content)
        self.created.append(key)
        return SimpleNamespace(key=key)

    def call(self):
        with patch.object(deployment, "frappe", self), \
             patch.object(deployment, "FrappeMappingDraftStorage", self.storage), \
             patch("hero_integration.api._validated_snapshot", return_value=(None, {})), \
             patch("frappe.custom.doctype.custom_field.custom_field.create_custom_fields") as create_fields:
            try:
                return deployment.install_package(self.package)
            finally:
                self.field_creates.extend(create_fields.call_args_list)


class TestMappingDraftDeployment(unittest.TestCase):
    def setUp(self):
        self.package = json.loads(deployment.PACKAGE_PATH.read_text())
        self.environment = Environment(self.package)

    def test_bundle_has_closed_rules_and_no_local_document_ids_or_approval(self):
        self.assertEqual(len(self.package["mappings"]), 3)
        self.assertEqual(sum(len(item["content"]["field_rules"]) for item in self.package["mappings"]), 57)
        for item in self.package["mappings"]:
            content = item["content"]
            self.assertEqual(set(content["mapping_definition_ids"]), set(content["field_rules"]))
            self.assertNotIn("field_rule_bases", content)
            self.assertNotIn("query_adapter_seal", content)
            self.assertNotIn("read_service", content.get("query_adapter", {}))
            self.assertNotIn("customer_service", content)
            self.assertNotIn("HERO-GQL-", json.dumps(item))
        for field in self.package["required_custom_fields"]:
            self.assertFalse(field["label"].lower().startswith("hero"))

    def test_other_sites_have_no_database_or_identity_effect(self):
        env = self.environment
        env.local.site = "unrelated.test"
        self.assertEqual(env.call(), {"status": "OTHER_SITE"})
        self.assertEqual(env.session.user, "migration-user")
        self.assertEqual(env.validated, [])
        self.assertEqual(env.created, [])

    def test_first_import_remaps_target_names_and_second_import_is_unchanged(self):
        env = self.environment
        self.assertEqual(env.call()["status"], "DRAFTS_ONLY")
        self.assertEqual(len(env.created), 3)
        self.assertEqual(env.session.user, "migration-user")
        products = next(c for _, c in env.validated if c["target_operation"] == "HeroProducts")
        self.assertEqual(products["query_adapter"]["read_service"], "TARGET-READER")
        before = deepcopy(env.drafts)
        self.assertEqual(env.call()["status"], "UNCHANGED")
        self.assertEqual(env.drafts, before)
        self.assertEqual(len(env.created), 3)

    def test_existing_edited_and_discarded_drafts_and_published_mapping_are_preserved(self):
        env = self.environment
        for item, state in zip(self.package["mappings"], ("edited", "discarded", "published")):
            identity = item["definition_id"]
            if state == "published":
                env.active.add(identity)
            else:
                env.drafts[draft_key(env.local.site, identity, "Administrator")] = {"state": state}
        before = deepcopy(env.drafts)
        self.assertEqual(env.call()["status"], "UNCHANGED")
        self.assertEqual(env.drafts, before)
        self.assertEqual(env.created, [])

    def test_missing_matching_schema_fails_before_field_or_draft_writes(self):
        env = self.environment
        env.snapshot = None
        with self.assertRaisesRegex(ValueError, "validated HERO schema"):
            env.call()
        self.assertEqual(env.created, [])
        self.assertEqual(env.field_creates, [])
        self.assertEqual(env.session.user, "migration-user")

    def test_field_conflict_blocks_even_when_other_fields_are_missing(self):
        env = self.environment
        keys = list(env.fields)
        del env.fields[keys[0]]
        env.fields[keys[-1]].fieldtype = "Check"
        with self.assertRaisesRegex(ValueError, "field conflicts"):
            env.call()
        self.assertEqual(env.created, [])
        self.assertEqual(env.field_creates, [])

    def test_only_missing_fields_are_created_without_updating_existing_fields(self):
        env = self.environment
        doctype, fieldname = next(iter(env.fields))
        del env.fields[doctype, fieldname]
        env.call()
        self.assertEqual(len(env.field_creates), 1)
        args, kwargs = env.field_creates[0]
        self.assertEqual(kwargs, {"update": False})
        self.assertEqual(list(args[0]), [doctype])
        self.assertEqual([f["fieldname"] for f in args[0][doctype]], [fieldname])

    def test_storage_failure_rolls_back_all_new_drafts_and_restores_actor(self):
        env = self.environment
        env.fail_at = 1
        with self.assertRaisesRegex(ValueError, "storage failure"):
            env.call()
        self.assertEqual(env.drafts, {})
        self.assertEqual(env.session.user, "migration-user")

    def test_ambiguous_services_are_not_bound_arbitrarily(self):
        env = self.environment
        env.services = ["ONE", "TWO"]
        env.call()
        for _, content in env.validated:
            self.assertNotIn("customer_service", content)
            self.assertNotIn("read_service", content.get("query_adapter", {}))


if __name__ == "__main__":
    unittest.main()
