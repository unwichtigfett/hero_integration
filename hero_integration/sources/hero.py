"""Versioned, finite HERO source contracts with explicit runtime controls."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from hero_integration.client.graphql import HeroGraphQLClient
from hero_integration.services.read_contract import ReadContract, ReadContractError, value_at


CUSTOMER_QUERY_V1 = """
query HeroCustomers($first: Int!, $offset: Int!, $orderBy: String!, $showDeleted: Boolean!) {
  contacts(first: $first, offset: $offset, orderBy: $orderBy, show_deleted: $showDeleted) {
    id nr first_name last_name full_name email phone_mobile phone_home company_name category type is_contact_person is_deleted
    address { id street zipcode city country { code } }
  }
}
"""
CUSTOMER_QUERY_V2 = """
query HeroCustomers($first: Int!, $offset: Int!, $orderBy: String!, $showDeleted: Boolean!) {
  contacts(first: $first, offset: $offset, orderBy: $orderBy, show_deleted: $showDeleted) {
    id nr first_name last_name full_name email phone_mobile phone_home company_name category type is_contact_person is_deleted
    address { id street line_2 zipcode city country { code } }
  }
}
"""
CUSTOMER_QUERY_V3 = """
query HeroCustomers($first: Int!, $offset: Int!, $orderBy: String!, $showDeleted: Boolean!) {
  contacts(first: $first, offset: $offset, orderBy: $orderBy, show_deleted: $showDeleted) {
    id nr first_name last_name full_name email phone_mobile phone_home company_name company_legal_form category type is_contact_person is_deleted
    address { id street line_2 zipcode city country { code } }
  }
}
"""
CUSTOMER_CONTRACT_V1 = "v1"
CUSTOMER_CONTRACT_V2 = "v2"
CUSTOMER_CONTRACT_V3 = "v3"
CUSTOMER_CONTRACT_V4 = "v4"
# V4 adds salutation title to V2 without the legal form.
CUSTOMER_QUERY_V4 = CUSTOMER_QUERY_V2.replace("id nr first_name last_name full_name ", "id nr title first_name last_name full_name ")
CUSTOMER_CONTRACT_V5 = "v5"
# V5 also reads the lead source (owner decision 2026-11-03).
CUSTOMER_QUERY_V5 = CUSTOMER_QUERY_V4.replace(" category type ", " category type source ")
PROJECT_QUERY_V1 = """
query HeroProjectMatches($first: Int!, $offset: Int!, $orderBy: String!) {
  project_matches(first: $first, offset: $offset, orderBy: $orderBy) {
    id customer_id project_id project_title project_nr project_type partner_notes
    customer { title full_name }
    address { full_address }
    display_id created modified
    current_project_match_status { id status_code name short_name created modified step_id }
  }
}
"""
PROJECT_CONTRACT_V1 = "v1"
PROJECT_CONTRACT_V2 = "v2"
# V2 also reads the HERO trade (Gewerk) used in the Project title.
PROJECT_QUERY_V2 = PROJECT_QUERY_V1.replace(
    "    display_id created modified\n", "    display_id created modified\n    measure { short name }\n",
)
PAGE_SIZE = 100
CUSTOMER_SELECTED_PATHS = (
    "id", "nr", "first_name", "last_name", "full_name", "email", "phone_mobile", "phone_home",
    "company_name", "category", "type", "is_contact_person", "is_deleted",
    "address.id", "address.street", "address.zipcode", "address.city", "address.country.code",
)
CUSTOMER_SELECTED_PATHS_V2 = (*CUSTOMER_SELECTED_PATHS, "address.line_2")
CUSTOMER_SELECTED_PATHS_V3 = (*CUSTOMER_SELECTED_PATHS_V2, "company_legal_form")
CUSTOMER_SELECTED_PATHS_V4 = (*CUSTOMER_SELECTED_PATHS_V2, "title")
CUSTOMER_SELECTED_PATHS_V5 = (*CUSTOMER_SELECTED_PATHS_V4, "source")
PROJECT_SELECTED_PATHS = (
    "id", "customer_id", "project_id", "project_title", "project_nr", "project_type", "partner_notes",
    "customer.title", "customer.full_name", "address.full_address", "display_id", "created", "modified",
    "current_project_match_status.id", "current_project_match_status.status_code",
    "current_project_match_status.name", "current_project_match_status.short_name",
    "current_project_match_status.created", "current_project_match_status.modified",
    "current_project_match_status.step_id",
)
PROJECT_SELECTED_PATHS_V2 = (*PROJECT_SELECTED_PATHS, "measure.short", "measure.name")
PROJECT_CONTRACT_V3 = "v3"
# V3 also reads the project volume in euros.
PROJECT_QUERY_V3 = PROJECT_QUERY_V2.replace("    measure { short name }\n", "    measure { short name }\n    volume\n")
PROJECT_SELECTED_PATHS_V3 = (*PROJECT_SELECTED_PATHS_V2, "volume")
PROJECT_CONTRACT_V4 = "v4"
# V4 also reads the designation, the lead source, the responsible partner and the assigned users
# (owner decision 2026-11-03).
PROJECT_QUERY_V4 = PROJECT_QUERY_V3.replace(
    "    volume\n",
    "    volume\n    name partner_source\n    partner { email }\n    project_match_assignments { user { email } }\n",
)
PROJECT_SELECTED_PATHS_V4 = (*PROJECT_SELECTED_PATHS_V3, "name", "partner_source", "partner.email",
                             "project_match_assignments")
PROJECT_CONTRACT_V5 = "v5"
# V5 identifies people by HERO id instead of e-mail: partner_id and the assignees' user_id.
PROJECT_QUERY_V5 = PROJECT_QUERY_V3.replace(
    "    volume\n",
    "    volume\n    name partner_source partner_id\n    project_match_assignments { user_id }\n",
)
PROJECT_SELECTED_PATHS_V5 = (*PROJECT_SELECTED_PATHS_V3, "name", "partner_source", "partner_id",
                             "project_match_assignments")


def project_query(contract_version: str) -> str:
    """Return one explicitly reviewed Project read-contract version."""

    if contract_version == PROJECT_CONTRACT_V1:
        return PROJECT_QUERY_V1
    if contract_version == PROJECT_CONTRACT_V2:
        return PROJECT_QUERY_V2
    if contract_version == PROJECT_CONTRACT_V3:
        return PROJECT_QUERY_V3
    if contract_version == PROJECT_CONTRACT_V4:
        return PROJECT_QUERY_V4
    if contract_version == PROJECT_CONTRACT_V5:
        return PROJECT_QUERY_V5
    raise ValueError("Unsupported Project read-contract version.")


def customer_query(contract_version: str) -> str:
    """Return one explicitly reviewed Customer read-contract version."""

    if contract_version == CUSTOMER_CONTRACT_V1:
        return CUSTOMER_QUERY_V1
    if contract_version == CUSTOMER_CONTRACT_V2:
        return CUSTOMER_QUERY_V2
    if contract_version == CUSTOMER_CONTRACT_V3:
        return CUSTOMER_QUERY_V3
    if contract_version == CUSTOMER_CONTRACT_V4:
        return CUSTOMER_QUERY_V4
    if contract_version == CUSTOMER_CONTRACT_V5:
        return CUSTOMER_QUERY_V5
    raise ValueError("Unsupported Customer read-contract version.")


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

    @staticmethod
    def testing_ordering(operation: str) -> dict[str, object]:
        """Native V1 readers have no verified descending HERO order contract."""
        return {
            "verified": False,
            "order": "id",
            "reason": "The native V1 reader's id order is not evidence of newest-first ordering.",
        }

    def _page(self, query: str, root: str, offset: int, **extra: object) -> SourcePage:
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000_000:
            raise ValueError("HERO read offset is invalid or exceeds the reviewed run bound.")
        data = self.client.query(query, {"first": self.page_size, "offset": offset, "orderBy": "id", **extra})
        if not isinstance(data, dict):
            raise ValueError(f"HERO {root} response does not match the V1 contract.")
        records = data.get(root)
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError(f"HERO {root} response does not match the V1 contract.")
        if len(records) > self.page_size:
            raise ValueError("HERO response exceeds the approved page size.")
        annotated = tuple(
            {**row, "_hero_source_instance": self.source_instance} if self.source_instance != "default" else dict(row)
            for row in records
        )
        # Only an empty page ends an offset scan: a short page can be a server
        # page cap below the requested size, not the end of the dataset.
        return SourcePage(annotated, offset + len(records) if records else None)


class GraphQLServiceSource:
    """Read one approved saved service without creating a second mapper."""
    def __init__(self, client: HeroGraphQLClient, service, contract: ReadContract, source_instance: str, *, revalidate=None):
        self.client, self.service, self.contract = client, service, contract
        self.source_instance = source_instance.strip() or "default"
        self.query_adapter = getattr(contract, "adapter_options", None) or {}
        self.revalidate = revalidate

    def customers(self, offset: int = 0) -> SourcePage:
        if getattr(self, 'customer_source', None) is not None:
            return self.customer_source.customers(offset)
        return self._page(offset, "HeroCustomers")

    def projects(self, offset: int = 0) -> SourcePage:
        return self._page(offset, "HeroProjectMatches")

    def project_reference_states(self, ids) -> dict[int, str]:
        """Classify missing ProjectMatch IDs as DELETED, LEAD or UNLISTED without writes.
        Lists omit deleted projects and KONT- leads still referenced by Tasks/Events.
        Query 50 IDs per request; omit unresolvable IDs."""
        from hero_integration.client.graphql import HeroClientError

        wanted = sorted({value for value in ids if type(value) is int and value > 0})
        states: dict[int, str] = {}
        for start in range(0, len(wanted), 50):
            chunk = wanted[start:start + 50]
            document = "query HeroProjectReferenceStates { " + " ".join(
                f"p{value}: project_match(project_match_id: {value}) {{ id is_deleted project_nr }}" for value in chunk
            ) + " }"
            try:
                data = self.client.query(document, {})
            except HeroClientError as error:
                data = getattr(error, "data", None) or {}
            data = data.get("data", data) if isinstance(data, dict) else {}
            for value in chunk:
                row = data.get(f"p{value}") if isinstance(data, dict) else None
                if not isinstance(row, dict) or row.get("id") != value:
                    continue
                if row.get("is_deleted") is True:
                    states[value] = "DELETED"
                elif isinstance(row.get("project_nr"), str) and row["project_nr"].upper().startswith("KONT-"):
                    states[value] = "LEAD"
                else:
                    states[value] = "UNLISTED"
        return states

    def project_status_steps(self) -> dict[int, str]:
        """{step_id: name} of all HERO project status steps (read-only, one query).

        HERO shows the step of a project's current status as its status
        ("☎️ANG Nachfassung"), not the status group name ("Detailgespräch").
        """
        data = self.client.query("query HeroProjectStatusSteps { project_types { id project_status_steps { id name } } }", {})
        data = data.get("data", data) if isinstance(data, dict) else {}
        steps = {}
        for project_type in (data.get("project_types") or []) if isinstance(data, dict) else []:
            for step in (project_type or {}).get("project_status_steps") or []:
                if isinstance(step, dict) and type(step.get("id")) is int and isinstance(step.get("name"), str):
                    steps[step["id"]] = step["name"]
        return steps

    def user_emails(self) -> dict[int, str]:
        """{HERO user id: e-mail} of all company users incl. deleted ones (read-only, one query).

        HERO has no user list; every user belongs to a partner of the company.
        """
        data = self.client.query(
            "query HeroUsers { company { partners(show_deleted: true) { user { id email } } } }", {})
        data = data.get("data", data) if isinstance(data, dict) else {}
        company = data.get("company") if isinstance(data, dict) else None
        users = {}
        for partner in (company or {}).get("partners") or []:
            user = (partner or {}).get("user") or {}
            if type(user.get("id")) is int and isinstance(user.get("email"), str) and user["email"].strip():
                users[user["id"]] = user["email"].strip()
        return users

    def projection(self, offset: int = 0) -> SourcePage:
        from hero_integration.services.projection_contracts import CONTRACTS
        if self.contract.target_operation not in CONTRACTS:
            raise ReadContractError('The selected read contract is not a projection.')
        return self._page(offset, self.contract.target_operation)

    @staticmethod
    def testing_ordering(operation: str) -> dict[str, object]:
        """Saved contracts must opt in with a reviewed descending order first."""
        return {
            "verified": False,
            "order": "UNVERIFIED",
            "reason": "The approved saved read contract does not prove newest-first ordering.",
        }

    def _page(self, offset: int, operation: str) -> SourcePage:
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000_000:
            raise ReadContractError('Read offset is invalid or exceeds the reviewed run bound.')
        if operation != self.contract.target_operation:
            return SourcePage((), None)
        lookup = self.contract.target_operation == 'HeroLookup'
        one_shot_lookup = False
        lookup_bound = None
        if lookup:
            if (type(self.query_adapter.get('version')) is not int or self.query_adapter.get('version') != 2
                    or self.query_adapter.get('role') != 'LOOKUP' or not callable(self.revalidate)):
                raise ReadContractError('Lookup reads require a sealed role and source revalidation.')
            lookup_bound = self.query_adapter.get('max_records')
            if type(lookup_bound) is not int or not 1 <= lookup_bound <= 1000:
                raise ReadContractError('Lookup result bound must be explicit.')
            one_shot_lookup = '$offset' not in self.contract.variables.values()
            if one_shot_lookup and offset != 0:
                raise ReadContractError('A nonpaginated lookup can only read its approved single request.')
            if not one_shot_lookup and self.contract.page_size > lookup_bound:
                raise ReadContractError('Lookup page size exceeds its approved record bound.')
        if callable(self.revalidate) and self.revalidate() != self.contract:
            raise ReadContractError('Read approval changed before transport.')
        request = getattr(self.contract, 'materialized_request', None)
        if request is not None:
            if not callable(self.revalidate):
                raise ReadContractError('Generated read approval changed or revalidation is missing.')
            variables = request.variables
            offsets = [binding.variable_name for binding in request.argument_bindings if binding.argument_name == 'offset']
            if len(offsets) > 1 or (not offsets and not one_shot_lookup):
                raise ReadContractError('Generated read has no unique offset binding.')
            if offsets and not one_shot_lookup:
                variables[offsets[0]] = offset
        else:
            variables = dict(self.contract.variables)
            for name, value in tuple(variables.items()):
                if value == "$offset": variables[name] = offset
                elif value == "$page_size": variables[name] = self.contract.page_size
        data = self.client.query(self.service.query_document, variables)
        if callable(self.revalidate) and self.revalidate() != self.contract:
            raise ReadContractError('Read approval changed during transport.')
        if request is not None:
            import json
            from hero_integration.graphql_protocol.graphql_result_decoder import decode_graphql_result
            from hero_integration.graphql_protocol.transport import Response
            decode_graphql_result(request, Response(True, json.dumps({'data': data}), True))
        records = value_at(data, self.contract.records_path)
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ReadContractError("The selected result path is not a list of objects.")
        bound = lookup_bound if one_shot_lookup else self.contract.page_size
        if len(records) > bound:
            raise ReadContractError('Response exceeds the approved lookup bound.' if lookup else 'Response exceeds the approved page size.')
        for row in records:
            value_at(row, self.contract.identity_path)
        if operation == 'HeroProjectMatches' and self.query_adapter.get('logbook', {}).get('offset_variable'):
            from hero_integration.services.project_logbook import complete_mapped_histories
            records = complete_mapped_histories(self, records, variables)
        annotated = tuple({**row, "_hero_source_instance": self.source_instance} for row in records)
        if one_shot_lookup:
            return SourcePage(annotated, None)
        # Only an empty page ends an offset scan (see HeroSource._page).
        return SourcePage(annotated, offset + len(records) if records else None)


class TestingPreviewSource:
    """Share a fail-closed request budget across approved newest-first Preview readers.
    The client uses zero retries, so each forwarded page is one external request."""

    def __init__(self, source, max_actions: int):
        if not isinstance(max_actions, int) or isinstance(max_actions, bool) or not 1 <= max_actions <= 100:
            raise ValueError("Testing Max. Actions must be between 1 and 100.")
        self.source = source
        self.source_instance = source.source_instance
        self.max_actions = max_actions
        self.executed_actions = 0
        self.records_received = 0
        self.results: dict[str, dict[str, object]] = {}
        self.status = "COMPLETED"

    def customers(self, offset: int = 0) -> SourcePage:
        return self._page("HeroCustomers", self.source.customers, offset)

    def projects(self, offset: int = 0) -> SourcePage:
        return self._page("HeroProjectMatches", self.source.projects, offset)

    def projection(self, offset: int = 0) -> SourcePage:
        operation = getattr(getattr(self.source, "contract", None), "target_operation", "Projection")
        return self._page(operation, self.source.projection, offset)

    def _page(self, operation: str, reader, offset: int) -> SourcePage:
        ordering = self._ordering(operation)
        if not ordering.get("verified"):
            self.status = "BLOCKED"
            self.results[operation] = {
                "status": "BLOCKED_ORDER_UNVERIFIED", "ordering": ordering.get("order", "UNVERIFIED"),
                "requests": 0, "records_received": 0, "reason": ordering.get("reason", "Newest-first ordering is unverified."),
            }
            return SourcePage((), None)
        if self.executed_actions >= self.max_actions:
            self.status = "LIMIT_REACHED"
            prior = self.results.get(operation, {})
            self.results[operation] = {
                **prior, "status": "SKIPPED_LIMIT" if not prior.get("requests") else "LIMIT_REACHED",
                "ordering": ordering.get("order", "VERIFIED"), "requests": int(prior.get("requests", 0)),
                "records_received": int(prior.get("records_received", 0)), "reason": "Shared testing request budget exhausted.",
            }
            return SourcePage((), None)
        self.executed_actions += 1
        try:
            page = reader(offset)
        except Exception:
            self.status = "FAILED"
            raise
        records = len(page.records)
        self.records_received += records
        prior = self.results.get(operation, {})
        self.results[operation] = {
            "status": "COMPLETED", "ordering": ordering.get("order", "VERIFIED"),
            "requests": int(prior.get("requests", 0)) + 1,
            "records_received": int(prior.get("records_received", 0)) + records,
        }
        return page

    def _ordering(self, operation: str) -> dict[str, object]:
        provider = getattr(self.source, "testing_ordering", None)
        value = provider(operation) if callable(provider) else None
        if not isinstance(value, dict):
            return {"verified": False, "order": "UNVERIFIED"}
        order = str(value.get("order", "")).strip()
        if not value.get("verified") or order.lower() not in {"created desc", "id desc"}:
            return {**value, "verified": False, "order": order or "UNVERIFIED"}
        return {**value, "order": order}
