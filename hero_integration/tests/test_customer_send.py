"""Customer Send to HERO: fields and values (owner decision 2026-11-02)."""

from __future__ import annotations

import unittest

from hero_integration.services import customer_send
from hero_integration.services.direct_mapping import SALUTATION_TABLE


class TestCustomerSendFields(unittest.TestCase):
    def test_the_customer_number_is_never_sent(self):
        sent = {key for _, _, key in customer_send.CUSTOMER_FIELDS} | {key for _, _, key in customer_send.ADDRESS_FIELDS}
        self.assertNotIn("nr", sent)
        self.assertNotIn("custom_hero_customer_number", {field for _, field, _ in customer_send.CUSTOMER_FIELDS})

    def test_salutations_go_back_to_a_value_the_import_reads_the_same(self):
        for erpnext in set(SALUTATION_TABLE.values()):
            hero = customer_send.HERO_TITLE.get(erpnext, erpnext)
            self.assertEqual(SALUTATION_TABLE[hero.lower()], erpnext)

    def test_empty_values_compare_equal(self):
        self.assertTrue(customer_send._same(None, ""))
        self.assertTrue(customer_send._same(" A ", "A"))
        self.assertFalse(customer_send._same("A", None))

    def test_the_mutation_reads_back_every_sent_field(self):
        for _, _, key in customer_send.CUSTOMER_FIELDS + customer_send.ADDRESS_FIELDS:
            self.assertIn(key, customer_send.MUTATION_DOCUMENT)
            self.assertIn(key, customer_send.READ_DOCUMENT)


if __name__ == "__main__":
    unittest.main()
