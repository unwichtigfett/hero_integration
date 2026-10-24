"""Canonical, execution-free model for the HERO/Frappe Mapping Registry v3."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from hero_integration.services.frappe_field_registry import FrappeFieldReference
from hero_integration.services.schema_registry import SchemaTypeReference

FORMAT_ID = "hero-frappe-mapping-v3"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_GRAPHQL_NAME = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*$")


class MappingRegistryError(ValueError):
    """A registry definition is structurally unsafe or ambiguous."""


class MappingDirection(StrEnum):
    HERO_TO_FRAPPE = "HERO_TO_FRAPPE"
    FRAPPE_TO_HERO = "FRAPPE_TO_HERO"


class GraphQLOperationKind(StrEnum):
    QUERY = "QUERY"
    MUTATION = "MUTATION"


class MappingAvailability(StrEnum):
    READY = "READY"
    WAITING_APP = "WAITING_APP"
    PROVISIONING_REQUIRED = "PROVISIONING_REQUIRED"
    INVALID = "INVALID"


class MissingValuePolicy(StrEnum):
    ERROR = "ERROR"
    OMIT = "OMIT"
    EXPLICIT_NULL = "EXPLICIT_NULL"


class TransformPolicy(StrEnum):
    IDENTITY = "IDENTITY"
    STRING_TO_NUMBER = "STRING_TO_NUMBER"
    NUMBER_TO_STRING = "NUMBER_TO_STRING"
    ENUM = "ENUM"
    LINK = "LINK"
    COLLECTION = "COLLECTION"


@dataclass(frozen=True, slots=True)
class MissingSemantics:
    missing: MissingValuePolicy
    null: MissingValuePolicy

    def __post_init__(self) -> None:
        if self.null == MissingValuePolicy.ERROR and self.missing == MissingValuePolicy.EXPLICIT_NULL:
            raise MappingRegistryError("Missing values cannot become null while null is rejected.")

    def to_payload(self) -> dict[str, str]:
        return {"missing": self.missing.value, "null": self.null.value}

    @classmethod
    def from_payload(cls, payload: object) -> "MissingSemantics":
        if not isinstance(payload, dict) or set(payload) != {"missing", "null"}:
            raise MappingRegistryError("Missing semantics must contain exactly missing and null.")
        try:
            return cls(MissingValuePolicy(payload["missing"]), MissingValuePolicy(payload["null"]))
        except (TypeError, ValueError) as error:
            raise MappingRegistryError("Missing semantics is invalid.") from error


@dataclass(frozen=True, slots=True)
class GraphQLReference:
    operation_kind: GraphQLOperationKind
    operation: str
    path: tuple[str, ...]
    argument: str | None = None

    def __post_init__(self) -> None:
        if not _GRAPHQL_NAME.fullmatch(self.operation) or not self.path or any(not _GRAPHQL_NAME.fullmatch(part) for part in self.path):
            raise MappingRegistryError("GraphQL reference has an invalid operation or path.")
        if self.operation_kind == GraphQLOperationKind.QUERY and self.argument is not None:
            raise MappingRegistryError("Query references do not have a mutation argument.")
        if self.operation_kind == GraphQLOperationKind.MUTATION and not _GRAPHQL_NAME.fullmatch(self.argument or ""):
            raise MappingRegistryError("Mutation references require an argument identity.")

    @property
    def identity(self) -> str:
        if self.operation_kind == GraphQLOperationKind.QUERY:
            return "hero.query." + self.operation + "." + ".".join(self.path)
        return "hero.mutation." + self.operation + "." + str(self.argument) + "." + ".".join(self.path)

    @classmethod
    def parse(cls, value: str) -> "GraphQLReference":
        parts = value.split(".") if isinstance(value, str) else []
        if len(parts) >= 4 and parts[:2] == ["hero", "query"]:
            return cls(GraphQLOperationKind.QUERY, parts[2], tuple(parts[3:]))
        if len(parts) >= 5 and parts[:2] == ["hero", "mutation"]:
            return cls(GraphQLOperationKind.MUTATION, parts[2], tuple(parts[4:]), parts[3])
        raise MappingRegistryError("GraphQL reference must use the Mapping Registry v3 public format.")


@dataclass(frozen=True, slots=True)
class ResolverMetadata:
    kind: str
    contract_id: str
    revision: int

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.kind) or not _IDENTIFIER.fullmatch(self.contract_id) or self.revision < 1:
            raise MappingRegistryError("Resolver metadata requires a stable kind, contract id and revision.")

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "contract_id": self.contract_id, "revision": self.revision}

    @classmethod
    def from_payload(cls, payload: object) -> "ResolverMetadata":
        if not isinstance(payload, dict) or set(payload) != {"kind", "contract_id", "revision"}:
            raise MappingRegistryError("Resolver metadata has unexpected fields.")
        return cls(payload["kind"], payload["contract_id"], payload["revision"])


@dataclass(frozen=True, slots=True)
class MappingDefinition:
    definition_id: str
    source_instance: str
    direction: MappingDirection
    graphql: GraphQLReference
    graphql_type: SchemaTypeReference
    frappe: FrappeFieldReference
    frappe_field_type: str
    transform: TransformPolicy
    missing: MissingSemantics
    snapshot_name: str
    schema_fingerprint: str
    mapping_revision: int
    resolver: ResolverMetadata | None = None
    validation_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.definition_id):
            raise MappingRegistryError("definition_id must be a stable non-secret identifier.")
        if not _IDENTIFIER.fullmatch(self.source_instance):
            raise MappingRegistryError("source_instance must be a stable identifier.")
        if not isinstance(self.graphql_type, SchemaTypeReference):
            raise MappingRegistryError("A structured GraphQL type reference is required.")
        if not isinstance(self.frappe, FrappeFieldReference) or not self.frappe_field_type:
            raise MappingRegistryError("A structured Frappe target and field type are required.")
        if not self.snapshot_name or not _FINGERPRINT.fullmatch(self.schema_fingerprint) or self.mapping_revision < 1:
            raise MappingRegistryError("A validated snapshot identity and positive mapping revision are required.")
        if tuple(sorted(self.validation_metadata)) != self.validation_metadata or len(set(self.validation_metadata)) != len(self.validation_metadata):
            raise MappingRegistryError("Validation metadata must be deterministic unique string pairs.")
        if any(not _IDENTIFIER.fullmatch(key) or not isinstance(value, str) for key, value in self.validation_metadata):
            raise MappingRegistryError("Validation metadata is malformed.")

    @property
    def hero_reference(self) -> str:
        return self.graphql.identity

    @property
    def frappe_reference(self) -> str:
        return self.frappe.identity

    def to_payload(self) -> dict[str, object]:
        return {
            "definition_id": self.definition_id, "source_instance": self.source_instance,
            "direction": self.direction.value, "hero_reference": self.hero_reference,
            "graphql_type": {"graphql_type": self.graphql_type.graphql_type, "named_type": self.graphql_type.named_type, "named_kind": self.graphql_type.named_kind, "wrappers": list(self.graphql_type.wrappers)},
            "frappe_reference": self.frappe_reference, "frappe_field_type": self.frappe_field_type,
            "transform": self.transform.value, "missing": self.missing.to_payload(),
            "snapshot_name": self.snapshot_name, "schema_fingerprint": self.schema_fingerprint,
            "mapping_revision": self.mapping_revision, "resolver": None if self.resolver is None else self.resolver.to_payload(),
            "validation_metadata": [[key, value] for key, value in self.validation_metadata],
        }

    @classmethod
    def from_payload(cls, payload: object) -> "MappingDefinition":
        required = {"definition_id", "source_instance", "direction", "hero_reference", "graphql_type", "frappe_reference", "frappe_field_type", "transform", "missing", "snapshot_name", "schema_fingerprint", "mapping_revision", "resolver", "validation_metadata"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise MappingRegistryError("Mapping definition has missing or unexpected fields.")
        graph_type = payload["graphql_type"]
        if not isinstance(graph_type, dict) or set(graph_type) != {"graphql_type", "named_type", "named_kind", "wrappers"} or not isinstance(graph_type["wrappers"], list):
            raise MappingRegistryError("GraphQL type payload is malformed.")
        metadata = payload["validation_metadata"]
        if not isinstance(metadata, list) or any(not isinstance(item, list) or len(item) != 2 or not all(isinstance(value, str) for value in item) for item in metadata):
            raise MappingRegistryError("Validation metadata payload is malformed.")
        try:
            return cls(
                definition_id=payload["definition_id"], source_instance=payload["source_instance"], direction=MappingDirection(payload["direction"]), graphql=GraphQLReference.parse(payload["hero_reference"]),
                graphql_type=SchemaTypeReference(graph_type["graphql_type"], graph_type["named_type"], graph_type["named_kind"], tuple(graph_type["wrappers"])), frappe=FrappeFieldReference.parse(payload["frappe_reference"]), frappe_field_type=payload["frappe_field_type"], transform=TransformPolicy(payload["transform"]), missing=MissingSemantics.from_payload(payload["missing"]), snapshot_name=payload["snapshot_name"], schema_fingerprint=payload["schema_fingerprint"], mapping_revision=payload["mapping_revision"], resolver=None if payload["resolver"] is None else ResolverMetadata.from_payload(payload["resolver"]), validation_metadata=tuple((item[0], item[1]) for item in metadata),
            )
        except (TypeError, ValueError) as error:
            raise MappingRegistryError("Mapping definition payload is invalid.") from error


@dataclass(frozen=True, slots=True)
class MappingSetCandidate:
    site_scope: str
    expected_revision: int
    definitions: tuple[MappingDefinition, ...]
    snapshot_name: str
    schema_fingerprint: str

    def __post_init__(self) -> None:
        if not self.site_scope or self.expected_revision < 0 or not self.snapshot_name or not _FINGERPRINT.fullmatch(self.schema_fingerprint):
            raise MappingRegistryError("Mapping candidate provenance is invalid.")


@dataclass(frozen=True, slots=True)
class MappingValidation:
    definition: MappingDefinition
    availability: MappingAvailability
    diagnostics: tuple[str, ...] = ()

    @property
    def active_candidate(self) -> bool:
        return self.availability in {MappingAvailability.READY, MappingAvailability.WAITING_APP}
