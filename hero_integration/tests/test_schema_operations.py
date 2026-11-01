from __future__ import annotations

import copy
import unittest

from graphql import parse

from hero_integration.services.schema_operations import SchemaOperationError, catalog, operation_service_draft, suggest


SCHEMA = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
    {"kind": "OBJECT", "name": "Query", "fields": [{"name": "heroCustomers", "args": [{"name": "first", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "Int"}}}], "type": {"kind": "OBJECT", "name": "Customer"}}]},
    {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "updateCustomer", "args": [], "type": {"kind": "OBJECT", "name": "Customer"}}]}, {"kind": "OBJECT", "name": "Customer", "fields": []},
]}}


class TestSchemaOperations(unittest.TestCase):
    def test_catalogs_every_detected_query_and_mutation_root_operation(self):
        self.assertEqual(catalog(SCHEMA, "QUERY")[0]["operation_name"], "heroCustomers")
        self.assertEqual(catalog(SCHEMA, "MUTATION")[0]["operation_name"], "updateCustomer")
        self.assertEqual(catalog(SCHEMA, "QUERY")[0]["arguments"][0], {"name": "first", "type": "Int!", "required": True})

    def test_generated_service_drafts_keep_operation_kind_and_required_variables_separate(self):
        schema = copy.deepcopy(SCHEMA)
        mutation = next(item for item in schema["__schema"]["types"] if item["name"] == "Mutation")
        mutation["fields"][0]["args"] = [
            {"name": "id", "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "ID"}}},
            {"name": "note", "type": {"kind": "SCALAR", "name": "String"}},
        ]
        for node, kind, variable in (("operation:query:heroCustomers", "query", "first"),
                                     ("operation:mutation:updateCustomer", "mutation", "id")):
            with self.subTest(kind=kind):
                draft = operation_service_draft(schema, node)
                self.assertEqual(draft["operation_kind"], kind)
                self.assertEqual([item["name"] for item in draft["required_variables"]], [variable])
                self.assertEqual(draft["payload"]["variables"], {})
                document = parse(draft["payload"]["query"])
                self.assertEqual(len(document.definitions), 1)
                self.assertEqual(document.definitions[0].operation.value, kind)
                self.assertIn("__typename", draft["payload"]["query"])
        with self.assertRaises(SchemaOperationError):
            operation_service_draft(schema, "operation:mutation:heroCustomers")

    def test_suggestions_are_explainable_and_draft_only_by_callers(self):
        result = suggest("customer_name", ["customerName", "projectTitle"])
        self.assertEqual(result[0]["candidate"], "customerName")
        self.assertIn("shared normalized names", result[0]["reason"])
