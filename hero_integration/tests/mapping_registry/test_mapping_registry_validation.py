from __future__ import annotations

import unittest

from hero_integration.services.frappe_field_registry import (
    FieldProvenance,
    FrappeFieldDescriptor,
    FrappeFieldReference,
    FrappeFieldRegistry,
    FrappeFieldTypeDescriptor,
    FrappeDocTypeDescriptor,
    OwnershipState,
)
from hero_integration.services.mapping_registry_model import (
    GraphQLOperationKind,
    GraphQLReference,
    MappingAvailability,
    MappingDefinition,
    MappingDirection,
    MissingSemantics,
    MissingValuePolicy,
    TransformPolicy,
)
from hero_integration.services.mapping_registry_validation import MappingValidationError, validate_complete_mapping_set, validate_mapping_definition
from hero_integration.services.schema_registry import SchemaField, SchemaOperation, SchemaRegistry, SchemaTypeReference


FINGERPRINT = "a" * 64


def type_ref(name="String", kind="SCALAR", wrappers=()):
    return SchemaTypeReference(name, name, kind, wrappers)


def schema(mutation_required=False):
    query_field = SchemaField("hero.query.lookup.value", ("value",), type_ref(), False, "", operation_kind="QUERY", source_fingerprint=FINGERPRINT)
    mutation_type = type_ref("String", "SCALAR", ("NON_NULL",)) if mutation_required else type_ref()
    mutation_field = SchemaField("hero.mutation.update.input.value", ("value",), mutation_type, False, "", operation_kind="MUTATION", source_fingerprint=FINGERPRINT)
    return SchemaRegistry("snapshot", FINGERPRINT, "source", (), (SchemaOperation("QUERY", "lookup", "Query", type_ref("Result", "OBJECT"), (), (query_field,), (), False, False, FINGERPRINT),), (SchemaOperation("MUTATION", "update", "Mutation", type_ref("Result", "OBJECT"), (), (), (mutation_field,), False, False, FINGERPRINT),))


def fields(apps=("example",), include=True, protected=False, required=False):
    reference = FrappeFieldReference("example", "Sample", "value")
    field = FrappeFieldDescriptor(reference, "Example", "Value", FrappeFieldTypeDescriptor("Data", "TEXT", True, True), required, False, False, 0, FieldProvenance.STANDARD, False, None, None, None, (), None, False, False, protected, not protected)
    doc = FrappeDocTypeDescriptor("example", "Sample", "Example", False, False, False, False, OwnershipState.RESOLVED)
    return FrappeFieldRegistry("test.local", apps, (doc,), (field,) if include else ())


def definition(direction=MappingDirection.HERO_TO_FRAPPE, reference=None, target="example.Sample.value", source="source", semantics=None, graphql_type=None):
    reference = reference or ("hero.query.lookup.value" if direction == MappingDirection.HERO_TO_FRAPPE else "hero.mutation.update.input.value")
    return MappingDefinition("definition-1", source, direction, GraphQLReference.parse(reference), graphql_type or type_ref(), FrappeFieldReference.parse(target), "Data", TransformPolicy.IDENTITY, semantics or MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.EXPLICIT_NULL), "snapshot", FINGERPRINT, 1)


class TestMappingRegistryValidation(unittest.TestCase):
    def test_valid_query_to_frappe_is_ready(self):
        result = validate_mapping_definition(definition(), schema(), fields())
        self.assertEqual(result.availability, MappingAvailability.READY)

    def test_valid_frappe_to_mutation_is_ready(self):
        result = validate_mapping_definition(definition(MappingDirection.FRAPPE_TO_HERO), schema(), fields())
        self.assertEqual(result.availability, MappingAvailability.READY)

    def test_missing_app_is_retained_waiting(self):
        result = validate_mapping_definition(definition(target="missing.Sample.value"), schema(), fields())
        self.assertEqual(result.availability, MappingAvailability.WAITING_APP)

    def test_missing_field_requires_provisioning(self):
        result = validate_mapping_definition(definition(), schema(), fields(include=False))
        self.assertEqual(result.availability, MappingAvailability.PROVISIONING_REQUIRED)

    def test_direction_and_reference_cannot_be_ambiguous(self):
        with self.assertRaises(MappingValidationError):
            validate_mapping_definition(definition(reference="hero.mutation.update.input.value"), schema(), fields())

    def test_snapshot_mismatch_fails_closed(self):
        with self.assertRaises(MappingValidationError):
            validate_mapping_definition(definition(source="other"), schema(), fields())

    def test_duplicate_and_conflicting_definitions_fail_closed(self):
        item = definition()
        with self.assertRaises(MappingValidationError):
            validate_complete_mapping_set((item, item), schema(), fields())
        second = MappingDefinition("definition-2", item.source_instance, item.direction, item.graphql, item.graphql_type, item.frappe, item.frappe_field_type, item.transform, item.missing, item.snapshot_name, item.schema_fingerprint, item.mapping_revision)
        with self.assertRaises(MappingValidationError):
            validate_complete_mapping_set((item, second), schema(), fields())

    def test_payload_round_trip_rejects_unknown_fields(self):
        item = definition()
        self.assertEqual(MappingDefinition.from_payload(item.to_payload()), item)
        payload = item.to_payload()
        payload["unexpected"] = True
        with self.assertRaises(Exception):
            MappingDefinition.from_payload(payload)

    def test_required_target_requires_error_for_missing_and_null(self):
        with self.assertRaises(MappingValidationError):
            validate_mapping_definition(definition(), schema(), fields(required=True))
        strict = definition(semantics=MissingSemantics(MissingValuePolicy.ERROR, MissingValuePolicy.ERROR))
        self.assertEqual(validate_mapping_definition(strict, schema(), fields(required=True)).availability, MappingAvailability.READY)

    def test_non_null_mutation_input_requires_error_for_missing_and_null(self):
        non_null = type_ref("String", "SCALAR", ("NON_NULL",))
        with self.assertRaises(MappingValidationError):
            validate_mapping_definition(definition(MappingDirection.FRAPPE_TO_HERO, graphql_type=non_null), schema(mutation_required=True), fields())
        strict = definition(MappingDirection.FRAPPE_TO_HERO, graphql_type=non_null, semantics=MissingSemantics(MissingValuePolicy.ERROR, MissingValuePolicy.ERROR))
        self.assertEqual(validate_mapping_definition(strict, schema(mutation_required=True), fields()).availability, MappingAvailability.READY)

    def test_protected_target_is_rejected(self):
        with self.assertRaises(MappingValidationError):
            validate_mapping_definition(definition(), schema(), fields(protected=True))
