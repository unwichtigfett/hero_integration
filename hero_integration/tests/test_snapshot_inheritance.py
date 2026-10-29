"""Snapshots inherit their parent's active mappings as personal drafts; nothing is activated."""
from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda *args, **kwargs: (lambda function: function)
    sys.modules["frappe"] = frappe

from hero_integration.services import snapshot_inheritance as inheritance
from hero_integration.services.mapping_draft_service import MappingDraftError

PARENT_FP, CHILD_FP = "a" * 64, "b" * 64


def _schema(email_type="String", with_email=True):
    contact_fields = [{"name": "id", "args": [], "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None}}}]
    if with_email:
        contact_fields.append({"name": "email", "args": [], "type": {"kind": "SCALAR", "name": email_type, "ofType": None}})
    return {"__schema": {"queryType": {"name": "Query"}, "mutationType": None, "subscriptionType": None, "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [{"name": "contacts", "args": [], "type": {"kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "Contact", "ofType": None}}}]},
        {"kind": "OBJECT", "name": "Contact", "fields": contact_fields},
        {"kind": "SCALAR", "name": "ID"}, {"kind": "SCALAR", "name": "String"}, {"kind": "SCALAR", "name": "Int"},
    ]}}


QUERY_CONTENT = {"target_operation": "HeroCustomers", "records_path": "contacts", "identity_path": "id", "page_size": 100,
                 "variables": {}, "mapping_definition_ids": ["rule-1"], "field_contexts": {"rule-1": {"object_type": "Contact"}},
                 "field_rule_bases": {"rule-1": 3}, "native_task_onboarding": {"granted": True}}
RULE = {"source_path": "email", "source_type": "string|null", "target_doctype": "Customer", "target_field": "email_id",
        "purpose": "Field", "transformation": "Identity", "relation": None, "priority": 10}


class _Doc(SimpleNamespace):
    def update(self, values):
        self.__dict__.update(values)

    def save(self, ignore_permissions=False):
        self.store[self.inheritance_key] = dict(self.__dict__)

    def insert(self, ignore_permissions=False):
        self.store[self.inheritance_key] = {key: value for key, value in self.__dict__.items() if key != "store"}


class _FakeFrappe:
    def __init__(self, mappings, rules, child_active=(), logs=None):
        self.mappings, self.rules, self.child_active = mappings, rules, list(child_active)
        self.logs = logs if logs is not None else {}
        self.savepoints, self.rollbacks = [], []
        self.local = SimpleNamespace(site="site.test")
        self.db = SimpleNamespace(savepoint=self.savepoints.append, rollback=lambda save_point: self.rollbacks.append(save_point),
                                  exists=lambda doctype, name: name in self.logs)

    def get_all(self, doctype, filters=None, fields=None, **_kwargs):
        filters = filters or {}
        if doctype == "HERO GraphQL Mapping" and filters.get("schema_snapshot") == "CHILD":
            return self.child_active
        if doctype == "HERO GraphQL Mapping":
            return self.mappings
        if doctype == "HERO Frappe Mapping":
            return [dict(rule) for rule in self.rules.get(filters["definition_id"], [])]
        if doctype == inheritance.INHERITANCE_DOCTYPE:
            return [row for row in self.logs.values() if row.get("status") == filters.get("status") and row.get("child_snapshot") == filters.get("child_snapshot")]
        raise AssertionError(doctype)

    def get_doc(self, doctype_or_payload, name=None):
        if isinstance(doctype_or_payload, dict):
            payload = {key: value for key, value in doctype_or_payload.items() if key != "doctype"}
            return _Doc(store=self.logs, **payload)
        return _Doc(store=self.logs, **self.logs[name])


def _row(direction="QUERY_TO_FRAPPE", content=QUERY_CONTENT, name="MAP-1", definition="def-1", operation="contacts"):
    return {"name": name, "definition_id": definition, "direction": direction,
            "operation_kind": "QUERY" if direction == "QUERY_TO_FRAPPE" else "MUTATION",
            "operation_name": operation, "mapping_json": json.dumps(content)}


