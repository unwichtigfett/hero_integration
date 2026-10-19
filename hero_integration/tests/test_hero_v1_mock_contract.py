from __future__ import annotations

import json
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

from hero_integration.client.graphql import HeroClientError, HeroGraphQLClient, validate_endpoint
from hero_integration.sources.hero import CUSTOMER_QUERY_V1, CUSTOMER_SELECTED_PATHS, PAGE_SIZE, PROJECT_QUERY_V1, PROJECT_SELECTED_PATHS, HeroSource
from hero_integration.tests.mapping_fixture import fixture_plan
from hero_integration.tests.mock_hero_v1 import (
    AMBIGUOUS_PROJECT,
    CUSTOMERS,
    DeterministicHeroMock,
    INVALID_CUSTOMER,
    INVALID_PROJECT,
    PROJECTS,
    UPDATED_CUSTOMER,
    graphql_envelope,
)


MATRIX_PATH = Path(__file__).parent / "hero_mapping_matrix_v3.json"
MATRIX = json.loads(MATRIX_PATH.read_text())
CUSTOMER = {
    "id": 101,
    "nr": "SYN-101",
    "first_name": " Ada ",
    "last_name": " Lovelace ",
    "full_name": " Ada Lovelace ",
    "email": "ada@synthetic.invalid",
    "phone_mobile": "+49 555 101",
    "phone_home": "+49 555 102",
    "company_name": "",
    "category": "synthetic",
    "type": "synthetic",
    "is_contact_person": False,
    "is_deleted": False,
    "address": {"id": 8001, "street": "Synthetic Street 1", "zipcode": "77777", "city": "Synthetic City"},
}
PROJECT = {
    "id": 201,
    "customer_id": 101,
    "project_id": 301,
    "project_title": " Synthetic Project ",
    "project_nr": "0001",
    "project_type": "PV",
    "partner_notes": "Synthetic partner note",
    "customer": {"title": "Herr", "full_name": "Synthetic Person"},
    "address": {"full_address": "Synthetic Street 1, 77777 Synthetic City"},
    "display_id": "PR-201",
    "created": "2026-10-18T10:00:00+00:00",
    "modified": "2026-10-18T11:00:00+00:00",
}


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _WireOpener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return _Response(self.payload)


class _TimeoutOpener:
    def __init__(self):
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        raise urllib.error.URLError("synthetic timeout")


class _StrictSource:
    def __init__(self):
        self.calls = []

    def query(self, query, variables):
        expected = {
            CUSTOMER_QUERY_V1: {"first": PAGE_SIZE, "offset": 0, "orderBy": "id", "showDeleted": True},
            PROJECT_QUERY_V1: {"first": PAGE_SIZE, "offset": 0, "orderBy": "id"},
        }
        if query not in expected or variables != expected[query]:
            raise AssertionError("operation or variables drifted")
        self.calls.append((query, variables))
        return {"contacts": [CUSTOMER]} if query == CUSTOMER_QUERY_V1 else {"project_matches": [PROJECT]}


