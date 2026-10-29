from __future__ import annotations

import unittest

from hero_integration.services.graphql_contract import GraphQLContractError, parse_mutation, validate_mutation


SCHEMA = {"__schema": {"queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": [
    {"kind": "OBJECT", "name": "Query", "fields": []},
    {"kind": "OBJECT", "name": "Mutation", "fields": [{"name": "changeCustomer", "args": [{"name": "input", "defaultValue": None, "type": {"kind": "NON_NULL", "ofType": {"kind": "INPUT_OBJECT", "name": "CustomerInput"}}}], "type": {"kind": "OBJECT", "name": "Customer"}}]},
    {"kind": "OBJECT", "name": "Customer", "fields": [{"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID"}}]},
    {"kind": "INPUT_OBJECT", "name": "CustomerInput", "inputFields": [{"name": "name", "defaultValue": None, "type": {"kind": "NON_NULL", "ofType": {"kind": "SCALAR", "name": "String"}}}, {"name": "state", "defaultValue": None, "type": {"kind": "ENUM", "name": "CustomerState"}}]},
    {"kind": "ENUM", "name": "CustomerState", "enumValues": [{"name": "ACTIVE"}, {"name": "INACTIVE"}]},
]}}


class TestGraphQLContract(unittest.TestCase):
    def test_root_argument_binding_requires_compatible_declared_variable_type(self):
        invalid = parse_mutation('mutation Demo($input: String!) { changeCustomer(input: $input) { id } }')
        with self.assertRaises(GraphQLContractError):
            validate_mutation(SCHEMA, invalid, {'input':'not-an-object'})
        valid = parse_mutation('mutation Demo($value: CustomerInput!) { changeCustomer(input: $value) { id } }')
        self.assertEqual(validate_mutation(SCHEMA, valid, {'value':{'name':'Ada'}}), {'value':{'name':'Ada'}})
        for document in ('mutation Demo($value: CustomerInput) { changeCustomer(input: $value) { id } }',
                         'mutation Demo($value: CustomerInput!) { changeCustomer(other: $value) { id } }',
                         'mutation Demo($value: CustomerInput!) { changeCustomer(input: $missing) { id } }'):
            with self.assertRaises(GraphQLContractError):
                validate_mutation(SCHEMA, parse_mutation(document), {'value':{'name':'Ada'}})
        with self.assertRaises(GraphQLContractError):
            parse_mutation('mutation Demo { changeCustomer(input: {name:"Ada"}) { id } }')
        with self.assertRaisesRegex(GraphQLContractError, 'Duplicate mutation argument'):
            parse_mutation('mutation Demo($value: CustomerInput!) { changeCustomer(input: $value, input: $value) { id } }')
    def test_ast_contract_accepts_one_mutation_and_validates_nested_input(self):
        contract = parse_mutation("mutation Change($input: CustomerInput!) { changeCustomer(input: $input) { id } }")
        self.assertEqual(contract.root_field, "changeCustomer")
        self.assertEqual(validate_mutation(SCHEMA, contract, {"input": {"name": "Ada", "state": "ACTIVE"}}), {"input": {"name": "Ada", "state": "ACTIVE"}})

    def test_multiple_root_operations_unknown_input_and_missing_required_value_are_blocked(self):
        with self.assertRaisesRegex(GraphQLContractError, "exactly one root"):
            parse_mutation("mutation { first { id } second { id } }")
        contract = parse_mutation("mutation($input: CustomerInput!) { changeCustomer(input: $input) { id } }")
        with self.assertRaisesRegex(GraphQLContractError, "Required input field"):
            validate_mutation(SCHEMA, contract, {"input": {"state": "ACTIVE"}})
        with self.assertRaisesRegex(GraphQLContractError, "unknown input"):
            validate_mutation(SCHEMA, contract, {"input": {"name": "Ada", "unknown": 1}})

    def test_literals_and_extra_operations_cannot_hide_a_second_mutation(self):
        with self.assertRaisesRegex(GraphQLContractError, "exactly one operation"):
            parse_mutation("mutation { changeCustomer { id } } query { x }")
