from __future__ import annotations

import unittest

from hero_integration.services.graphql_graph import GraphSnapshotError, analyze_snapshot, normalize_schema


class TestGraphqlGraphContract(unittest.TestCase):
    def test_graph_exposes_root_operations_arguments_inputs_and_custom_scalars(self):
        graph = analyze_snapshot({"__schema": {
            "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"},
            "types": [
                {"kind": "OBJECT", "name": "Query", "fields": [{
                    "name": "customer", "args": [{"name": "id", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "ID"}}}],
                    "type": {"kind": "OBJECT", "name": "Customer"},
                }]},
                {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "change", "type": {"kind": "SCALAR", "name": "String"}}]},
                {"kind": "OBJECT", "name": "Customer", "fields": [{"name": "score", "type": {"kind": "SCALAR", "name": "Money"}}]},
                {"kind": "INPUT_OBJECT", "name": "CustomerInput", "inputFields": [{"name": "name", "type": {"kind": "SCALAR", "name": "String"}}]},
                {"kind": "ENUM", "name": "Status", "enumValues": [{"name": "OPEN"}]},
                {"kind": "SCALAR", "name": "ID"}, {"kind": "SCALAR", "name": "String"}, {"kind": "SCALAR", "name": "Money"},
            ],
        }})

        nodes = {node["id"]: node for node in graph.nodes}
        self.assertEqual(nodes["operation:query:customer"]["kind"], "QUERY_OPERATION")
        self.assertEqual(nodes["argument:Query.customer.id"]["type_ref"], "ID!")
        self.assertEqual(nodes["input_field:CustomerInput.name"]["kind"], "INPUT_FIELD")
        self.assertEqual(nodes["enum_value:Status.OPEN"]["kind"], "ENUM_VALUE")
        self.assertEqual(nodes["type:Money"]["kind"], "SCALAR")
        self.assertIn({"source": "root:query", "target": "operation:query:customer", "kind": "ROOT_HAS_OPERATION"}, graph.edges)

    def test_public_normalizer_preserves_standard_schema_shape(self):
        standard = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": []}}
        self.assertIs(normalize_schema(standard), standard["__schema"])

    def test_conflicting_root_declarations_fail_closed(self):
        with self.assertRaisesRegex(GraphSnapshotError, "conflicting mutation root"):
            normalize_schema({"schema": {
                "mutationType": {"name": "Mutation"}, "mutation_type_name": "OtherMutation", "types": [],
            }})
