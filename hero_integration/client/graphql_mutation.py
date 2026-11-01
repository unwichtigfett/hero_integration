"""Dedicated mutation transport boundary.

Mutation execution paths reuse the shared HTTPS implementation for TLS,
proxy denial and redirect denial. The wrapper does not itself validate the
operation or authorize a caller; those checks belong to the execution paths.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from .graphql import HeroEndpoint, HeroGraphQLClient


class HeroMutationClient:
    def __init__(self, endpoint: HeroEndpoint, token_provider: Callable[[], str], timeout: int = 30):
        self._transport = HeroGraphQLClient(endpoint, token_provider, timeout=timeout, max_retries=0)

    def execute_once(self, document: str, variables: Mapping[str, object]) -> dict[str, object]:
        # Deliberately no retry: after an ambiguous timeout a caller must verify
        # idempotency before a human explicitly submits another request.
        return self._transport._post(document, variables)
