from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re

from .schema import (
    GraphQLField,
    GraphQLSchema,
)
from .schema_snapshot import (
    schema_fingerprint,
)

from .graphql_argument_values import (
    GraphQLArgumentValueError,
    validate_typed_argument_values,
)
from .graphql_query_composer import (
    GraphQLQueryComposerError,
    GraphQLQueryComposerModel,
    build_mutation_composer_model,
    build_query_composer_model,
)
from .graphql_selection_set import (
    GraphQLSelectionPathSegment,
    GraphQLSelectionSetError,
    GraphQLSelectionTree,
    build_stable_selection_tree,
)
from .graphql_write_safety import (
    GraphQLWriteRequest,
    detect_operation_kind,
)
from .operation_kind import GraphQLOperationKind
from .source_proposals import (
    SourceApiProposalModel,
    SourceOperationProposal,
)


_GRAPHQL_NAME = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*$")


class GraphQLOperationMaterializationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphQLOperationBinding:
    binding_id: str
    schema_fingerprint: str
    operation_node_id: str
    operation_kind: GraphQLOperationKind
    operation_name: str
    root_alias: str
    selected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        binding_id = self.binding_id.strip()
        fingerprint = self.schema_fingerprint.strip().lower()
        operation_node_id = self.operation_node_id.strip()
        operation_name = self.operation_name.strip()
        root_alias = self.root_alias.strip()

        if not binding_id:
            raise GraphQLOperationMaterializationError(
                "binding_id must not be empty."
            )

        if (
            len(fingerprint) != 64
            or any(
                char not in "0123456789abcdef"
                for char in fingerprint
            )
        ):
            raise GraphQLOperationMaterializationError(
                "schema_fingerprint must be a SHA-256 hex digest."
            )

        if not operation_node_id:
            raise GraphQLOperationMaterializationError(
                "operation_node_id must not be empty."
            )

        if self.operation_kind not in {
            GraphQLOperationKind.QUERY,
            GraphQLOperationKind.MUTATION,
        }:
            raise GraphQLOperationMaterializationError(
                "Only GraphQL query and mutation bindings are supported."
            )

        if not _GRAPHQL_NAME.fullmatch(operation_name):
            raise GraphQLOperationMaterializationError(
                "operation_name must be a valid GraphQL name."
            )

        if not _GRAPHQL_NAME.fullmatch(root_alias):
            raise GraphQLOperationMaterializationError(
                "root_alias must be a valid GraphQL name."
            )

        normalized_paths = tuple(
            path.strip()
            for path in self.selected_paths
        )

        if any(
            not path
            for path in normalized_paths
        ):
            raise GraphQLOperationMaterializationError(
                "selected_paths must not contain empty paths."
            )

        if len(normalized_paths) != len(
            set(normalized_paths)
        ):
            raise GraphQLOperationMaterializationError(
                "selected_paths contains duplicates."
            )

        object.__setattr__(
            self,
            "binding_id",
            binding_id,
        )
        object.__setattr__(
            self,
            "schema_fingerprint",
            fingerprint,
        )
        object.__setattr__(
            self,
            "operation_node_id",
            operation_node_id,
        )
        object.__setattr__(
            self,
            "operation_name",
            operation_name,
        )
        object.__setattr__(
            self,
            "root_alias",
            root_alias,
        )
        object.__setattr__(
            self,
            "selected_paths",
            normalized_paths,
        )


@dataclass(frozen=True, slots=True)
class GraphQLArgumentBinding:
    argument_name: str
    graphql_type: str
    variable_name: str


@dataclass(frozen=True, slots=True)
class GraphQLResultBinding:
    selection_path: tuple[str, ...]
    protocol_field_node_ids: tuple[str, ...]
    response_path: tuple[str, ...]
    graphql_type: str


@dataclass(frozen=True, slots=True)
class GraphQLOperationRequest:
    binding_id: str
    schema_fingerprint: str
    operation_node_id: str
    operation_kind: GraphQLOperationKind
    protocol_field_name: str
    operation_name: str
    root_alias: str
    document: str
    argument_bindings: tuple[GraphQLArgumentBinding, ...]
    result_bindings: tuple[GraphQLResultBinding, ...]
    request_fingerprint: str
    mutation_safety_request: GraphQLWriteRequest | None
    _canonical_variables: str

    @property
    def variables(self) -> dict[str, object]:
        # Detached copy: callers cannot mutate the sealed request.
        return json.loads(self._canonical_variables)


