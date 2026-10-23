from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .schema import GraphQLSchema
from .schema_snapshot import schema_fingerprint

from .operation_kind import GraphQLOperationKind
from .source_api_classification import (
    SemanticApiClassification,
    SemanticConfidence,
    SemanticEntityCandidate,
    SemanticEvidence,
    SemanticOperationClassification,
)


SOURCE_PROPOSAL_MODEL_VERSION = 1


class SourceProposalError(ValueError):
    pass


class TechnicalWrapperKind(str, Enum):
    CONNECTION = "connection"
    EDGE = "edge"
    PAGE_INFO = "page_info"
    PAYLOAD = "payload"
    RESULT = "result"
    METADATA = "metadata"
    OTHER = "other"


class SourceOperationRole(str, Enum):
    COLLECTION = "collection"
    LOOKUP = "lookup"
    SEARCH = "search"
    SINGLETON = "singleton"
    VALUE = "value"

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    INSTANCE_ACTION = "instance_action"
    BULK_ACTION = "bulk_action"

    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TechnicalWrapperProposal:
    source_type_node_id: str
    graphql_type_name: str
    kind: TechnicalWrapperKind
    confidence: SemanticConfidence
    member_node_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    evidence: tuple[SemanticEvidence, ...]


@dataclass(frozen=True, slots=True)
class SourceEntityProposal:
    source_type_node_id: str
    source_candidate_id: str
    graphql_type_name: str
    proposed_name: str
    confidence: SemanticConfidence
    member_node_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    operation_node_ids: tuple[str, ...]
    evidence: tuple[SemanticEvidence, ...]

    @property
    def review_required(self) -> bool:
        return self.confidence is SemanticConfidence.AMBIGUOUS


@dataclass(frozen=True, slots=True)
class SourceOperationProposal:
    operation_node_id: str
    protocol_name: str
    operation_kind: GraphQLOperationKind
    role: SourceOperationRole
    target_type_node_id: str | None
    argument_node_ids: tuple[str, ...]
    input_object_type_node_ids: tuple[str, ...]
    result_path_node_ids: tuple[str, ...]
    evidence: tuple[SemanticEvidence, ...]

    @property
    def review_required(self) -> bool:
        return (
            self.role is SourceOperationRole.OTHER
            or self.target_type_node_id is None
        )


@dataclass(frozen=True, slots=True)
class SourceApiProposalModel:
    model_version: int
    schema_fingerprint: str
    entities: tuple[SourceEntityProposal, ...]
    technical_wrappers: tuple[TechnicalWrapperProposal, ...]
    operations: tuple[SourceOperationProposal, ...]

    def entity_for_type(
        self,
        source_type_node_id: str,
    ) -> SourceEntityProposal | None:
        return next(
            (
                item
                for item in self.entities
                if item.source_type_node_id == source_type_node_id
            ),
            None,
        )

    def wrapper_for_type(
        self,
        source_type_node_id: str,
    ) -> TechnicalWrapperProposal | None:
        return next(
            (
                item
                for item in self.technical_wrappers
                if item.source_type_node_id == source_type_node_id
            ),
            None,
        )

    def operations_for_type(
        self,
        source_type_node_id: str,
    ) -> tuple[SourceOperationProposal, ...]:
        return tuple(
            item
            for item in self.operations
            if item.target_type_node_id == source_type_node_id
        )


