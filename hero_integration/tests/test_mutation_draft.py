from __future__ import annotations

import unittest

from hero_integration.services.mutation_draft import operation_kind, validate


from pathlib import Path
import json

SCHEMA = json.loads((Path(__file__).with_name('fixtures') / 'mutation_draft_schema.json').read_text())


class TestMutationDraft(unittest.TestCase):
    def test_local_validation_uses_saved_schema_without_execution(self):
        self.assertEqual(validate(SCHEMA, "mutation Update { changeCase { id } }")["return_type"], "Case")

    def test_unknown_or_non_mutation_documents_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Cannot query field"):
            validate(SCHEMA, "mutation { deleteCase { id } }")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate(SCHEMA, "query { changeCase { id } }")

    def test_mixed_operations_and_secret_literals_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate(SCHEMA, "mutation { changeCase { id } } query { changeCase { id } }")
        with self.assertRaisesRegex(ValueError, "literal values"):
            validate(SCHEMA, 'mutation { changeCase(api_token: "not-a-real-secret") { id } }')

    def test_local_literal_check_does_not_reject_numeric_boolean_or_enum_values(self):
        document = "mutation { changeCase(id: 123456, enabled: true, state: ACTIVE) { id } }"
        self.assertEqual(validate(SCHEMA, document)["operation"], "changeCase")

    def test_root_fragments_aliases_and_comments_use_the_full_ast(self):
        document = ('# a comment with "quotes" and the word query\n'
            'mutation Change($id:Int=3) { ...Root } '
            'fragment Root on Mutation { selected:changeCase(id:$id) { ...Fields } } '
            'fragment Fields on Case { id }')
        self.assertEqual(operation_kind(document), 'mutation')
        self.assertEqual(validate(SCHEMA, document)['operation'], 'changeCase')
        from hero_integration.services.mutation_draft import validate_variables
        self.assertEqual(validate_variables(SCHEMA, document, {}), {'id':3})

    def test_every_selected_field_and_argument_is_validated(self):
        for document in ('mutation { changeCase { missing } }',
                         'mutation { changeCase(unknown:1) { id } }',
                         'mutation { changeCase(enabled:123) { id } }',
                         'mutation { changeCase { ... on Query { version } } }',
                         'mutation { changeCase { ...Fields } } fragment Fields on Case { missing }'):
            with self.subTest(document=document), self.assertRaises(ValueError):
                validate(SCHEMA, document)

    def test_multiple_root_selections_remain_visible_to_the_service_boundary(self):
        document = 'mutation Change($id:Int) { first:changeCase(id:$id) { id } second:changeCase { id } }'
        detail = validate(SCHEMA, document)
        self.assertEqual(detail['root_fields'], ['changeCase', 'changeCase'])
        from hero_integration.services.mutation_draft import validate_variables
        self.assertEqual(validate_variables(SCHEMA, document, {'id':3}), {'id':3})

    def test_query_kind_allows_anonymous_queries_but_not_mixed_documents(self):
        self.assertEqual(operation_kind("{ changeCase { id } }"), "query")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            operation_kind("{ changeCase { id } } mutation { changeCase { id } }")

    def test_historic_full_snapshot_uses_the_shared_normalizer(self):
        from graphql import build_schema, introspection_from_schema
        body = introspection_from_schema(build_schema('schema { query:PartnerQuery mutation:PartnerMutation } type PartnerQuery { version:String } '
            'type PartnerMutation { changePartner:Partner } type Partner { id:ID }'))['__schema']
        body['query_type_name'] = body.pop('queryType')['name']
        body['mutation_type_name'] = body.pop('mutationType')['name']
        for type_ in body['types']:
            for field in type_.get('fields') or []:
                field['arguments'] = field.pop('args')
                field['type_ref'] = field.pop('type')
        def omit_null_metadata(value):
            if isinstance(value, dict):
                return {key:omit_null_metadata(item) for key,item in value.items() if item is not None}
            if isinstance(value, list):
                return [omit_null_metadata(item) for item in value]
            return value
        historic = {'schema':omit_null_metadata(body)}
        detail = validate(historic, "mutation Update { changePartner { id } }")
        self.assertEqual(detail, {"operation": "changePartner", "root": "PartnerMutation", "return_type": "Partner"})

    def test_already_normalized_schema_is_accepted(self):
        normalized = SCHEMA["__schema"]
        self.assertEqual(validate(normalized, "mutation { changeCase { id } }")["root"], "Mutation")

    def test_missing_null_unknown_and_conflicting_mutation_roots_are_rejected(self):
        cases = (
            {"__schema": {"types": SCHEMA["__schema"]["types"]}},
            {"__schema": {"mutationType": None, "types": SCHEMA["__schema"]["types"]}},
            {"__schema": {"mutationType": {"name": "Missing"}, "types": SCHEMA["__schema"]["types"]}},
            {"schema": {
                "mutationType": {"name": "Mutation"}, "mutation_type_name": "PartnerMutation",
                "types": SCHEMA["__schema"]["types"],
            }},
        )
        for schema in cases:
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "saved schema"):
                validate(schema, "mutation { changeCase { id } }")