class GraphQLOperationMaterializer:
    """Pure single-operation GraphQL materializer.

    Query and Mutation share one protocol contract. This component performs
    no HTTP I/O, no Frappe persistence and no Process-Core publication.
    """

    def materialize(
        self,
        schema: GraphQLSchema,
        proposals: SourceApiProposalModel,
        binding: GraphQLOperationBinding,
        argument_values: Mapping[str, object],
        *,
        request_scope: str | None = None,
    ) -> GraphQLOperationRequest:
        if not isinstance(
            schema,
            GraphQLSchema,
        ):
            raise TypeError(
                "schema must be a GraphQLSchema."
            )

        if not isinstance(
            proposals,
            SourceApiProposalModel,
        ):
            raise TypeError(
                "proposals must be a SourceApiProposalModel."
            )

        if not isinstance(
            binding,
            GraphQLOperationBinding,
        ):
            raise TypeError(
                "binding must be a GraphQLOperationBinding."
            )

        if not isinstance(
            argument_values,
            Mapping,
        ):
            raise TypeError(
                "argument_values must be a mapping."
            )

        current_fingerprint = schema_fingerprint(
            schema
        )

        if (
            current_fingerprint
            != proposals.schema_fingerprint
            or current_fingerprint
            != binding.schema_fingerprint
        ):
            raise GraphQLOperationMaterializationError(
                "Schema, proposals and operation binding "
                "use different fingerprints."
            )

        operation = self._operation(
            proposals,
            binding,
        )

        root_field = self._root_field(
            schema,
            operation,
        )

        model = self._composer_model(
            schema,
            root_field,
            binding.operation_kind,
        )

        try:
            validated = validate_typed_argument_values(
                model,
                dict(argument_values),
                schema=schema,
            )
        except GraphQLArgumentValueError as exc:
            raise GraphQLOperationMaterializationError(
                str(exc)
            ) from exc

        argument_bindings, variables = (
            self._arguments(
                model,
                validated,
            )
        )

        selection_tree, result_bindings = (
            self._selection(
                schema,
                model,
                binding,
            )
        )

        document = self._render(
            binding,
            root_field,
            argument_bindings,
            selection_tree,
        )

        detected = detect_operation_kind(
            document,
            operation_name=binding.operation_name,
        )

        if detected is not binding.operation_kind:
            raise GraphQLOperationMaterializationError(
                "Rendered GraphQL operation kind does not "
                "match the sealed binding."
            )

        canonical_variables = self._canonical_variables(
            variables
        )

        mutation_safety_request = None

        if (
            binding.operation_kind
            is GraphQLOperationKind.MUTATION
        ):
            normalized_scope = (
                request_scope.strip()
                if isinstance(
                    request_scope,
                    str,
                )
                else ""
            )

            if not normalized_scope:
                raise GraphQLOperationMaterializationError(
                    "Mutation materialization requires "
                    "a non-empty request_scope."
                )

            mutation_safety_request = (
                GraphQLWriteRequest.create(
                    document,
                    variables=json.loads(
                        canonical_variables
                    ),
                    operation_name=(
                        binding.operation_name
                    ),
                    request_scope=normalized_scope,
                )
            )

            if (
                mutation_safety_request.operation_kind
                is not GraphQLOperationKind.MUTATION
            ):
                raise GraphQLOperationMaterializationError(
                    "Mutation safety request resolved to "
                    "a non-mutation operation."
                )

        elif request_scope is not None:
            raise GraphQLOperationMaterializationError(
                "request_scope is mutation-only."
            )

        request_fingerprint = (
            self._request_fingerprint(
                binding,
                document,
                canonical_variables,
                result_bindings,
                (
                    mutation_safety_request.request_scope
                    if mutation_safety_request
                    is not None
                    else None
                ),
            )
        )

        return GraphQLOperationRequest(
            binding_id=binding.binding_id,
            schema_fingerprint=(
                binding.schema_fingerprint
            ),
            operation_node_id=(
                binding.operation_node_id
            ),
            operation_kind=(
                binding.operation_kind
            ),
            protocol_field_name=(
                root_field.name
            ),
            operation_name=(
                binding.operation_name
            ),
            root_alias=binding.root_alias,
            document=document,
            argument_bindings=(
                argument_bindings
            ),
            result_bindings=(
                result_bindings
            ),
            request_fingerprint=(
                request_fingerprint
            ),
            mutation_safety_request=(
                mutation_safety_request
            ),
            _canonical_variables=(
                canonical_variables
            ),
        )

    @staticmethod
    def _operation(
        proposals: SourceApiProposalModel,
        binding: GraphQLOperationBinding,
    ) -> SourceOperationProposal:
        operation = next(
            (
                item
                for item in proposals.operations
                if (
                    item.operation_node_id
                    == binding.operation_node_id
                )
            ),
            None,
        )

        if operation is None:
            raise GraphQLOperationMaterializationError(
                "Operation binding references an "
                "unknown source operation."
            )

        if (
            operation.operation_kind
            is not binding.operation_kind
        ):
            raise GraphQLOperationMaterializationError(
                "Operation kind differs from the "
                "source proposal."
            )

        return operation

    @staticmethod
    def _root_field(
        schema: GraphQLSchema,
        operation: SourceOperationProposal,
    ) -> GraphQLField:
        if (
            operation.operation_kind
            is GraphQLOperationKind.QUERY
        ):
            fields = schema.query_fields

        elif (
            operation.operation_kind
            is GraphQLOperationKind.MUTATION
        ):
            fields = schema.mutation_fields

        else:
            raise GraphQLOperationMaterializationError(
                "Unsupported GraphQL operation kind."
            )

        field = next(
            (
                item
                for item in fields
                if (
                    item.name
                    == operation.protocol_name
                )
            ),
            None,
        )

        if field is None:
            raise GraphQLOperationMaterializationError(
                "Source operation root field is "
                "missing from schema."
            )

        return field

    @staticmethod
    def _composer_model(
        schema: GraphQLSchema,
        root_field: GraphQLField,
        kind: GraphQLOperationKind,
    ) -> GraphQLQueryComposerModel:
        try:
            if kind is GraphQLOperationKind.QUERY:
                return build_query_composer_model(
                    schema,
                    root_field,
                )

            return build_mutation_composer_model(
                schema,
                root_field,
            )

        except GraphQLQueryComposerError as exc:
            raise GraphQLOperationMaterializationError(
                str(exc)
            ) from exc

    @staticmethod
    def _arguments(
        model: GraphQLQueryComposerModel,
        validated: Mapping[str, object],
    ) -> tuple[
        tuple[GraphQLArgumentBinding, ...],
        dict[str, object],
    ]:
        included = sorted(
            (
                spec
                for spec in model.arguments
                if spec.name in validated
            ),
            key=lambda item: item.name,
        )

        bindings: list[
            GraphQLArgumentBinding
        ] = []

        variables: dict[
            str,
            object,
        ] = {}

        for index, spec in enumerate(
            included,
            start=1,
        ):
            variable_name = f"v{index}"

            bindings.append(
                GraphQLArgumentBinding(
                    argument_name=spec.name,
                    graphql_type=spec.type_name,
                    variable_name=variable_name,
                )
            )

            variables[variable_name] = (
                validated[spec.name]
            )

        return (
            tuple(bindings),
            variables,
        )

    @staticmethod
    def _selection(
        schema: GraphQLSchema,
        model: GraphQLQueryComposerModel,
        binding: GraphQLOperationBinding,
    ) -> tuple[
        GraphQLSelectionTree,
        tuple[GraphQLResultBinding, ...],
    ]:
        root_type = model.root_return_type

        if root_type.kind in {
            "SCALAR",
            "ENUM",
        }:
            if binding.selected_paths:
                raise GraphQLOperationMaterializationError(
                    "Scalar/enum root must not contain "
                    "selected child paths."
                )

            return (
                GraphQLSelectionTree(),
                (
                    GraphQLResultBinding(
                        selection_path=(),
                        protocol_field_node_ids=(),
                        response_path=(
                            "data",
                            binding.root_alias,
                        ),
                        graphql_type=(
                            model.root_field.type_ref.render()
                        ),
                    ),
                ),
            )

        if root_type.kind == "UNION":
            raise GraphQLOperationMaterializationError(
                "Union operation results require an "
                "explicit fragment contract."
            )

        if root_type.kind not in {
            "OBJECT",
            "INTERFACE",
        }:
            raise GraphQLOperationMaterializationError(
                "Unsupported GraphQL operation return kind: "
                f"{root_type.kind}."
            )

        if not binding.selected_paths:
            raise GraphQLOperationMaterializationError(
                "Object/interface operation requires "
                "explicit selected_paths."
            )

        by_path = {
            item.path: item
            for item in model.selectable_fields
        }

        stable_paths = []
        result_bindings = []

        for raw_path in sorted(
            binding.selected_paths
        ):
            path = tuple(
                part
                for part in raw_path.split(".")
                if part
            )

            leaf = by_path.get(
                path
            )

            if leaf is None:
                raise GraphQLOperationMaterializationError(
                    "Selected GraphQL path is not available: "
                    f"{raw_path}"
                )

            leaf_type_name = (
                leaf.field.type_ref.named_type()
            )

            leaf_type = (
                schema.type_named(
                    leaf_type_name
                )
                if leaf_type_name is not None
                else None
            )

            if (
                leaf_type is None
                or leaf_type.kind
                not in {
                    "SCALAR",
                    "ENUM",
                }
            ):
                raise GraphQLOperationMaterializationError(
                    "Selected GraphQL result path must "
                    "terminate in a scalar or enum field: "
                    f"{raw_path}"
                )

            segments = []
            protocol_ids = []

            for depth in range(
                1,
                len(path) + 1,
            ):
                prefix = path[:depth]
                selection = by_path.get(
                    prefix
                )

                if selection is None:
                    raise GraphQLOperationMaterializationError(
                        "Selected GraphQL path has an "
                        "unresolvable prefix: "
                        f"{'.'.join(prefix)}"
                    )

                required_arguments = tuple(
                    argument
                    for argument
                    in selection.field.arguments
                    if (
                        argument.type_ref.kind
                        == "NON_NULL"
                        and argument.default_value
                        is None
                    )
                )

                if required_arguments:
                    raise GraphQLOperationMaterializationError(
                        "Selected nested GraphQL field "
                        "requires unbound arguments: "
                        f"{'.'.join(prefix)}"
                    )

                protocol_node_id = (
                    f"field:"
                    f"{selection.parent_type_name}."
                    f"{selection.field.name}"
                )

                protocol_ids.append(
                    protocol_node_id
                )

                segments.append(
                    GraphQLSelectionPathSegment(
                        protocol_node_id=(
                            protocol_node_id
                        ),
                        field_name=(
                            selection.field.name
                        ),
                    )
                )

            stable_paths.append(
                tuple(segments)
            )

            result_bindings.append(
                GraphQLResultBinding(
                    selection_path=path,
                    protocol_field_node_ids=(
                        tuple(protocol_ids)
                    ),
                    response_path=(
                        "data",
                        binding.root_alias,
                        *path,
                    ),
                    graphql_type=(
                        leaf.field.type_ref.render()
                    ),
                )
            )

        try:
            tree = build_stable_selection_tree(
                stable_paths
            )
        except GraphQLSelectionSetError as exc:
            raise GraphQLOperationMaterializationError(
                str(exc)
            ) from exc

        return (
            tree,
            tuple(
                sorted(
                    result_bindings,
                    key=lambda item: (
                        item.selection_path
                    ),
                )
            ),
        )

    @staticmethod
    def _render(
        binding: GraphQLOperationBinding,
        root_field: GraphQLField,
        arguments: tuple[
            GraphQLArgumentBinding,
            ...,
        ],
        selection_tree: GraphQLSelectionTree,
    ) -> str:
        declarations = ", ".join(
            (
                f"${item.variable_name}: "
                f"{item.graphql_type}"
            )
            for item in arguments
        )

        argument_values = ", ".join(
            (
                f"{item.argument_name}: "
                f"${item.variable_name}"
            )
            for item in arguments
        )

        signature = (
            f"({declarations})"
            if declarations
            else ""
        )

        call_arguments = (
            f"({argument_values})"
            if argument_values
            else ""
        )

        selection = (
            selection_tree.render()
        )

        selection_suffix = (
            f" {selection}"
            if selection
            else ""
        )

        keyword = (
            binding.operation_kind.value
        )

        return (
            f"{keyword} "
            f"{binding.operation_name}"
            f"{signature} {{\n"
            f"  {binding.root_alias}: "
            f"{root_field.name}"
            f"{call_arguments}"
            f"{selection_suffix}\n"
            "}"
        )

    @staticmethod
    def _canonical_variables(
        variables: Mapping[str, object],
    ) -> str:
        try:
            return json.dumps(
                dict(variables),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (
            TypeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise GraphQLOperationMaterializationError(
                "GraphQL variables must be "
                "JSON serializable."
            ) from exc

    @staticmethod
    def _request_fingerprint(
        binding: GraphQLOperationBinding,
        document: str,
        canonical_variables: str,
        result_bindings: tuple[
            GraphQLResultBinding,
            ...,
        ],
        request_scope: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "binding_id": binding.binding_id,
                "schema_fingerprint": (
                    binding.schema_fingerprint
                ),
                "operation_node_id": (
                    binding.operation_node_id
                ),
                "operation_kind": (
                    binding.operation_kind.value
                ),
                "operation_name": (
                    binding.operation_name
                ),
                "root_alias": binding.root_alias,
                "document": document,
                "variables": json.loads(
                    canonical_variables
                ),
                "result_paths": [
                    {
                        "selection_path": (
                            list(
                                item.selection_path
                            )
                        ),
                        "protocol_field_node_ids": (
                            list(
                                item.protocol_field_node_ids
                            )
                        ),
                        "response_path": (
                            list(
                                item.response_path
                            )
                        ),
                        "graphql_type": (
                            item.graphql_type
                        ),
                    }
                    for item
                    in result_bindings
                ],
                "request_scope": request_scope,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        return hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()


__all__ = [
    "GraphQLOperationMaterializationError",
    "GraphQLOperationBinding",
    "GraphQLArgumentBinding",
    "GraphQLResultBinding",
    "GraphQLOperationRequest",
    "GraphQLOperationMaterializer",
]
