"""Deterministic synthetic data for the fixed HERO V1 operations."""
from __future__ import annotations

from hero_integration.sources.hero import CUSTOMER_QUERY_V1, PAGE_SIZE, PROJECT_QUERY_V1


def _customer(index: int) -> dict[str, object]:
    identifier = 1000 + index
    is_company = index % 3 == 0
    return {
        "id": identifier,
        "nr": f"SYN-C-{identifier}",
        "first_name": None if index % 7 == 0 else f"Synthetic{index}",
        "last_name": "" if index % 5 == 0 else f"Person{index}",
        "full_name": f"Synthetic Person {index}",
        "email": None if index % 6 == 0 else f"person{index}@synthetic.invalid",
        "phone_mobile": "" if index % 4 == 0 else f"+49 555 {identifier}",
        "phone_home": None if index % 8 == 0 else f"+49 556 {identifier}",
        "company_name": f"Synthetic Company {index}" if is_company else "",
        "category": None if index % 4 == 0 else "synthetic-category",
        "type": "synthetic-type",
        "is_deleted": index == 25,
    }


def _project(index: int) -> dict[str, object]:
    identifier = 2000 + index
    customer_id = 1000 + ((index - 1) % 24 + 1)
    return {
        "id": identifier,
        "customer_id": customer_id,
        "project_id": None if index % 5 == 0 else 3000 + index,
        "project_title": f"Synthetic Project {index}",
        "display_id": "" if index % 6 == 0 else f"SYN-P-{identifier}",
        "created": f"2026-10-{(index % 28) + 1:02d}T10:00:00+00:00",
        "modified": None if index % 7 == 0 else f"2026-10-{(index % 28) + 1:02d}T11:00:00+00:00",
    }


CUSTOMERS = tuple(_customer(index) for index in range(1, 26))
PROJECTS = tuple(_project(index) for index in range(1, 41))
REPEATED_CUSTOMER = CUSTOMERS[0]
UPDATED_CUSTOMER = {**REPEATED_CUSTOMER, "email": "person1-updated@synthetic.invalid"}
INVALID_CUSTOMER = {**CUSTOMERS[1], "id": 1099, "company_name": "", "full_name": ""}
AMBIGUOUS_PROJECT = {**PROJECTS[0], "id": 2099, "customer_id": 999999}
INVALID_PROJECT = {**PROJECTS[1], "id": 2199, "customer_id": None, "project_title": ""}

# Four identical pages establish a real 100-row first page without adding new
# identities. The following changed record exercises pagination and updates.
CUSTOMER_PAGE_0 = CUSTOMERS * (PAGE_SIZE // len(CUSTOMERS))
CUSTOMER_PAGE_100 = (UPDATED_CUSTOMER,)


class DeterministicHeroMock:
    """A query-aware mock returning normalized GraphQL data envelopes."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []

    def query(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        self.calls.append((query, dict(variables)))
        if query == CUSTOMER_QUERY_V1:
            self._require(variables, {"first": PAGE_SIZE, "orderBy": "id", "showDeleted": True})
            records = _customer_page(variables["offset"])
            return {"contacts": list(records)}
        if query == PROJECT_QUERY_V1:
            self._require(variables, {"first": PAGE_SIZE, "offset": 0, "orderBy": "id"})
            return {"project_matches": list(PROJECTS)}
        raise AssertionError("unexpected HERO operation")

    @staticmethod
    def _require(variables: dict[str, object], expected: dict[str, object]) -> None:
        if not isinstance(variables.get("offset"), int):
            raise AssertionError("offset variable must be an Int")
        if variables != {**expected, "offset": variables.get("offset")}:
            raise AssertionError("operation variables drifted")


def graphql_envelope(query: str, variables: dict[str, object]) -> dict[str, object]:
    """Return the documented GraphQL response envelope for a fixed operation."""
    return {"data": DeterministicHeroMock().query(query, variables)}


def _customer_page(offset: int) -> tuple[dict[str, object], ...]:
    if offset == 0:
        return CUSTOMER_PAGE_0
    if offset == PAGE_SIZE:
        return CUSTOMER_PAGE_100
    return ()