class SourceApiProposalBuilder:
    """Pure HERO/GraphQL source projection.

    This layer performs no Frappe persistence and owns no Process-Core
    SemanticClass, CanonicalVariable, SourceMapping or Capability records.
    """

    def build(
        self,
        schema: GraphQLSchema,
        classification: SemanticApiClassification,
    ) -> SourceApiProposalModel:
        if not isinstance(schema, GraphQLSchema):
            raise TypeError("schema must be a GraphQLSchema.")

        if not isinstance(
            classification,
            SemanticApiClassification,
        ):
            raise TypeError(
                "classification must be a SemanticApiClassification."
            )

        fingerprint = schema_fingerprint(schema)

        if fingerprint != classification.schema_fingerprint:
            raise SourceProposalError(
                "Classification does not belong to supplied schema."
            )

        wrapper_by_type = {
            entity.type_node_id: kind
            for entity in classification.entities
            if (
                kind := technical_wrapper_kind(entity)
            ) is not None
        }

        operations = tuple(
            sorted(
                (
                    self._operation(
                        schema,
                        operation,
                        classification,
                    )
                    for operation in classification.operations
                ),
                key=lambda item: item.operation_node_id,
            )
        )

        operation_ids_by_type: dict[str, list[str]] = {}

        for operation in operations:
            if operation.target_type_node_id is None:
                continue

            operation_ids_by_type.setdefault(
                operation.target_type_node_id,
                [],
            ).append(
                operation.operation_node_id
            )

        entities = tuple(
            sorted(
                (
                    SourceEntityProposal(
                        source_type_node_id=entity.type_node_id,
                        source_candidate_id=entity.candidate_id,
                        graphql_type_name=entity.graphql_type_name,
                        proposed_name=entity.proposed_name,
                        confidence=entity.confidence,
                        member_node_ids=entity.member_node_ids,
                        relationship_ids=entity.relationship_ids,
                        operation_node_ids=tuple(
                            sorted(
                                operation_ids_by_type.get(
                                    entity.type_node_id,
                                    (),
                                )
                            )
                        ),
                        evidence=entity.evidence,
                    )
                    for entity in classification.entities
                    if entity.type_node_id not in wrapper_by_type
                ),
                key=lambda item: item.source_type_node_id,
            )
        )

        wrappers = tuple(
            sorted(
                (
                    TechnicalWrapperProposal(
                        source_type_node_id=entity.type_node_id,
                        graphql_type_name=entity.graphql_type_name,
                        kind=wrapper_by_type[entity.type_node_id],
                        confidence=entity.confidence,
                        member_node_ids=entity.member_node_ids,
                        relationship_ids=entity.relationship_ids,
                        evidence=entity.evidence,
                    )
                    for entity in classification.entities
                    if entity.type_node_id in wrapper_by_type
                ),
                key=lambda item: item.source_type_node_id,
            )
        )

        if len(entities) + len(wrappers) != len(
            classification.entities
        ):
            raise SourceProposalError(
                "Source entity partition is incomplete."
            )

        entity_ids = {
            item.source_type_node_id
            for item in entities
        }

        wrapper_ids = {
            item.source_type_node_id
            for item in wrappers
        }

        if entity_ids & wrapper_ids:
            raise SourceProposalError(
                "Source type classified as entity and wrapper."
            )

        return SourceApiProposalModel(
            model_version=SOURCE_PROPOSAL_MODEL_VERSION,
            schema_fingerprint=fingerprint,
            entities=entities,
            technical_wrappers=wrappers,
            operations=operations,
        )

    @staticmethod
    def _operation(
        schema: GraphQLSchema,
        operation: SemanticOperationClassification,
        classification: SemanticApiClassification,
    ) -> SourceOperationProposal:
        role = operation_role(
            schema,
            operation,
            classification,
        )

        return SourceOperationProposal(
            operation_node_id=operation.operation_node_id,
            protocol_name=operation.protocol_name,
            operation_kind=operation.kind,
            role=role,
            target_type_node_id=(
                operation.target_entity_type_node_id
            ),
            argument_node_ids=operation.argument_node_ids,
            input_object_type_node_ids=(
                operation.input_object_type_node_ids
            ),
            result_path_node_ids=operation.result_path_node_ids,
            evidence=operation.evidence,
        )


def technical_wrapper_kind(
    entity: SemanticEntityCandidate,
) -> TechnicalWrapperKind | None:
    name = entity.graphql_type_name.casefold()
    evidence = set(entity.evidence)

    if name.endswith("connection"):
        return TechnicalWrapperKind.CONNECTION

    if name.endswith("edge"):
        return TechnicalWrapperKind.EDGE

    if (
        name.endswith("pageinfo")
        or name.endswith("page_info")
    ):
        return TechnicalWrapperKind.PAGE_INFO

    if name.endswith("payload"):
        return TechnicalWrapperKind.PAYLOAD

    if name.endswith("result"):
        return TechnicalWrapperKind.RESULT

    if SemanticEvidence.METADATA_LIKE_NAME in evidence:
        return TechnicalWrapperKind.METADATA

    if SemanticEvidence.STRUCTURAL_WRAPPER_SHAPE in evidence:
        return TechnicalWrapperKind.OTHER

    return None


