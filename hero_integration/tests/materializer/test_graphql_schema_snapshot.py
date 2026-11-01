"""Offline evidence for canonical schema fingerprints and snapshot integrity."""

from dataclasses import replace

import pytest

from hero_integration.graphql_protocol.schema import (
    GraphQLArgument, GraphQLField, GraphQLSchema, GraphQLType, GraphQLTypeRef,
)
from hero_integration.graphql_protocol.schema_snapshot import (
    GraphQLSchemaSnapshot, GraphQLSchemaSnapshotError, schema_fingerprint,
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
    with pytest.raises(GraphQLSchemaSnapshotError, match="fingerprint does not match"):
        GraphQLSchemaSnapshot.from_dict(stored)
