from __future__ import annotations

import unittest

from hero_integration.services.mutation_draft import validate


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
        with self.assertRaisesRegex(ValueError, "must start"):
            validate(SCHEMA, "query { changeCase { id } }")
