"""Query results with partial data, redacted errors, local variable validation and the mutation gate.

All HERO transports are mocked; no test sends a request or a mutation to HERO.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Imported first: it installs the minimal frappe stand-in when Frappe is not available.
from hero_integration.tests.test_workspace_service_execution import QUERY, SCHEMA, _Frappe, _Service
from hero_integration import api
from hero_integration.client.graphql import HeroClientError, HeroEndpoint, HeroGraphQLClient

MUTATION = "mutation UpdateContact($input: ContactInput!) { update_contact(input: $input) { id } }"


class _Base(unittest.TestCase):
    def setUp(self):
        self.frappe = _Frappe(_Service())
        self.snapshot = SimpleNamespace(name="SNAPSHOT", schema_fingerprint="fp")
        self.gate = MagicMock(return_value={"mutation_execution_enabled": False})
        for patcher in (patch.object(api, "frappe", self.frappe),
                        patch.object(api, "_validated_snapshot", MagicMock(return_value=(self.snapshot, SCHEMA))),
                        patch.object(api, "_mutation_execution_state", self.gate)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = MagicMock()
        self.client_class = MagicMock(return_value=self.client)
        self.mutation_client = MagicMock()
        self.mutation_class = MagicMock(return_value=self.mutation_client)
        configuration = SimpleNamespace(endpoint="endpoint", api_token="token", timeout_seconds=20)
        for patcher in (patch("hero_integration.client.graphql.HeroGraphQLClient", self.client_class),
                        patch("hero_integration.client.graphql_mutation.HeroMutationClient", self.mutation_class),
                        patch("hero_integration.client.settings.get_configuration", MagicMock(return_value=configuration))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, variables='{"id": "7"}'):
        return api.workspace_service_execute("SNAPSHOT", "HERO-GQL-00001", 3, variables)


class TestQueryResults(_Base):
    def test_partial_graphql_data_stays_visible_and_errors_are_flagged(self):
        self.client.query.side_effect = HeroClientError(
            "HERO GraphQL rejected the request.", code="GRAPHQL_ERRORS", details=("email: not allowed",),
            data={"contact": {"id": "7", "email": None}})
        result = self.execute()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["data"], {"contact": {"id": "7", "email": None}})
        self.assertEqual((result["error_kind"], result["graphql_errors"]), ("GRAPHQL", ["email: not allowed"]))
        self.assertGreater(result["size_bytes"], 0)

    def test_errors_without_data_are_never_reported_as_an_empty_success(self):
        self.client.query.side_effect = HeroClientError("HERO GraphQL rejected the request.", code="GRAPHQL_ERRORS",
                                                        details=("boom",))
        result = self.execute()
        self.assertEqual(result["status"], "ERROR")
        self.assertNotIn("data", result)
        self.client.query.side_effect = HeroClientError("HERO GraphQL response contains no data.", code="MISSING_DATA")
        self.assertEqual(self.execute()["error_kind"], "RESPONSE")

    def test_error_details_redact_an_exact_long_variable_value(self):
        self.client.query.side_effect = HeroClientError(
            "HERO GraphQL rejected the request.", code="GRAPHQL_ERRORS",
            details=('Contact "secret-4711" not found',))
        result = self.execute('{"id": "secret-4711"}')
        self.assertEqual(result["graphql_errors"], ['Contact "[redacted]" not found'])
        self.assertNotIn("secret-4711", json.dumps(result))

    def test_nested_overlapping_values_are_redacted_longest_first(self):
        variables = {"filters": [{"name": "Alice"}, {"name": "Alice Cooper"}], "number": 12345}
        details = ["Alice Cooper and Alice: 12345", "unrelated"]
        self.assertEqual(api._redacted_messages(details, variables),
                         ["[redacted] and [redacted]: [redacted]", "unrelated"])

    def test_transport_keeps_partial_data_on_graphql_errors(self):
        client = HeroGraphQLClient(HeroEndpoint("https://hero.example/graphql", "hero.example"), lambda: "token")
        payload = {"data": {"contact": None}, "errors": [{"message": "denied"}]}
        with patch.object(HeroGraphQLClient, "_request_with_retry", return_value=payload):
            with self.assertRaises(HeroClientError) as caught:
                client.query(QUERY, {"id": "7"})
        self.assertEqual((caught.exception.code, caught.exception.details, caught.exception.data),
                         ("GRAPHQL_ERRORS", ("denied",), {"contact": None}))


class TestLocalVariableValidation(_Base):
    def validate(self, document, variables, **kwargs):
        return api.workspace_service_validate(
            "SNAPSHOT", payload_json=json.dumps({"name": "x", "query": document}), variables_json=json.dumps(variables), **kwargs)

    def test_query_variables_follow_the_declarations_not_the_names(self):
        self.assertEqual(self.validate(QUERY, {"id": 5})["variables_validated"], ["id"])
        for variables, message in (({}, "Required query variable"), ({"id": "7", "extra": 1}, "extra"),
                                   ({"id": True}, "ID"), ({"id": [1]}, "ID")):
            with self.subTest(variables=variables), self.assertRaisesRegex(ValueError, message):
                self.validate(QUERY, variables)
        with self.assertRaisesRegex(ValueError, "JSON object"):
            api.workspace_service_validate("SNAPSHOT", payload_json=json.dumps({"name": "x", "query": QUERY}), variables_json="[1]")

    def test_saved_query_variables_are_checked_without_persisting_them(self):
        self.frappe.service.status = "Draft"
        result = api.workspace_service_validate("SNAPSHOT", query_id="HERO-GQL-00001", expected_revision=3,
                                                variables_json='{"id": "7"}')
        self.assertEqual(result["variables_validated"], ["id"])
        self.assertNotIn("_document", result)
        self.assertNotIn("7", json.dumps(result["query"]["variables"]))

    def test_mutation_draft_variables_validate_locally_while_the_gate_is_off(self):
        payload = json.dumps({"name": "m", "query": MUTATION, "operation_kind": "mutation"})
        ok = api.workspace_mutation_draft_validate("SNAPSHOT", payload, json.dumps({"input": {"email": "a@b"}}))
        self.assertEqual((ok["variables_validated"], ok["can_execute"]), (["input"], False))
        with self.assertRaisesRegex(ValueError, "."):
            api.workspace_mutation_draft_validate("SNAPSHOT", payload, json.dumps({"input": {"email": 5}}))
        with self.assertRaisesRegex(ValueError, "missing"):
            api.workspace_mutation_draft_validate("SNAPSHOT", payload, "{}")
        self.mutation_class.assert_not_called()
        self.client.query.assert_not_called()


class TestMutationExecutionContract(_Base):
    """Mock-only positive and negative contract checks; HERO is never contacted."""

    def setUp(self):
        super().setUp()
        self.contract = SimpleNamespace(status="Active", contract_revision=2)
        self.service = SimpleNamespace(operation_name="update_contact", query_document=MUTATION)
        self.request = MagicMock(return_value=(self.contract, self.service, self.snapshot, {"input": {"email": "a@b"}}, "req-fp"))
        self.audit = SimpleNamespace(correlation_id="corr-1")
        self.finalize = MagicMock()
        for patcher in (patch.object(api, "_validated_mutation_request", self.request),
                        patch.object(api, "_reserve_mutation", MagicMock(return_value=self.audit)),
                        patch.object(api, "_finalize_mutation", self.finalize)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_mutation(self, fingerprint="req-fp", confirmed=1):
        return api.execute_graphql_service("CONTRACT", '{"input": {"email": "a@b"}}', fingerprint, confirmed)

    def test_gate_off_blocks_before_any_validation_or_transport(self):
        with self.assertRaisesRegex(ValueError, "MUTATION_EXECUTION_DISABLED|disabled"):
            self.run_mutation()
        self.request.assert_not_called()
        self.mutation_class.assert_not_called()

    def test_released_gate_still_requires_active_contract_and_exact_confirmation(self):
        self.gate.return_value = {"mutation_execution_enabled": True}
        for prepare, kwargs in ((lambda: None, {"fingerprint": "other"}), (lambda: None, {"confirmed": 0}),
                                (lambda: setattr(self.contract, "status", "Draft"), {})):
            with self.subTest(kwargs=kwargs):
                prepare()
                with self.assertRaises(ValueError):
                    self.run_mutation(**kwargs)
                self.contract.status = "Active"
        self.mutation_class.assert_not_called()

    def test_failed_reservation_never_constructs_transport(self):
        self.gate.return_value = {"mutation_execution_enabled": True}
        api._reserve_mutation.side_effect = ValueError("reservation unavailable")
        with self.assertRaisesRegex(ValueError, "reservation unavailable"):
            self.run_mutation()
        self.mutation_class.assert_not_called()
        self.finalize.assert_not_called()

    def test_released_gate_with_contract_and_confirmation_sends_exactly_once_to_the_mock(self):
        self.gate.return_value = {"mutation_execution_enabled": True}
        result = self.run_mutation()
        self.assertEqual(result, {"status": "SUCCEEDED", "correlation_id": "corr-1", "operation": "update_contact"})
        self.mutation_client.execute_once.assert_called_once_with(MUTATION, {"input": {"email": "a@b"}})
        self.client.query.assert_not_called()
        self.assertEqual(self.finalize.call_args.args[1], "Succeeded")

    def test_mock_transport_failure_is_not_retried(self):
        self.gate.return_value = {"mutation_execution_enabled": True}
        self.mutation_client.execute_once.side_effect = OSError("down")
        with self.assertRaisesRegex(ValueError, "no automatic retry"):
            self.run_mutation()
        self.assertEqual(self.mutation_client.execute_once.call_count, 1)
        self.assertEqual(self.finalize.call_args.args[1], "Failed")


if __name__ == "__main__":
    unittest.main()


class TestServiceDraftOrigin(_Base):
    def test_graph_viewer_origin_is_recorded_and_unknown_origins_are_rejected(self):
        draft = api.workspace_analyzer_service_draft("SNAPSHOT", "operation:query:contact")
        payload = json.dumps({**draft["payload"], "query": "query Contact($id: ID!) { contact(id: $id) { id } }"})
        saved = api.workspace_analyzer_service_save("SNAPSHOT", "operation:query:contact", payload, "Graph Viewer")
        self.assertEqual(saved["query"]["provenance"], "Graph Viewer")
        self.assertEqual(api.workspace_analyzer_service_save("SNAPSHOT", "operation:query:contact", payload)["query"]["provenance"],
                         "API Analyzer")
        with self.assertRaisesRegex(ValueError, "Unknown service draft origin"):
            api.workspace_analyzer_service_save("SNAPSHOT", "operation:query:contact", payload, "GraphQL Services")
        self.client.query.assert_not_called()
