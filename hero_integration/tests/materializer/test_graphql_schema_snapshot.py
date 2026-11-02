"""Offline evidence for canonical schema fingerprints and snapshot integrity."""

from dataclasses import replace

import re
import unittest
from contextlib import contextmanager


@contextmanager
def _raises(expected, match):
    """unittest replacement for pytest.raises(expected, match=...)."""
    try:
        yield
    except expected as error:
        if not re.search(match, str(error)):
            raise AssertionError(f"{expected.__name__} message {str(error)!r} does not match {match!r}") from error
        return
    raise AssertionError(f"{expected.__name__} not raised")

from hero_integration.graphql_protocol.schema import (
    GraphQLArgument, GraphQLField, GraphQLSchema, GraphQLType, GraphQLTypeRef,
)
from hero_integration.graphql_protocol.schema_snapshot import (
    GraphQLSchemaSnapshot, GraphQLSchemaSnapshotError, GraphQLSchemaSnapshotStore, schema_fingerprint,
)


def _schema():
    string = GraphQLTypeRef("SCALAR", "String")
    arguments = (GraphQLArgument("first", None, string, None), GraphQLArgument("last", None, string, None))
    fields = (
        GraphQLField("one", None, arguments, string, False, None),
        GraphQLField("two", None, (), string, False, None),
    )
    return GraphQLSchema("Query", None, None, (
        GraphQLType("OBJECT", "Query", None, fields), GraphQLType("SCALAR", "String", None),
    ))


def test_fingerprint_ignores_schema_type_field_and_argument_order():
    schema = _schema()
    query, scalar = schema.types
    first, second = query.fields
    reordered_query = replace(query, fields=(second, replace(first, arguments=first.arguments[::-1])))
    reordered = replace(schema, types=(scalar, reordered_query))
    assert schema_fingerprint(reordered) == schema_fingerprint(schema)
    changed_field = replace(first, type_ref=GraphQLTypeRef("NON_NULL", of_type=first.type_ref))
    changed = replace(schema, types=(replace(query, fields=(changed_field, second)), scalar))
    assert schema_fingerprint(changed) != schema_fingerprint(schema)


def test_snapshot_rejects_modified_schema_under_existing_fingerprint():
    snapshot = GraphQLSchemaSnapshot.create(
        _schema(), connection_id="connection", endpoint_fingerprint="endpoint", complete_analysis=True,
    )
    stored = snapshot.to_dict()
    assert GraphQLSchemaSnapshot.from_dict(stored) == snapshot
    stored["schema"]["types"][0]["description"] = "tampered"
    with _raises(GraphQLSchemaSnapshotError, "fingerprint does not match"):
        GraphQLSchemaSnapshot.from_dict(stored)


def test_incomplete_snapshot_cannot_replace_complete_and_previous_is_preserved(tmp_path):
    store = GraphQLSchemaSnapshotStore(tmp_path)
    first = GraphQLSchemaSnapshot.create(
        _schema(), connection_id="connection", endpoint_fingerprint="endpoint", complete_analysis=True,
    )
    changed_schema = replace(first.schema, types=(replace(first.schema.types[0], description="changed"),
                                                  first.schema.types[1]))
    store.save(first)
    store.save(GraphQLSchemaSnapshot.create(changed_schema))
    assert store.load() == first
    assert store.load_previous() is None

    next_snapshot = GraphQLSchemaSnapshot.create(
        changed_schema, connection_id="connection", endpoint_fingerprint="endpoint", complete_analysis=True,
    )
    store.save(next_snapshot)
    assert store.load() == next_snapshot
    assert store.load_previous() == first


class TestOfflineEvidence(unittest.TestCase):
    """Runs the evidence functions under the bench unittest runner (no pytest installed)."""

    def test_fingerprint_ignores_schema_type_field_and_argument_order(self):
        test_fingerprint_ignores_schema_type_field_and_argument_order()

    def test_snapshot_rejects_modified_schema_under_existing_fingerprint(self):
        test_snapshot_rejects_modified_schema_under_existing_fingerprint()

    def test_incomplete_snapshot_cannot_replace_complete_and_previous_is_preserved(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            test_incomplete_snapshot_cannot_replace_complete_and_previous_is_preserved(Path(directory))