class TestHeroV1MockContract(unittest.TestCase):
    def test_wire_preserves_query_variables_and_response_envelope(self):
        opener = _WireOpener({"data": {"contacts": [CUSTOMER]}})
        endpoint = validate_endpoint("https://hero.synthetic.invalid/graphql", {"hero.synthetic.invalid"})
        client = HeroGraphQLClient(endpoint, lambda: "synthetic", max_retries=0)
        variables = {"first": 100, "offset": 0, "orderBy": "id", "showDeleted": True}
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener):
            self.assertEqual(client.query(CUSTOMER_QUERY_V1, variables), {"contacts": [CUSTOMER]})
        self.assertEqual(
            json.loads(opener.requests[0][0].data),
            {"query": CUSTOMER_QUERY_V1, "variables": variables},
        )

    def test_graphql_error_envelope_is_rejected_without_egress(self):
        endpoint = validate_endpoint("https://hero.synthetic.invalid/graphql", {"hero.synthetic.invalid"})
        client = HeroGraphQLClient(endpoint, lambda: "synthetic", max_retries=0)
        opener = _WireOpener({"errors": [{"message": "synthetic"}]})
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(HeroClientError):
                client.query(
                    CUSTOMER_QUERY_V1,
                    {"first": 100, "offset": 0, "orderBy": "id", "showDeleted": True},
                )

    def test_timeout_retries_only_the_mocked_transport(self):
        endpoint = validate_endpoint("https://hero.synthetic.invalid/graphql", {"hero.synthetic.invalid"})
        client = HeroGraphQLClient(endpoint, lambda: "synthetic", max_retries=1)
        opener = _TimeoutOpener()
        variables = {"first": 100, "offset": 0, "orderBy": "id", "showDeleted": True}
        with mock.patch("hero_integration.client.graphql.urllib.request.build_opener", return_value=opener):
            with mock.patch("hero_integration.client.graphql.time.sleep") as sleep:
                with self.assertRaises(HeroClientError):
                    client.query(CUSTOMER_QUERY_V1, variables)
        self.assertEqual(opener.calls, 2)
        sleep.assert_called_once_with(1)

    def test_source_and_mapping_cover_all_fixed_values(self):
        source = _StrictSource()
        customers, projects = HeroSource(source).customers(), HeroSource(source).projects()
        self.assertEqual(customers.records, (CUSTOMER,))
        self.assertEqual(projects.records, (PROJECT,))
        customer_plan = fixture_plan("HeroCustomers")
        project_plan = fixture_plan("HeroProjectMatches")
        self.assertEqual(customer_plan.values(CUSTOMER), {
            "customer_name": "Ada Lovelace", "customer_type": "Individual", "first_name": "Ada",
            "last_name": "Lovelace", "email_id": "ada@synthetic.invalid", "mobile_no": "+49 555 101",
            "custom_hero_source_id": "101", "custom_hero_customer_number": "SYN-101",
            "custom_hero_customer_category": "synthetic", "custom_hero_customer_type": "synthetic",
            "custom_hero_is_contact_person": False,
        })
        self.assertEqual(project_plan.values(PROJECT, lambda value: "CUST-0001", lambda: "Synthetic Company"), {
            "project_name": "Synthetic Project", "customer": "CUST-0001", "company": "Synthetic Company",
            "custom_hero_source_id": "201", "custom_hero_related_project_id": "301",
            "custom_hero_display_id": "PR-201", "custom_hero_source_created": "2026-10-18T10:00:00+00:00",
            "custom_hero_source_modified": "2026-10-18T11:00:00+00:00",
            "custom_hero_project_number": "0001", "custom_hero_project_type": "PV",
            "custom_hero_partner_notes": "Synthetic partner note",
        })

    def test_matrix_accounts_for_each_selected_path(self):
        selected = {
            "HeroCustomers": set(CUSTOMER_SELECTED_PATHS),
            "HeroProjectMatches": set(PROJECT_SELECTED_PATHS),
        }
        matrix = {}
        for row in MATRIX["fields"]:
            root, field = row["path"].split("[]", 1)
            matrix.setdefault(row["operation"], set()).add((root, field.lstrip(".")))
        expected = {
            "HeroCustomers": {("contacts", field) for field in selected["HeroCustomers"]},
            "HeroProjectMatches": {
                ("project_matches", field) for field in selected["HeroProjectMatches"]
            },
        }
        self.assertEqual(matrix, expected)
        self.assertEqual(sum(map(len, matrix.values())), 30)
        self.assertEqual(sum(row["status"] == "NATIVE_MAPPED" for row in MATRIX["fields"]), 17)
        self.assertEqual(sum(row["status"] == "CUSTOM_FIELD_MAPPED" for row in MATRIX["fields"]), 10)
        self.assertEqual(sum(row["status"] == "HERO_DOCTYPE_MAPPED" for row in MATRIX["fields"]), 3)
        self.assertTrue(all(not row["exclusion_reason"] for row in MATRIX["fields"]))

    def test_deterministic_dataset_covers_pagination_and_negative_cases(self):
        mock_source = DeterministicHeroMock()
        source = HeroSource(mock_source)

        first_page = source.customers()
        second_page = source.customers(first_page.next_offset or 0)
        projects = source.projects()

        self.assertEqual((len(CUSTOMERS), len(PROJECTS)), (25, 40))
        self.assertEqual((len(first_page.records), len(second_page.records)), (100, 1))
        self.assertEqual((first_page.next_offset, second_page.next_offset), (100, None))
        self.assertEqual(second_page.records[0], UPDATED_CUSTOMER)
        self.assertEqual(projects.records, PROJECTS)
        valid_customer_ids = {row["id"] for row in CUSTOMERS if not row["is_deleted"]}
        self.assertTrue(all(row["customer_id"] in valid_customer_ids for row in PROJECTS))
        self.assertFalse(fixture_plan("HeroCustomers").values(INVALID_CUSTOMER).get("customer_name"))
        self.assertFalse(fixture_plan("HeroProjectMatches").values(INVALID_PROJECT, lambda value: "CUST-0001", lambda: "Synthetic Company").get("project_name"))
        self.assertNotIn(AMBIGUOUS_PROJECT["customer_id"], valid_customer_ids)
        self.assertEqual(
            graphql_envelope(CUSTOMER_QUERY_V1, mock_source.calls[0][1]),
            {"data": {"contacts": list(first_page.records)}},
        )


def _selected_fields(query, root):
    selection = query.split(f"{root}(", 1)[1].split("}", 1)[0].split("{")[-1]
    return set(selection.split())
