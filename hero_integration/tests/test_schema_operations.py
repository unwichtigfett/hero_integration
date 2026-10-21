from __future__ import annotations

import unittest

from hero_integration.services.schema_operations import catalog, suggest


SCHEMA = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
    {"kind": "OBJECT", "name": "Query", "fields": [{"name": "heroCustomers", "args": [{"name": "first", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}}], "type": {"kind": "OBJECT", "name": "Customer"}}]},
    {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "updateCustomer", "args": [], "type": {"kind": "OBJECT", "name": "Customer"}}]}, {"kind": "OBJECT", "name": "Customer", "fields": []},
]}}


class TestSchemaOperations(unittest.TestCase):
    def test_catalogs_every_detected_query_and_mutation_root_operation(self):
        self.assertEqual(catalog(SCHEMA, "QUERY")[0]["operation_name"], "heroCustomers")
        self.assertEqual(catalog(SCHEMA, "MUTATION")[0]["operation_name"], "updateCustomer")
        self.assertEqual(catalog(SCHEMA, "QUERY")[0]["arguments"][0], {"name": "first", "type": "Int!", "required": True})

    def test_suggestions_are_explainable_and_draft_only_by_callers(self):
        result = suggest("customer_name", ["customerName", "projectTitle"])
        self.assertEqual(result[0]["candidate"], "customerName")
        self.assertIn("shared normalized names", result[0]["reason"])
