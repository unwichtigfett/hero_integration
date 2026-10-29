"""Fail-closed validation of Mapping Registry v3 against B1 and B3 catalogs."""
from __future__ import annotations

from hero_integration.services.frappe_field_registry import FrappeFieldRegistry, ResolutionCode
from hero_integration.services.mapping_registry_model import (
    GraphQLOperationKind,
    MappingAvailability,
    MappingDefinition,
    MappingDirection,
    MappingRegistryError,
    MappingValidation,
    TransformPolicy,
)
from hero_integration.services.schema_registry import SchemaField, SchemaRegistry, SchemaTypeReference


class MappingValidationError(MappingRegistryError):
    """A complete mapping candidate cannot be activated safely."""


def validate_mapping_definition(definition: MappingDefinition, schema: SchemaRegistry, fields: FrappeFieldRegistry) -> MappingValidation:
    """Validate one definition; missing applications remain explicitly disabled."""
    if not isinstance(definition, MappingDefinition) or not isinstance(schema, SchemaRegistry) or not isinstance(fields, FrappeFieldRegistry):
        raise MappingValidationError("Validated B1 and B3 registry contracts are required.")
    if definition.source_instance != schema.source_instance:
        raise MappingValidationError("Mapping source instance does not match the schema registry.")
    if definition.snapshot_name != schema.snapshot_name or definition.schema_fingerprint != schema.fingerprint:
        raise MappingValidationError("Mapping snapshot identity does not match the schema registry.")
    source = _source_field(definition, schema)
    if source.type_ref != definition.graphql_type:
        raise MappingValidationError("Mapping GraphQL type does not match the registered reference.")
    resolution = fields.resolve(definition.frappe_reference)
    if resolution.code == ResolutionCode.MISSING_APP:
        return MappingValidation(definition, MappingAvailability.WAITING_APP, ("Target application is not installed; definition is disabled.",))
    if resolution.code in {ResolutionCode.MISSING_DOCTYPE, ResolutionCode.MISSING_DOCFIELD}:
        detail = resolution.diagnostics[0].message if resolution.diagnostics else resolution.code.value
        return MappingValidation(
            definition,
            MappingAvailability.PROVISIONING_REQUIRED,
            (f"B5 schema provisioning required for {definition.frappe_reference}: {detail}",),
        )
    if not resolution.ok or resolution.field is None:
        detail = resolution.diagnostics[0].message if resolution.diagnostics else resolution.code.value
        raise MappingValidationError(f"Frappe target is not an allowed mapping field: {detail}")
    target = resolution.field
    if definition.frappe_field_type != target.fieldtype.name:
        raise MappingValidationError("Mapping Frappe field type does not match effective metadata.")
    _nullability(definition, source.type_ref, target.required)
    _compatibility(definition, source.type_ref, target.fieldtype.family, target.select_options)
    return MappingValidation(definition, MappingAvailability.READY)


def validate_complete_mapping_set(definitions: tuple[MappingDefinition, ...], schema: SchemaRegistry, fields: FrappeFieldRegistry) -> tuple[MappingValidation, ...]:
    """Validate a whole candidate before an immutable replacement version is built."""
    if not isinstance(definitions, tuple):
        raise MappingValidationError("Complete mapping sets must be immutable tuples.")
    identifiers = [item.definition_id for item in definitions]
    if len(identifiers) != len(set(identifiers)):
        raise MappingValidationError("Duplicate mapping definition IDs are forbidden.")
    ownership: set[tuple[str, str]] = set()
    for item in definitions:
        key = (item.direction.value, item.frappe_reference)
        if key in ownership:
            raise MappingValidationError("Conflicting mappings own the same direction and Frappe field.")
        ownership.add(key)
    return tuple(validate_mapping_definition(item, schema, fields) for item in sorted(definitions, key=lambda item: item.definition_id))


def _source_field(definition: MappingDefinition, schema: SchemaRegistry) -> SchemaField:
    expected = GraphQLOperationKind.QUERY if definition.direction == MappingDirection.HERO_TO_FRAPPE else GraphQLOperationKind.MUTATION
    if definition.graphql.operation_kind != expected:
        raise MappingValidationError("Mapping direction and GraphQL operation kind are ambiguous.")
    operation = schema.operation(expected.value, definition.graphql.operation)
    candidates = operation.output_fields if expected == GraphQLOperationKind.QUERY else operation.input_fields
    result = next((item for item in candidates if item.reference_id == definition.hero_reference), None)
    if result is None:
        raise MappingValidationError("GraphQL reference is unknown or is not valid for this direction.")
    return result


def _compatibility(definition: MappingDefinition, source: SchemaTypeReference, field_family: str, options: tuple[str, ...]) -> None:
    scalar = source.named_type
    list_like = source.list_depth > 0
    if list_like:
        if definition.transform != TransformPolicy.COLLECTION or definition.resolver is None:
            raise MappingValidationError("Collection references require an explicit COLLECTION transform and resolver.")
        return
    if definition.transform == TransformPolicy.COLLECTION:
        raise MappingValidationError("COLLECTION transform requires a GraphQL list source.")
    compatible = (
        (scalar in {"String", "ID"} and field_family == "TEXT" and definition.transform == TransformPolicy.IDENTITY)
        or (scalar in {"Int", "Float"} and field_family == "NUMBER" and definition.transform == TransformPolicy.IDENTITY)
        or (scalar == "Boolean" and field_family == "BOOLEAN" and definition.transform == TransformPolicy.IDENTITY)
        or (scalar in {"Date", "DateTime", "Time"} and field_family == "DATE_TIME" and definition.transform == TransformPolicy.IDENTITY)
        or (scalar == "String" and field_family == "NUMBER" and definition.transform == TransformPolicy.STRING_TO_NUMBER)
        or (scalar in {"Int", "Float"} and field_family == "TEXT" and definition.transform == TransformPolicy.NUMBER_TO_STRING)
        or (source.named_kind == "ENUM" and field_family == "ENUM" and definition.transform == TransformPolicy.ENUM and bool(options))
        or (field_family == "REFERENCE" and definition.transform == TransformPolicy.LINK and definition.resolver is not None)
    )
    if not compatible:
        raise MappingValidationError("GraphQL and Frappe field types require an unsupported or implicit conversion.")


def _nullability(definition: MappingDefinition, source: SchemaTypeReference, target_required: bool) -> None:
    """Require explicit failure semantics where either side cannot accept absence."""
    semantics = definition.missing
    if definition.direction == MappingDirection.HERO_TO_FRAPPE and target_required:
        if semantics.missing.value != "ERROR" or semantics.null.value != "ERROR":
            raise MappingValidationError("Required Frappe targets require ERROR semantics for missing and null source values.")
    if definition.direction == MappingDirection.FRAPPE_TO_HERO and source.required:
        if semantics.missing.value != "ERROR" or semantics.null.value != "ERROR":
            raise MappingValidationError("Non-null mutation inputs cannot omit or send null values.")
