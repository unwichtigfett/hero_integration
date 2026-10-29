"""Generic decoding of sealed HERO GraphQL operation results.

The operation materializer is authoritative for response paths.  This module
does not infer schema paths, does not persist Process-Core data and does not
apply domain-specific identity/cardinality rules.

A GraphQL response may contain collection nodes between the response root and
a selected scalar leaf.  In that case the decoder projects the remaining path
over every collection item while preserving order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
from types import MappingProxyType

from .graphql_response import (
    parse_graphql_response,
    strict_json_loads,
)
from .transport import Response
from .graphql_operation_materializer import (
    GraphQLOperationRequest,
    GraphQLResultBinding,
)


class GraphQLResultDecodeError(RuntimeError):
    """Fail-closed generic GraphQL result decoding error."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code:
            raise TypeError("code must be a non-empty string.")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GraphQLDecodedValue:
    """One decoded value tied to its sealed result binding."""

    binding: GraphQLResultBinding
    value: object


@dataclass(frozen=True, slots=True)
class GraphQLDecodedResult:
    """Immutable decoded result of one sealed GraphQL operation request."""

    values: tuple[GraphQLDecodedValue, ...]

    def value_for(
        self,
        selection_path: tuple[str, ...],
    ) -> object:
        if not isinstance(selection_path, tuple) or not all(
            isinstance(part, str) and part
            for part in selection_path
        ):
            if selection_path != ():
                raise TypeError(
                    "selection_path must be a tuple of non-empty strings."
                )

        matches = tuple(
            item
            for item in self.values
            if item.binding.selection_path == selection_path
        )

        if len(matches) != 1:
            raise GraphQLResultDecodeError(
                "RESULT_BINDING_NOT_UNIQUE"
            )

        return matches[0].value

    def selection_map(self):
        """Return an immutable selection-path -> value projection."""

        return MappingProxyType(
            {
                item.binding.selection_path: item.value
                for item in self.values
            }
        )


def _freeze(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)

    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze(item)
                for key, item in value.items()
            }
        )

    return value


def _project_path(
    value: object,
    path: tuple[str, ...],
    index: int = 0,
) -> tuple[object, int]:
    """Resolve a response path and project it through collections.

    The returned integer is the number of collection projection dimensions
    introduced while traversing *before* reaching the bound GraphQL leaf.

    Lists that are themselves the bound leaf therefore do not count as
    projection dimensions.
    """

    if index == len(path):
        return value, 0

    if value is None:
        # Nullable parent: every deeper selected child is effectively null.
        return None, 0

    if isinstance(value, list):
        projected: list[object] = []
        child_depth = 0

        for item in value:
            child_value, depth = _project_path(
                item,
                path,
                index,
            )
            projected.append(child_value)
            child_depth = max(child_depth, depth)

        return projected, child_depth + 1

    if not isinstance(value, Mapping):
        raise GraphQLResultDecodeError(
            "RESULT_RESPONSE_PATH_INVALID"
        )

    key = path[index]

    if key not in value:
        raise GraphQLResultDecodeError(
            "RESULT_RESPONSE_PATH_MISSING"
        )

    return _project_path(
        value[key],
        path,
        index + 1,
    )