def operation_role(
    schema: GraphQLSchema,
    operation: SemanticOperationClassification,
    classification: SemanticApiClassification,
) -> SourceOperationRole:
    if operation.kind is GraphQLOperationKind.QUERY:
        return _query_role(
            schema,
            operation,
            classification,
        )

    if operation.kind is GraphQLOperationKind.MUTATION:
        return _mutation_role(operation)

    return SourceOperationRole.OTHER


def _query_role(
    schema: GraphQLSchema,
    operation: SemanticOperationClassification,
    classification: SemanticApiClassification,
) -> SourceOperationRole:
    normalized = operation.protocol_name.casefold()

    if "search" in normalized:
        return SourceOperationRole.SEARCH

    if (
        any(
            item.operation_node_id
            == operation.operation_node_id
            for item in classification.collections
        )
        or operation.result_shape.is_list
    ):
        return SourceOperationRole.COLLECTION

    field = _operation_field(
        schema,
        operation,
    )

    argument_names = tuple(
        item.name.casefold()
        for item in field.arguments
    )

    if len(argument_names) == 1:
        identifier = argument_names[0]

        if (
            identifier in {
                "id",
                "uuid",
                "key",
            }
            or identifier.endswith("_id")
            or identifier.endswith("_uuid")
            or identifier.endswith("_key")
        ):
            return SourceOperationRole.LOOKUP

    if operation.result_shape.named_kind in {
        "SCALAR",
        "ENUM",
    }:
        return SourceOperationRole.VALUE

    if (
        operation.target_entity_type_node_id is not None
        and not field.arguments
    ):
        return SourceOperationRole.SINGLETON

    return SourceOperationRole.OTHER


def _mutation_role(
    operation: SemanticOperationClassification,
) -> SourceOperationRole:
    normalized = _snake_case(
        operation.protocol_name
    )

    tokens = set(
        normalized.split("_")
    )

    if tokens & {
        "bulk",
        "batch",
        "many",
    }:
        return SourceOperationRole.BULK_ACTION

    if normalized.startswith(
        (
            "create_",
            "add_",
            "insert_",
        )
    ):
        return SourceOperationRole.CREATE

    if normalized.startswith(
        (
            "update_",
            "set_",
            "rename_",
            "patch_",
        )
    ):
        return SourceOperationRole.UPDATE

    if normalized.startswith(
        (
            "delete_",
            "remove_",
        )
    ):
        return SourceOperationRole.DELETE

    if operation.target_entity_type_node_id is not None:
        return SourceOperationRole.INSTANCE_ACTION

    return SourceOperationRole.OTHER


def _operation_field(
    schema: GraphQLSchema,
    operation: SemanticOperationClassification,
):
    if operation.kind is GraphQLOperationKind.QUERY:
        root_name = schema.query_type_name

    elif operation.kind is GraphQLOperationKind.MUTATION:
        root_name = schema.mutation_type_name

    else:
        raise SourceProposalError(
            "Only query and mutation operations are proposal-ready."
        )

    if root_name is None:
        raise SourceProposalError(
            "GraphQL root type is missing."
        )

    root = next(
        (
            item
            for item in schema.types
            if item.name == root_name
        ),
        None,
    )

    if root is None:
        raise SourceProposalError(
            f"GraphQL root type does not exist: {root_name}"
        )

    field = next(
        (
            item
            for item in root.fields
            if item.name == operation.protocol_name
        ),
        None,
    )

    if field is None:
        raise SourceProposalError(
            "GraphQL operation field does not exist: "
            f"{operation.protocol_name}"
        )

    return field


def _snake_case(value: str) -> str:
    result: list[str] = []

    for index, char in enumerate(value):
        if (
            char.isupper()
            and index
            and (
                value[index - 1].islower()
                or (
                    index + 1 < len(value)
                    and value[index + 1].islower()
                )
            )
        ):
            result.append("_")

        if char in "- .":
            if result and result[-1] != "_":
                result.append("_")
            continue

        result.append(char.lower())

    return "".join(result).strip("_")


__all__ = [
    "SOURCE_PROPOSAL_MODEL_VERSION",
    "SourceProposalError",
    "TechnicalWrapperKind",
    "SourceOperationRole",
    "TechnicalWrapperProposal",
    "SourceEntityProposal",
    "SourceOperationProposal",
    "SourceApiProposalModel",
    "SourceApiProposalBuilder",
    "technical_wrapper_kind",
    "operation_role",
]
