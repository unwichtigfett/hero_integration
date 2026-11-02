"""Skip deleted HERO projects and KONT- requests omitted from project lists but referenced by Tasks and Events."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.client.graphql import HeroClientError
from hero_integration.sources.hero import GraphQLServiceSource


class _Client:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.documents = rows, fail, []

    def query(self, document, variables):
        self.documents.append(document)
        data = {f"p{key}": value for key, value in self.rows.items() if f"project_match_id: {key})" in document}
        if self.fail:
            raise HeroClientError("partial", code="GRAPHQL_ERRORS", details=("Project not found.",), data=data)
        return {"data": data}


def _source(client):
    return GraphQLServiceSource(client, SimpleNamespace(), SimpleNamespace(), "default")


class TestProjectReferenceStates(unittest.TestCase):
    def test_states_are_read_in_bundles_and_partial_errors_are_tolerated(self):
        rows = {1: {"id": 1, "is_deleted": True, "project_nr": "PV-1"},
                2: {"id": 2, "is_deleted": False, "project_nr": "KONT-22"},
                3: {"id": 3, "is_deleted": False, "project_nr": "PV-3"}}
        client = _Client(rows, fail=True)
        states = _source(client).project_reference_states(list(range(1, 61)) + [0, None, True])
        self.assertEqual(states, {1: "DELETED", 2: "LEAD", 3: "UNLISTED"})
        self.assertEqual(len(client.documents), 2)  # 60 IDs, 50 per bundle
        self.assertTrue(all(document.startswith("query ") and "mutation" not in document for document in client.documents))


class TestMigrationProjectReferences(unittest.TestCase):
    def setUp(self):
        from hero_integration.services import migration
        self.migration = migration
        migration._PROJECT_REFERENCE_STATES.clear()
        self.addCleanup(migration._PROJECT_REFERENCE_STATES.clear)

    def test_deleted_lead_and_unknown_references(self):
        m = self.migration
        m._PROJECT_REFERENCE_STATES.update({("default", 7): "DELETED", ("default", 8): "LEAD", ("default", 9): "UNLISTED"})
        with patch.object(m, "_link", return_value=None):
            with self.assertRaises(m.DeletedSourceProjectError):
                m._project_name_for_source(7, "default")
            self.assertIsNone(m._project_name_for_source(8, "default"))
            for unknown in (9, 10):
                with self.assertRaisesRegex(m.MappingPlanError, "no verified native Project binding"):
                    m._project_name_for_source(unknown, "default")

    def test_prefetch_reads_only_unbound_references_once_per_id(self):
        m = self.migration
        calls = []
        source = SimpleNamespace(source_instance="default",
                                 project_reference_states=lambda ids: calls.append(sorted(ids)) or {5: "DELETED"})
        records = [{"target_project_match_id": value} for value in (5, 5, 6, 0, None)]
        with patch.object(m, "_link", side_effect=lambda kind, value, instance: value == "6"):
            m._prefetch_project_references(source, "HeroTasks", records)
            m._prefetch_project_references(source, "HeroTasks", records)
        self.assertEqual(calls, [[5]])
        self.assertEqual(m._PROJECT_REFERENCE_STATES, {("default", 5): "DELETED"})


if __name__ == "__main__":
    unittest.main()