def _named_type(value: object, name: str) -> None:
    if value is None:
        return

    if name == "String":
        if not isinstance(value, str):
            raise GraphQLResultDecodeError(
                "RESULT_TYPE_INVALID"
            )
        return

    if name == "Boolean":
        if type(value) is not bool:
            raise GraphQLResultDecodeError(
                "RESULT_TYPE_INVALID"
            )
        return

    if name == "Int":
        if (
            type(value) is not int
            or value < -(2**31)
            or value > (2**31 - 1)
        ):
            raise GraphQLResultDecodeError(
                "RESULT_TYPE_INVALID"
            )
        return

    if name == "Float":
        if (
            type(value) not in {int, float}
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise GraphQLResultDecodeError(
                "RESULT_TYPE_INVALID"
            )
        return

    if name == "ID":
        if not (
            isinstance(value, str)
            or (
                type(value) is int
                and not isinstance(value, bool)
            )
        ):
            raise GraphQLResultDecodeError(
                "RESULT_TYPE_INVALID"
            )
        return

    # Enum and custom scalar values are leaf values.  Without the schema the
    # decoder deliberately does not guess custom coercion semantics, but it
    # still rejects structured objects at a scalar/enum result binding.
    if isinstance(value, (Mapping, list)):
        raise GraphQLResultDecodeError(
            "RESULT_TYPE_INVALID"
        )

    if type(value) not in {
        str,
        int,
        float,
        bool,
    }:
        raise GraphQLResultDecodeError(
            "RESULT_TYPE_INVALID"
        )


def _validate_graphql_type(
    value: object,
    type_text: str,
) -> None:
    if not isinstance(type_text, str) or not type_text.strip():
        raise GraphQLResultDecodeError(
            "RESULT_GRAPHQL_TYPE_INVALID"
        )

    text = type_text.strip()

    # Nullability is already governed by GraphQL execution semantics.  A
    # nullable ancestor may legitimately make a deeper non-null leaf absent,
    # therefore decoding preserves null instead of second-guessing bubbling.
    if value is None:
        return

    if text.endswith("!"):
        inner = text[:-1].strip()
        if not inner:
            raise GraphQLResultDecodeError(
                "RESULT_GRAPHQL_TYPE_INVALID"
            )
        _validate_graphql_type(value, inner)
        return

    if text.startswith("["):
        if not text.endswith("]"):
            raise GraphQLResultDecodeError(
                "RESULT_GRAPHQL_TYPE_INVALID"
            )

        inner = text[1:-1].strip()

        if not inner:
            raise GraphQLResultDecodeError(
                "RESULT_GRAPHQL_TYPE_INVALID"
            )

        if not isinstance(value, list):
            raise GraphQLResultDecodeError(
                "RESULT_TYPE_INVALID"
            )

        for item in value:
            _validate_graphql_type(
                item,
                inner,
            )
        return

    if any(token in text for token in "[]!"):
        raise GraphQLResultDecodeError(
            "RESULT_GRAPHQL_TYPE_INVALID"
        )

    _named_type(value, text)


def _validate_projected(
    value: object,
    graphql_type: str,
    projection_depth: int,
) -> None:
    if projection_depth < 0:
        raise GraphQLResultDecodeError(
            "RESULT_PROJECTION_INVALID"
        )

    if projection_depth == 0:
        _validate_graphql_type(
            value,
            graphql_type,
        )
        return

    if value is None:
        return

    if not isinstance(value, list):
        raise GraphQLResultDecodeError(
            "RESULT_PROJECTION_INVALID"
        )

    for item in value:
        _validate_projected(
            item,
            graphql_type,
            projection_depth - 1,
        )


def decode_graphql_result(
    request: GraphQLOperationRequest,
    response: Response,
) -> GraphQLDecodedResult:
    """Decode one transport response through sealed result bindings.

    No domain-specific cardinality, identity or Process-Core mapping rules are
    applied here.  Those remain responsibilities of the calling adapter layer.
    """

    if not isinstance(
        request,
        GraphQLOperationRequest,
    ):
        raise TypeError(
            "request must be a GraphQLOperationRequest."
        )

    if not isinstance(response, Response):
        raise TypeError(
            "response must be a Response."
        )

    if not response.transport_succeeded:
        raise GraphQLResultDecodeError(
            "RESULT_TRANSPORT_FAILED"
        )

    try:
        envelope = parse_graphql_response(
            response.payload
        )
    except (
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise GraphQLResultDecodeError(
            "RESULT_RESPONSE_INVALID"
        ) from None

    if not response.success or envelope.errors:
        raise GraphQLResultDecodeError(
            "RESULT_GRAPHQL_ERROR"
        )

    if not envelope.data_present:
        raise GraphQLResultDecodeError(
            "RESULT_DATA_MISSING"
        )

    try:
        payload = strict_json_loads(
            response.payload
        )
    except (
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise GraphQLResultDecodeError(
            "RESULT_RESPONSE_INVALID"
        ) from None

    if not isinstance(payload, Mapping):
        raise GraphQLResultDecodeError(
            "RESULT_RESPONSE_INVALID"
        )

    if not request.result_bindings:
        raise GraphQLResultDecodeError(
            "RESULT_BINDINGS_EMPTY"
        )

    selection_paths = tuple(
        item.selection_path
        for item in request.result_bindings
    )

    if len(selection_paths) != len(
        set(selection_paths)
    ):
        raise GraphQLResultDecodeError(
            "RESULT_BINDING_DUPLICATE"
        )

    decoded: list[GraphQLDecodedValue] = []

    for binding in request.result_bindings:
        if (
            not isinstance(
                binding.response_path,
                tuple,
            )
            or not binding.response_path
            or any(
                not isinstance(part, str)
                or not part
                for part
                in binding.response_path
            )
            or binding.response_path[0]
            != "data"
        ):
            raise GraphQLResultDecodeError(
                "RESULT_BINDING_INVALID"
            )

        value, projection_depth = (
            _project_path(
                payload,
                binding.response_path,
            )
        )

        _validate_projected(
            value,
            binding.graphql_type,
            projection_depth,
        )

        decoded.append(
            GraphQLDecodedValue(
                binding=binding,
                value=_freeze(value),
            )
        )

    return GraphQLDecodedResult(
        values=tuple(decoded)
    )


__all__ = [
    "GraphQLResultDecodeError",
    "GraphQLDecodedValue",
    "GraphQLDecodedResult",
    "decode_graphql_result",
]
