from __future__ import annotations

import unittest
from types import SimpleNamespace

from hero_integration.client.graphql import HeroClientError, assert_read_only, validate_endpoint
from hero_integration.integrations.erpnext.company import confirmed_project_company
from hero_integration.sources.hero import CUSTOMER_QUERY_V1, PROJECT_QUERY_V1
from hero_integration.tests.mapping_fixture import fixture_plan


class TestOfflineContracts(unittest.TestCase):
    def test_endpoint_requires_exact_allowed_https_host(self):
        endpoint = validate_endpoint("https://api.hero.example/graphql", {"api.hero.example"})
        self.assertEqual(endpoint.url, "https://api.hero.example/graphql")
        with self.assertRaises(HeroClientError):
            validate_endpoint("http://api.hero.example/graphql", {"api.hero.example"})
        with self.assertRaises(HeroClientError):
            validate_endpoint("https://other.example/graphql", {"api.hero.example"})

    def test_graphql_mutation_and_subscription_are_blocked(self):
        with self.assertRaises(HeroClientError):
            assert_read_only("mutation Change { change { id } }")
        with self.assertRaises(HeroClientError):
            assert_read_only("subscription Events { event { id } }")

    def test_test_fixture_uses_explicit_name_and_type_bindings(self):
        plan = fixture_plan("HeroCustomers")
        company = plan.values({
            "company_name": " Acme ", "full_name": "Ada", "first_name": " Ada ",
            "last_name": " Lovelace ", "email": "ada@synthetic.invalid", "phone_mobile": " 555 ",
        })
        self.assertEqual(company, {
            "customer_name": "Acme", "customer_type": "Company", "first_name": "Ada",
            "last_name": "Lovelace", "email_id": "ada@synthetic.invalid", "mobile_no": "555",
        })
        individual = plan.values({"company_name": "", "full_name": "Ada"})
        self.assertEqual(individual["customer_type"], "Individual")
        self.assertFalse(plan.values({"first_name": "Ada", "last_name": "Lovelace"}).get("customer_name"))
        self.assertEqual(
            plan.values({"company_name": "", "full_name": "Ada", "email": None, "phone_mobile": ""}),
            {"customer_name": "Ada", "customer_type": "Individual"},
        )
        project_plan = fixture_plan("HeroProjectMatches")
        self.assertEqual(
            project_plan.values({"customer_id": 1, "project_title": "Roof"}, lambda value: "Acme", lambda: "Executing Company"),
            {"project_name": "Roof", "customer": "Acme", "company": "Executing Company"},
        )
        self.assertFalse(project_plan.values({"customer_id": 1, "project_title": ""}, lambda value: "Acme", lambda: "Executing Company").get("project_name"))

    def test_test_fixture_keeps_source_attributes_out_of_business_enums(self):
        customer = fixture_plan("HeroCustomers").values({
            "id": 10, "full_name": "Ada", "nr": "NR-10", "category": "Gold", "type": "External",
        })
        project = fixture_plan("HeroProjectMatches").values({
            "id": 20, "project_id": 30, "project_title": "Roof", "display_id": "P-20",
            "created": "2026-10-18T10:00:00+00:00", "modified": "2026-10-18T11:00:00+00:00",
            "customer_id": 10,
        }, lambda value: "Ada", lambda: "Executing Company")

        self.assertEqual(customer["custom_hero_customer_number"], "NR-10")
        self.assertEqual(customer["custom_hero_customer_category"], "Gold")
        self.assertEqual(customer["custom_hero_customer_type"], "External")
        self.assertNotIn("customer_group", customer)
        self.assertEqual(project["custom_hero_related_project_id"], "30")
        self.assertEqual(project["custom_hero_display_id"], "P-20")
        self.assertEqual(project["custom_hero_source_created"], "2026-10-18T10:00:00+00:00")
        self.assertEqual(project["custom_hero_source_modified"], "2026-10-18T11:00:00+00:00")

    def test_confirmed_company_requires_existing_readable_enabled_company(self):
        valid = _FakeFrappe(company="Executing Company", confirmed=1)
        self.assertEqual(confirmed_project_company(valid).name, "Executing Company")
        self.assertFalse(confirmed_project_company(_FakeFrappe(company="", confirmed=0)).is_valid)
        self.assertFalse(confirmed_project_company(_FakeFrappe(company="Executing Company", confirmed=0)).is_valid)
        self.assertFalse(confirmed_project_company(_FakeFrappe(company="Missing", confirmed=1, exists=False)).is_valid)
        self.assertFalse(confirmed_project_company(_FakeFrappe(company="Executing Company", confirmed=1, readable=False)).is_valid)
        self.assertFalse(confirmed_project_company(_FakeFrappe(company="Executing Company", confirmed=1, disabled=1)).is_valid)

    def test_queries_are_fixed_and_read_only(self):
        self.assertIn("contacts(first:", CUSTOMER_QUERY_V1)
        self.assertIn("project_matches(first:", PROJECT_QUERY_V1)
        assert_read_only(CUSTOMER_QUERY_V1)
        assert_read_only(PROJECT_QUERY_V1)


class _FakeMeta:
    def has_field(self, fieldname):
        return fieldname == "disabled"


class _FakeDatabase:
    def __init__(self, exists, disabled):
        self._exists = exists
        self._disabled = disabled

    def exists(self, doctype, name):
        return doctype == "Company" and self._exists and name != "Missing"

    def get_value(self, doctype, name, fieldname):
        return self._disabled


class _FakeFrappe:
    def __init__(self, company, confirmed, exists=True, readable=True, disabled=0):
        self._settings = SimpleNamespace(erpnext_company=company, erpnext_company_confirmed=confirmed)
        self._readable = readable
        self.db = _FakeDatabase(exists, disabled)

    def get_single(self, doctype):
        self.assert_doctype(doctype, "HERO Integration Settings")
        return self._settings

    def has_permission(self, doctype, permission_type, name):
        self.assert_doctype(doctype, "Company")
        return permission_type == "read" and self._readable

    def get_meta(self, doctype):
        self.assert_doctype(doctype, "Company")
        return _FakeMeta()

    @staticmethod
    def assert_doctype(actual, expected):
        if actual != expected:
            raise AssertionError(f"expected {expected}, got {actual}")
