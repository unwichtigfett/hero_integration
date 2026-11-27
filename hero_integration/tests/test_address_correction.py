"""Scope of a reviewed external location: a changed source row must require new evidence."""
import copy
from unittest import TestCase

from hero_integration.services.address_correction import key, source_fingerprint


class TestAddressCorrection(TestCase):
    def test_source_fingerprint_covers_owner_witnesses_and_primary_fax(self):
        rec = {'id': 1, 'nr': '100', 'phone_fax': '', 'address_id': 9, 'address': {
            'id': 9, 'street': 'Example 1', 'city': '', 'zipcode': '12345'}, 'customer_addresses': [
                {'is_deleted': False, 'address': {'id': 10, 'street': 'Example 1', 'city': 'Berlin', 'zipcode': '12345'}},
                {'is_deleted': False, 'address': {'id': 11, 'street': 'Example 2', 'city': 'Bonn', 'zipcode': '12345'}}]}
        prior = source_fingerprint(rec)
        for fields in ({'id': 2}, {'address_id': 10}, {'phone_fax': '123'}, {'nr': '101'}, {'is_deleted': True}):
            self.assertNotEqual(source_fingerprint({**rec, **fields}), prior)
        reordered = copy.deepcopy(rec)
        reordered['customer_addresses'].reverse()
        self.assertEqual(source_fingerprint(reordered), prior)
        reordered['customer_addresses'][0]['address']['street'] = 'Moved 2'
        self.assertNotEqual(source_fingerprint(reordered), prior)
        self.assertNotEqual(key('source', 1, 9), key('source', 2, 9))
        self.assertNotEqual(key('source', 1, 9), key('other', 1, 9))
