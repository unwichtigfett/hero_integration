from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.services.read_contract import ReadContractError, from_documents
from hero_integration.sources.hero import CUSTOMER_QUERY_V1, GraphQLServiceSource


class TestSavedReadContract(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(name="SNAP", schema_fingerprint="fp", source_instance="synthetic")
        self.mapping = SimpleNamespace(
            name="MAP", status="Active", schema_snapshot="SNAP", schema_fingerprint="fp", mapping_revision=3,
            mapping_json=(
                '{"target_operation":"HeroCustomers","records_path":"contacts",'
                '"identity_path":"id","mapping_definition_ids":["customer-id"],'
                '"variables":{"first":"$page_size","offset":"$offset",'
                '"orderBy":"id","showDeleted":true},"page_size":2}'
            ),
        )
        self.service = SimpleNamespace(
            name="SERVICE", status="Validated", schema_snapshot="SNAP", schema_fingerprint="fp", service_revision=7,
            read_mapping="MAP", execution_status="Approved", approved_service_revision=7, approved_mapping_revision=3,
            query_document=CUSTOMER_QUERY_V1,
        )

    def test_selected_service_document_and_variables_reach_the_transport(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({"contacts": [{"id": 41, "full_name": "Synthetic Ada"}]})
        page = GraphQLServiceSource(client, self.service, contract, "synthetic").customers()
        self.assertEqual(
            client.calls,
            [(self.service.query_document, {"first": 2, "offset": 0, "orderBy": "id", "showDeleted": True})],
        )
        self.assertEqual(page.records[0]["_hero_source_instance"], "synthetic")
        self.assertIsNone(page.next_offset)

    def test_unapproved_or_stale_contract_is_rejected_before_transport(self):
        self.service.execution_status = "Pending Approval"
        with self.assertRaisesRegex(ReadContractError, "explicit execution approval"):
            from_documents(self.service, self.mapping, self.snapshot)
        self.service.execution_status = "Approved"
        self.service.approved_mapping_revision = 2
        with self.assertRaisesRegex(ReadContractError, "mapping approval is stale"):
            from_documents(self.service, self.mapping, self.snapshot)

    def test_missing_selected_identity_path_is_rejected_before_effect(self):
        contract = from_documents(self.service, self.mapping, self.snapshot)
        client = _Client({"contacts": [{"full_name": "Synthetic Ada"}]})
        with self.assertRaisesRegex(ReadContractError, "absent"):
            GraphQLServiceSource(client, self.service, contract, "synthetic").customers()


class _Client:
    def __init__(self, data): self.data, self.calls = data, []
    def query(self, document, variables):
        self.calls.append((document, variables)); return self.data
