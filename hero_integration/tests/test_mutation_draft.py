from __future__ import annotations

import unittest

from hero_integration.services.mutation_draft import operation_kind, validate


SCHEMA = {"__schema": {"mutationType": {"name": "Mutation"}, "types": [
    {"name": "Mutation", "fields": [{"name": "changeCase", "type": {"kind": "OBJECT", "name": "Case"}}]},
    {"name": "Case", "fields": []},
]}}


class TestMutationDraft(unittest.TestCase):
    def test_local_validation_uses_saved_schema_without_execution(self):
        self.assertEqual(validate(SCHEMA, "mutation Update { changeCase { id } }")["return_type"], "Case")

    def test_unknown_or_non_mutation_documents_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            validate(SCHEMA, "mutation { deleteCase { id } }")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate(SCHEMA, "query { changeCase { id } }")

    def test_mixed_operations_and_secret_literals_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            validate(SCHEMA, "mutation { changeCase { id } } query { changeCase { id } }")
        with self.assertRaisesRegex(ValueError, "literal values"):
            validate(SCHEMA, 'mutation { changeCase(api_token: "not-a-real-secret") { id } }')

    def test_query_kind_allows_anonymous_queries_but_not_mixed_documents(self):
        self.assertEqual(operation_kind("{ changeCase { id } }"), "query")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            operation_kind("{ changeCase { id } } mutation { changeCase { id } }")

    def test_historic_full_snapshot_uses_the_shared_normalizer(self):
        historic = {"schema": {
            "query_type_name": "PartnerQuery", "mutation_type_name": "PartnerMutation",
            "types": [
                {"kind": "OBJECT", "name": "PartnerQuery", "fields": []},
                {"kind": "OBJECT", "name": "PartnerMutation", "fields": [{
                    "name": "changePartner", "arguments": [],
                    "type_ref": {"kind": "OBJECT", "name": "Partner", "of_type": None},
                }]},
                {"kind": "OBJECT", "name": "Partner", "fields": []},
            ],
        }}
        detail = validate(historic, "mutation Update { changePartner { id } }")
        self.assertEqual(detail, {"operation": "changePartner", "root": "PartnerMutation", "return_type": "Partner"})

    def test_already_normalized_schema_is_accepted(self):
        normalized = {"types": SCHEMA["__schema"]["types"], "mutationType": {"name": "Mutation"}}
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
