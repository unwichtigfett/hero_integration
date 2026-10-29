from __future__ import annotations

from dataclasses import dataclass

from .schema import GraphQLField, GraphQLSchema, GraphQLType


@dataclass(frozen=True, slots=True)
class StructuralTargetPath:
    target_type_name: str
    field_node_ids: tuple[str, ...]
    field_names: tuple[str, ...]
    collection: bool


_STRUCTURAL_FIELD_NAMES = frozenset(
    {
        "data",
        "edge",
        "edges",
        "item",
        "items",
        "node",
        "nodes",
        "result",
        "results",
        "value",
        "values",
    }
)
_TECHNICAL_FIELD_NAMES = frozenset(
    {"cursor", "hasnextpage", "haspreviouspage", "pageinfo", "totalcount"}
)


def resolve_structural_target(
    schema: GraphQLSchema,
    start_type_name: str,
    *,
    require_collection: bool,
    max_depth: int = 5,
) -> StructuralTargetPath | None:
    """Resolve a wrapper path using type shape before naming evidence."""

    if max_depth < 1:
        raise ValueError("Structural target max_depth must be at least 1.")
    start = schema.type_named(start_type_name)
    if start is None or start.kind not in {"OBJECT", "INTERFACE"}:
        return None
    if _entity_shape(schema, start):
        return None
    if not is_structural_wrapper(schema, start):
        return None

    candidates: list[StructuralTargetPath] = []

    def visit(
        current: GraphQLType,
        *,
        field_ids: tuple[str, ...],
        field_names: tuple[str, ...],
        collection: bool,
        ancestors: tuple[str, ...],
    ) -> None:
        if len(field_ids) >= max_depth:
            return
        for field in sorted(current.fields, key=lambda item: item.name):
            named = field.type_ref.named_type()
            target = schema.type_named(named) if named is not None else None
            if target is None or target.kind not in {"OBJECT", "INTERFACE"}:
                continue
            field_id = f"field:{current.name}.{field.name}"
            path_ids = (*field_ids, field_id)
            path_names = (*field_names, field.name)
            is_collection = collection or _is_list(field)
            if _entity_shape(schema, target) and (
                is_collection or not require_collection
            ):
                candidates.append(
                    StructuralTargetPath(
                        target_type_name=target.name,
                        field_node_ids=path_ids,
                        field_names=path_names,
                        collection=is_collection,
                    )
                )
            if target.name in ancestors:
                continue
            if _traversable_wrapper(schema, current, field, target):
                visit(
                    target,
                    field_ids=path_ids,
                    field_names=path_names,
                    collection=is_collection,
                    ancestors=(*ancestors, target.name),
                )

    visit(
        start,
        field_ids=(),
        field_names=(),
        collection=False,
        ancestors=(start.name,),
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            len(item.field_node_ids),
            item.field_node_ids,
            item.target_type_name,
        ),
    )


def is_structural_wrapper(
    schema: GraphQLSchema,
    graphql_type: GraphQLType,
) -> bool:
    if graphql_type.kind not in {"OBJECT", "INTERFACE"}:
        return False
    if _identity_fields(graphql_type):
        return False
    object_fields = tuple(
        field
        for field in graphql_type.fields
        if (
            (target_name := field.type_ref.named_type()) is not None
            and (target := schema.type_named(target_name)) is not None
            and target.kind in {"OBJECT", "INTERFACE", "UNION"}
        )
    )
    normalized_names = {_normalized(field.name) for field in graphql_type.fields}
    structural_name = bool(normalized_names & _STRUCTURAL_FIELD_NAMES)
    technical_metadata = bool(normalized_names & _TECHNICAL_FIELD_NAMES)
    exactly_one_object_result = len(object_fields) == 1 and len(
        _business_scalar_fields(schema, graphql_type)
    ) <= 1
    return structural_name or technical_metadata or exactly_one_object_result


def _traversable_wrapper(
    schema: GraphQLSchema,
    owner: GraphQLType,
    field: GraphQLField,
    target: GraphQLType,
) -> bool:
    return (
        _is_list(field)
        or _normalized(field.name) in _STRUCTURAL_FIELD_NAMES
        or not _entity_shape(schema, target)
        or len(_business_scalar_fields(schema, owner)) <= 1
    )


def _entity_shape(schema: GraphQLSchema, graphql_type: GraphQLType) -> bool:
    return bool(_identity_fields(graphql_type)) or len(
        _business_scalar_fields(schema, graphql_type)
    ) >= 2


def _identity_fields(graphql_type: GraphQLType) -> tuple[GraphQLField, ...]:
    return tuple(
        field
        for field in graphql_type.fields
        if field.type_ref.named_type() == "ID"
        and _normalized(field.name) in {"id", "key", "uuid"}
    )


def _business_scalar_fields(
    schema: GraphQLSchema,
    graphql_type: GraphQLType,
) -> tuple[GraphQLField, ...]:
    return tuple(
        field
        for field in graphql_type.fields
        if (
            (named := field.type_ref.named_type()) is not None
            and (target := schema.type_named(named)) is not None
            and target.kind in {"SCALAR", "ENUM"}
        )
        and _normalized(field.name) not in _TECHNICAL_FIELD_NAMES
        and not field.arguments
    )


def _is_list(field: GraphQLField) -> bool:
    current = field.type_ref
    while current is not None:
        if current.kind == "LIST":
            return True
        current = current.of_type
    return False


def _normalized(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


__all__ = [
    "StructuralTargetPath",
    "is_structural_wrapper",
    "resolve_structural_target",
]