class _Base(unittest.TestCase):
    def run_inherit(self, fake, child_schema=None, *, parent_source="hero", child_parent="PARENT"):
        child = SimpleNamespace(name="CHILD", parent_snapshot=child_parent, source_instance="hero", schema_fingerprint=CHILD_FP)
        parent = SimpleNamespace(name="PARENT", source_instance=parent_source, schema_fingerprint=PARENT_FP)
        snapshots = {"CHILD": (child, child_schema or _schema()), "PARENT": (parent, _schema())}
        self.service = MagicMock()
        storage = MagicMock()
        storage.return_value.service.return_value = self.service
        with patch.object(inheritance, "frappe", fake), \
                patch("hero_integration.api._validated_snapshot", side_effect=lambda name: snapshots[name]), \
                patch("hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage", storage):
            return inheritance.inherit_parent_mappings("CHILD", "admin@example.com")


class TestQueryInheritance(_Base):
    def test_active_query_definition_becomes_a_validated_child_draft_with_fresh_rule_identities(self):
        fake = _FakeFrappe([_row()], {"rule-1": [RULE]})
        result = self.run_inherit(fake)
        self.assertEqual((result["status"], result["counts"]["Draft Created"], result["activation"]), ("COMPLETED", 1, False))
        definition, actor, content = self.service.open_new_draft.call_args.args
        self.assertEqual(actor, "admin@example.com")
        self.assertEqual((definition["snapshot_name"], definition["schema_fingerprint"], definition["source_instance"]), ("CHILD", CHILD_FP, "hero"))
        self.assertEqual((definition["direction"], definition["operation_kind"], definition["operation_name"]), ("QUERY_TO_FRAPPE", "QUERY", "contacts"))
        self.assertNotEqual(definition["definition_id"], "def-1")
        (new_rule,) = content["mapping_definition_ids"]
        self.assertNotEqual(new_rule, "rule-1")
        self.assertEqual(content["field_rules"], {new_rule: {key: value for key, value in RULE.items() if value is not None}})
        self.assertEqual(content["field_contexts"], {new_rule: {"object_type": "Contact"}})
        for removed in ("field_rule_bases", "native_task_onboarding"):
            self.assertNotIn(removed, content, "inherited content never carries provenance or authority")
        (log,) = fake.logs.values()
        self.assertEqual((log["status"], log["parent_definition"], log["child_definition"]), ("Draft Created", "def-1", definition["definition_id"]))

    def test_a_rule_that_no_longer_fits_the_child_schema_is_recorded_as_incompatible(self):
        for schema in (_schema(with_email=False), _schema(email_type="Int")):
            fake = _FakeFrappe([_row()], {"rule-1": [RULE]})
            result = self.run_inherit(fake, schema)
            self.assertEqual(result["counts"]["Incompatible"], 1)
            self.service.open_new_draft.assert_not_called()
            self.assertEqual(len(fake.rollbacks), 1, "the per-definition savepoint is rolled back")
            self.assertEqual(next(iter(fake.logs.values()))["status"], "Incompatible")

    def test_missing_or_ambiguous_parent_rules_and_draft_service_rejections_are_incompatible(self):
        fake = _FakeFrappe([_row()], {"rule-1": [RULE, RULE]})
        self.assertEqual(self.run_inherit(fake)["results"][0]["status"], "Incompatible")
        storage_error = MappingDraftError("New query field rule is incompatible with target metadata.")
        fake2 = _FakeFrappe([_row()], {"rule-1": [RULE]})
        child = SimpleNamespace(name="CHILD", parent_snapshot="PARENT", source_instance="hero", schema_fingerprint=CHILD_FP)
        parent = SimpleNamespace(name="PARENT", source_instance="hero", schema_fingerprint=PARENT_FP)
        service = MagicMock()
        service.open_new_draft.side_effect = storage_error
        storage = MagicMock()
        storage.return_value.service.return_value = service
        with patch.object(inheritance, "frappe", fake2), \
                patch("hero_integration.api._validated_snapshot", side_effect=lambda name: {"CHILD": (child, _schema()), "PARENT": (parent, _schema())}[name]), \
                patch("hero_integration.services.frappe_mapping_draft_storage.FrappeMappingDraftStorage", storage):
            result = inheritance.inherit_parent_mappings("CHILD", "admin@example.com")
        self.assertEqual(result["results"][0]["reason"], "New query field rule is incompatible with target metadata.")
        self.assertEqual(len(fake2.rollbacks), 1)

    def test_inheritance_is_idempotent_and_own_child_mappings_win(self):
        fake = _FakeFrappe([_row()], {"rule-1": [RULE]})
        self.run_inherit(fake)
        again = self.run_inherit(fake)
        self.service.open_new_draft.assert_not_called()
        self.assertEqual(again["results"][0]["reason"], "Already inherited earlier.")
        own = _FakeFrappe([_row()], {"rule-1": [RULE]}, child_active=[{"direction": "QUERY_TO_FRAPPE", "operation_name": "contacts"}])
        result = self.run_inherit(own)
        self.assertEqual(result["results"][0]["status"], "Skipped")
        self.service.open_new_draft.assert_not_called()

    def test_no_parent_or_a_parent_of_another_source_inherits_nothing(self):
        self.assertEqual(self.run_inherit(_FakeFrappe([_row()], {}), child_parent=None)["status"], "NO_PARENT")
        self.assertEqual(self.run_inherit(_FakeFrappe([_row()], {}), parent_source="other")["status"], "PARENT_SOURCE_MISMATCH")
        self.service.open_new_draft.assert_not_called()


