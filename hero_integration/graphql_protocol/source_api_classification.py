from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib

from .graphql_api_graph import (
    CanonicalApiGraph,
    project_schema_to_api_graph,
)
from .schema import (
    GraphQLField,
    GraphQLSchema,
    GraphQLType,
    GraphQLTypeRef,
)
from .schema_snapshot import schema_fingerprint
from .graphql_semantic_structure import resolve_structural_target
from .operation_kind import GraphQLOperationKind


SEMANTIC_API_CLASSIFIER_VERSION = 1


class SemanticApiClassificationError(ValueError):
    pass


class SemanticConfidence(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    AMBIGUOUS = "ambiguous"


class SemanticEvidence(str, Enum):
    ROOT_QUERY_RETURNS_OBJECT_LIST = "root_query_returns_object_list"
    ROOT_QUERY_RETURNS_OBJECT = "root_query_returns_object"
    ROOT_QUERY_RETURNS_WRAPPER_COLLECTION = (
        "root_query_returns_wrapper_collection"
    )
    ROOT_MUTATION_RETURNS_OBJECT = "root_mutation_returns_object"
    HAS_IDENTIFIER_FIELD = "has_identifier_field"
    HAS_MULTIPLE_BUSINESS_FIELDS = "has_multiple_business_fields"
    PARTICIPATES_IN_COLLECTION = "participates_in_collection"
    REUSED_ACROSS_OPERATIONS = "reused_across_operations"
    STRUCTURAL_WRAPPER_SHAPE = "structural_wrapper_shape"
    METADATA_LIKE_NAME = "metadata_like_name"
    PAYLOAD_LIKE_NAME = "payload_like_name"
    NESTED_OBJECT_FIELD = "nested_object_field"
    NESTED_COLLECTION_FIELD = "nested_collection_field"
    REFERENCE_IDENTIFIER_PAIR = "reference_identifier_pair"
    SCALAR_FIELD = "scalar_field"
    ENUM_FIELD = "enum_field"
    OBJECT_FIELD = "object_field"
    LIST_FIELD = "list_field"
    QUERY_READ_CAPABILITY = "query_read_capability"
    MUTATION_WRITE_CAPABILITY = "mutation_write_capability"
    HAS_ARGUMENT = "has_argument"
    INPUT_OBJECT_ARGUMENT = "input_object_argument"
    INPUT_FIELD = "input_field"
    NESTED_INPUT_OBJECT = "nested_input_object"
    REQUIRED_VALUE = "required_value"
    OPTIONAL_VALUE = "optional_value"


class SemanticFieldRole(str, Enum):
    SCALAR_MEMBER = "scalar_member"
    ENUM_MEMBER = "enum_member"
    OBJECT_MEMBER = "object_member"
    COLLECTION_MEMBER = "collection_member"
    UNKNOWN_MEMBER = "unknown_member"


class SemanticRelationshipKind(str, Enum):
    NESTED_OBJECT = "nested_object"
    COLLECTION = "collection"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class GraphQLTypeShape:
    rendered: str
    named_type: str
    named_kind: str
    wrapper_kinds: tuple[str, ...]
    nullable: bool
    list_depth: int
    element_nullable: bool | None

    @property
    def is_list(self) -> bool:
        return self.list_depth > 0


@dataclass(frozen=True, slots=True)
class SemanticFieldClassification:
    node_id: str
    owner_type_node_id: str
    name: str
    role: SemanticFieldRole
    shape: GraphQLTypeShape
    evidence: tuple[SemanticEvidence, ...]


@dataclass(frozen=True, slots=True)
class SemanticRelationshipClassification:
    relationship_id: str
    source_type_node_id: str
    field_node_id: str
    target_type_node_id: str
    kind: SemanticRelationshipKind
    confidence: SemanticConfidence
    evidence: tuple[SemanticEvidence, ...]


@dataclass(frozen=True, slots=True)
class SemanticEntityCandidate:
    candidate_id: str
    type_node_id: str
    graphql_type_name: str
    proposed_name: str
    confidence: SemanticConfidence
    evidence: tuple[SemanticEvidence, ...]
    source_operation_node_ids: tuple[str, ...]
    member_node_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticCollectionCandidate:
    candidate_id: str
    operation_node_id: str
    entity_type_node_id: str
    result_path_node_ids: tuple[str, ...]
    wrapper_type_node_id: str | None
    confidence: SemanticConfidence
    evidence: tuple[SemanticEvidence, ...]


@dataclass(frozen=True, slots=True)
class SemanticInputValueClassification:
    node_id: str
    operation_node_id: str | None
    parent_input_type_node_id: str | None
    path_node_ids: tuple[str, ...]
    name: str
    shape: GraphQLTypeShape
    required: bool
    evidence: tuple[SemanticEvidence, ...]


@dataclass(frozen=True, slots=True)
class SemanticInputObjectClassification:
    type_node_id: str
    graphql_type_name: str
    field_node_ids: tuple[str, ...]
    nested_input_type_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticOperationClassification:
    operation_node_id: str
    protocol_name: str
    kind: GraphQLOperationKind
    result_shape: GraphQLTypeShape
    target_entity_type_node_id: str | None
    result_path_node_ids: tuple[str, ...]
    argument_node_ids: tuple[str, ...]
    input_object_type_node_ids: tuple[str, ...]
    evidence: tuple[SemanticEvidence, ...]


@dataclass(frozen=True, slots=True)
class SemanticApiClassification:
    classifier_version: int
    schema_fingerprint: str
    entities: tuple[SemanticEntityCandidate, ...]
    collections: tuple[SemanticCollectionCandidate, ...]
    fields: tuple[SemanticFieldClassification, ...]
    relationships: tuple[SemanticRelationshipClassification, ...]
    operations: tuple[SemanticOperationClassification, ...]
    input_objects: tuple[SemanticInputObjectClassification, ...]
    input_values: tuple[SemanticInputValueClassification, ...]

    def entity_for_type(
        self,
        type_node_id: str,
    ) -> SemanticEntityCandidate | None:
        return next(
            (
                item
                for item in self.entities
                if item.type_node_id == type_node_id
            ),
            None,
        )


class SemanticApiClassifier:
    """Pure product-level projection of one canonical GraphQL API graph."""

    def classify(
        self,
        schema: GraphQLSchema,
        graph: CanonicalApiGraph,
    ) -> SemanticApiClassification:
        if not isinstance(schema, GraphQLSchema):
            raise TypeError("schema must be a GraphQLSchema.")
        if not isinstance(graph, CanonicalApiGraph):
            raise TypeError("graph must be a CanonicalApiGraph.")
        if graph != project_schema_to_api_graph(schema):
            raise SemanticApiClassificationError(
                "CanonicalApiGraph does not match the supplied GraphQLSchema."
            )

        fingerprint = schema_fingerprint(schema)
        type_index = {item.name: item for item in schema.types}
        root_names = {
            schema.query_type_name,
            schema.mutation_type_name,
            schema.subscription_type_name,
        }
        object_types = tuple(
            sorted(
                (
                    item
                    for item in schema.types
                    if item.kind == "OBJECT" and item.name not in root_names and not item.name.startswith("__")
                ),
                key=lambda item: item.name,
            )
        )
        field_owner_types = tuple(
            sorted(
                (
                    item
                    for item in schema.types
                    if item.name not in root_names and not item.name.startswith("__") and item.fields
                ),
                key=lambda item: item.name,
            )
        )

        fields = self._classify_fields(field_owner_types, type_index)
        field_by_node = {item.node_id: item for item in fields}
        operation_seeds = self._operation_seeds(schema, graph, type_index)
        collection_seeds = tuple(
            item
            for item in operation_seeds
            if item.collection_entity_type_name is not None
        )
        entity_facts = self._entity_facts(
            object_types,
            operation_seeds,
            collection_seeds,
            fields,
        )
        entity_confidence = self._entity_confidence(
            object_types,
            entity_facts,
            operation_seeds,
        )
        relationships = self._classify_relationships(
            object_types,
            type_index,
            entity_confidence,
            fingerprint,
        )
        relationship_by_source: dict[str, list[str]] = {}
        for relationship in relationships:
            relationship_by_source.setdefault(
                relationship.source_type_node_id,
                [],
            ).append(relationship.relationship_id)

        operation_sources: dict[str, list[str]] = {}
        for seed in operation_seeds:
            target = (
                seed.collection_entity_type_name
                or seed.direct_entity_type_name
            )
            if target is not None:
                operation_sources.setdefault(target, []).append(
                    seed.operation_node_id
                )

        entities = tuple(
            SemanticEntityCandidate(
                candidate_id=_stable_id(
                    "entity",
                    fingerprint,
                    f"type:{graphql_type.name}",
                ),
                type_node_id=f"type:{graphql_type.name}",
                graphql_type_name=graphql_type.name,
                proposed_name=_snake_case(graphql_type.name),
                confidence=entity_confidence[graphql_type.name],
                evidence=_evidence(entity_facts[graphql_type.name]),
                source_operation_node_ids=tuple(
                    sorted(operation_sources.get(graphql_type.name, ()))
                ),
                member_node_ids=tuple(
                    item.node_id
                    for item in fields
                    if item.owner_type_node_id
                    == f"type:{graphql_type.name}"
                ),
                relationship_ids=tuple(
                    sorted(
                        relationship_by_source.get(
                            f"type:{graphql_type.name}",
                            (),
                        )
                    )
                ),
            )
            for graphql_type in object_types
        )

        collections = tuple(
            SemanticCollectionCandidate(
                candidate_id=_stable_id(
                    "collection",
                    fingerprint,
                    seed.operation_node_id,
                    *seed.result_path_node_ids,
                ),
                operation_node_id=seed.operation_node_id,
                entity_type_node_id=(
                    f"type:{seed.collection_entity_type_name}"
                ),
                result_path_node_ids=seed.result_path_node_ids,
                wrapper_type_node_id=(
                    f"type:{seed.direct_entity_type_name}"
                    if seed.result_path_node_ids
                    else None
                ),
                confidence=SemanticConfidence.STRONG,
                evidence=_evidence(seed.collection_evidence),
            )
            for seed in collection_seeds
        )

        input_objects, input_values = self._classify_inputs(
            schema,
            type_index,
        )
        values_by_operation: dict[
            str, list[SemanticInputValueClassification]
        ] = {}
        for value in input_values:
            if value.operation_node_id is not None:
                values_by_operation.setdefault(
                    value.operation_node_id,
                    [],
                ).append(value)

        operations = tuple(
            self._operation_classification(
                seed,
                values_by_operation,
            )
            for seed in operation_seeds
        )

        # Ensure every graph field referenced by a candidate was classified.
        if any(
            node_id not in field_by_node
            for entity in entities
            for node_id in entity.member_node_ids
        ):
            raise AssertionError("Semantic field projection is incomplete.")

        return SemanticApiClassification(
            classifier_version=SEMANTIC_API_CLASSIFIER_VERSION,
            schema_fingerprint=fingerprint,
            entities=entities,
            collections=collections,
            fields=fields,
            relationships=relationships,
            operations=operations,
            input_objects=input_objects,
            input_values=input_values,
        )

    @staticmethod
    def _classify_fields(
        object_types: tuple[GraphQLType, ...],
        type_index: dict[str, GraphQLType],
    ) -> tuple[SemanticFieldClassification, ...]:
        result: list[SemanticFieldClassification] = []
        for owner in object_types:
            for field in sorted(owner.fields, key=lambda item: item.name):
                shape = _shape(field.type_ref, type_index)
                role, evidence = _field_role(shape)
                result.append(
                    SemanticFieldClassification(
                        node_id=f"field:{owner.name}.{field.name}",
                        owner_type_node_id=f"type:{owner.name}",
                        name=field.name,
                        role=role,
                        shape=shape,
                        evidence=_evidence(evidence),
                    )
                )
        return tuple(sorted(result, key=lambda item: item.node_id))

    def _operation_seeds(
        self,
        schema: GraphQLSchema,
        graph: CanonicalApiGraph,
        type_index: dict[str, GraphQLType],
    ) -> tuple[_OperationSeed, ...]:
        result: list[_OperationSeed] = []
        roots = (
            ("query", schema.query_fields),
            ("mutation", schema.mutation_fields),
        )
        for root_kind, fields in roots:
            for field in sorted(fields, key=lambda item: item.name):
                operation_node_id = (
                    f"operation:{root_kind}:{field.name}"
                )
                if graph.node(operation_node_id) is None:
                    raise SemanticApiClassificationError(
                        "Canonical operation node is missing: "
                        f"{operation_node_id}."
                    )
                shape = _shape(field.type_ref, type_index)
                direct_entity = (
                    shape.named_type
                    if shape.named_kind == "OBJECT"
                    else None
                )
                collection_entity: str | None = None
                result_path: tuple[str, ...] = ()
                collection_evidence: set[SemanticEvidence] = set()

                if shape.is_list and direct_entity is not None:
                    collection_entity = direct_entity
                    collection_evidence.update(
                        {
                            SemanticEvidence.ROOT_QUERY_RETURNS_OBJECT_LIST,
                            SemanticEvidence.PARTICIPATES_IN_COLLECTION,
                        }
                    )
                elif (
                    root_kind == "query"
                    and direct_entity is not None
                ):
                    wrapper_target = resolve_structural_target(
                        schema,
                        direct_entity,
                        require_collection=True,
                    )
                    if wrapper_target is not None:
                        collection_entity = wrapper_target.target_type_name
                        result_path = wrapper_target.field_node_ids
                        collection_evidence.update(
                            {
                                SemanticEvidence.ROOT_QUERY_RETURNS_WRAPPER_COLLECTION,
                                SemanticEvidence.PARTICIPATES_IN_COLLECTION,
                            }
                        )

                result.append(
                    _OperationSeed(
                        operation_node_id=operation_node_id,
                        protocol_name=field.name,
                        root_kind=root_kind,
                        field=field,
                        result_shape=shape,
                        direct_entity_type_name=direct_entity,
                        collection_entity_type_name=collection_entity,
                        result_path_node_ids=result_path,
                        collection_evidence=collection_evidence,
                    )
                )
        return tuple(
            sorted(result, key=lambda item: item.operation_node_id)
        )

    @staticmethod
    def _entity_facts(
        object_types: tuple[GraphQLType, ...],
        operation_seeds: tuple[_OperationSeed, ...],
        collection_seeds: tuple[_OperationSeed, ...],
        fields: tuple[SemanticFieldClassification, ...],
    ) -> dict[str, set[SemanticEvidence]]:
        facts = {item.name: set() for item in object_types}
        operations_by_type: dict[str, set[str]] = {}

        for graphql_type in object_types:
            if _identifier_fields(graphql_type):
                facts[graphql_type.name].add(
                    SemanticEvidence.HAS_IDENTIFIER_FIELD
                )
            business_fields = [
                item
                for item in fields
                if item.owner_type_node_id == f"type:{graphql_type.name}"
                and item.role
                in {
                    SemanticFieldRole.SCALAR_MEMBER,
                    SemanticFieldRole.ENUM_MEMBER,
                }
                and item.name.casefold() not in {"id", "uuid", "key"}
            ]
            if len(business_fields) >= 2:
                facts[graphql_type.name].add(
                    SemanticEvidence.HAS_MULTIPLE_BUSINESS_FIELDS
                )
            normalized = _snake_case(graphql_type.name)
            if normalized in {
                "metadata",
                "meta_data",
                "page_info",
                "pagination",
            }:
                facts[graphql_type.name].add(
                    SemanticEvidence.METADATA_LIKE_NAME
                )
            if normalized.endswith("_payload"):
                facts[graphql_type.name].add(
                    SemanticEvidence.PAYLOAD_LIKE_NAME
                )

        for seed in operation_seeds:
            target = seed.direct_entity_type_name
            if target is None or target not in facts:
                continue
            operations_by_type.setdefault(target, set()).add(
                seed.operation_node_id
            )
            if seed.root_kind == "query":
                facts[target].add(
                    SemanticEvidence.ROOT_QUERY_RETURNS_OBJECT
                )
                if seed.result_path_node_ids:
                    facts[target].add(
                        SemanticEvidence.STRUCTURAL_WRAPPER_SHAPE
                    )
            else:
                facts[target].add(
                    SemanticEvidence.ROOT_MUTATION_RETURNS_OBJECT
                )

        for seed in collection_seeds:
            target = seed.collection_entity_type_name
            if target is not None and target in facts:
                facts[target].update(seed.collection_evidence)
                operations_by_type.setdefault(target, set()).add(
                    seed.operation_node_id
                )

        for type_name, operation_ids in operations_by_type.items():
            if len(operation_ids) > 1:
                facts[type_name].add(
                    SemanticEvidence.REUSED_ACROSS_OPERATIONS
                )
        return facts

    @staticmethod
    def _entity_confidence(
        object_types: tuple[GraphQLType, ...],
        facts: dict[str, set[SemanticEvidence]],
        operation_seeds: tuple[_OperationSeed, ...],
    ) -> dict[str, SemanticConfidence]:
        result: dict[str, SemanticConfidence] = {}
        nested_targets: set[str] = set()
        for owner in object_types:
            for field in owner.fields:
                named = field.type_ref.named_type()
                if named in facts:
                    nested_targets.add(str(named))

        for graphql_type in object_types:
            evidence = facts[graphql_type.name]
            if (
                SemanticEvidence.STRUCTURAL_WRAPPER_SHAPE in evidence
                or SemanticEvidence.METADATA_LIKE_NAME in evidence
                or SemanticEvidence.PAYLOAD_LIKE_NAME in evidence
            ):
                result[graphql_type.name] = SemanticConfidence.AMBIGUOUS
            elif (
                SemanticEvidence.PARTICIPATES_IN_COLLECTION in evidence
            ):
                result[graphql_type.name] = SemanticConfidence.STRONG
            elif (
                SemanticEvidence.ROOT_QUERY_RETURNS_OBJECT in evidence
                and SemanticEvidence.HAS_IDENTIFIER_FIELD in evidence
            ):
                result[graphql_type.name] = SemanticConfidence.STRONG
            elif (
                SemanticEvidence.ROOT_QUERY_RETURNS_OBJECT in evidence
                and SemanticEvidence.HAS_MULTIPLE_BUSINESS_FIELDS in evidence
            ):
                result[graphql_type.name] = SemanticConfidence.MODERATE
            elif (
                graphql_type.name in nested_targets
                and SemanticEvidence.HAS_MULTIPLE_BUSINESS_FIELDS in evidence
            ):
                result[graphql_type.name] = SemanticConfidence.MODERATE
            else:
                result[graphql_type.name] = SemanticConfidence.AMBIGUOUS
        return result

    @staticmethod
    def _classify_relationships(
        object_types: tuple[GraphQLType, ...],
        type_index: dict[str, GraphQLType],
        entity_confidence: dict[str, SemanticConfidence],
        fingerprint: str,
    ) -> tuple[SemanticRelationshipClassification, ...]:
        result: list[SemanticRelationshipClassification] = []
        for owner in object_types:
            owner_identifiers = {
                _snake_case(item.name): item
                for item in owner.fields
                if _is_identifier_field(item)
            }
            for field in sorted(owner.fields, key=lambda item: item.name):
                shape = _shape(field.type_ref, type_index)
                if shape.named_kind not in {
                    "OBJECT",
                    "INTERFACE",
                    "UNION",
                }:
                    continue
                target_name = shape.named_type
                target_node_id = f"type:{target_name}"
                evidence: set[SemanticEvidence] = set()
                kind = SemanticRelationshipKind.NESTED_OBJECT
                if shape.is_list:
                    kind = SemanticRelationshipKind.COLLECTION
                    evidence.add(
                        SemanticEvidence.NESTED_COLLECTION_FIELD
                    )
                else:
                    evidence.add(SemanticEvidence.NESTED_OBJECT_FIELD)
                    reference_names = {
                        f"{_snake_case(field.name)}_id",
                        f"{_snake_case(target_name)}_id",
                    }
                    if reference_names & owner_identifiers.keys():
                        kind = SemanticRelationshipKind.REFERENCE
                        evidence.add(
                            SemanticEvidence.REFERENCE_IDENTIFIER_PAIR
                        )
                source_confidence = entity_confidence.get(
                    owner.name,
                    SemanticConfidence.AMBIGUOUS,
                )
                target_confidence = entity_confidence.get(
                    target_name,
                    SemanticConfidence.AMBIGUOUS,
                )
                confidence = (
                    SemanticConfidence.STRONG
                    if source_confidence is SemanticConfidence.STRONG
                    and target_confidence
                    is not SemanticConfidence.AMBIGUOUS
                    else SemanticConfidence.MODERATE
                    if source_confidence
                    is not SemanticConfidence.AMBIGUOUS
                    else SemanticConfidence.AMBIGUOUS
                )
                field_node_id = f"field:{owner.name}.{field.name}"
                result.append(
                    SemanticRelationshipClassification(
                        relationship_id=_stable_id(
                            "relationship",
                            fingerprint,
                            f"type:{owner.name}",
                            field_node_id,
                            target_node_id,
                            kind.value,
                        ),
                        source_type_node_id=f"type:{owner.name}",
                        field_node_id=field_node_id,
                        target_type_node_id=target_node_id,
                        kind=kind,
                        confidence=confidence,
                        evidence=_evidence(evidence),
                    )
                )
        return tuple(sorted(result, key=lambda item: item.relationship_id))

    @staticmethod
    def _classify_inputs(
        schema: GraphQLSchema,
        type_index: dict[str, GraphQLType],
    ) -> tuple[
        tuple[SemanticInputObjectClassification, ...],
        tuple[SemanticInputValueClassification, ...],
    ]:
        input_objects: list[SemanticInputObjectClassification] = []
        for graphql_type in sorted(
            (
                item
                for item in schema.types
                if item.kind == "INPUT_OBJECT"
            ),
            key=lambda item: item.name,
        ):
            nested = {
                field.type_ref.named_type()
                for field in graphql_type.input_fields
                if (
                    field.type_ref.named_type() in type_index
                    and type_index[str(field.type_ref.named_type())].kind
                    == "INPUT_OBJECT"
                )
            }
            input_objects.append(
                SemanticInputObjectClassification(
                    type_node_id=f"type:{graphql_type.name}",
                    graphql_type_name=graphql_type.name,
                    field_node_ids=tuple(
                        sorted(
                            f"input-field:{graphql_type.name}.{item.name}"
                            for item in graphql_type.input_fields
                        )
                    ),
                    nested_input_type_node_ids=tuple(
                        sorted(f"type:{item}" for item in nested if item)
                    ),
                )
            )

        values: list[SemanticInputValueClassification] = []
        for root_kind, fields in (
            ("query", schema.query_fields),
            ("mutation", schema.mutation_fields),
        ):
            for field in sorted(fields, key=lambda item: item.name):
                operation_node_id = (
                    f"operation:{root_kind}:{field.name}"
                )
                for argument in sorted(
                    field.arguments,
                    key=lambda item: item.name,
                ):
                    argument_id = (
                        f"argument:{root_kind}:{field.name}:{argument.name}"
                    )
                    SemanticApiClassifier._append_input_path(
                        values,
                        argument,
                        node_id=argument_id,
                        operation_node_id=operation_node_id,
                        parent_input_type_node_id=None,
                        path=(argument_id,),
                        type_index=type_index,
                        visited=(),
                    )
        return (
            tuple(input_objects),
            tuple(
                sorted(
                    values,
                    key=lambda item: (
                        item.operation_node_id or "",
                        item.path_node_ids,
                    ),
                )
            ),
        )

    @staticmethod
    def _append_input_path(
        result: list[SemanticInputValueClassification],
        value: object,
        *,
        node_id: str,
        operation_node_id: str,
        parent_input_type_node_id: str | None,
        path: tuple[str, ...],
        type_index: dict[str, GraphQLType],
        visited: tuple[str, ...],
    ) -> None:
        type_ref = value.type_ref
        shape = _shape(type_ref, type_index)
        evidence = {
            SemanticEvidence.HAS_ARGUMENT
            if len(path) == 1
            else SemanticEvidence.INPUT_FIELD,
            SemanticEvidence.REQUIRED_VALUE
            if type_ref.kind == "NON_NULL"
            else SemanticEvidence.OPTIONAL_VALUE,
        }
        if shape.named_kind == "INPUT_OBJECT":
            evidence.add(SemanticEvidence.INPUT_OBJECT_ARGUMENT)
            if len(path) > 1:
                evidence.add(SemanticEvidence.NESTED_INPUT_OBJECT)
        elif len(path) > 2:
            evidence.add(SemanticEvidence.NESTED_INPUT_OBJECT)
        result.append(
            SemanticInputValueClassification(
                node_id=node_id,
                operation_node_id=operation_node_id,
                parent_input_type_node_id=parent_input_type_node_id,
                path_node_ids=path,
                name=value.name,
                shape=shape,
                required=type_ref.kind == "NON_NULL",
                evidence=_evidence(evidence),
            )
        )
        if shape.named_kind != "INPUT_OBJECT":
            return
        if shape.named_type in visited:
            return
        input_type = type_index.get(shape.named_type)
        if input_type is None:
            return
        for child in sorted(input_type.input_fields, key=lambda item: item.name):
            child_id = f"input-field:{input_type.name}.{child.name}"
            SemanticApiClassifier._append_input_path(
                result,
                child,
                node_id=child_id,
                operation_node_id=operation_node_id,
                parent_input_type_node_id=f"type:{input_type.name}",
                path=(*path, child_id),
                type_index=type_index,
                visited=(*visited, shape.named_type),
            )

    @staticmethod
    def _operation_classification(
        seed: _OperationSeed,
        values_by_operation: dict[
            str, list[SemanticInputValueClassification]
        ],
    ) -> SemanticOperationClassification:
        if seed.root_kind == "query":
            kind = GraphQLOperationKind.QUERY
        elif seed.root_kind == "mutation":
            kind = GraphQLOperationKind.MUTATION
        else:
            raise SemanticApiClassificationError(
                "Unsupported executable GraphQL root kind: "
                f"{seed.root_kind!r}."
            )
        values = values_by_operation.get(seed.operation_node_id, [])
        arguments = tuple(
            item.node_id for item in values if len(item.path_node_ids) == 1
        )
        input_objects = tuple(
            sorted(
                {
                    f"type:{item.shape.named_type}"
                    for item in values
                    if item.shape.named_kind == "INPUT_OBJECT"
                }
            )
        )
        target = (
            seed.collection_entity_type_name
            or seed.direct_entity_type_name
        )
        evidence = {
            SemanticEvidence.QUERY_READ_CAPABILITY
            if kind is GraphQLOperationKind.QUERY
            else SemanticEvidence.MUTATION_WRITE_CAPABILITY
        }
        if arguments:
            evidence.add(SemanticEvidence.HAS_ARGUMENT)
        return SemanticOperationClassification(
            operation_node_id=seed.operation_node_id,
            protocol_name=seed.protocol_name,
            kind=kind,
            result_shape=seed.result_shape,
            target_entity_type_node_id=(
                f"type:{target}" if target is not None else None
            ),
            result_path_node_ids=seed.result_path_node_ids,
            argument_node_ids=tuple(sorted(arguments)),
            input_object_type_node_ids=input_objects,
            evidence=_evidence(evidence),
        )


@dataclass(frozen=True, slots=True)
class _OperationSeed:
    operation_node_id: str
    protocol_name: str
    root_kind: str
    field: GraphQLField
    result_shape: GraphQLTypeShape
    direct_entity_type_name: str | None
    collection_entity_type_name: str | None
    result_path_node_ids: tuple[str, ...]
    collection_evidence: set[SemanticEvidence]


def _shape(
    type_ref: GraphQLTypeRef,
    type_index: dict[str, GraphQLType],
) -> GraphQLTypeShape:
    wrappers: list[str] = []
    current: GraphQLTypeRef | None = type_ref
    list_depth = 0
    element_nullable: bool | None = None
    while current is not None:
        wrappers.append(current.kind)
        if current.kind == "LIST":
            list_depth += 1
            if element_nullable is None:
                element_nullable = (
                    current.of_type is None
                    or current.of_type.kind != "NON_NULL"
                )
        if current.name:
            named_type = current.name
            break
        current = current.of_type
    else:
        raise SemanticApiClassificationError(
            "GraphQL type reference has no named type."
        )
    known = type_index.get(named_type)
    named_kind = known.kind if known is not None else current.kind
    return GraphQLTypeShape(
        rendered=type_ref.render(),
        named_type=named_type,
        named_kind=named_kind,
        wrapper_kinds=tuple(wrappers),
        nullable=type_ref.kind != "NON_NULL",
        list_depth=list_depth,
        element_nullable=element_nullable,
    )


def _field_role(
    shape: GraphQLTypeShape,
) -> tuple[SemanticFieldRole, set[SemanticEvidence]]:
    if shape.is_list:
        return (
            SemanticFieldRole.COLLECTION_MEMBER,
            {SemanticEvidence.LIST_FIELD},
        )
    if shape.named_kind == "ENUM":
        return (
            SemanticFieldRole.ENUM_MEMBER,
            {SemanticEvidence.ENUM_FIELD},
        )
    if shape.named_kind in {"OBJECT", "INTERFACE", "UNION"}:
        return (
            SemanticFieldRole.OBJECT_MEMBER,
            {SemanticEvidence.OBJECT_FIELD},
        )
    if shape.named_kind == "SCALAR":
        return (
            SemanticFieldRole.SCALAR_MEMBER,
            {SemanticEvidence.SCALAR_FIELD},
        )
    return SemanticFieldRole.UNKNOWN_MEMBER, set()


def _identifier_fields(
    graphql_type: GraphQLType,
) -> tuple[GraphQLField, ...]:
    return tuple(
        item for item in graphql_type.fields if _is_identifier_field(item)
    )


def _is_identifier_field(field: GraphQLField) -> bool:
    named = field.type_ref.named_type()
    normalized = _snake_case(field.name)
    return named == "ID" and (
        normalized in {"id", "uuid", "key"}
        or normalized.endswith("_id")
    )


def _snake_case(value: str) -> str:
    pieces: list[str] = []
    previous_lower_or_digit = False
    for character in value.strip():
        if character.isalnum():
            if character.isupper() and previous_lower_or_digit:
                pieces.append("_")
            pieces.append(character.casefold())
            previous_lower_or_digit = character.islower() or character.isdigit()
        else:
            if pieces and pieces[-1] != "_":
                pieces.append("_")
            previous_lower_or_digit = False
    normalized = "".join(pieces).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized or "semantic_candidate"


def _evidence(
    values: object,
) -> tuple[SemanticEvidence, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))


def _stable_id(kind: str, *parts: str) -> str:
    canonical = "\x1f".join((str(SEMANTIC_API_CLASSIFIER_VERSION), *parts))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"semantic-{kind}-v{SEMANTIC_API_CLASSIFIER_VERSION}-{digest}"


__all__ = [
    "GraphQLTypeShape",
    "SEMANTIC_API_CLASSIFIER_VERSION",
    "SemanticApiClassification",
    "SemanticApiClassificationError",
    "SemanticApiClassifier",
    "SemanticCollectionCandidate",
    "SemanticConfidence",
    "SemanticEntityCandidate",
    "SemanticEvidence",
    "SemanticFieldClassification",
    "SemanticFieldRole",
    "SemanticInputObjectClassification",
    "SemanticInputValueClassification",
    "SemanticOperationClassification",
    "SemanticRelationshipClassification",
    "SemanticRelationshipKind",
]
