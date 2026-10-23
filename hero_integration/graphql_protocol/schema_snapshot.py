from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .schema import (
    GraphQLArgument,
    GraphQLDirective,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLSchema,
    GraphQLType,
    GraphQLTypeRef,
)


SCHEMA_SNAPSHOT_FORMAT_VERSION = 2
LEGACY_SCHEMA_SNAPSHOT_FORMAT_VERSION = 1
DEFAULT_SCHEMA_SNAPSHOT_FILENAME = "graphql_schema_snapshot.json"
DEFAULT_PREVIOUS_SCHEMA_SNAPSHOT_FILENAME = "graphql_schema_snapshot.previous.json"


class GraphQLSchemaSnapshotError(ValueError):
    pass


def schema_to_dict(schema: GraphQLSchema) -> dict[str, object]:
    result: dict[str, object] = {
        "query_type_name": schema.query_type_name,
        "mutation_type_name": schema.mutation_type_name,
        "subscription_type_name": schema.subscription_type_name,
        "types": [
            _type_to_dict(graphql_type)
            for graphql_type in sorted(
                schema.types,
                key=lambda item: (item.name, item.kind),
            )
        ],
    }

    if schema.directives:
        result["directives"] = [
            _directive_to_dict(directive)
            for directive in sorted(
                schema.directives,
                key=lambda item: item.name,
            )
        ]

    return result


def schema_from_dict(value: object) -> GraphQLSchema:
    item = _mapping(value, "schema")

    query_type_name = _required_string(
        item.get("query_type_name"),
        "schema.query_type_name",
    )
    mutation_type_name = _optional_string(
        item.get("mutation_type_name"),
        "schema.mutation_type_name",
    )
    subscription_type_name = _optional_string(
        item.get("subscription_type_name"),
        "schema.subscription_type_name",
    )

    raw_types = item.get("types")
    if not isinstance(raw_types, list):
        raise GraphQLSchemaSnapshotError("schema.types must be a list.")

    raw_directives = item.get("directives", [])
    if not isinstance(raw_directives, list):
        raise GraphQLSchemaSnapshotError(
            "schema.directives must be a list."
        )

    schema = GraphQLSchema(
        query_type_name=query_type_name,
        mutation_type_name=mutation_type_name,
        subscription_type_name=subscription_type_name,
        types=tuple(
            _type_from_dict(
                raw_type,
                f"schema.types[{index}]",
            )
            for index, raw_type in enumerate(raw_types)
        ),
        directives=tuple(
            _directive_from_dict(
                directive,
                f"schema.directives[{index}]",
            )
            for index, directive in enumerate(raw_directives)
        ),
    )

    if schema.type_named(query_type_name) is None:
        raise GraphQLSchemaSnapshotError(
            f"Query root type {query_type_name!r} is missing from snapshot."
        )

    return schema


