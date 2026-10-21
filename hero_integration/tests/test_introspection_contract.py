from __future__ import annotations

import unittest

from hero_integration.services.introspection import IntrospectionError, canonical_schema, structural_diff


def _payload() -> dict[str, object]:
    return {"data": {"__schema": {
        "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "subscriptionType": None,
        "types": [
            {"kind": "OBJECT", "name": "Query", "fields": [{"name": "customers", "args": [], "type": {"kind": "LIST", "name": None, "ofType": {"kind": "OBJECT", "name": "Customer", "ofType": None}}}], "inputFields": None, "enumValues": None, "interfaces": [], "possibleTypes": []},
            {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "changeCustomer", "args": [{"name": "input", "defaultValue": None, "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "INPUT_OBJECT", "name": "CustomerInput", "ofType": None}}}], "type": {"kind": "OBJECT", "name": "Customer", "ofType": None}}], "inputFields": None, "enumValues": None, "interfaces": [], "possibleTypes": []},
            {"kind": "OBJECT", "name": "Customer", "fields": [{"name": "id", "args": [], "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None}}}], "inputFields": None, "enumValues": None, "interfaces": [], "possibleTypes": []},
            {"kind": "INPUT_OBJECT", "name": "CustomerInput", "fields": None, "inputFields": [{"name": "name", "defaultValue": None, "type": {"kind": "SCALAR", "name": "String", "ofType": None}}], "enumValues": None, "interfaces": None, "possibleTypes": None},
        ], "directives": []
    }}}


class TestIntrospectionContract(unittest.TestCase):
    def test_complete_introspection_preserves_roots_nested_types_and_arguments(self):
        schema = canonical_schema(_payload())
        self.assertEqual(schema["queryType"], {"name": "Query"})
        mutation = next(item for item in schema["types"] if item["name"] == "Mutation")
        self.assertEqual(mutation["fields"][0]["args"][0]["type"]["kind"], "NON_NULL")

    def test_errors_or_missing_query_root_never_become_a_snapshot(self):
        with self.assertRaisesRegex(IntrospectionError, "contains errors"):
            canonical_schema({"errors": [{"message": "denied"}], "data": {"__schema": {}}})
        invalid = _payload()
        invalid["data"]["__schema"]["queryType"] = None
        with self.assertRaisesRegex(IntrospectionError, "query root"):
            canonical_schema(invalid)

    def test_structural_diff_blocks_removed_fields_and_never_claims_compatibility(self):
        before = canonical_schema(_payload())
        after = canonical_schema(_payload())
        query = next(item for item in after["types"] if item["name"] == "Query")
        query["fields"] = []
        diff = structural_diff(before, after)
        self.assertTrue(diff["breaking"])
        self.assertIn("Query.customers", diff["removed_fields"])
