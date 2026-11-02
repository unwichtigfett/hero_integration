"""HERO customer title -> Contact salutation through a fixed table."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services.direct_mapping import (
    DirectMappingError, MappingSpec, TRANSFORM_SALUTATION, transformed_value, validate_spec,
)
from hero_integration.sources import hero

RULE = MappingSpec(operation="HeroCustomers", source_path="title", target_doctype="Customer", target_field="salutation",
                   source_type="string|null", purpose="Customer Primary Contact", transformation=TRANSFORM_SALUTATION,
                   definition_id="customer-salutation")


class TestCustomerSalutation(unittest.TestCase):
    def test_contract_v4_reads_the_title_without_legal_form(self):
        query = hero.customer_query("v4")
        self.assertIn(" title ", query)
        self.assertNotIn("company_legal_form", query)
        self.assertIn("line_2", query)
        self.assertNotIn(" title ", hero.customer_query("v2"))

    def test_only_the_salutation_table_may_feed_the_contact_salutation(self):
        validate_spec(RULE, SimpleNamespace())
        with self.assertRaises(DirectMappingError):
            validate_spec(MappingSpec(**{**RULE.__dict__, "transformation": "Trim"}), SimpleNamespace())

    def test_table_values_and_unknown_titles(self):
        expected = {"Herr": "Herr", " frau ": "Frau", "Familie": "Familie", "Eheleute": "Eheleute",
                    "Dr.": "Dr", "Mr": None, "": None, None: None}
        for title, salutation in expected.items():
            with self.subTest(title=title):
                self.assertEqual(transformed_value(RULE, title), salutation)

    def test_contact_adapter_writes_and_reads_the_salutation(self):
        from hero_integration.integrations.erpnext import contact as adapter
        saved = []
        contact = SimpleNamespace(salutation=None, first_name=None, last_name=None, links=[], email_ids=[], phone_nos=[],
                                  get=lambda field: getattr(contact, field, None), save=lambda: saved.append(True))
        customer = SimpleNamespace(name="KD-1", get=lambda field: "CON-1" if field == "customer_primary_contact" else None)
        with patch.object(adapter, "_primary_contact", return_value=contact), \
                patch.object(adapter, "_is_exclusive_customer_contact", return_value=True), \
                patch.object(adapter, "_set_field", side_effect=lambda doc, field, value: setattr(doc, field, value)), \
                patch.object(adapter, "_set_primary_child", return_value=False):
            adapter.synchronize_primary_contact(customer, {"salutation": "Frau"})
            self.assertEqual((contact.salutation, saved), ("Frau", [True]))
            self.assertEqual(adapter.primary_contact_values(customer, {"salutation"}), {"salutation": "Frau"})


if __name__ == "__main__":
    unittest.main()