def schema_fingerprint(schema: GraphQLSchema) -> str:
    canonical = json.dumps(
        schema_to_dict(schema),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class GraphQLSchemaSnapshot:
    schema: GraphQLSchema
    fingerprint: str
    format_version: int = SCHEMA_SNAPSHOT_FORMAT_VERSION
    connection_id: str | None = None
    endpoint_fingerprint: str | None = None
    complete_analysis: bool = False

    @classmethod
    def create(
        cls,
        schema: GraphQLSchema,
        *,
        connection_id: str | None = None,
        endpoint_fingerprint: str | None = None,
        complete_analysis: bool = False,
    ) -> "GraphQLSchemaSnapshot":
        normalized_connection = _optional_identity(
            connection_id,
            "snapshot.connection_id",
        )
        normalized_endpoint = _optional_identity(
            endpoint_fingerprint,
            "snapshot.endpoint_fingerprint",
        )
        if complete_analysis and (
            normalized_connection is None
            or normalized_endpoint is None
        ):
            raise GraphQLSchemaSnapshotError(
                "A complete API snapshot requires connection and endpoint "
                "identity bindings."
            )
        return cls(
            schema=schema,
            fingerprint=schema_fingerprint(schema),
            connection_id=normalized_connection,
            endpoint_fingerprint=normalized_endpoint,
            complete_analysis=bool(complete_analysis),
        )

    @classmethod
    def from_dict(cls, value: object) -> "GraphQLSchemaSnapshot":
        item = _mapping(value, "snapshot")

        format_version = item.get("format_version")
        if format_version not in {
            LEGACY_SCHEMA_SNAPSHOT_FORMAT_VERSION,
            SCHEMA_SNAPSHOT_FORMAT_VERSION,
        }:
            raise GraphQLSchemaSnapshotError(
                "Unsupported GraphQL schema snapshot format version: "
                f"{format_version!r}."
            )

        fingerprint = _required_string(
            item.get("fingerprint"),
            "snapshot.fingerprint",
        )
        schema = schema_from_dict(item.get("schema"))
        expected_fingerprint = schema_fingerprint(schema)

        if fingerprint != expected_fingerprint:
            raise GraphQLSchemaSnapshotError(
                "GraphQL schema snapshot fingerprint does not match its content."
            )

        if format_version == LEGACY_SCHEMA_SNAPSHOT_FORMAT_VERSION:
            return cls(
                schema=schema,
                fingerprint=fingerprint,
                format_version=LEGACY_SCHEMA_SNAPSHOT_FORMAT_VERSION,
                complete_analysis=False,
            )

        connection_id = _optional_identity(
            item.get("connection_id"),
            "snapshot.connection_id",
        )
        endpoint_fingerprint = _optional_identity(
            item.get("endpoint_fingerprint"),
            "snapshot.endpoint_fingerprint",
        )
        complete_analysis = item.get("complete_analysis")
        if not isinstance(complete_analysis, bool):
            raise GraphQLSchemaSnapshotError(
                "snapshot.complete_analysis must be a boolean."
            )
        if complete_analysis and (
            connection_id is None or endpoint_fingerprint is None
        ):
            raise GraphQLSchemaSnapshotError(
                "A complete API snapshot requires connection and endpoint "
                "identity bindings."
            )

        return cls(
            schema=schema,
            fingerprint=fingerprint,
            format_version=SCHEMA_SNAPSHOT_FORMAT_VERSION,
            connection_id=connection_id,
            endpoint_fingerprint=endpoint_fingerprint,
            complete_analysis=complete_analysis,
        )

    def to_dict(self) -> dict[str, object]:
        expected_fingerprint = schema_fingerprint(self.schema)
        if self.fingerprint != expected_fingerprint:
            raise GraphQLSchemaSnapshotError(
                "GraphQL schema snapshot fingerprint is stale."
            )

        if self.format_version == LEGACY_SCHEMA_SNAPSHOT_FORMAT_VERSION:
            return {
                "format_version": self.format_version,
                "fingerprint": self.fingerprint,
                "schema": schema_to_dict(self.schema),
            }
        if self.format_version != SCHEMA_SNAPSHOT_FORMAT_VERSION:
            raise GraphQLSchemaSnapshotError(
                "Unsupported GraphQL schema snapshot format version: "
                f"{self.format_version!r}."
            )
        if self.complete_analysis and (
            self.connection_id is None
            or self.endpoint_fingerprint is None
        ):
            raise GraphQLSchemaSnapshotError(
                "A complete API snapshot requires connection and endpoint "
                "identity bindings."
            )

        return {
            "format_version": SCHEMA_SNAPSHOT_FORMAT_VERSION,
            "fingerprint": self.fingerprint,
            "connection_id": self.connection_id,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "complete_analysis": self.complete_analysis,
            "schema": schema_to_dict(self.schema),
        }

    def is_complete_for(
        self,
        *,
        connection_id: str,
        endpoint_fingerprint: str,
    ) -> bool:
        """Return whether this snapshot is trusted for semantic restoration."""

        return (
            self.format_version == SCHEMA_SNAPSHOT_FORMAT_VERSION
            and self.complete_analysis
            and self.connection_id == connection_id
            and self.endpoint_fingerprint == endpoint_fingerprint
        )


class GraphQLSchemaSnapshotStore:
    def __init__(
        self,
        directory: Path | str,
        *,
        filename: str = DEFAULT_SCHEMA_SNAPSHOT_FILENAME,
        previous_filename: str = (
            DEFAULT_PREVIOUS_SCHEMA_SNAPSHOT_FILENAME
        ),
    ) -> None:
        self.directory = Path(directory)
        self.path = self.directory / filename
        self.previous_path = self.directory / previous_filename

    def load(self) -> GraphQLSchemaSnapshot | None:
        return self._load_path(
            self.path,
            label="GraphQL schema snapshot",
        )

    def load_previous(self) -> GraphQLSchemaSnapshot | None:
        return self._load_path(
            self.previous_path,
            label="Previous GraphQL schema snapshot",
        )

    @staticmethod
    def _load_path(
        path: Path,
        *,
        label: str,
    ) -> GraphQLSchemaSnapshot | None:
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphQLSchemaSnapshotError(
                f"{label} could not be read: {exc}"
            ) from exc

        return GraphQLSchemaSnapshot.from_dict(raw)

    def save(self, snapshot: GraphQLSchemaSnapshot) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

        current = self.load()

        # A narrower legacy/discovery snapshot must never silently destroy the
        # complete, connection-bound source needed for semantic restart.
        if (
            current is not None
            and current.complete_analysis
            and not snapshot.complete_analysis
        ):
            return

        if (
            current is not None
            and current.fingerprint != snapshot.fingerprint
        ):
            self._write_snapshot(
                self.previous_path,
                current,
                label="Previous GraphQL schema snapshot",
            )

        self._write_snapshot(
            self.path,
            snapshot,
            label="GraphQL schema snapshot",
        )

    @staticmethod
    def _write_snapshot(
        path: Path,
        snapshot: GraphQLSchemaSnapshot,
        *,
        label: str,
    ) -> None:
        payload = json.dumps(
            snapshot.to_dict(),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        temporary = path.with_suffix(path.suffix + ".tmp")

        try:
            temporary.write_text(
                payload + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise GraphQLSchemaSnapshotError(
                f"{label} could not be saved: {exc}"
            ) from exc


def _type_to_dict(graphql_type: GraphQLType) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": graphql_type.kind,
        "name": graphql_type.name,
        "description": graphql_type.description,
        "fields": [
            _field_to_dict(field)
            for field in sorted(
                graphql_type.fields,
                key=lambda item: item.name,
            )
        ],
    }

    optional_collections = (
        (
            "input_fields",
            graphql_type.input_fields,
            _argument_to_dict,
        ),
        (
            "enum_values",
            graphql_type.enum_values,
            _enum_value_to_dict,
        ),
        (
            "interfaces",
            graphql_type.interfaces,
            _type_ref_to_dict,
        ),
        (
            "possible_types",
            graphql_type.possible_types,
            _type_ref_to_dict,
        ),
    )

    for key, values, serializer in optional_collections:
        if not values:
            continue
        result[key] = [
            serializer(value)
            for value in sorted(
                values,
                key=_schema_item_sort_key,
            )
        ]

    return result



def _enum_value_to_dict(
    value: GraphQLEnumValue,
) -> dict[str, object]:
    return {
        "name": value.name,
        "description": value.description,
        "is_deprecated": value.is_deprecated,
        "deprecation_reason": value.deprecation_reason,
    }


def _directive_to_dict(
    directive: GraphQLDirective,
) -> dict[str, object]:
    return {
        "name": directive.name,
        "description": directive.description,
        "locations": sorted(directive.locations),
        "arguments": [
            _argument_to_dict(argument)
            for argument in sorted(
                directive.arguments,
                key=lambda item: item.name,
            )
        ],
        "is_repeatable": directive.is_repeatable,
    }


def _schema_item_sort_key(value: object) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name

    render = getattr(value, "render", None)
    if callable(render):
        rendered = render()
        if isinstance(rendered, str):
            return rendered

    raise TypeError("Unsupported schema collection item.")

def _field_to_dict(field: GraphQLField) -> dict[str, object]:
    return {
        "name": field.name,
        "description": field.description,
        "arguments": [
            _argument_to_dict(argument)
            for argument in sorted(
                field.arguments,
                key=lambda item: item.name,
            )
        ],
        "type_ref": _type_ref_to_dict(field.type_ref),
        "is_deprecated": field.is_deprecated,
        "deprecation_reason": field.deprecation_reason,
    }


def _argument_to_dict(argument: GraphQLArgument) -> dict[str, object]:
    return {
        "name": argument.name,
        "description": argument.description,
        "type_ref": _type_ref_to_dict(argument.type_ref),
        "default_value": argument.default_value,
    }


def _type_ref_to_dict(type_ref: GraphQLTypeRef) -> dict[str, object]:
    return {
        "kind": type_ref.kind,
        "name": type_ref.name,
        "of_type": (
            _type_ref_to_dict(type_ref.of_type)
            if type_ref.of_type is not None
            else None
        ),
    }


def _type_from_dict(value: object, path: str) -> GraphQLType:
    item = _mapping(value, path)

    raw_fields = item.get("fields")
    if not isinstance(raw_fields, list):
        raise GraphQLSchemaSnapshotError(f"{path}.fields must be a list.")

    return GraphQLType(
        kind=_required_string(item.get("kind"), f"{path}.kind"),
        name=_required_string(item.get("name"), f"{path}.name"),
        description=_optional_string(
            item.get("description"),
            f"{path}.description",
        ),
        fields=tuple(
            _field_from_dict(field, f"{path}.fields[{index}]")
            for index, field in enumerate(raw_fields)
        ),
        input_fields=_optional_snapshot_items(
            item,
            "input_fields",
            path,
            _argument_from_dict,
        ),
        enum_values=_optional_snapshot_items(
            item,
            "enum_values",
            path,
            _enum_value_from_dict,
        ),
        interfaces=_optional_snapshot_items(
            item,
            "interfaces",
            path,
            _type_ref_from_dict,
        ),
        possible_types=_optional_snapshot_items(
            item,
            "possible_types",
            path,
            _type_ref_from_dict,
        ),
    )



def _enum_value_from_dict(
    value: object,
    path: str,
) -> GraphQLEnumValue:
    item = _mapping(value, path)

    is_deprecated = item.get("is_deprecated")
    if not isinstance(is_deprecated, bool):
        raise GraphQLSchemaSnapshotError(
            f"{path}.is_deprecated must be a boolean."
        )

    return GraphQLEnumValue(
        name=_required_string(item.get("name"), f"{path}.name"),
        description=_optional_string(
            item.get("description"),
            f"{path}.description",
        ),
        is_deprecated=is_deprecated,
        deprecation_reason=_optional_string(
            item.get("deprecation_reason"),
            f"{path}.deprecation_reason",
        ),
    )


def _directive_from_dict(
    value: object,
    path: str,
) -> GraphQLDirective:
    item = _mapping(value, path)

    raw_locations = item.get("locations")
    if not isinstance(raw_locations, list):
        raise GraphQLSchemaSnapshotError(
            f"{path}.locations must be a list."
        )

    raw_arguments = item.get("arguments")
    if not isinstance(raw_arguments, list):
        raise GraphQLSchemaSnapshotError(
            f"{path}.arguments must be a list."
        )

    is_repeatable = item.get("is_repeatable")
    if not isinstance(is_repeatable, bool):
        raise GraphQLSchemaSnapshotError(
            f"{path}.is_repeatable must be a boolean."
        )

    return GraphQLDirective(
        name=_required_string(item.get("name"), f"{path}.name"),
        description=_optional_string(
            item.get("description"),
            f"{path}.description",
        ),
        locations=tuple(
            _required_string(
                location,
                f"{path}.locations[{index}]",
            )
            for index, location in enumerate(raw_locations)
        ),
        arguments=tuple(
            _argument_from_dict(
                argument,
                f"{path}.arguments[{index}]",
            )
            for index, argument in enumerate(raw_arguments)
        ),
        is_repeatable=is_repeatable,
    )


def _optional_snapshot_items(
    item: dict[str, object],
    key: str,
    path: str,
    parser: object,
) -> tuple:
    raw_values = item.get(key, [])
    if not isinstance(raw_values, list):
        raise GraphQLSchemaSnapshotError(
            f"{path}.{key} must be a list."
        )

    return tuple(
        parser(
            value,
            f"{path}.{key}[{index}]",
        )
        for index, value in enumerate(raw_values)
    )

def _field_from_dict(value: object, path: str) -> GraphQLField:
    item = _mapping(value, path)

    raw_arguments = item.get("arguments")
    if not isinstance(raw_arguments, list):
        raise GraphQLSchemaSnapshotError(
            f"{path}.arguments must be a list."
        )

    is_deprecated = item.get("is_deprecated")
    if not isinstance(is_deprecated, bool):
        raise GraphQLSchemaSnapshotError(
            f"{path}.is_deprecated must be a boolean."
        )

    return GraphQLField(
        name=_required_string(item.get("name"), f"{path}.name"),
        description=_optional_string(
            item.get("description"),
            f"{path}.description",
        ),
        arguments=tuple(
            _argument_from_dict(
                argument,
                f"{path}.arguments[{index}]",
            )
            for index, argument in enumerate(raw_arguments)
        ),
        type_ref=_type_ref_from_dict(
            item.get("type_ref"),
            f"{path}.type_ref",
        ),
        is_deprecated=is_deprecated,
        deprecation_reason=_optional_string(
            item.get("deprecation_reason"),
            f"{path}.deprecation_reason",
        ),
    )


def _argument_from_dict(value: object, path: str) -> GraphQLArgument:
    item = _mapping(value, path)

    return GraphQLArgument(
        name=_required_string(item.get("name"), f"{path}.name"),
        description=_optional_string(
            item.get("description"),
            f"{path}.description",
        ),
        type_ref=_type_ref_from_dict(
            item.get("type_ref"),
            f"{path}.type_ref",
        ),
        default_value=_optional_string(
            item.get("default_value"),
            f"{path}.default_value",
        ),
    )


def _type_ref_from_dict(value: object, path: str) -> GraphQLTypeRef:
    item = _mapping(value, path)

    kind = _required_string(item.get("kind"), f"{path}.kind")
    name = _optional_string(item.get("name"), f"{path}.name")
    raw_of_type = item.get("of_type")
    of_type = (
        _type_ref_from_dict(raw_of_type, f"{path}.of_type")
        if raw_of_type is not None
        else None
    )

    if kind in {"NON_NULL", "LIST"}:
        if of_type is None:
            raise GraphQLSchemaSnapshotError(
                f"{path}.{kind} requires of_type."
            )
    elif name is None:
        raise GraphQLSchemaSnapshotError(
            f"{path} named type {kind!r} requires a name."
        )

    return GraphQLTypeRef(
        kind=kind,
        name=name,
        of_type=of_type,
    )


def _mapping(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GraphQLSchemaSnapshotError(f"{path} must be an object.")
    return value


def _required_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise GraphQLSchemaSnapshotError(
            f"{path} must be a non-empty string."
        )
    return value


def _optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GraphQLSchemaSnapshotError(
            f"{path} must be a string or null."
        )
    return value


def _optional_identity(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GraphQLSchemaSnapshotError(
            f"{path} must be null or a non-empty string."
        )
    if value != value.strip() or any(
        character.isspace() for character in value
    ):
        raise GraphQLSchemaSnapshotError(
            f"{path} must not contain whitespace."
        )
    return value
