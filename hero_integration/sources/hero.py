"""Versioned, finite HERO source contracts with explicit runtime controls."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from hero_integration.client.graphql import HeroGraphQLClient


CUSTOMER_QUERY_V1 = """
query HeroCustomers($first: Int!, $offset: Int!, $orderBy: String!, $showDeleted: Boolean!) {
  contacts(first: $first, offset: $offset, orderBy: $orderBy, show_deleted: $showDeleted) {
    id nr first_name last_name full_name email phone_mobile phone_home company_name category type is_contact_person is_deleted
    address { id street zipcode city }
  }
}
"""
PROJECT_QUERY_V1 = """
query HeroProjectMatches($first: Int!, $offset: Int!, $orderBy: String!) {
  project_matches(first: $first, offset: $offset, orderBy: $orderBy) {
    id customer_id project_id project_title project_nr project_type partner_notes
    customer { title full_name }
    address { full_address }
    display_id created modified
  }
}
"""
PAGE_SIZE = 100
CUSTOMER_SELECTED_PATHS = (
    "id", "nr", "first_name", "last_name", "full_name", "email", "phone_mobile", "phone_home",
    "company_name", "category", "type", "is_contact_person", "is_deleted",
    "address.id", "address.street", "address.zipcode", "address.city",
)
PROJECT_SELECTED_PATHS = (
    "id", "customer_id", "project_id", "project_title", "project_nr", "project_type", "partner_notes",
    "customer.title", "customer.full_name", "address.full_address", "display_id", "created", "modified",
)


@dataclass(frozen=True)
class SourcePage:
    records: tuple[dict[str, object], ...]
    next_offset: int | None


class HeroSource:
    def __init__(
        self,
        client: HeroGraphQLClient,
        source_instance: str = "default",
        *,
        page_size: int = PAGE_SIZE,
        filters: Mapping[str, Mapping[str, object]] | None = None,
        pagination_strategy: str = "OFFSET",
    ):
        self.client = client
        self.source_instance = source_instance.strip() if isinstance(source_instance, str) and source_instance.strip() else "default"
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
            raise ValueError("HERO page size must be between 1 and 1000.")
        if pagination_strategy != "OFFSET":
            # The fixed V1 contracts expose offsets only.  A cursor must not be
            # fabricated from an offset-like value.
            raise ValueError("The approved HERO V1 source contracts support OFFSET pagination only.")
        self.page_size = page_size
        self.filters = {str(key): dict(value) for key, value in (filters or {}).items() if isinstance(value, Mapping)}

    def customers(self, offset: int = 0) -> SourcePage:
        values = self.filters.get("HeroCustomers", {})
        show_deleted = values.get("showDeleted", True)
        if not isinstance(show_deleted, bool):
            raise ValueError("HeroCustomers.showDeleted must be a boolean.")
        return self._page(CUSTOMER_QUERY_V1, "contacts", offset, showDeleted=show_deleted)

    def projects(self, offset: int = 0) -> SourcePage:
        if self.filters.get("HeroProjectMatches"):
            raise ValueError("HeroProjectMatches has no approved V1 filter variables.")
        return self._page(PROJECT_QUERY_V1, "project_matches", offset)

    def _page(self, query: str, root: str, offset: int, **extra: object) -> SourcePage:
        data = self.client.query(query, {"first": self.page_size, "offset": offset, "orderBy": "id", **extra})
        records = data.get(root)
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError(f"HERO {root} response does not match the V1 contract.")
        annotated = tuple(
            {**row, "_hero_source_instance": self.source_instance} if self.source_instance != "default" else dict(row)
            for row in records
        )
        return SourcePage(annotated, offset + len(records) if len(records) == self.page_size else None)
