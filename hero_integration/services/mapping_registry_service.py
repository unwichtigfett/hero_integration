"""Backend-owned replace-only Mapping Registry v3 operations.

No whitelisted endpoints, schema provisioning or HERO execution. Validation
builds immutable versions; isolated tests do not prove Frappe transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Literal

from hero_integration.services.frappe_field_registry import FrappeFieldRegistry
from hero_integration.services.mapping_registry_json import (
    MappingRegistryJsonError, parse_mapping_registry_v3, serialize_mapping_registry_v3,
)
from hero_integration.services.mapping_registry_model import (
    FORMAT_ID, MappingAvailability, MappingDefinition, MappingSetCandidate, MappingValidation,
)
from hero_integration.services.mapping_registry_storage import MappingRegistryStorage, StoredMappingVersion
from hero_integration.services.mapping_registry_validation import MappingValidationError, validate_complete_mapping_set
from hero_integration.services.schema_registry import SchemaRegistry


IMPORT_REPLACEMENT_WARNING = (
    "Warning: Import replaces the entire mapping configuration. Mappings are never merged automatically. "
    "To combine mappings, merge the JSON files manually before importing. Export and back up the current mapping configuration first."
)


class MappingRegistryOperationError(ValueError):
    """A registry operation cannot safely replace the current complete version."""


@dataclass(frozen=True, slots=True)
class MappingRegistryOperationResult:
    status: Literal["ACTIVATED", "PENDING_PROVISIONING"]
    revision: int | None
    validations: tuple[MappingValidation, ...]
    warning: str = IMPORT_REPLACEMENT_WARNING


class MappingRegistryService:
    """Typed configuration service with no merge, HTTP, or execution capability."""

    def __init__(self, storage: MappingRegistryStorage, schema: SchemaRegistry, fields: FrappeFieldRegistry) -> None:
        self._storage = storage
        self._schema = schema
        self._fields = fields

    def import_complete_json(self, raw: str | bytes, *, expected_revision: int | None) -> MappingRegistryOperationResult:
        """Validate a complete JSON candidate then atomically replace this site's active set."""
        try:
            incoming = parse_mapping_registry_v3(raw)
        except MappingRegistryJsonError as error:
            raise MappingRegistryOperationError(str(error)) from error
        self._assert_site_scope(incoming.site_scope)
        self._assert_provenance(incoming.provenance)
        candidate = MappingSetCandidate(
            incoming.site_scope,
            0 if expected_revision is None else expected_revision,
            incoming.definitions,
            self._schema.snapshot_name,
            self._schema.fingerprint,
        )
        return self._replace(candidate, expected_revision)

    def export_active_json(self, site_scope: str) -> str:
        """Serialize exactly one active revision; export cannot mutate registry state."""
        self._assert_site_scope(site_scope)
        active = self._storage.read_active(site_scope)
        if active is None:
            return serialize_mapping_registry_v3(
                site_scope=site_scope, registry_revision=0, provenance=self._provenance(), definitions=(),
            )
        definitions = self._definitions(active)
        self._validate(definitions)
        return serialize_mapping_registry_v3(
            site_scope=site_scope, registry_revision=active.revision,
            provenance=self._provenance(), definitions=definitions,
        )

    def revalidate_active(self, site_scope: str) -> tuple[MappingValidation, ...]:
        """Derive current target availability without persisting or activating anything."""
        self._assert_site_scope(site_scope)
        active = self._storage.read_active(site_scope)
        return () if active is None else self._validate(self._definitions(active))

    def compile_active_query(self, snapshot, *, expected_revision, definition_ids, operation, arguments,
                             identity_reference, alias=None, pagination_argument=None, snapshot_revision=1):
        """Compile a revision-pinned active selection without granting execution authority."""
        from hero_integration.services.materializer_routing import MaterializerRouter
        current = self._require_current(expected_revision)
        selected = self._selected_definitions(current, definition_ids)
        plan = MaterializerRouter().compile_query_mappings(
            snapshot, self._schema, self._fields, selected, operation=operation, arguments=arguments,
            identity_reference=identity_reference, revision=snapshot_revision, alias=alias,
            pagination_argument=pagination_argument,
        )
        return replace(plan, mapping_registry_revision=current.revision)

    def compile_active_mutation(self, snapshot, *, expected_revision, definition_ids, operation, arguments,
                                output_references, alias=None, snapshot_revision=1):
        """Compile revision-pinned mutation inputs without granting execution authority."""
        from hero_integration.services.materializer_routing import MaterializerRouter
        current = self._require_current(expected_revision)
        selected = self._selected_definitions(current, definition_ids)
        plan = MaterializerRouter().compile_mutation_mappings(
            snapshot, self._schema, self._fields, selected, operation=operation, arguments=arguments,
            output_references=output_references, revision=snapshot_revision, alias=alias,
        )
        return replace(plan, mapping_registry_revision=current.revision)

    def _selected_definitions(self, current, definition_ids):
        if (not isinstance(definition_ids, tuple) or not definition_ids
                or any(not isinstance(value, str) or not value for value in definition_ids)
                or len(set(definition_ids)) != len(definition_ids)):
            raise MappingRegistryOperationError('An explicit unique set of mapping definition IDs is required.')
        definitions = {item.definition_id: item for item in self._definitions(current)}
        if set(definition_ids) - set(definitions):
            raise MappingRegistryOperationError('Selected mapping definitions are absent from the active revision.')
        return tuple(definitions[identity] for identity in sorted(definition_ids))

    def add_definition(self, definition: MappingDefinition, *, expected_revision: int) -> MappingRegistryOperationResult:
        current = self._require_current(expected_revision)
        definitions = self._definitions(current)
        if any(item.definition_id == definition.definition_id for item in definitions):
            raise MappingRegistryOperationError("A manual add cannot overwrite an existing definition.")
        return self._replace(self._candidate((*definitions, definition), expected_revision), expected_revision)

    def update_definition(self, definition: MappingDefinition, *, expected_revision: int) -> MappingRegistryOperationResult:
        current = self._require_current(expected_revision)
        definitions = self._definitions(current)
        old = next((item for item in definitions if item.definition_id == definition.definition_id), None)
        if old is None or definition.mapping_revision <= old.mapping_revision:
            raise MappingRegistryOperationError("A manual update requires an existing ID and a higher mapping revision.")
        return self._replace(self._candidate(tuple(definition if item.definition_id == definition.definition_id else item for item in definitions), expected_revision), expected_revision)

    def remove_definition(self, definition_id: str, *, expected_revision: int) -> MappingRegistryOperationResult:
        current = self._require_current(expected_revision)
        definitions = self._definitions(current)
        reduced = tuple(item for item in definitions if item.definition_id != definition_id)
        if len(reduced) == len(definitions):
            raise MappingRegistryOperationError("A manual remove requires an existing definition ID.")
        # This only revises mapping configuration; it does not touch legacy or future bindings.
        return self._replace(self._candidate(reduced, expected_revision), expected_revision)

    def _replace(self, candidate: MappingSetCandidate, expected_revision: int | None) -> MappingRegistryOperationResult:
        validations = self._validate(candidate.definitions)
        if any(item.availability == MappingAvailability.PROVISIONING_REQUIRED for item in validations):
            return MappingRegistryOperationResult("PENDING_PROVISIONING", None, validations)
        payload = {
            "format": FORMAT_ID,
            "schema_fingerprint": self._schema.fingerprint,
            "snapshot_name": self._schema.snapshot_name,
            "definitions": [item.to_payload() for item in sorted(candidate.definitions, key=lambda item: item.definition_id)],
            "availability": {item.definition.definition_id: item.availability.value for item in validations},
        }
        stored = self._storage.replace(candidate.site_scope, expected_revision, payload)
        return MappingRegistryOperationResult("ACTIVATED", stored.revision, validations)

    def _require_current(self, expected_revision: int) -> StoredMappingVersion:
        if type(expected_revision) is not int or expected_revision < 1:
            raise MappingRegistryOperationError("Manual edits require an explicit positive expected revision.")
        current = self._storage.read_active(self._fields.site)
        if current is None or current.revision != expected_revision:
            raise MappingRegistryOperationError("Manual edit revision conflicts with the active mapping version.")
        return current

    def _candidate(self, definitions: tuple[MappingDefinition, ...], revision: int) -> MappingSetCandidate:
        return MappingSetCandidate(self._fields.site, revision, definitions, self._schema.snapshot_name, self._schema.fingerprint)

    def _definitions(self, version: StoredMappingVersion) -> tuple[MappingDefinition, ...]:
        payload = version.payload
        if not isinstance(payload, Mapping) or payload.get("format") != FORMAT_ID or not isinstance(payload.get("definitions"), tuple):
            raise MappingRegistryOperationError("Stored active mapping version is not a valid Registry v3 payload.")
        try:
            return tuple(MappingDefinition.from_payload(_plain(item)) for item in payload["definitions"])
        except Exception as error:
            raise MappingRegistryOperationError("Stored active mapping definitions are invalid.") from error

    def _validate(self, definitions: tuple[MappingDefinition, ...]) -> tuple[MappingValidation, ...]:
        try:
            return validate_complete_mapping_set(definitions, self._schema, self._fields)
        except MappingValidationError as error:
            raise MappingRegistryOperationError(str(error)) from error

    def _assert_site_scope(self, site_scope: str) -> None:
        if site_scope != self._fields.site:
            raise MappingRegistryOperationError("Mapping imports cannot cross site scope.")

    def _assert_provenance(self, provenance: object) -> None:
        if not isinstance(provenance, Mapping) or dict(provenance) != self._provenance():
            raise MappingRegistryOperationError("Mapping import provenance does not match the selected schema catalog.")

    def _provenance(self) -> dict[str, str]:
        return {"source_instance": self._schema.source_instance, "snapshot_name": self._schema.snapshot_name, "schema_fingerprint": self._schema.fingerprint}


def _plain(value: object) -> object:
    """Thaw the isolated storage provider's immutable containers for decoding."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
