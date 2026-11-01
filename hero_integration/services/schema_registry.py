"""Pure, deterministic catalog of a validated HERO GraphQL schema."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from hero_integration.services.graphql_graph import GraphSnapshotError, normalize_schema
from hero_integration.services.mapping_contract_v1 import (
    MappingContractError,
    SnapshotAdapter,
    StoredSnapshot,
)


class SchemaRegistryError(ValueError):
    """The saved schema cannot be represented without ambiguity or inference."""


_NAME = re.compile(r"^[_A-Za-z][_0-9A-Za-z]*$")
_NAMED_KINDS = {"SCALAR", "OBJECT", "INPUT_OBJECT", "ENUM", "INTERFACE", "UNION"}
MAX_REFERENCE_PATH_DEPTH = 3
MAX_REFERENCE_PATHS_PER_OPERATION = 512


@dataclass(frozen=True)
class SchemaTypeReference:
    graphql_type: str
    named_type: str
    named_kind: str
    wrappers: tuple[str, ...]

    @property
    def required(self) -> bool:
        return bool(self.wrappers and self.wrappers[0] == "NON_NULL")

    @property
    def list_depth(self) -> int:
        return self.wrappers.count("LIST")


@dataclass(frozen=True)
class SchemaArgument:
    reference_id: str
    name: str
    type_ref: SchemaTypeReference
    deprecated: bool
    deprecation_reason: str
    operation_kind: str = ""
    source_fingerprint: str = ""


@dataclass(frozen=True)
class SchemaField:
    reference_id: str
    path: tuple[str, ...]
    type_ref: SchemaTypeReference
    deprecated: bool
    deprecation_reason: str
    cycle_boundary: bool = False
    operation_kind: str = ""
    source_fingerprint: str = ""


@dataclass(frozen=True)
class SchemaMember:
    name: str
    type_ref: SchemaTypeReference
    deprecated: bool
    deprecation_reason: str
    arguments: tuple[SchemaArgument, ...] = ()


@dataclass(frozen=True)
class SchemaEnumValue:
    name: str
    deprecated: bool
    deprecation_reason: str


@dataclass(frozen=True)
class SchemaType:
    name: str
    kind: str
    fields: tuple[SchemaMember, ...]
    input_fields: tuple[SchemaMember, ...]
    enum_values: tuple[str, ...]
    enum_members: tuple[SchemaEnumValue, ...]
    possible_types: tuple[str, ...]
    interfaces: tuple[str, ...]


@dataclass(frozen=True)
class SchemaOperation:
    kind: str
    name: str
    root_type: str
    return_type: SchemaTypeReference
    arguments: tuple[SchemaArgument, ...]
    output_fields: tuple[SchemaField, ...]
    input_fields: tuple[SchemaField, ...]
    output_paths_truncated: bool
    input_paths_truncated: bool
    source_fingerprint: str = ""


@dataclass(frozen=True)
class SchemaRegistry:
    snapshot_name: str
    fingerprint: str
    source_instance: str
    types: tuple[SchemaType, ...]
    query_operations: tuple[SchemaOperation, ...]
    mutation_operations: tuple[SchemaOperation, ...]
    execution_authority: str = "DISCOVERED_ONLY"
    max_reference_path_depth: int = MAX_REFERENCE_PATH_DEPTH
    max_reference_paths_per_operation: int = MAX_REFERENCE_PATHS_PER_OPERATION

    def type(self, name: str) -> SchemaType:
        for item in self.types:
            if item.name == name:
                return item
        raise SchemaRegistryError(f"Unknown cataloged GraphQL type: {name}")

    def operation(self, kind: str, name: str) -> SchemaOperation:
        collection = {
            "QUERY": self.query_operations,
            "MUTATION": self.mutation_operations,
        }.get(kind.upper())
        if collection is None:
            raise SchemaRegistryError("Operation kind must be QUERY or MUTATION.")
        for item in collection:
            if item.name == name:
                return item
        raise SchemaRegistryError(f"Unknown cataloged {kind.upper()} operation: {name}")


def build_schema_registry(snapshot: StoredSnapshot) -> SchemaRegistry:
    """Build a discovery catalog with bounded paths from a validated snapshot.

    This function has no Frappe, transport, persistence or execution dependency.
    Discovery is non-authoritative; path depth and count are bounded separately.
    The truncation flags reflect exhaustion of the count budget, not depth or cycles.
    """
    if not isinstance(snapshot, StoredSnapshot):
        raise SchemaRegistryError("A validated stored snapshot is required.")
    try:
        validated = SnapshotAdapter().adapt(snapshot)
        raw = _snapshot_payload(snapshot.snapshot_json)
        schema = normalize_schema(raw)
    except (GraphSnapshotError, MappingContractError, TypeError, ValueError) as error:
        raise SchemaRegistryError(str(error)) from error
    if validated.fingerprint != snapshot.fingerprint:
        raise SchemaRegistryError("Snapshot fingerprint validation failed.")

    types = _type_index(schema)
    query_root = _root_type(schema, "queryType", types, "QUERY")
    mutation_root = _root_type(schema, "mutationType", types, "MUTATION")
    cataloged_types = tuple(_catalog_type(item, types) for _, item in sorted(types.items()))
    query_operations = _with_reference_metadata(
        _catalog_operations("QUERY", query_root, types), validated.fingerprint
    )
    mutation_operations = _with_reference_metadata(
        _catalog_operations("MUTATION", mutation_root, types), validated.fingerprint
    )
    _assert_unique_references(query_operations, mutation_operations)
    return SchemaRegistry(
        snapshot_name=snapshot.name,
        fingerprint=validated.fingerprint,
        source_instance=snapshot.source_instance,
        types=cataloged_types,
        query_operations=query_operations,
        mutation_operations=mutation_operations,
    )


def _snapshot_payload(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        import json

        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return parsed
    raise SchemaRegistryError("Snapshot JSON must be an object.")


def _type_index(schema: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw_types = schema.get("types")
    if not isinstance(raw_types, list):
        raise SchemaRegistryError("GraphQL schema types must be a list.")
    result: dict[str, Mapping[str, object]] = {}
    for item in raw_types:
        if not isinstance(item, Mapping):
            raise SchemaRegistryError("GraphQL schema contains a malformed type.")
        name, kind = item.get("name"), item.get("kind")
        _validate_name(name, "type")
        if not isinstance(kind, str) or kind not in _NAMED_KINDS:
            raise SchemaRegistryError(f"Unsupported GraphQL type kind for {name!r}.")
        if name in result:
            raise SchemaRegistryError(f"Duplicate GraphQL type: {name}")
        result[name] = item
    return result


def _root_type(
    schema: Mapping[str, object], key: str, types: Mapping[str, Mapping[str, object]], kind: str
) -> Mapping[str, object]:
    value = schema.get(key)
    name = _named_type(value)
    if not name or name not in types or types[name].get("kind") != "OBJECT":
        raise SchemaRegistryError(f"GraphQL schema has no valid {kind} root type.")
    return types[name]


def _catalog_type(item: Mapping[str, object], types: Mapping[str, Mapping[str, object]]) -> SchemaType:
    name, kind = str(item["name"]), str(item["kind"])
    fields = tuple(
        _member(field, types, f"type {name}")
        for field in _members(item.get("fields"), f"fields for {name}")
    )
    input_fields = tuple(
        _member(field, types, f"input fields for {name}")
        for field in _members(item.get("inputFields"), f"input fields for {name}")
    )
    enum_members = tuple(
        _enum_member(value, name)
        for value in _members(item.get("enumValues"), f"enum values for {name}")
    )
    possible_types = _named_references(item.get("possibleTypes"), types, "possible type", {"OBJECT"})
    interfaces = _named_references(item.get("interfaces"), types, "interface", {"INTERFACE"})
    return SchemaType(
        name=name,
        kind=kind,
        fields=fields,
        input_fields=input_fields,
        enum_values=tuple(member.name for member in enum_members),
        enum_members=enum_members,
        possible_types=possible_types,
        interfaces=interfaces,
    )


def _with_reference_metadata(
    operations: tuple[SchemaOperation, ...], fingerprint: str
) -> tuple[SchemaOperation, ...]:
    return tuple(
        replace(
            operation,
            arguments=tuple(
                replace(item, operation_kind=operation.kind, source_fingerprint=fingerprint)
                for item in operation.arguments
            ),
            output_fields=tuple(
                replace(item, operation_kind=operation.kind, source_fingerprint=fingerprint)
                for item in operation.output_fields
            ),
            input_fields=tuple(
                replace(item, operation_kind=operation.kind, source_fingerprint=fingerprint)
                for item in operation.input_fields
            ),
            source_fingerprint=fingerprint,
        )
        for operation in operations
    )


def _catalog_operations(
    kind: str, root: Mapping[str, object], types: Mapping[str, Mapping[str, object]]
) -> tuple[SchemaOperation, ...]:
    root_name = str(root["name"])
    operations = []
    for field in _members(root.get("fields"), f"fields for {kind} root"):
        name = field.get("name")
        _validate_name(name, f"{kind} operation")
        if kind == "MUTATION" and name == "result":
            raise SchemaRegistryError("Mutation operation name result is reserved for return references.")
        return_type = _type_reference(field.get("type"), types)
        arguments = tuple(
            _argument(kind, str(name), argument, types)
            for argument in _members(field.get("args"), f"arguments for {name}")
        )
        if kind == "MUTATION" and any(argument.name == "result" for argument in arguments):
            raise SchemaRegistryError("Mutation argument result collides with return references.")
        prefix = f"hero.{kind.lower()}.{name}"
        output_budget = [MAX_REFERENCE_PATHS_PER_OPERATION]
        output_prefix = f"{prefix}.result" if kind == "MUTATION" else prefix
        output_fields = tuple(_walk_output(output_prefix, return_type, types, (), budget=output_budget))
        output_paths_truncated = output_budget[0] == 0
        input_budget = [MAX_REFERENCE_PATHS_PER_OPERATION]
        input_fields = tuple(
            reference
            for argument in arguments
            if argument.type_ref.named_kind == "INPUT_OBJECT"
            for reference in _walk_input(prefix, argument.name, argument.type_ref, types, (), budget=input_budget)
        )
        input_paths_truncated = input_budget[0] == 0
        operations.append(
            SchemaOperation(
                kind, str(name), root_name, return_type, arguments, output_fields, input_fields,
                output_paths_truncated, input_paths_truncated,
            )
        )
    return tuple(sorted(operations, key=lambda item: item.name))


def _argument(
    kind: str, operation: str, value: Mapping[str, object], types: Mapping[str, Mapping[str, object]]
) -> SchemaArgument:
    name = value.get("name")
    _validate_name(name, f"argument for {operation}")
    prefix = f"hero.{kind.lower()}.{operation}"
    reference = f"{prefix}.{name}" if kind == "MUTATION" else f"{prefix}.argument.{name}"
    deprecated, reason = _deprecation(value)
    return SchemaArgument(reference, str(name), _type_reference(value.get("type"), types), deprecated, reason)


def _walk_output(
    prefix: str,
    type_ref: SchemaTypeReference,
    types: Mapping[str, Mapping[str, object]],
    ancestors: tuple[str, ...],
    depth: int = 0,
    budget: list[int] | None = None,
) -> tuple[SchemaField, ...]:
    if budget is None:
        budget = [MAX_REFERENCE_PATHS_PER_OPERATION]
    if type_ref.named_kind not in {"OBJECT", "INTERFACE"} or depth >= MAX_REFERENCE_PATH_DEPTH:
        return ()
    current = types[type_ref.named_type]
    if type_ref.named_type in ancestors:
        return ()
    next_ancestors = (*ancestors, type_ref.named_type)
    result: list[SchemaField] = []
    for field in _members(current.get("fields"), f"fields for {type_ref.named_type}"):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        name = field.get("name")
        _validate_name(name, f"field for {type_ref.named_type}")
        nested = _type_reference(field.get("type"), types)
        deprecated, reason = _deprecation(field)
        path = (str(name),)
        result.append(SchemaField(f"{prefix}.{name}", path, nested, deprecated, reason, nested.named_type in next_ancestors))
        if nested.named_type not in next_ancestors:
            for child in _walk_output(prefix, nested, types, next_ancestors, depth + 1, budget):
                result.append(
                    SchemaField(
                        f"{prefix}.{name}.{".".join(child.path)}",
                        (str(name), *child.path),
                        child.type_ref,
                        child.deprecated,
                        child.deprecation_reason,
                        child.cycle_boundary,
                    )
                )
    return tuple(result)


def _walk_input(
    prefix: str,
    argument: str,
    type_ref: SchemaTypeReference,
    types: Mapping[str, Mapping[str, object]],
    ancestors: tuple[str, ...],
    depth: int = 0,
    budget: list[int] | None = None,
) -> tuple[SchemaField, ...]:
    if budget is None:
        budget = [MAX_REFERENCE_PATHS_PER_OPERATION]
    if type_ref.named_kind != "INPUT_OBJECT" or type_ref.named_type in ancestors or depth >= MAX_REFERENCE_PATH_DEPTH:
        return ()
    current = types[type_ref.named_type]
    next_ancestors = (*ancestors, type_ref.named_type)
    result: list[SchemaField] = []
    for field in _members(current.get("inputFields"), f"input fields for {type_ref.named_type}"):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        name = field.get("name")
        _validate_name(name, f"input field for {type_ref.named_type}")
        nested = _type_reference(field.get("type"), types)
        deprecated, reason = _deprecation(field)
        path = (str(name),)
        result.append(SchemaField(f"{prefix}.{argument}.{name}", path, nested, deprecated, reason, nested.named_type in next_ancestors))
        if nested.named_type not in next_ancestors:
            for child in _walk_input(prefix, argument, nested, types, next_ancestors, depth + 1, budget):
                result.append(
                    SchemaField(
                        f"{prefix}.{argument}.{name}.{".".join(child.path)}",
                        (str(name), *child.path),
                        child.type_ref,
                        child.deprecated,
                        child.deprecation_reason,
                        child.cycle_boundary,
                    )
                )
    return tuple(result)


def _member(value: Mapping[str, object], types: Mapping[str, Mapping[str, object]], label: str) -> SchemaMember:
    name = value.get("name")
    _validate_name(name, label)
    deprecated, reason = _deprecation(value)
    arguments = tuple(
        _argument("QUERY", str(name), argument, types)
        for argument in _members(value.get("args"), f"arguments for {name}")
    )
    return SchemaMember(str(name), _type_reference(value.get("type"), types), deprecated, reason, arguments)


def _enum_member(value: Mapping[str, object], type_name: str) -> SchemaEnumValue:
    name = value.get("name")
    _validate_name(name, f"enum value for {type_name}")
    deprecated, reason = _deprecation(value)
    return SchemaEnumValue(str(name), deprecated, reason)


def _type_reference(value: object, types: Mapping[str, Mapping[str, object]]) -> SchemaTypeReference:
    wrappers: list[str] = []
    current = value
    while isinstance(current, Mapping) and current.get("kind") in {"NON_NULL", "LIST"}:
        kind = current["kind"]
        nested = current.get("ofType")
        if not isinstance(nested, Mapping):
            raise SchemaRegistryError(f"GraphQL {kind} wrapper has no nested type.")
        wrappers.append(str(kind))
        current = nested
    if not isinstance(current, Mapping):
        raise SchemaRegistryError("GraphQL type reference is malformed.")
    name, kind = current.get("name"), current.get("kind")
    _validate_name(name, "type reference")
    if not isinstance(kind, str) or kind not in _NAMED_KINDS:
        raise SchemaRegistryError(f"Unsupported GraphQL type reference for {name!r}.")
    declared = types.get(str(name))
    if declared is None:
        raise SchemaRegistryError(f"GraphQL schema references unknown type {name!r}.")
    if declared.get("kind") != kind:
        raise SchemaRegistryError(f"GraphQL type kind mismatch for {name!r}.")
    return SchemaTypeReference(_render_type(value), str(name), kind, tuple(wrappers))


def _render_type(value: object) -> str:
    if not isinstance(value, Mapping):
        raise SchemaRegistryError("GraphQL type reference is malformed.")
    kind = value.get("kind")
    if kind == "NON_NULL":
        return f"{_render_type(value.get('ofType'))}!"
    if kind == "LIST":
        return f"[{_render_type(value.get('ofType'))}]"
    name = value.get("name")
    _validate_name(name, "type reference")
    return str(name)


def _named_references(
    values: object,
    types: Mapping[str, Mapping[str, object]],
    label: str,
    allowed_kinds: set[str],
) -> tuple[str, ...]:
    names = []
    for value in _members(values, label):
        name = _named_type(value)
        if not name or name not in types or types[name].get("kind") not in allowed_kinds:
            raise SchemaRegistryError(f"GraphQL {label} has an invalid type reference.")
        names.append(name)
    if len(set(names)) != len(names):
        raise SchemaRegistryError(f"GraphQL {label} contains duplicates.")
    return tuple(sorted(names))


def _named_type(value: object) -> str:
    current = value
    while isinstance(current, Mapping):
        name = current.get("name")
        if isinstance(name, str) and name:
            return name
        current = current.get("ofType")
    return ""


def _members(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise SchemaRegistryError(f"GraphQL {label} must be a list of objects.")
    names = [item.get("name") for item in value]
    if len(names) != len(set(names)):
        raise SchemaRegistryError(f"GraphQL {label} contains duplicate names.")
    return tuple(sorted(value, key=lambda item: str(item.get("name") or "")))


def _deprecation(value: Mapping[str, object]) -> tuple[bool, str]:
    deprecated = value.get("isDeprecated", False)
    if not isinstance(deprecated, bool):
        raise SchemaRegistryError("GraphQL deprecation flag must be boolean.")
    reason = value.get("deprecationReason") or ""
    if not isinstance(reason, str):
        raise SchemaRegistryError("GraphQL deprecation reason must be text.")
    return deprecated, reason


def _validate_name(value: object, label: str) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SchemaRegistryError(f"Invalid GraphQL {label} name.")


def _assert_unique_references(*collections: tuple[SchemaOperation, ...]) -> None:
    references: set[str] = set()
    for operations in collections:
        for operation in operations:
            for item in (*operation.arguments, *operation.output_fields, *operation.input_fields):
                if item.reference_id in references:
                    raise SchemaRegistryError(f"Ambiguous GraphQL reference identifier: {item.reference_id}")
                references.add(item.reference_id)
