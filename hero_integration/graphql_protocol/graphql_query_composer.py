from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    GraphQLArgument,
    GraphQLField,
    GraphQLSchema,
    GraphQLType,
)


class GraphQLQueryComposerError(ValueError):
    pass


@dataclass(frozen=True)
class GraphQLArgumentSpec:
    argument: GraphQLArgument

    @property
    def name(self) -> str:
        return self.argument.name

    @property
    def type_name(self) -> str:
        return self.argument.type_ref.render()

    @property
    def required(self) -> bool:
        return (
            self.argument.type_ref.kind == "NON_NULL"
            and self.argument.default_value is None
        )

    @property
    def default_value(self) -> str | None:
        return self.argument.default_value


@dataclass(frozen=True)
class GraphQLSelectionField:
    path: tuple[str, ...]
    field: GraphQLField
    parent_type_name: str

    @property
    def path_text(self) -> str:
        return ".".join(self.path)

    @property
    def return_type(self) -> str:
        return self.field.type_ref.render()

@dataclass(frozen=True)
class GraphQLQueryComposerModel:
    root_field: GraphQLField
    root_return_type: GraphQLType
    arguments: tuple[GraphQLArgumentSpec, ...]
    selectable_fields: tuple[GraphQLSelectionField, ...]

    def selection_named(
        self,
        path: str,
    ) -> GraphQLSelectionField | None:
        normalized = tuple(
            part
            for part in path.strip().split(".")
            if part
        )
        return next(
            (
                selection
                for selection in self.selectable_fields
                if selection.path == normalized
            ),
            None,
        )


def build_query_composer_model(
    schema: GraphQLSchema,
    root_field: GraphQLField,
    *,
    max_depth: int = 4,
) -> GraphQLQueryComposerModel:
    return _build_root_composer_model(
        schema,
        root_field,
        root_fields=schema.query_fields,
        root_label="query",
        max_depth=max_depth,
    )


def build_mutation_composer_model(
    schema: GraphQLSchema,
    root_field: GraphQLField,
    *,
    max_depth: int = 4,
) -> GraphQLQueryComposerModel:
    return _build_root_composer_model(
        schema,
        root_field,
        root_fields=schema.mutation_fields,
        root_label="mutation",
        max_depth=max_depth,
    )


def _build_root_composer_model(
    schema: GraphQLSchema,
    root_field: GraphQLField,
    *,
    root_fields: tuple[GraphQLField, ...],
    root_label: str,
    max_depth: int,
) -> GraphQLQueryComposerModel:
    if root_field not in root_fields:
        raise GraphQLQueryComposerError(
            f"GraphQL field {root_field.name!r} is not part of "
            f"the {root_label} root."
        )

    if max_depth < 1:
        raise GraphQLQueryComposerError(
            "Query composer max_depth must be at least 1."
        )

    root_type = _resolve_named_type(
        schema,
        root_field,
        context=f"{root_label} field {root_field.name!r}",
    )

    arguments = tuple(
        GraphQLArgumentSpec(argument)
        for argument in root_field.arguments
    )

    selectable_fields = _collect_selection_fields(
        schema,
        root_type,
        path=(),
        depth=1,
        max_depth=max_depth,
        ancestors=(root_type.name,),
    )

    return GraphQLQueryComposerModel(
        root_field=root_field,
        root_return_type=root_type,
        arguments=arguments,
        selectable_fields=selectable_fields,
    )




def _collect_selection_fields(
    schema: GraphQLSchema,
    graphql_type: GraphQLType,
    *,
    path: tuple[str, ...],
    depth: int,
    max_depth: int,
    ancestors: tuple[str, ...],
) -> tuple[GraphQLSelectionField, ...]:
    if graphql_type.kind in {"SCALAR", "ENUM"}:
        return ()

    if graphql_type.kind not in {"OBJECT", "INTERFACE"}:
        return ()

    selections: list[GraphQLSelectionField] = []

    for field in graphql_type.fields:
        current_path = path + (field.name,)
        selection = GraphQLSelectionField(
            path=current_path,
            field=field,
            parent_type_name=graphql_type.name,
        )
        selections.append(selection)

        if depth >= max_depth:
            continue

        named_type_name = field.type_ref.named_type()
        if named_type_name is None:
            continue

        child_type = schema.type_named(named_type_name)
        if child_type is None:
            continue

        if child_type.kind not in {"OBJECT", "INTERFACE"}:
            continue

        if child_type.name in ancestors:
            continue

        selections.extend(
            _collect_selection_fields(
                schema,
                child_type,
                path=current_path,
                depth=depth + 1,
                max_depth=max_depth,
                ancestors=ancestors + (child_type.name,),
            )
        )

    return tuple(selections)


def _resolve_named_type(
    schema: GraphQLSchema,
    field: GraphQLField,
    *,
    context: str,
) -> GraphQLType:
    named_type = field.type_ref.named_type()
    if named_type is None:
        raise GraphQLQueryComposerError(
            f"{context} has no named return type."
        )

    graphql_type = schema.type_named(named_type)
    if graphql_type is None:
        raise GraphQLQueryComposerError(
            f"Return type {named_type!r} for {context} is missing from schema."
        )

    return graphql_type
