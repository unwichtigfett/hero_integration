from __future__ import annotations

import unittest

from graphql import build_schema, parse, introspection_from_schema
from graphql.execution.values import get_variable_values

from hero_integration.services.mutation_binding_contract import GraphQLContractError, parse_mutation, validate_mutation


SCHEMA = introspection_from_schema(build_schema('type Query { ping:String } '
    'type Mutation { changeCustomer(input:CustomerInput!):Customer } type Customer { id:ID } '
    'input CustomerInput { name:String! state:CustomerState } enum CustomerState { ACTIVE INACTIVE }'))


def optional_default_schema():
    return introspection_from_schema(build_schema('type Query { ping:String } '
        'type Mutation { changeCustomer(value:Int):String }'))


class TestGraphQLContract(unittest.TestCase):
    def test_pre_materializer_approval_fingerprint_remains_stable(self):
        document = 'mutation Change($input: CustomerInput!) { changeCustomer(input: $input) { id } }'
        self.assertEqual(parse_mutation(document).fingerprint,
                         'a7bb0f38316343347c2cbba4e741fa493e87808b7c444b84f5abc182dad34ccd')

    def test_fragment_selections_use_the_same_full_document_validation(self):
        document = ('mutation Change($input:CustomerInput!) { ...Root } '
            'fragment Root on Mutation { changeCustomer(input:$input) { ...Fields } } '
            'fragment Fields on Customer { id }')
        contract = parse_mutation(document)
        self.assertEqual(contract.root_field, 'changeCustomer')
        self.assertEqual(validate_mutation(SCHEMA, contract, {'input':{'name':'Ada'}}), {'input':{'name':'Ada'}})
        invalid = parse_mutation(document.replace('Fields on Customer { id }', 'Fields on Customer { missing }'))
        with self.assertRaises(GraphQLContractError):
            validate_mutation(SCHEMA, invalid, {'input':{'name':'Ada'}})

    def test_return_selections_and_binding_metadata_cannot_bypass_validation(self):
        document = 'mutation Change($input:CustomerInput!) { changeCustomer(input:$input) { missing } }'
        with self.assertRaises(GraphQLContractError):
            validate_mutation(SCHEMA, parse_mutation(document), {'input':{'name':'Ada'}})
        contract = parse_mutation(document.replace('missing', 'id'))
        contract.argument_variables['input'] = 'other'
        with self.assertRaises(GraphQLContractError):
            validate_mutation(SCHEMA, contract, {'input':{'name':'Ada'}})

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
        with self.assertRaisesRegex(GraphQLContractError, "declared types"):
            validate_mutation(SCHEMA, contract, {"input": {"state": "ACTIVE"}})
        with self.assertRaisesRegex(GraphQLContractError, "declared types"):
            validate_mutation(SCHEMA, contract, {"input": {"name": "Ada", "unknown": 1}})

    def test_optional_variable_default_is_included_in_effective_input(self):
        schema = optional_default_schema()
        document = "mutation Change($value: Int = 7) { changeCustomer(value: $value) }"
        contract = parse_mutation(document)
        self.assertEqual(validate_mutation(schema, contract, {}), {"value":7})
        self.assertNotEqual(contract.fingerprint, parse_mutation(document.replace("= 7", "= 8")).fingerprint)
        graphql_schema = build_schema("type Query { ping: String } type Mutation { changeCustomer(value: Int): String }")
        definitions = parse(document).definitions[0].variable_definitions
        self.assertEqual(get_variable_values(graphql_schema, definitions, {}), {"value": 7})

    def test_root_literal_arguments_are_not_write_contract_bindings(self):
        for literal in ("123456", "true", "ACTIVE"):
            with self.subTest(literal=literal), self.assertRaises(GraphQLContractError):
                parse_mutation(f"mutation {{ changeCustomer(value: {literal}) {{ id }} }}")

    def test_literals_and_extra_operations_cannot_hide_a_second_mutation(self):
        with self.assertRaisesRegex(GraphQLContractError, "exactly one operation"):
            parse_mutation("mutation { changeCustomer { id } } query { x }")
