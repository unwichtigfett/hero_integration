from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphQLTypeRef:
    kind: str
    name: str | None = None
    of_type: "GraphQLTypeRef | None" = None

    def named_type(self) -> str | None:
        current: GraphQLTypeRef | None = self
        while current is not None:
            if current.name:
                return current.name
            current = current.of_type
        return None

    def render(self) -> str:
        if self.kind == "NON_NULL":
            if self.of_type is None:
                raise ValueError("NON_NULL type reference requires of_type.")
            return f"{self.of_type.render()}!"

        if self.kind == "LIST":
            if self.of_type is None:
                raise ValueError("LIST type reference requires of_type.")
            return f"[{self.of_type.render()}]"

        if not self.name:
            raise ValueError(
                f"Named GraphQL type reference {self.kind!r} requires a name."
            )

        return self.name


@dataclass(frozen=True)
class GraphQLArgument:
    name: str
    description: str | None
    type_ref: GraphQLTypeRef
    default_value: str | None


@dataclass(frozen=True)
class GraphQLField:
    name: str
    description: str | None
    arguments: tuple[GraphQLArgument, ...]
    type_ref: GraphQLTypeRef
    is_deprecated: bool
    deprecation_reason: str | None


@dataclass(frozen=True)
class GraphQLEnumValue:
    name: str
    description: str | None
    is_deprecated: bool
    deprecation_reason: str | None


@dataclass(frozen=True)
class GraphQLType:
    kind: str
    name: str
    description: str | None
    fields: tuple[GraphQLField, ...] = ()
    input_fields: tuple[GraphQLArgument, ...] = ()
    enum_values: tuple[GraphQLEnumValue, ...] = ()
    interfaces: tuple[GraphQLTypeRef, ...] = ()
    possible_types: tuple[GraphQLTypeRef, ...] = ()


@dataclass(frozen=True)
class GraphQLDirective:
    name: str
    description: str | None
    locations: tuple[str, ...]
    arguments: tuple[GraphQLArgument, ...]
    is_repeatable: bool = False


@dataclass(frozen=True)
class GraphQLSchema:
    query_type_name: str
    mutation_type_name: str | None
    subscription_type_name: str | None
    types: tuple[GraphQLType, ...]
    directives: tuple[GraphQLDirective, ...] = ()

    def type_named(self, name: str) -> GraphQLType | None:
        return next(
            (graphql_type for graphql_type in self.types if graphql_type.name == name),
            None,
        )

    @property
    def query_type(self) -> GraphQLType:
        query_type = self.type_named(self.query_type_name)
        if query_type is None:
            raise LookupError(
                f"Query root type {self.query_type_name!r} is missing from schema."
            )
        return query_type

    @property
    def query_fields(self) -> tuple[GraphQLField, ...]:
        return self.query_type.fields

    @property
    def mutation_fields(self) -> tuple[GraphQLField, ...]:
        if self.mutation_type_name is None:
            return ()

        mutation_type = next(
            (
                item
                for item in self.types
                if item.name == self.mutation_type_name
            ),
            None,
        )

        if mutation_type is None:
            return ()

        return mutation_type.fields
