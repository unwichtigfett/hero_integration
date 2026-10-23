from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from uuid import uuid4

from .graphql_readonly import graphql_tokens
from .operation_kind import GraphQLOperationKind


class GraphQLWriteSafetyError(ValueError):
    pass


class GraphQLWriteSafetyState(str, Enum):
    READ_ONLY = "read_only"
    BLOCKED = "blocked"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"


@dataclass(frozen=True, slots=True)
class GraphQLWriteRequest:
    document: str
    operation_name: str | None
    operation_kind: GraphQLOperationKind
    request_scope: str | None
    fingerprint: str
    _canonical_variables: str

    @property
    def variables(self) -> Mapping[str, object]:
        """Return a detached immutable view of the exact safety-bound values."""

        return MappingProxyType(json.loads(self._canonical_variables))

    @classmethod
    def create(
        cls,
        document: str,
        *,
        variables: Mapping[str, object] | None = None,
        operation_name: str | None = None,
        request_scope: str | None = None,
    ) -> GraphQLWriteRequest:
        normalized_document = document.strip()

        if not normalized_document:
            raise GraphQLWriteSafetyError(
                "GraphQL document must not be empty."
            )

        normalized_operation_name = (
            operation_name.strip()
            if isinstance(operation_name, str)
            else None
        ) or None

        normalized_request_scope = (
            request_scope.strip()
            if isinstance(request_scope, str)
            else None
        ) or None
        if request_scope is not None and normalized_request_scope is None:
            raise GraphQLWriteSafetyError(
                "GraphQL request scope must be None or a non-empty string."
            )
        if variables is not None and not isinstance(variables, Mapping):
            raise GraphQLWriteSafetyError(
                "GraphQL variables must be a mapping."
            )
        if variables is not None and not all(
            isinstance(name, str) and name for name in variables
        ):
            raise GraphQLWriteSafetyError(
                "GraphQL variable names must be non-empty strings."
            )

        operation_kind = detect_operation_kind(
            normalized_document,
            operation_name=normalized_operation_name,
        )

        try:
            canonical_variables = json.dumps(
                dict(variables or {}),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise GraphQLWriteSafetyError(
                "GraphQL variables must be JSON serializable."
            ) from exc

        canonical = json.dumps(
            {
                "document": normalized_document,
                "operation_name": normalized_operation_name,
                "request_scope": normalized_request_scope,
                "variables": json.loads(canonical_variables),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        fingerprint = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()

        return cls(
            document=normalized_document,
            operation_name=normalized_operation_name,
            operation_kind=operation_kind,
            request_scope=normalized_request_scope,
            fingerprint=fingerprint,
            _canonical_variables=canonical_variables,
        )


@dataclass(frozen=True)
class GraphQLMutationApproval:
    approval_id: str
    request_fingerprint: str

    @classmethod
    def create(
        cls,
        request: GraphQLWriteRequest,
    ) -> GraphQLMutationApproval:
        if (
            request.operation_kind
            is not GraphQLOperationKind.MUTATION
        ):
            raise GraphQLWriteSafetyError(
                "Mutation approval can only target a mutation request."
            )

        return cls(
            approval_id=uuid4().hex,
            request_fingerprint=request.fingerprint,
        )


@dataclass(frozen=True)
class GraphQLWriteSafetyAssessment:
    state: GraphQLWriteSafetyState
    operation_kind: GraphQLOperationKind
    request_fingerprint: str
    reason: str

    @property
    def can_execute(self) -> bool:
        return self.state in {
            GraphQLWriteSafetyState.READ_ONLY,
            GraphQLWriteSafetyState.APPROVED,
        }


class GraphQLWriteSafetyPolicy:
    def __init__(
        self,
        *,
        mutations_enabled: bool = False,
    ) -> None:
        self.mutations_enabled = mutations_enabled

    def assess(
        self,
        request: GraphQLWriteRequest,
        *,
        approval: GraphQLMutationApproval | None = None,
    ) -> GraphQLWriteSafetyAssessment:
        kind = request.operation_kind

        if kind is GraphQLOperationKind.QUERY:
            return GraphQLWriteSafetyAssessment(
                state=GraphQLWriteSafetyState.READ_ONLY,
                operation_kind=kind,
                request_fingerprint=request.fingerprint,
                reason="Read-only GraphQL query.",
            )

        if kind is GraphQLOperationKind.SUBSCRIPTION:
            return GraphQLWriteSafetyAssessment(
                state=GraphQLWriteSafetyState.BLOCKED,
                operation_kind=kind,
                request_fingerprint=request.fingerprint,
                reason="GraphQL subscriptions are not supported.",
            )

        if kind is GraphQLOperationKind.UNKNOWN:
            return GraphQLWriteSafetyAssessment(
                state=GraphQLWriteSafetyState.BLOCKED,
                operation_kind=kind,
                request_fingerprint=request.fingerprint,
                reason=(
                    "GraphQL operation could not be resolved safely."
                ),
            )

        if not self.mutations_enabled:
            return GraphQLWriteSafetyAssessment(
                state=GraphQLWriteSafetyState.BLOCKED,
                operation_kind=kind,
                request_fingerprint=request.fingerprint,
                reason=(
                    "GraphQL mutation capability is disabled."
                ),
            )

        if approval is None:
            return GraphQLWriteSafetyAssessment(
                state=(
                    GraphQLWriteSafetyState.APPROVAL_REQUIRED
                ),
                operation_kind=kind,
                request_fingerprint=request.fingerprint,
                reason=(
                    "Explicit approval is required for this "
                    "exact mutation request."
                ),
            )

        if (
            approval.request_fingerprint
            != request.fingerprint
        ):
            return GraphQLWriteSafetyAssessment(
                state=GraphQLWriteSafetyState.BLOCKED,
                operation_kind=kind,
                request_fingerprint=request.fingerprint,
                reason=(
                    "Mutation approval does not match the "
                    "current request fingerprint."
                ),
            )

        return GraphQLWriteSafetyAssessment(
            state=GraphQLWriteSafetyState.APPROVED,
            operation_kind=kind,
            request_fingerprint=request.fingerprint,
            reason=(
                "Exact mutation request has explicit approval."
            ),
        )


def detect_operation_kind(
    document: str,
    *,
    operation_name: str | None = None,
) -> GraphQLOperationKind:
    tokens = graphql_tokens(document)

    definitions: list[
        tuple[GraphQLOperationKind, str | None]
    ] = []

    depth = 0
    pending_definition = False
    index = 0

    operation_tokens = {
        "query": GraphQLOperationKind.QUERY,
        "mutation": GraphQLOperationKind.MUTATION,
        "subscription": GraphQLOperationKind.SUBSCRIPTION,
    }

    while index < len(tokens):
        token_type, value = tokens[index]

        if depth == 0:
            if (
                token_type == "NAME"
                and value in operation_tokens
            ):
                kind = operation_tokens[value]
                name = None

                if (
                    index + 1 < len(tokens)
                    and tokens[index + 1][0] == "NAME"
                ):
                    name = tokens[index + 1][1]

                definitions.append((kind, name))
                pending_definition = True

            elif (
                token_type == "NAME"
                and value == "fragment"
            ):
                pending_definition = True

            elif value == "{":
                if not pending_definition:
                    definitions.append(
                        (
                            GraphQLOperationKind.QUERY,
                            None,
                        )
                    )

                depth = 1
                pending_definition = False
                index += 1
                continue

        if value == "{":
            depth += 1
        elif value == "}":
            depth = max(0, depth - 1)

            if depth == 0:
                pending_definition = False

        index += 1

    normalized_name = (
        operation_name.strip()
        if isinstance(operation_name, str)
        else ""
    )

    if normalized_name:
        matches = [
            kind
            for kind, name in definitions
            if name == normalized_name
        ]

        if len(matches) == 1:
            return matches[0]

        return GraphQLOperationKind.UNKNOWN

    if len(definitions) == 1:
        return definitions[0][0]

    return GraphQLOperationKind.UNKNOWN
