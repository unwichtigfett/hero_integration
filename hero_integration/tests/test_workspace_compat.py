from __future__ import annotations

import unittest

from hero_integration.services.workspace_compat import graph_projection, node_projection, schema_projection


SNAPSHOT = {"__schema": {
    "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
    "types": [
        {"kind": "OBJECT", "name": "Query", "fields": [{"name": "customer", "type": {"kind": "OBJECT", "name": "Customer"}}]},
        {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "change", "type": {"kind": "SCALAR", "name": "String"}}]},
        {"kind": "OBJECT", "name": "Customer", "fields": [{"name": "id", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "ID"}}}]},
        {"kind": "SCALAR", "name": "ID"}, {"kind": "SCALAR", "name": "String"},
    ],
}}


class TestWorkspaceCompatibilityProjection(unittest.TestCase):
    def test_complete_saved_schema_projection_keeps_roots_types_and_counts(self):
        data = schema_projection(SNAPSHOT)

        self.assertEqual(data["roots"], {"query": "Query", "mutation": "Mutation"})
        self.assertEqual(data["counts"]["types"], 5)
        self.assertEqual(data["counts"]["queries"], 1)
        self.assertEqual(data["counts"]["mutations"], 1)
        self.assertEqual(data["counts"]["fields"], 3)

    def test_graph_and_node_projection_use_saved_data_without_transport(self):
        graph = graph_projection(SNAPSHOT)

        self.assertIn("type:Customer", {node["node_id"] for node in graph["nodes"]})
        detail = node_projection(SNAPSHOT, "field:Customer.id")
        self.assertEqual(detail["node"]["name"], "id")
        self.assertEqual(detail["incoming"][0]["source_id"], "type:Customer")

    def test_search_filters_the_display_projection_not_the_fingerprint(self):
        complete = schema_projection(SNAPSHOT)
        filtered = schema_projection(SNAPSHOT, search="customer", kind="OBJECT")

        self.assertEqual(complete["fingerprint"], filtered["fingerprint"])
        self.assertEqual([row["name"] for row in filtered["types"]], ["Query", "Customer"])

    def test_historic_complete_snapshot_shape_normalizes_without_transport(self):
        historic = {"schema": {
            "query_type_name": "PartnerQuery", "mutation_type_name": "PartnerMutation",
            "types": [
                {"kind": "OBJECT", "name": "PartnerQuery", "fields": [{"name": "partners", "arguments": [], "type_ref": {"kind": "SCALAR", "name": "String", "of_type": None}}]},
                {"kind": "OBJECT", "name": "PartnerMutation", "fields": [{"name": "change", "arguments": [], "type_ref": {"kind": "SCALAR", "name": "String", "of_type": None}}]},
                {"kind": "INPUT_OBJECT", "name": "PartnerInput", "fields": [], "input_fields": [{"name": "name", "type_ref": {"kind": "SCALAR", "name": "String", "of_type": None}}]},
                {"kind": "SCALAR", "name": "String", "fields": []},
            ],
        }}
        data = schema_projection(historic)

        self.assertEqual(data["roots"], {"query": "PartnerQuery", "mutation": "PartnerMutation"})
        self.assertEqual(data["counts"]["fields"], 3)
