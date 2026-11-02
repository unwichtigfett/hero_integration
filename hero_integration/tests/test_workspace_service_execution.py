"""GraphQL Services: schema validation, read-only query execution and analyzer drafts.

The HERO transport is always mocked; no test contacts HERO.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hero_integration.tests._frappe_test_import import load_api

api = load_api()
from hero_integration.client.graphql import HeroClientError


def _named(kind, name):
    return {"kind": kind, "name": name, "ofType": None}


def _non_null(ref):
    return {"kind": "NON_NULL", "name": None, "ofType": ref}


SCHEMA = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
    {"kind": "OBJECT", "name": "Query", "fields": [
        {"name": "contact", "args": [{"name": "id", "type": _non_null(_named("SCALAR", "ID")), "defaultValue": None},
                                     {"name": "lang", "type": _named("SCALAR", "String"), "defaultValue": None}],
         "type": _named("OBJECT", "Contact")},
        {"name": "version", "args": [], "type": _named("SCALAR", "String")},
    ]},
    {"kind": "OBJECT", "name": "Mutation", "fields": [
        {"name": "update_contact", "args": [{"name": "input", "type": _non_null(_named("INPUT_OBJECT", "ContactInput")), "defaultValue": None}],
         "type": _named("OBJECT", "Contact")}]},
    {"kind": "OBJECT", "name": "Contact", "fields": [{"name": "id", "args": [], "type": _non_null(_named("SCALAR", "ID"))},
                                                    {"name": "email", "args": [], "type": _named("SCALAR", "String")}]},
    {"kind": "INPUT_OBJECT", "name": "ContactInput", "inputFields": [{"name": "email", "type": _named("SCALAR", "String"), "defaultValue": None}]},
    {"kind": "SCALAR", "name": "ID"}, {"kind": "SCALAR", "name": "String"},
]}}
QUERY = "query Contact($id: ID!) { contact(id: $id) { id email } }"


class _Service:
    def __init__(self, document=QUERY, status="Validated", revision=3):
        self.name, self.schema_snapshot, self.schema_fingerprint = "HERO-GQL-00001", "SNAPSHOT", "fp"
        self.service_name, self.operation_name, self.query_document = "Contact", "Contact", document
        self.service_revision, self.status, self.validation_summary, self.provenance = revision, status, "", ""
        self.saves = 0

    def save(self):
        self.saves += 1

    def as_dict(self):
        return {key: value for key, value in self.__dict__.items() if key != "saves"}


class _Frappe:
    _ = staticmethod(lambda message: message)

    def __init__(self, service=None):
        self.service, self.denied, self.created = service, False, []

    def only_for(self, role):
        if self.denied:
            raise PermissionError(role)

    def throw(self, message):
        raise ValueError(message)

    def get_doc(self, doctype, name):
        assert doctype == "HERO GraphQL Service" and name == self.service.name, (doctype, name)
        return self.service

    def new_doc(self, doctype):
        assert doctype == "HERO GraphQL Service"
        self.service = _Service(document="", status="Draft", revision=1)
        self.created.append(self.service)
        return self.service


class TestWorkspaceServiceExecution(unittest.TestCase):
    def setUp(self):
        self.frappe = _Frappe(_Service())
        self.snapshot = SimpleNamespace(name="SNAPSHOT", schema_fingerprint="fp")
        self.patches = [patch.object(api, "frappe", self.frappe),
                        patch.object(api, "_validated_snapshot", MagicMock(return_value=(self.snapshot, SCHEMA))),
                        patch.object(api, "_mutation_execution_state", MagicMock(return_value={"mutation_execution_enabled": False}))]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = MagicMock()
        self.client.query.return_value = {"contact": {"id": "7", "email": "a@b"}}
        self.client_class = MagicMock(return_value=self.client)
        self.configuration = SimpleNamespace(endpoint="endpoint", api_token="token", timeout_seconds=20)
        for patcher in (patch("hero_integration.client.graphql.HeroGraphQLClient", self.client_class),
                        patch("hero_integration.client.settings.get_configuration", MagicMock(return_value=self.configuration))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, variables='{"id": "7"}', revision=3):
        return api.workspace_service_execute("SNAPSHOT", "HERO-GQL-00001", revision, variables)

    # -- validation -------------------------------------------------------------
    def test_saved_query_validation_checks_schema_and_enables_execution(self):
        self.frappe.service.status = "Draft"
        result = api.workspace_service_validate("SNAPSHOT", query_id="HERO-GQL-00001", expected_revision=3)
        self.assertTrue(result["can_execute"])
        self.assertEqual(result["query_detail"]["required_variables"], ["id"])
        self.assertEqual(self.frappe.service.status, "Validated")
        self.frappe.service.query_document = "{ contact { unknown } }"
        with self.assertRaisesRegex(ValueError, "Required argument|does not exist"):
            api.workspace_service_validate("SNAPSHOT", query_id="HERO-GQL-00001", expected_revision=3)

    def test_unsaved_query_preview_validates_but_cannot_execute(self):
        result = api.workspace_service_validate("SNAPSHOT", payload_json=json.dumps({"name": "x", "query": QUERY}))
        self.assertFalse(result["can_execute"])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            api.workspace_service_validate("SNAPSHOT", payload_json=json.dumps({"name": "x", "query": "{ nope }"}))

    # -- execution --------------------------------------------------------------
    def test_query_executes_once_read_only_with_validated_variables_while_mutations_are_off(self):
        result = self.execute()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["data"], {"contact": {"id": "7", "email": "a@b"}})
        self.client.query.assert_called_once_with(QUERY, {"id": "7"})
        self.assertEqual(self.client_class.call_args.kwargs["max_retries"], 0)
        self.assertEqual(self.frappe.service.saves, 0, "execution must not persist anything")

    def test_execution_preconditions_fail_closed_before_transport(self):
        cases = [
            (lambda: setattr(self.frappe.service, "status", "Draft"), {}, "Validate the saved query"),
            (lambda: None, {"revision": 2}, "changed"),
            (lambda: setattr(self.frappe.service, "schema_fingerprint", "old"), {}, "another schema fingerprint"),
            (lambda: None, {"variables": "{}"}, "Required query variable"),
            (lambda: None, {"variables": '{"id": 7.5}'}, "ID"),
            (lambda: None, {"variables": "not json"}, "JSON"),
            (lambda: setattr(self.frappe.service, "query_document", "mutation M { update_contact(input: {}) { id } }"), {},
             "MUTATION_EXECUTION_DISABLED"),
        ]
        for prepare, kwargs, message in cases:
            with self.subTest(message=message):
                self.frappe.service = _Service()
                prepare()
                with self.assertRaisesRegex(ValueError, message):
                    self.execute(**kwargs)
        self.client.query.assert_not_called()

    def test_permission_is_checked_before_any_lookup(self):
        self.frappe.denied = True
        with self.assertRaises(PermissionError):
            self.execute()
        self.client.query.assert_not_called()

    def test_graphql_and_transport_errors_are_distinguishable(self):
        self.client.query.side_effect = HeroClientError("HERO GraphQL rejected the request.", code="GRAPHQL_ERRORS",
                                                        details=("Contact not found",))
        graphql = self.execute()
        self.assertEqual((graphql["status"], graphql["error_kind"], graphql["graphql_errors"]), ("ERROR", "GRAPHQL", ["Contact not found"]))
        self.client.query.side_effect = HeroClientError("HERO request failed.", code="HTTP_503")
        transport = self.execute()
        self.assertEqual((transport["error_kind"], transport["error_code"]), ("TRANSPORT", "HTTP_503"))
        self.assertEqual(self.client.query.call_count, 2, "one outgoing attempt per click, no retry")

    def test_oversized_responses_are_not_returned(self):
        self.client.query.return_value = {"contact": {"id": "7", "email": "x" * (api.QUERY_RESULT_LIMIT_BYTES + 1)}}
        result = self.execute()
        self.assertEqual(result["status"], "TOO_LARGE")
        self.assertNotIn("data", result)

    def test_direct_mutation_execution_rpc_stays_gated_off(self):
        with self.assertRaisesRegex(ValueError, "."):
            api.execute_graphql_service("HERO-GQL-00001", "{}", "fp", 1)
        self.client.query.assert_not_called()

    # -- analyzer drafts ----------------------------------------------------------
    def test_analyzer_query_draft_uses_required_variables_and_typename_only(self):
        draft = api.workspace_analyzer_service_draft("SNAPSHOT", "operation:query:contact")
        self.assertEqual(draft["operation_kind"], "query")
        self.assertEqual(draft["required_variables"], [{"name": "id", "type": "ID!"}])
        self.assertEqual(draft["payload"]["query"], "query Contact($id: ID!) {\n  contact(id: $id) { __typename }\n}")
        self.assertEqual(api.workspace_analyzer_service_draft("SNAPSHOT", "operation:query:version")["payload"]["query"],
                         "query Version {\n  version\n}")
        with self.assertRaisesRegex(ValueError, "Select a Query or Mutation"):
            api.workspace_analyzer_service_draft("SNAPSHOT", "type:Contact")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            api.workspace_analyzer_service_draft("SNAPSHOT", "operation:query:missing")

    def test_analyzer_save_records_provenance_and_keeps_the_selected_operation(self):
        draft = api.workspace_analyzer_service_draft("SNAPSHOT", "operation:mutation:update_contact")
        self.assertEqual(draft["payload"]["query"], "mutation UpdateContact($input: ContactInput!) {\n  update_contact(input: $input) { __typename }\n}")
        saved = api.workspace_analyzer_service_save("SNAPSHOT", "operation:mutation:update_contact", json.dumps(draft["payload"]))
        self.assertEqual((saved["query"]["provenance"], saved["query"]["service_kind"]), ("API Analyzer", "mutation"))
        self.assertEqual(self.frappe.created[-1].status, "Draft")
        other = {"name": "x", "query": "query V { version }"}
        with self.assertRaisesRegex(ValueError, "selected operation"):
            api.workspace_analyzer_service_save("SNAPSHOT", "operation:query:contact", json.dumps(other))
        self.client.query.assert_not_called()

    def test_graphql_services_save_records_its_own_provenance(self):
        saved = api.workspace_service_save("SNAPSHOT", json.dumps({"name": "Version", "query": "query V { version }"}))
        self.assertEqual(saved["query"]["provenance"], "GraphQL Services")


if __name__ == "__main__":
    unittest.main()
