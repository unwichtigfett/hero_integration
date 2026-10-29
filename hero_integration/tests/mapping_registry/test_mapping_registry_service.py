from __future__ import annotations

import json
import unittest

from hero_integration.services.frappe_field_registry import (
    FieldProvenance, FrappeDocTypeDescriptor, FrappeFieldDescriptor,
    FrappeFieldReference, FrappeFieldRegistry, FrappeFieldTypeDescriptor,
    OwnershipState,
)
from hero_integration.services.mapping_registry_json import serialize_mapping_registry_v3
from hero_integration.services.mapping_registry_model import (
    GraphQLReference, GraphQLOperationKind, MappingAvailability,
    MappingDefinition, MappingDirection, MissingSemantics, MissingValuePolicy,
    TransformPolicy,
)
from hero_integration.services.mapping_registry_service import (
    IMPORT_REPLACEMENT_WARNING, MappingRegistryOperationError, MappingRegistryService,
)
from hero_integration.services.mapping_registry_storage import (
    InMemoryMappingRegistryStorage, MappingRegistryInjectedFailure,
)
from hero_integration.services.schema_registry import SchemaField, SchemaOperation, SchemaRegistry, SchemaTypeReference


FINGERPRINT = "a" * 64


def _type() -> SchemaTypeReference:
    return SchemaTypeReference("String", "String", "SCALAR", ())


def _schema() -> SchemaRegistry:
    query = SchemaField("hero.query.lookup.value", ("value",), _type(), False, "", operation_kind="QUERY", source_fingerprint=FINGERPRINT)
    mutation = SchemaField("hero.mutation.update.input.value", ("value",), _type(), False, "", operation_kind="MUTATION", source_fingerprint=FINGERPRINT)
    return SchemaRegistry(
        "snapshot", FINGERPRINT, "source", (),
        (SchemaOperation("QUERY", "lookup", "Query", _type(), (), (query,), (), False, False, FINGERPRINT),),
        (SchemaOperation("MUTATION", "update", "Mutation", _type(), (), (), (mutation,), False, False, FINGERPRINT),),
    )


def _fields(*, app: str = "example", include: bool = True) -> FrappeFieldRegistry:
    ref = FrappeFieldReference("example", "Sample", "value")
    field = FrappeFieldDescriptor(ref, "Example", "Value", FrappeFieldTypeDescriptor("Data", "TEXT", True, True), False, False, False, 0, FieldProvenance.STANDARD, False, None, None, None, (), None, False, False, False, True)
    doc = FrappeDocTypeDescriptor("example", "Sample", "Example", False, False, False, False, OwnershipState.RESOLVED)
    return FrappeFieldRegistry("test.local", (app,), (doc,), (field,) if include else ())


def _definition(identifier: str = "mapping.one", *, target: str = "example.Sample.value", revision: int = 1, direction: MappingDirection = MappingDirection.HERO_TO_FRAPPE) -> MappingDefinition:
    reference = GraphQLReference(GraphQLOperationKind.QUERY, "lookup", ("value",)) if direction == MappingDirection.HERO_TO_FRAPPE else GraphQLReference(GraphQLOperationKind.MUTATION, "update", ("value",), "input")
    return MappingDefinition(identifier, "source", direction, reference, _type(), FrappeFieldReference.parse(target), "Data", TransformPolicy.IDENTITY, MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.EXPLICIT_NULL), "snapshot", FINGERPRINT, revision)


def _raw(definitions: tuple[MappingDefinition, ...], *, source: str = "source") -> str:
    return serialize_mapping_registry_v3(site_scope="test.local", registry_revision=0, provenance={"source_instance": source, "snapshot_name": "snapshot", "schema_fingerprint": FINGERPRINT}, definitions=definitions)


class TestMappingRegistryService(unittest.TestCase):
    def _service(self, *, fields: FrappeFieldRegistry | None = None) -> tuple[MappingRegistryService, InMemoryMappingRegistryStorage]:
        storage = InMemoryMappingRegistryStorage()
        return MappingRegistryService(storage, _schema(), fields or _fields()), storage

    def test_empty_fresh_registry_exports_no_associations(self):
        service, _ = self._service()
        payload = json.loads(service.export_active_json("test.local"))
        self.assertEqual(payload["registry_revision"], 0)
        self.assertEqual(payload["definitions"], [])

    def test_complete_import_replaces_omitted_definition_and_round_trips(self):
        service, _ = self._service()
        first = service.import_complete_json(_raw((_definition("one"), _definition("two", direction=MappingDirection.FRAPPE_TO_HERO))), expected_revision=None)
        second = service.import_complete_json(_raw((_definition("two", direction=MappingDirection.FRAPPE_TO_HERO),)), expected_revision=first.revision)
        self.assertEqual((first.status, second.status, second.revision), ("ACTIVATED", "ACTIVATED", 2))
        exported = json.loads(service.export_active_json("test.local"))
        self.assertEqual([item["definition_id"] for item in exported["definitions"]], ["two"])
        self.assertEqual(second.warning, IMPORT_REPLACEMENT_WARNING)

    def test_missing_app_is_disabled_but_retained(self):
        service, _ = self._service(fields=_fields(app="other"))
        result = service.import_complete_json(_raw((_definition(target="missing.Sample.value"),)), expected_revision=None)
        self.assertEqual(result.status, "ACTIVATED")
        self.assertEqual(result.validations[0].availability, MappingAvailability.WAITING_APP)

    def test_missing_schema_is_pending_and_leaves_previous_active_version(self):
        service, storage = self._service(fields=_fields(include=False))
        result = service.import_complete_json(_raw((_definition(),)), expected_revision=None)
        self.assertEqual(result.status, "PENDING_PROVISIONING")
        self.assertIn("B5 schema provisioning required for example.Sample.value", result.validations[0].diagnostics[0])
        self.assertIsNone(storage.read_active("test.local"))

    def test_invalid_import_and_manual_conflict_preserve_active_version(self):
        service, _ = self._service()
        first = service.import_complete_json(_raw((_definition(),)), expected_revision=None)
        with self.assertRaises(MappingRegistryOperationError):
            service.import_complete_json("{not-json", expected_revision=first.revision)
        self.assertEqual(json.loads(service.export_active_json("test.local"))["registry_revision"], first.revision)
        with self.assertRaises(MappingRegistryOperationError):
            service.add_definition(_definition("another"), expected_revision=first.revision + 1)

    def test_injected_activation_failure_preserves_previous_active_version(self):
        service, storage = self._service()
        first = service.import_complete_json(_raw((_definition(),)), expected_revision=None)
        storage.fail_next("activate")
        with self.assertRaises(MappingRegistryInjectedFailure):
            service.import_complete_json(_raw((_definition("replacement"),)), expected_revision=first.revision)
        self.assertEqual(json.loads(service.export_active_json("test.local"))["registry_revision"], first.revision)

    def test_manual_update_creates_new_complete_revision_without_binding_side_effects(self):
        service, _ = self._service()
        first = service.import_complete_json(_raw((_definition(),)), expected_revision=None)
        updated = service.update_definition(_definition(revision=2), expected_revision=first.revision)
        self.assertEqual((updated.status, updated.revision), ("ACTIVATED", 2))
        self.assertEqual(len(json.loads(service.export_active_json("test.local"))["definitions"]), 1)


if __name__ == "__main__":
    unittest.main()
