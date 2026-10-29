"""Pure canonical routing for typed B1 GraphQL operation references."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from hero_integration.graphql_protocol.graphql_operation_materializer import (
    GraphQLOperationBinding,
    GraphQLOperationMaterializationError,
    GraphQLOperationMaterializer,
    GraphQLOperationRequest,
    GraphQLResultBinding,
)
from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.graphql_protocol.source_proposals import (
    SourceApiProposalModel,
    SourceOperationProposal,
    SourceOperationRole,
)
from hero_integration.services.mapping_contract_v1 import (
    MappingContractError,
    SnapshotAdapter,
    StoredSnapshot,
    ValidatedSnapshot,
)
from hero_integration.services.schema_registry import (
    SchemaField,
    SchemaOperation,
    SchemaRegistry,
    SchemaRegistryError,
)


class MaterializerRoutingError(ValueError):
    """A B1 catalog reference cannot be safely materialized."""


@dataclass(frozen=True)
class QueryRoutingRequest:
    operation: str
    output_references: tuple[str, ...]
    arguments: Mapping[str, object]
    identity_reference: str | None = None
    revision: int = 1
    alias: str | None = None
    pagination_argument: str | None = None


@dataclass(frozen=True)
class MutationRoutingRequest:
    operation: str
    input_references: tuple[str, ...]
    arguments: Mapping[str, object]
    output_references: tuple[str, ...] = ()
    revision: int = 1
    alias: str | None = None


@dataclass(frozen=True)
class QueryOperationPlan:
    direction: Literal["QUERY_OUTPUT"]
    operation_identity: str
    snapshot_name: str
    source_instance: str
    snapshot_fingerprint: str
    snapshot_revision: int
    request: GraphQLOperationRequest
    selected_reference_ids: tuple[str, ...]
    selected_response_paths: tuple[tuple[str, ...], ...]
    identity_reference: str | None
    identity_result_binding: GraphQLResultBinding | None
    collection: bool
    pagination_argument: str | None
    pagination_variable: str | None
    execution_authority: Literal["NONE"] = "NONE"
    mapping_registry_revision: int | None = None


@dataclass(frozen=True)
class MutationOperationPlan:
    direction: Literal["MUTATION_INPUT"]
    operation_identity: str
    snapshot_name: str
    source_instance: str
    snapshot_fingerprint: str
    snapshot_revision: int
    request: GraphQLOperationRequest
    input_reference_ids: tuple[str, ...]
    output_reference_ids: tuple[str, ...]
    selected_response_paths: tuple[tuple[str, ...], ...]
    execution_authority: Literal["NONE"] = "NONE"
    mapping_registry_revision: int | None = None


class MaterializerRouter:
    """Compile B1 discovery references to sealed local-only materializer plans."""

    def compile_query_mappings(self, snapshot, registry, fields, definitions, *, operation, arguments,
                               identity_reference, revision=1, alias=None, pagination_argument=None):
        """Compile validated mapping fields and an explicit record identity without a stored document.

        The result is a discovery plan only; approval and execution remain separate.
        """
        references = _mapping_references(definitions, registry, fields, operation, 'HERO_TO_FRAPPE')
        if not isinstance(identity_reference, str) or not identity_reference:
            raise MaterializerRoutingError('An explicit query identity reference is required.')
        selected = tuple(sorted(set(references) | {identity_reference}))
        return self.compile_query(snapshot, registry, QueryRoutingRequest(
            operation=operation, output_references=selected, arguments=arguments,
            identity_reference=identity_reference, revision=revision, alias=alias,
            pagination_argument=pagination_argument,
        ))

    def compile_mutation_mappings(self, snapshot, registry, fields, definitions, *, operation, arguments,
                                  output_references, revision=1, alias=None):
        """Compile mapped mutation inputs; the result carries no execution permission."""
        references = _mapping_references(definitions, registry, fields, operation, 'FRAPPE_TO_HERO')
        return self.compile_mutation(snapshot, registry, MutationRoutingRequest(
            operation=operation, input_references=references, arguments=arguments,
            output_references=output_references, revision=revision, alias=alias,
        ))

    def compile_query(
        self,
        snapshot: StoredSnapshot,
        registry: SchemaRegistry,
        route: QueryRoutingRequest,
    ) -> QueryOperationPlan:
        if not isinstance(route, QueryRoutingRequest):
            raise TypeError("QueryRoutingRequest is required.")
        validated = _validated_context(snapshot, registry)
        operation = _operation(registry, "QUERY", route.operation)
        fields = _output_fields(operation, route.output_references, "QUERY")
        _require_revision(route.revision)
        _require_mapping(route.arguments)
        alias = _alias(route.alias, operation.name)
        identity = _identity(route.identity_reference, fields)
        request = _materialize(
            validated,
            operation,
            GraphQLOperationKind.QUERY,
            alias,
            tuple(".".join(item.path) for item in fields),
            route.arguments,
        )
        result_bindings = _bindings_for(fields, request)
        pagination_argument, pagination_variable = _pagination(
            route.pagination_argument, operation, route.arguments, request
        )
        return QueryOperationPlan(
            direction="QUERY_OUTPUT",
            operation_identity=f"hero.query.{operation.name}",
            snapshot_name=registry.snapshot_name,
            source_instance=registry.source_instance,
            snapshot_fingerprint=registry.fingerprint,
            snapshot_revision=route.revision,
            request=request,
            selected_reference_ids=tuple(item.reference_id for item in fields),
            selected_response_paths=tuple(item.response_path for item in result_bindings),
            identity_reference=identity.reference_id if identity else None,
            identity_result_binding=next(
                (item for item in result_bindings if item.selection_path == identity.path),
                None,
            ) if identity else None,
            collection=operation.return_type.list_depth > 0,
            pagination_argument=pagination_argument,
            pagination_variable=pagination_variable,
        )

    def compile_mutation(
        self,
        snapshot: StoredSnapshot,
        registry: SchemaRegistry,
        route: MutationRoutingRequest,
    ) -> MutationOperationPlan:
        if not isinstance(route, MutationRoutingRequest):
            raise TypeError("MutationRoutingRequest is required.")
        validated = _validated_context(snapshot, registry)
        operation = _operation(registry, "MUTATION", route.operation)
        _require_revision(route.revision)
        _require_mapping(route.arguments)
        input_ids = _input_reference_ids(operation, route.input_references)
        _validate_mutation_inputs(operation, input_ids, route.arguments)
        outputs = _output_fields(operation, route.output_references, "MUTATION")
        alias = _alias(route.alias, operation.name)
        request = _materialize(
            validated,
            operation,
            GraphQLOperationKind.MUTATION,
            alias,
            tuple(".".join(item.path) for item in outputs),
            route.arguments,
            request_scope=f"materializer-routing:{operation.name}",
        )
        if request.mutation_safety_request is None:
            raise MaterializerRoutingError("Mutation plan has no sealed safety request.")
        result_bindings = _bindings_for(outputs, request)
        return MutationOperationPlan(
            direction="MUTATION_INPUT",
            operation_identity=f"hero.mutation.{operation.name}",
            snapshot_name=registry.snapshot_name,
            source_instance=registry.source_instance,
            snapshot_fingerprint=registry.fingerprint,
            snapshot_revision=route.revision,
            request=request,
            input_reference_ids=input_ids,
            output_reference_ids=tuple(item.reference_id for item in outputs),
            selected_response_paths=tuple(item.response_path for item in result_bindings),
        )


def _mapping_references(definitions, registry, fields, operation, direction):
    from hero_integration.services.mapping_registry_model import MappingAvailability, MappingDefinition
    from hero_integration.services.mapping_registry_validation import validate_complete_mapping_set

    if not isinstance(definitions, tuple) or not definitions or any(not isinstance(item, MappingDefinition) for item in definitions):
        raise MaterializerRoutingError('An explicit nonempty mapping definition tuple is required.')
    if not isinstance(operation, str) or not operation or any(
            item.direction.value != direction or item.graphql.operation != operation for item in definitions):
        raise MaterializerRoutingError('Mapping direction or operation binding does not match.')
    try:
        validated = validate_complete_mapping_set(definitions, registry, fields)
    except ValueError as error:
        raise MaterializerRoutingError('Mapping definitions are invalid for this schema and target.') from error
    if any(item.availability != MappingAvailability.READY for item in validated):
        raise MaterializerRoutingError('Mapping definitions require an available target and valid metadata.')
    return tuple(sorted({item.definition.hero_reference for item in validated}))


def _validated_context(snapshot: StoredSnapshot, registry: SchemaRegistry) -> ValidatedSnapshot:
    if not isinstance(snapshot, StoredSnapshot) or not isinstance(registry, SchemaRegistry):
        raise MaterializerRoutingError("Validated snapshot and schema registry are required.")
    try:
        validated = SnapshotAdapter().adapt(snapshot)
    except MappingContractError as error:
        raise MaterializerRoutingError(str(error)) from error
    if (
        registry.execution_authority != "DISCOVERED_ONLY"
        or registry.snapshot_name != snapshot.name
        or registry.source_instance != snapshot.source_instance
        or registry.fingerprint != snapshot.fingerprint
        or validated.fingerprint != registry.fingerprint
    ):
        raise MaterializerRoutingError("Registry does not match the validated snapshot.")
    return validated


def _operation(registry: SchemaRegistry, kind: str, name: str) -> SchemaOperation:
    try:
        operation = registry.operation(kind, name)
    except SchemaRegistryError as error:
        raise MaterializerRoutingError(str(error)) from error
    if operation.kind != kind or operation.source_fingerprint != registry.fingerprint:
        raise MaterializerRoutingError("Operation catalog identity is stale.")
    return operation


def _require_revision(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MaterializerRoutingError("Snapshot revision must be a positive integer.")


def _require_mapping(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        raise MaterializerRoutingError("Argument values must be a mapping.")


def _alias(value: str | None, fallback: str) -> str:
    alias = fallback if value is None else value
    if not isinstance(alias, str) or not alias:
        raise MaterializerRoutingError("Root alias must be a GraphQL name.")
    return alias


def _references(values: tuple[str, ...], allowed: Mapping[str, SchemaField], label: str) -> tuple[SchemaField, ...]:
    if not isinstance(values, tuple) or any(not isinstance(item, str) for item in values):
        raise MaterializerRoutingError(f"{label} references must be a tuple of strings.")
    if len(values) != len(set(values)):
        raise MaterializerRoutingError(f"{label} references contain duplicates.")
    try:
        resolved = tuple(allowed[item] for item in sorted(values))
    except KeyError as error:
        raise MaterializerRoutingError(f"Unknown or incompatible {label} reference.") from error
    return resolved


def _output_fields(
    operation: SchemaOperation, values: tuple[str, ...], kind: str
) -> tuple[SchemaField, ...]:
    prefix = f"hero.{kind.lower()}.{operation.name}" + (".result" if kind == "MUTATION" else "")
    allowed = {
        item.reference_id: item
        for item in operation.output_fields
        if item.operation_kind == kind
        and item.source_fingerprint == operation.source_fingerprint
        and item.reference_id.startswith(prefix + ".")
    }
    return _references(values, allowed, f"{kind.lower()} output")


def _input_reference_ids(operation: SchemaOperation, values: tuple[str, ...]) -> tuple[str, ...]:
    prefix = f"hero.mutation.{operation.name}."
    allowed = {
        item.reference_id
        for item in operation.input_fields
        if item.operation_kind == "MUTATION"
        and item.source_fingerprint == operation.source_fingerprint
        and item.reference_id.startswith(prefix)
    } | {
        item.reference_id
        for item in operation.arguments
        if item.operation_kind == "MUTATION"
        and item.source_fingerprint == operation.source_fingerprint
    }
    if not isinstance(values, tuple) or any(not isinstance(item, str) for item in values):
        raise MaterializerRoutingError("Mutation input references must be a tuple of strings.")
    if len(values) != len(set(values)) or any(item not in allowed for item in values):
        raise MaterializerRoutingError("Unknown or incompatible mutation input reference.")
    return tuple(sorted(values))


def _identity(value: str | None, fields: tuple[SchemaField, ...]) -> SchemaField | None:
    if value is None:
        return None
    match = next((item for item in fields if item.reference_id == value), None)
    if match is None:
        raise MaterializerRoutingError("Identity reference must be one selected query output.")
    return match


def _proposal(validated: ValidatedSnapshot, operation: SchemaOperation, kind: GraphQLOperationKind) -> SourceApiProposalModel:
    return SourceApiProposalModel(
        model_version=1,
        schema_fingerprint=validated.ir_fingerprint,
        entities=(),
        technical_wrappers=(),
        operations=(
            SourceOperationProposal(
                operation_node_id=f"operation:{kind.value.lower()}:{operation.name}",
                protocol_name=operation.name,
                operation_kind=kind,
                role=SourceOperationRole.OTHER,
                target_type_node_id=None,
                argument_node_ids=(),
                input_object_type_node_ids=(),
                result_path_node_ids=(),
                evidence=(),
            ),
        ),
    )


def _materialize(
    validated: ValidatedSnapshot,
    operation: SchemaOperation,
    kind: GraphQLOperationKind,
    alias: str,
    paths: tuple[str, ...],
    arguments: Mapping[str, object],
    *,
    request_scope: str | None = None,
) -> GraphQLOperationRequest:
    binding = GraphQLOperationBinding(
        binding_id=f"routing:{kind.value.lower()}:{operation.name}",
        schema_fingerprint=validated.ir_fingerprint,
        operation_node_id=f"operation:{kind.value.lower()}:{operation.name}",
        operation_kind=kind,
        operation_name=operation.name,
        root_alias=alias,
        selected_paths=paths,
    )
    try:
        return GraphQLOperationMaterializer().materialize(
            validated.ir, _proposal(validated, operation, kind), binding, arguments,
            request_scope=request_scope,
        )
    except GraphQLOperationMaterializationError as error:
        raise MaterializerRoutingError(str(error)) from error


def _bindings_for(
    fields: tuple[SchemaField, ...], request: GraphQLOperationRequest
) -> tuple[GraphQLResultBinding, ...]:
    expected = {item.path for item in fields}
    bindings = tuple(
        item for item in request.result_bindings if item.selection_path in expected
    )
    if len(bindings) != len(fields):
        raise MaterializerRoutingError("Materialized result bindings do not match cataloged paths.")
    return bindings


def _pagination(
    value: str | None,
    operation: SchemaOperation,
    arguments: Mapping[str, object],
    request: GraphQLOperationRequest,
) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    argument = next((item for item in operation.arguments if item.name == value), None)
    if argument is None or value not in arguments:
        raise MaterializerRoutingError("Pagination argument must be a supplied operation argument.")
    variable = next((item.variable_name for item in request.argument_bindings if item.argument_name == value), None)
    if variable is None:
        raise MaterializerRoutingError("Pagination argument was not materialized.")
    return value, variable


def _validate_mutation_inputs(
    operation: SchemaOperation,
    references: tuple[str, ...],
    arguments: Mapping[str, object],
) -> None:
    allowed = set(references)
    root_arguments = {item.name: item for item in operation.arguments}
    for name, value in arguments.items():
        argument = root_arguments.get(name)
        if argument is None:
            continue
        if argument.type_ref.named_kind != "INPUT_OBJECT":
            if argument.reference_id not in allowed:
                raise MaterializerRoutingError("Mutation scalar argument lacks a selected input reference.")
            continue
        if isinstance(value, Mapping) and not value:
            if argument.reference_id not in allowed:
                raise MaterializerRoutingError(
                    "Empty mutation input object lacks its selected root input reference."
                )
            continue
        for path in _mapping_leaf_paths(value):
            reference = f"hero.mutation.{operation.name}.{name}." + ".".join(path)
            if reference not in allowed:
                raise MaterializerRoutingError("Mutation value path lacks a selected input reference.")


def _mapping_leaf_paths(value: object, prefix: tuple[str, ...] = ()) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, Mapping):
        if not value:
            return (prefix,)
        result: list[tuple[str, ...]] = []
        for name in sorted(value):
            if not isinstance(name, str):
                raise MaterializerRoutingError("Mutation input object keys must be strings.")
            result.extend(_mapping_leaf_paths(value[name], (*prefix, name)))
        return tuple(result)
    if isinstance(value, list):
        result: list[tuple[str, ...]] = []
        for item in value:
            result.extend(_mapping_leaf_paths(item, prefix))
        return tuple(result or (prefix,))
    return (prefix,)
