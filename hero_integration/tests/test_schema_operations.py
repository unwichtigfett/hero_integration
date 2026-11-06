from __future__ import annotations

import copy
import unittest

from graphql import build_schema, introspection_from_schema, parse

from hero_integration.services.schema_operations import SchemaOperationError, catalog, operation_service_draft


SCHEMA = introspection_from_schema(build_schema('''
    type Query { heroCustomers(first:Int!):Customer }
    type Mutation { updateCustomer:Customer }
    type Customer { id:ID }
'''))


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

    def test_incomplete_schema_cannot_generate_an_apparently_valid_service_draft(self):
        schema = copy.deepcopy(SCHEMA)
        schema['__schema']['types'] = [item for item in schema['__schema']['types'] if item['name'] != 'Customer']
        with self.assertRaisesRegex(SchemaOperationError,'cannot be materialized'):
            operation_service_draft(schema,'operation:query:heroCustomers')

    def test_input_catalog_bounds_cycles_depth_and_list_mapping(self):
        schema = copy.deepcopy(SCHEMA)
        mutation = next(item for item in schema["__schema"]["types"] if item["name"] == "Mutation")
        mutation["fields"][0]["args"] = [{"name": "input", "type": {"kind": "INPUT_OBJECT", "name": "A"}}]
        for current, child in zip("ABCD", "BCDE"):
            fields = [{"name": "next", "type": {"kind": "INPUT_OBJECT", "name": child}}]
            if current == "A":
                fields.extend([
                    {"name": "cycle", "type": {"kind": "INPUT_OBJECT", "name": "A"}},
                    {"name": "many", "type": {"kind": "LIST", "ofType": {"kind": "SCALAR", "name": "String"}}},
                ])
            schema["__schema"]["types"].append({"kind": "INPUT_OBJECT", "name": current, "inputFields": fields})
        schema["__schema"]["types"].append({"kind": "INPUT_OBJECT", "name": "E", "inputFields": [
            {"name": "value", "type": {"kind": "SCALAR", "name": "String"}},
        ]})
        fields = catalog(schema, "MUTATION")[0]["input_fields"]["input"]
        by_path = {item["path"]: item for item in fields}
        self.assertIn("next.next.next.next", by_path)
        self.assertNotIn("next.next.next.next.value", by_path)
        self.assertIn("cycle", by_path)
        self.assertFalse(any(path.startswith("cycle.") for path in by_path))
        self.assertFalse(by_path["many"]["selectable"])
        self.assertIn("collection resolver", by_path["many"]["reason"])
