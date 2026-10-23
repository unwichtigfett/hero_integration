from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    GraphQLArgument,
    GraphQLSchema,
    GraphQLTypeRef,
)


@dataclass(frozen=True, slots=True)
class ApiGraphNode:
    node_id: str
    kind: str
    name: str
    description: str | None = None
    type_ref: str | None = None
    is_deprecated: bool = False


@dataclass(frozen=True, slots=True)
class ApiGraphEdge:
    source_id: str
    target_id: str
    kind: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class ApiGraphCoverage:
    query_operations: int
    mutation_operations: int
    subscription_operations: int
    types: int
    object_types: int
    input_objects: int
    enums: int
    scalars: int
    interfaces: int
    unions: int
    fields: int
    arguments: int
    input_fields: int
    enum_values: int
    directives: int
    nodes: int
    edges: int
    credential_scope: str = "current_profile"
    introspection_complete: bool = True


@dataclass(frozen=True, slots=True)
class CanonicalApiGraph:
    nodes: tuple[ApiGraphNode, ...]
    edges: tuple[ApiGraphEdge, ...]
    coverage: ApiGraphCoverage

    def node(self, node_id: str) -> ApiGraphNode | None:
        return next(
            (node for node in self.nodes if node.node_id == node_id),
            None,
        )


