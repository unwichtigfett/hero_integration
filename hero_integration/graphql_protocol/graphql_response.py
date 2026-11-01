from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json


class GraphQLResponseEnvelopeError(ValueError):
    """Malformed GraphQL JSON envelope or error metadata."""


def _reject_constant(name: str) -> object:
    raise ValueError(f"JSON constant {name} is not valid GraphQL data.")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains a duplicate key.")
        result[key] = value
    return result


def strict_json_loads(text: str | bytes) -> object:
    """Reject NaN/Infinity tokens and duplicate keys; numeric overflow is not checked here."""
    return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


@dataclass(frozen=True, slots=True)
class GraphQLResponseError:
    message: str
    path: tuple[str | int, ...] | None


@dataclass(frozen=True, slots=True)
class GraphQLResponseEnvelope:
    data_present: bool
    data: object
    errors: tuple[GraphQLResponseError, ...]


def parse_graphql_response(
    response_body: str,
) -> GraphQLResponseEnvelope:
    """Parse one GraphQL envelope while preserving missing data versus null."""

    if not isinstance(response_body, str):
        raise TypeError("response_body must be JSON text.")
    try:
        envelope = strict_json_loads(response_body)
    except json.JSONDecodeError as exc:
        raise GraphQLResponseEnvelopeError(
            f"GraphQL response is not valid JSON: {exc.msg}."
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise GraphQLResponseEnvelopeError("GraphQL response is not valid GraphQL JSON.") from exc
    if not isinstance(envelope, Mapping):
        raise GraphQLResponseEnvelopeError(
            "GraphQL response envelope must be an object."
        )

    errors = _parse_errors(envelope)
    return GraphQLResponseEnvelope(
        data_present="data" in envelope,
        data=envelope.get("data"),
        errors=errors,
    )


def _parse_errors(
    envelope: Mapping[object, object],
) -> tuple[GraphQLResponseError, ...]:
    if "errors" not in envelope:
        return ()
    raw = envelope.get("errors")
    if not isinstance(raw, list):
        raise GraphQLResponseEnvelopeError("GraphQL errors must be a list.")
    parsed: list[GraphQLResponseError] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise GraphQLResponseEnvelopeError(
                "GraphQL error entry must be an object."
            )
        message = item.get("message")
        if not isinstance(message, str) or not message:
            raise GraphQLResponseEnvelopeError(
                "GraphQL error message is invalid."
            )
        path = item.get("path")
        if path is not None:
            if (
                not isinstance(path, list)
                or not path
                or not isinstance(path[0], str)
                or not all(
                    not isinstance(token, bool)
                    and isinstance(token, (str, int))
                    for token in path
                )
            ):
                raise GraphQLResponseEnvelopeError(
                    "GraphQL error path is invalid."
                )
            frozen_path: tuple[str | int, ...] | None = tuple(path)
        else:
            frozen_path = None
        parsed.append(GraphQLResponseError(message=message, path=frozen_path))
    return tuple(parsed)


__all__ = [
    "GraphQLResponseEnvelope",
    "GraphQLResponseEnvelopeError",
    "GraphQLResponseError",
    "parse_graphql_response",
]
