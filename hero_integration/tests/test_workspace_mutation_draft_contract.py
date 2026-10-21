from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda: lambda function: function
    sys.modules["frappe"] = frappe

from hero_integration import api


SCHEMA = {"__schema": {"mutationType": {"name": "Mutation"}, "types": [
    {"name": "Mutation", "fields": [{"name": "changeCase", "type": {"kind": "OBJECT", "name": "Case"}}]},
    {"name": "Case", "fields": []},
]}}
PAYLOAD = {
    "name": "Change case draft",
    "operation_name": "ChangeCase",
    "operation_kind": "mutation",
    "query": "mutation ChangeCase { changeCase { id } }",
    "variables": {"api_token": "must-not-be-stored"},
}


class _Service:
    def __init__(self, name="HERO-GQL-00001", revision=1):
        self.name = name
        self.schema_snapshot = "SNAPSHOT"
        self.service_name = ""
        self.operation_name = ""
        self.query_document = ""
        self.service_revision = revision
        self.status = "Draft"
        self.validation_summary = ""
        self.save_calls = 0

    def save(self):
        self.save_calls += 1

    def as_dict(self):
        return self.__dict__.copy()


class _FrappeMock:
    def __init__(self, service=None, permission_error=None):
        self.service = service
        self.permission_error = permission_error
        self.only_for_calls = []
        self.new_doc_calls = []
        self.get_all_calls = []

    def only_for(self, role):
        self.only_for_calls.append(role)
        if self.permission_error:
            raise self.permission_error

    def throw(self, message):
        raise ValueError(message)

    def new_doc(self, doctype):
        self.new_doc_calls.append(doctype)
        self.service = _Service()
        return self.service

    def get_doc(self, doctype, name):
        if doctype != "HERO GraphQL Service" or name != self.service.name:
            raise AssertionError((doctype, name))
        return self.service

    def get_all(self, doctype, **_kwargs):
        self.get_all_calls.append(doctype)
        if doctype != "HERO GraphQL Service":
            raise AssertionError(doctype)
        return [self.service.as_dict()] if self.service else []


class TestWorkspaceMutationDraftContract(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(name="SNAPSHOT", schema_fingerprint="synthetic")
        self.frappe = _FrappeMock()
        self.patch_frappe = patch.object(api, "frappe", self.frappe)
        self.patch_snapshot = patch.object(api, "_validated_snapshot", return_value=(self.snapshot, SCHEMA))
        self.patch_frappe.start()
        self.patch_snapshot.start()
        self.addCleanup(self.patch_frappe.stop)
        self.addCleanup(self.patch_snapshot.stop)

    def test_mutation_draft_can_be_saved_reloaded_and_edited_without_variables(self):
        created = api.workspace_service_save("SNAPSHOT", json.dumps(PAYLOAD))
        service = self.frappe.service

        self.assertEqual(created["query"]["service_kind"], "mutation")
        self.assertEqual(created["query"]["status"], "Draft")
        self.assertEqual(service.save_calls, 1)
        self.assertFalse(hasattr(service, "variables"))
        self.assertEqual(api.workspace_service_list("SNAPSHOT", "mutation")["rows"], [created["query"]])
        self.assertEqual(api.workspace_service_detail(service.name)["query"], created["query"])

        edited_payload = {**PAYLOAD, "name": "Edited case draft", "query": "mutation ChangeCase { changeCase { id } }"}
        edited = api.workspace_service_save("SNAPSHOT", json.dumps(edited_payload), service.name, expected_revision=1)
        self.assertEqual(edited["query"]["name"], "Edited case draft")
        self.assertEqual(service.status, "Draft")
        self.assertEqual(service.save_calls, 2)

    def test_mutation_validation_is_local_and_never_executable(self):
        result = api.workspace_service_validate("SNAPSHOT", json.dumps(PAYLOAD))

        self.assertTrue(result["valid"])
        self.assertFalse(result["can_execute"])
        self.assertEqual(result["mutation"]["return_type"], "Case")
        self.assertIn("disabled", result["reason"])

    def test_saved_mutation_validation_never_activates_or_resaves_the_draft(self):
        api.workspace_service_save("SNAPSHOT", json.dumps(PAYLOAD))
        service = self.frappe.service

        result = api.workspace_service_validate("SNAPSHOT", query_id=service.name, expected_revision=1)

        self.assertTrue(result["valid"])
        self.assertFalse(result["can_execute"])
        self.assertEqual(service.status, "Draft")
        self.assertEqual(service.save_calls, 1)

    def test_mismatched_and_mixed_documents_are_rejected_before_save(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            api.workspace_service_save("SNAPSHOT", json.dumps({**PAYLOAD, "operation_kind": "query"}))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            api.workspace_service_save(
                "SNAPSHOT", json.dumps({**PAYLOAD, "query": PAYLOAD["query"] + " query { changeCase { id } }"}),
            )
        self.assertIsNone(self.frappe.service)

    def test_every_available_mutation_execute_facade_fails_closed(self):
        with patch.object(api, "_assert_mutation_execution_released", side_effect=ValueError(api.MUTATION_EXECUTION_DISABLED)):
            with self.assertRaisesRegex(ValueError, api.MUTATION_EXECUTION_DISABLED):
                api.execute_graphql_service("HERO-GQL-00001")

    def test_missing_permission_is_denied_before_draft_persistence(self):
        blocked = _FrappeMock(permission_error=PermissionError("denied"))
        with patch.object(api, "frappe", blocked), self.assertRaisesRegex(PermissionError, "denied"):
            api.workspace_service_save("SNAPSHOT", json.dumps(PAYLOAD))

        self.assertEqual(blocked.new_doc_calls, [])