def project_schema_to_api_graph(
    schema: GraphQLSchema,
) -> CanonicalApiGraph:
    nodes: dict[str, ApiGraphNode] = {}
    edges: set[ApiGraphEdge] = set()
    type_ids = {
        graphql_type.name: f"type:{graphql_type.name}"
        for graphql_type in schema.types
    }

    def add_node(node: ApiGraphNode) -> None:
        existing = nodes.get(node.node_id)
        if existing is not None and existing != node:
            raise ValueError(
                f"Conflicting API graph node: {node.node_id}"
            )
        nodes[node.node_id] = node

    def add_edge(
        source_id: str,
        target_id: str,
        kind: str,
        label: str | None = None,
    ) -> None:
        edges.add(
            ApiGraphEdge(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                label=label,
            )
        )

    def add_type_reference(
        owner_id: str,
        type_ref: GraphQLTypeRef,
        relation: str,
    ) -> None:
        named = type_ref.named_type()
        if named is None:
            return
        target_id = type_ids.get(named)
        if target_id is None:
            return
        add_edge(
            owner_id,
            target_id,
            relation,
            type_ref.render(),
        )

    def add_arguments(
        owner_id: str,
        arguments: tuple[GraphQLArgument, ...],
        prefix: str,
    ) -> None:
        for argument in arguments:
            argument_id = f"{prefix}:{argument.name}"
            add_node(
                ApiGraphNode(
                    node_id=argument_id,
                    kind="ARGUMENT",
                    name=argument.name,
                    description=argument.description,
                    type_ref=argument.type_ref.render(),
                )
            )
            add_edge(owner_id, argument_id, "HAS_ARGUMENT")
            add_type_reference(
                argument_id,
                argument.type_ref,
                "ARGUMENT_TYPE",
            )

    for graphql_type in schema.types:
        add_node(
            ApiGraphNode(
                node_id=type_ids[graphql_type.name],
                kind=graphql_type.kind,
                name=graphql_type.name,
                description=graphql_type.description,
            )
        )

    root_specs = (
        (
            "query",
            schema.query_type_name,
            "QUERY_ROOT",
            "QUERY_OPERATION",
        ),
        (
            "mutation",
            schema.mutation_type_name,
            "MUTATION_ROOT",
            "MUTATION_OPERATION",
        ),
        (
            "subscription",
            schema.subscription_type_name,
            "SUBSCRIPTION_ROOT",
            "SUBSCRIPTION_OPERATION",
        ),
    )

    root_type_names: set[str] = set()

    for root_name, type_name, root_kind, operation_kind in root_specs:
        if type_name is None:
            continue

        root_type_names.add(type_name)
        root_id = f"root:{root_name}"
        type_id = type_ids.get(type_name)

        add_node(
            ApiGraphNode(
                node_id=root_id,
                kind=root_kind,
                name=type_name,
            )
        )

        if type_id is not None:
            add_edge(root_id, type_id, "ROOT_TYPE")

        root_type = schema.type_named(type_name)
        if root_type is None:
            continue

        for field in root_type.fields:
            operation_id = f"operation:{root_name}:{field.name}"
            add_node(
                ApiGraphNode(
                    node_id=operation_id,
                    kind=operation_kind,
                    name=field.name,
                    description=field.description,
                    type_ref=field.type_ref.render(),
                    is_deprecated=field.is_deprecated,
                )
            )
            add_edge(
                root_id,
                operation_id,
                "ROOT_HAS_OPERATION",
            )
            add_arguments(
                operation_id,
                field.arguments,
                f"argument:{root_name}:{field.name}",
            )
            add_type_reference(
                operation_id,
                field.type_ref,
                "RETURNS",
            )

    for graphql_type in schema.types:
        type_id = type_ids[graphql_type.name]

        if graphql_type.name not in root_type_names:
            for field in graphql_type.fields:
                field_id = f"field:{graphql_type.name}.{field.name}"
                add_node(
                    ApiGraphNode(
                        node_id=field_id,
                        kind="FIELD",
                        name=field.name,
                        description=field.description,
                        type_ref=field.type_ref.render(),
                        is_deprecated=field.is_deprecated,
                    )
                )
                add_edge(type_id, field_id, "HAS_FIELD")
                add_arguments(
                    field_id,
                    field.arguments,
                    f"argument:field:{graphql_type.name}.{field.name}",
                )
                add_type_reference(
                    field_id,
                    field.type_ref,
                    "RETURNS",
                )

        for input_field in graphql_type.input_fields:
            input_id = (
                f"input-field:{graphql_type.name}.{input_field.name}"
            )
            add_node(
                ApiGraphNode(
                    node_id=input_id,
                    kind="INPUT_FIELD",
                    name=input_field.name,
                    description=input_field.description,
                    type_ref=input_field.type_ref.render(),
                )
            )
            add_edge(
                type_id,
                input_id,
                "HAS_INPUT_FIELD",
            )
            add_type_reference(
                input_id,
                input_field.type_ref,
                "INPUT_TYPE",
            )

        for enum_value in graphql_type.enum_values:
            enum_id = (
                f"enum-value:{graphql_type.name}.{enum_value.name}"
            )
            add_node(
                ApiGraphNode(
                    node_id=enum_id,
                    kind="ENUM_VALUE",
                    name=enum_value.name,
                    description=enum_value.description,
                    is_deprecated=enum_value.is_deprecated,
                )
            )
            add_edge(type_id, enum_id, "HAS_ENUM_VALUE")

        for interface in graphql_type.interfaces:
            named = interface.named_type()
            if named is None:
                continue
            target = type_ids.get(named)
            if target is not None:
                add_edge(
                    type_id,
                    target,
                    "IMPLEMENTS",
                    interface.render(),
                )

        for possible_type in graphql_type.possible_types:
            named = possible_type.named_type()
            if named is None:
                continue
            target = type_ids.get(named)
            if target is not None:
                add_edge(
                    type_id,
                    target,
                    "POSSIBLE_TYPE",
                    possible_type.render(),
                )

    for directive in schema.directives:
        directive_id = f"directive:{directive.name}"
        add_node(
            ApiGraphNode(
                node_id=directive_id,
                kind="DIRECTIVE",
                name=directive.name,
                description=directive.description,
            )
        )
        add_arguments(
            directive_id,
            directive.arguments,
            f"argument:directive:{directive.name}",
        )

    ordered_nodes = tuple(
        sorted(nodes.values(), key=lambda node: node.node_id)
    )
    ordered_edges = tuple(
        sorted(
            edges,
            key=lambda edge: (
                edge.source_id,
                edge.kind,
                edge.target_id,
                edge.label or "",
            ),
        )
    )

    node_kinds = [node.kind for node in ordered_nodes]

    coverage = ApiGraphCoverage(
        query_operations=node_kinds.count("QUERY_OPERATION"),
        mutation_operations=node_kinds.count("MUTATION_OPERATION"),
        subscription_operations=node_kinds.count(
            "SUBSCRIPTION_OPERATION"
        ),
        types=len(schema.types),
        object_types=sum(
            graphql_type.kind == "OBJECT"
            for graphql_type in schema.types
        ),
        input_objects=sum(
            graphql_type.kind == "INPUT_OBJECT"
            for graphql_type in schema.types
        ),
        enums=sum(
            graphql_type.kind == "ENUM"
            for graphql_type in schema.types
        ),
        scalars=sum(
            graphql_type.kind == "SCALAR"
            for graphql_type in schema.types
        ),
        interfaces=sum(
            graphql_type.kind == "INTERFACE"
            for graphql_type in schema.types
        ),
        unions=sum(
            graphql_type.kind == "UNION"
            for graphql_type in schema.types
        ),
        fields=node_kinds.count("FIELD"),
        arguments=node_kinds.count("ARGUMENT"),
        input_fields=node_kinds.count("INPUT_FIELD"),
        enum_values=node_kinds.count("ENUM_VALUE"),
        directives=node_kinds.count("DIRECTIVE"),
        nodes=len(ordered_nodes),
        edges=len(ordered_edges),
    )

    return CanonicalApiGraph(
        nodes=ordered_nodes,
        edges=ordered_edges,
        coverage=coverage,
    )
