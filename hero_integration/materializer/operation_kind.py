from __future__ import annotations

from enum import Enum


class GraphQLOperationKind(str, Enum):
    QUERY = "query"
    MUTATION = "mutation"
    SUBSCRIPTION = "subscription"
    UNKNOWN = "unknown"


__all__ = ["GraphQLOperationKind"]
