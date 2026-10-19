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
