"""Versioned, fixed HERO queries. Do not add runtime field selection."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from hero_integration.client.graphql import HeroGraphQLClient


CUSTOMER_QUERY_V1 = """
query HeroCustomers($first: Int!, $offset: Int!, $orderBy: String!, $showDeleted: Boolean!) {
  contacts(first: $first, offset: $offset, orderBy: $orderBy, show_deleted: $showDeleted) {
    id nr first_name last_name full_name email phone_mobile phone_home company_name category type is_deleted
  }
}
"""
PROJECT_QUERY_V1 = """
query HeroProjectMatches($first: Int!, $offset: Int!, $orderBy: String!) {
  project_matches(first: $first, offset: $offset, orderBy: $orderBy) {
    id customer_id project_id project_title display_id created modified
  }
}
"""
PAGE_SIZE = 100


@dataclass(frozen=True)
class SourcePage:
    records: tuple[dict[str, object], ...]
    next_offset: int | None


class HeroSource:
    def __init__(self, client: HeroGraphQLClient):
        self.client = client

    def customers(self, offset: int = 0) -> SourcePage:
        return self._page(CUSTOMER_QUERY_V1, "contacts", offset, showDeleted=True)

    def projects(self, offset: int = 0) -> SourcePage:
        return self._page(PROJECT_QUERY_V1, "project_matches", offset)

    def _page(self, query: str, root: str, offset: int, **extra: object) -> SourcePage:
        data = self.client.query(query, {"first": PAGE_SIZE, "offset": offset, "orderBy": "id", **extra})
        records = data.get(root)
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError(f"HERO {root} response does not match the V1 contract.")
        return SourcePage(tuple(records), offset + len(records) if len(records) == PAGE_SIZE else None)
