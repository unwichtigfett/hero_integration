from __future__ import annotations

import json

from hero_integration.services.mapping_registry_json import (
    FORMAT,
    FORMAT_VERSION,
    MappingRegistryJsonError,
    canonical_mapping_registry_json,
    parse_mapping_registry_v3,
    serialize_mapping_registry_v3,
)
from hero_integration.services.mapping_registry_model import (
    GraphQLReference,
    GraphQLOperationKind,
    MappingDefinition,
    MappingDirection,
    MissingSemantics,
    MissingValuePolicy,
    TransformPolicy,
)
from hero_integration.services.frappe_field_registry import FrappeFieldReference
from hero_integration.services.schema_registry import SchemaTypeReference


def _definition(identifier: str = "mapping.case.title") -> MappingDefinition:
    return MappingDefinition(
        definition_id=identifier, source_instance="hero_a", direction=MappingDirection.HERO_TO_FRAPPE,
        graphql=GraphQLReference(GraphQLOperationKind.QUERY, "case", ("title",)),
        graphql_type=SchemaTypeReference("String", "String", "SCALAR", ()),
        frappe=FrappeFieldReference("hero_integration", "CaseMirror", "title"), frappe_field_type="Data",
        transform=TransformPolicy.IDENTITY,
        missing=MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.EXPLICIT_NULL),
        snapshot_name="snapshot-a", schema_fingerprint="a" * 64, mapping_revision=1,
    )


def _provenance() -> dict[str, object]:
    return {"source_instance": "hero-a", "snapshot_name": "snapshot-a", "schema_fingerprint": "a" * 64}


def _export(definitions: list[MappingDefinition] | None = None) -> str:
    return serialize_mapping_registry_v3(
        site_scope="test.local",
        registry_revision=7,
        provenance=_provenance(),
        definitions=definitions or [_definition()],
    )


def _expect_error(callable_: object) -> None:
    try:
        callable_()
    except MappingRegistryJsonError:
        return
    raise AssertionError("MappingRegistryJsonError was expected")


def test_serializes_frozen_complete_format_deterministically() -> None:
    first = _export([_definition("b"), _definition("a")])
    second = _export([_definition("b"), _definition("a")])
    payload = json.loads(first)
    assert first == second
    assert payload["format"] == FORMAT
    assert payload["format_version"] == FORMAT_VERSION
    assert set(payload) == {"format", "format_version", "site_scope", "registry_revision", "provenance", "definitions", "integrity"}
    assert payload["definitions"][0]["definition_id"] == "a"


def test_export_parse_round_trip_preserves_complete_set() -> None:
    exported = _export([_definition("one"), _definition("two")])
    parsed = parse_mapping_registry_v3(exported)
    assert parsed.site_scope == "test.local"
    assert parsed.registry_revision == 7
    assert [item.definition_id for item in parsed.definitions] == ["one", "two"]
    assert canonical_mapping_registry_json(parsed) == exported


def test_rejects_duplicate_json_key_before_activation() -> None:
    _expect_error(lambda: parse_mapping_registry_v3('{"format":"hero-frappe-mapping-v3","format":"hero-frappe-mapping-v3"}'))


def test_rejects_credentials_bindings_and_executable_instructions() -> None:
    raw = json.loads(_export())
    for field in ("api_key", "bindings", "query_document", "execute"):
        raw["definitions"][0][field] = "not allowed"
        _expect_error(lambda: parse_mapping_registry_v3(json.dumps(raw)))
        del raw["definitions"][0][field]


def test_rejects_unknown_schema_duplicate_ids_and_tampering() -> None:
    _expect_error(lambda: _export([_definition("same"), _definition("same")]))
    raw = json.loads(_export())
    raw["unknown"] = True
    _expect_error(lambda: parse_mapping_registry_v3(json.dumps(raw)))
    raw = json.loads(_export())
    raw["definitions"][0]["frappe_field_type"] = "changed"
    _expect_error(lambda: parse_mapping_registry_v3(json.dumps(raw)))


def test_rejects_unsupported_version_and_missing_definition_metadata() -> None:
    raw = json.loads(_export())
    raw["format_version"] = 99
    _expect_error(lambda: parse_mapping_registry_v3(json.dumps(raw)))
    raw = json.loads(_export())
    del raw["definitions"][0]["graphql_type"]
    _expect_error(lambda: parse_mapping_registry_v3(json.dumps(raw)))


if __name__ == "__main__":
    for test in tuple(value for name, value in globals().items() if name.startswith("test_")):
        test()