class TestMutationInheritance(_Base):
    MUTATION = {"version": 1, "source": {"doctype": "Customer", "field": "email_id"},
                "target": {"operation": "update_contact", "argument": "input", "field": "email"}, "transformation": "Identity",
                "additional_mappings": [{"id": "child-1", "source": {"doctype": "Customer", "field": "phone"},
                                         "target": {"operation": "update_contact", "argument": "input", "field": "phone"}}]}

    def test_mutation_assignments_get_fresh_identities(self):
        fake = _FakeFrappe([_row("FRAPPE_TO_MUTATION", self.MUTATION, operation="update_contact")], {})
        self.run_inherit(fake)
        _definition, _actor, content = self.service.open_new_draft.call_args.args
        self.assertNotEqual(content["additional_mappings"][0]["id"], "child-1")
        self.assertEqual(content["source"], self.MUTATION["source"])

    def test_unsupported_mutation_configuration_is_incompatible(self):
        fake = _FakeFrappe([_row("FRAPPE_TO_MUTATION", {**self.MUTATION, "execution": "auto"}, operation="update_contact")], {})
        result = self.run_inherit(fake)
        self.assertEqual(result["results"][0]["status"], "Incompatible")
        self.assertIn("execution", result["results"][0]["reason"])


class TestParentLinks(unittest.TestCase):
    def test_parent_lookup_uses_creation_order_and_handles_a_missing_endpoint(self):
        fake = MagicMock()
        fake.get_all.return_value = [{"name": "P"}]
        with patch.object(inheritance, "frappe", fake):
            self.assertEqual(inheritance.parent_for("hero", None), "P")
        kwargs = fake.get_all.call_args.kwargs
        self.assertEqual(kwargs["filters"]["endpoint_fingerprint"], ["is", "not set"])
        self.assertEqual(kwargs["order_by"], "creation desc, name desc")

    def test_backfill_patch_links_each_snapshot_to_the_previous_one_of_its_lineage(self):
        from hero_integration.patches.v0_1 import backfill_snapshot_parents as patch_module

        rows = [{"name": "A1", "source_instance": "hero", "endpoint_fingerprint": "e", "parent_snapshot": None},
                {"name": "T1", "source_instance": "test", "endpoint_fingerprint": None, "parent_snapshot": None},
                {"name": "A2", "source_instance": "hero", "endpoint_fingerprint": "e", "parent_snapshot": None},
                {"name": "A3", "source_instance": "hero", "endpoint_fingerprint": "e", "parent_snapshot": "KEEP"}]
        writes = []
        fake = SimpleNamespace(get_all=lambda *args, **kwargs: rows,
                               db=SimpleNamespace(has_column=lambda *args: True,
                                                  set_value=lambda doctype, name, field, value, update_modified=True: writes.append((name, value))))
        with patch.object(patch_module, "frappe", fake):
            patch_module.execute()
        self.assertEqual(writes, [("A2", "A1")], "existing links are kept and other sources never become a parent")


class TestCaptureSafety(unittest.TestCase):
    def test_failed_inheritance_never_undoes_the_captured_snapshot(self):
        from hero_integration import api

        db = SimpleNamespace(savepoint=MagicMock(), rollback=MagicMock())
        fake = SimpleNamespace(db=db, session=SimpleNamespace(user="admin@example.com"), log_error=MagicMock())
        with patch.object(api, "frappe", fake), \
                patch("hero_integration.services.snapshot_inheritance.inherit_parent_mappings", side_effect=RuntimeError("boom")):
            result = api._inherit_after_capture("CHILD")
        self.assertEqual((result["status"], result["activation"]), ("FAILED", False))
        db.rollback.assert_called_once_with(save_point="hero_snapshot_inheritance")
        fake.log_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
