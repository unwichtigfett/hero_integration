"""Newly mapped product fields need proof before replacing native values."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import product_migration as products
from hero_integration.tests.test_product_mapping import _Frappe, BUILTIN, MANUFACTURER_NR, own_read
from hero_integration.tests.test_product_migration import ROW


class TestProductMappingExtension(TestCase):
    def initial(self, read):
        site = _Frappe()
        with patch.object(products, 'frappe', site):
            self.assertEqual(products.upsert(ROW, None, products._Masters(), 'source-a', read=read), 'added')
        return site, site.items['PV-1001'], SimpleNamespace(name='L1', **site.links[-1][1])

    def test_new_description_preserves_nonempty_local_value_before_any_master_or_price_write(self):
        site, item, link = self.initial(own_read(drop=('description',)))
        item['description'] = 'Local description'
        before = deepcopy(dict(item))
        with patch.object(products, 'frappe', site), patch.object(products._Masters, 'for_item') as masters, \
                patch.object(products, 'plan_price', return_value=None) as prices:
            result = products.upsert(ROW, link, products._Masters(), 'source-a', read=BUILTIN)
        self.assertEqual(result, 'conflict')
        self.assertEqual(dict(item), before)
        masters.assert_not_called()
        prices.assert_not_called()

    def test_new_description_adopts_only_empty_or_equal_local_values(self):
        for local in (None, '', ROW['base_data']['description']):
            with self.subTest(local=local):
                site, item, link = self.initial(own_read(drop=('description',)))
                item['description'] = local
                with patch.object(products, 'frappe', site):
                    self.assertEqual(products.upsert(ROW, link, products._Masters(), 'source-a', read=BUILTIN), 'updated')
                self.assertEqual(item['description'], ROW['base_data']['description'])
                self.assertIn('description', json.loads(site.links[-1][2]['target_field_hashes'])['fields'])

    def test_new_numeric_field_does_not_treat_zero_as_unowned_empty(self):
        site, item, link = self.initial(own_read())
        item['min_order_qty'] = 0
        quantity = replace(MANUFACTURER_NR, target_field='min_order_qty', source_path='quantity_min',
                           source_type='float|null', transformation='Number')
        incoming = {**ROW, 'quantity_min': 2.0}
        with patch.object(products, 'frappe', site):
            self.assertEqual(products.upsert(incoming, link, products._Masters(), 'source-a',
                                             read=own_read(drop=('min_order_qty',), add=(quantity,))), 'conflict')
        self.assertEqual(item['min_order_qty'], 0)

    def test_required_insert_fallback_is_not_an_owned_mapping_field(self):
        site, item, link = self.initial(own_read(drop=('stock_uom',)))
        self.assertNotIn('stock_uom', json.loads(link.target_field_hashes)['fields'])
        incoming = {**ROW, 'base_data': {**ROW['base_data'], 'unit_type': 'Box'}}
        with patch.object(products, 'frappe', site):
            self.assertEqual(products.upsert(incoming, link, products._Masters(), 'source-a', read=BUILTIN), 'conflict')
        self.assertEqual(item['stock_uom'], products.REQUIRED_FALLBACKS['stock_uom'])

    def test_legacy_aggregate_does_not_claim_ownership_of_new_custom_fields(self):
        site, item, link = self.initial(own_read())
        legacy = json.loads(link.target_field_hashes)
        legacy.pop('fields')
        link.target_field_hashes = json.dumps(legacy)
        item['custom_hero_matchcode'] = 'LOCAL'
        incoming = {**ROW, 'base_data': {**ROW['base_data'], 'matchcode': 'REMOTE'}}
        with patch.object(products, 'frappe', site):
            self.assertEqual(products.upsert(incoming, link, products._Masters(), 'source-a',
                                             read=own_read(add=(MANUFACTURER_NR,))), 'conflict')
        self.assertEqual(item['custom_hero_matchcode'], 'LOCAL')
