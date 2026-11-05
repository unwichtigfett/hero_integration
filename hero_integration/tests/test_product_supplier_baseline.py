"""Product updates preserve local supplier rows and optional UOM mappings."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import product_migration as pm
from hero_integration.tests.test_product_mapping import _Frappe, SUPPLIER_ROW, own_read


class TestSupplierBaseline(unittest.TestCase):
    def setUp(self):
        self.fake = _Frappe()
        self.fake.db.get_value = lambda doctype, filters, field: next((
            row['supplier_name'] for row in self.fake.masters
            if doctype == 'Supplier' and row['doctype'] == doctype
            and row['supplier_name'] == filters['supplier_name']), None)
        self.patch = patch.object(pm, 'frappe', self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.read = own_read()

    def initial_import(self, read=None):
        self.read = read or self.read
        self.assertEqual(pm.upsert(SUPPLIER_ROW, None, pm._Masters(), 'source-a', read=self.read), 'added')
        self.item = self.fake.items['PV-1001']
        self.link = SimpleNamespace(name='LINK-1', **self.fake.links[-1][1])

    def update(self, **source_fields):
        row = {**SUPPLIER_ROW, 'base_data': {**SUPPLIER_ROW['base_data'], 'name': 'New HERO name', **source_fields}}
        return pm.upsert(row, self.link, pm._Masters(), 'source-a', read=self.read)

    def test_import_then_local_supplier_part_change_blocks_before_item_write(self):
        self.initial_import()
        baseline = json.loads(self.link.target_field_hashes)['supplier']
        self.assertEqual(baseline, {'supplier': 'Krannich Solar', 'supplier_part_no': '0151231'})
        self.item['supplier_items'][0]['supplier_part_no'] = 'LOCAL'
        before = deepcopy(dict(self.item))
        self.assertEqual(self.update(), 'conflict')
        self.assertEqual(dict(self.item), before)
        self.assertFalse(self.item.saved)
        self.assertEqual(self.fake.links[-1][2]['sync_status'], 'Conflict')

    def test_source_part_change_updates_managed_row_and_preserves_local_other_supplier(self):
        self.initial_import()
        self.item['supplier_items'].append({'supplier': 'Unrelated local supplier', 'supplier_part_no': 'LOCAL'})
        self.assertEqual(self.update(supplier_id='NEW-HERO-PART'), 'updated')
        self.assertEqual(self.item['supplier_items'], [
            {'supplier': 'Krannich Solar', 'supplier_part_no': 'NEW-HERO-PART'},
            {'supplier': 'Unrelated local supplier', 'supplier_part_no': 'LOCAL'}])
        self.assertEqual(json.loads(self.fake.links[-1][2]['target_field_hashes'])['supplier']['supplier_part_no'],
                         'NEW-HERO-PART')

    def test_missing_or_ambiguous_managed_supplier_row_blocks(self):
        self.initial_import()
        for changed in ([], [{'supplier': 'Krannich Solar', 'supplier_part_no': '0151231'}] * 2):
            with self.subTest(rows=changed):
                self.item['supplier_items'] = changed
                self.assertEqual(self.update(), 'conflict')
                self.assertFalse(self.item.saved)

    def test_legacy_baseline_adopts_only_an_already_matching_row(self):
        self.initial_import()
        original = json.loads(self.link.target_field_hashes)
        original.pop('supplier')
        self.link.target_field_hashes = json.dumps(original)
        self.assertEqual(self.update(), 'updated')
        self.assertEqual(json.loads(self.fake.links[-1][2]['target_field_hashes'])['supplier']['supplier_part_no'], '0151231')

    def test_legacy_baseline_does_not_overwrite_a_local_supplier_part(self):
        self.initial_import()
        original = json.loads(self.link.target_field_hashes)
        original.pop('supplier')
        self.link.target_field_hashes = json.dumps(original)
        self.item['supplier_items'][0]['supplier_part_no'] = 'LOCAL'
        self.assertEqual(self.update(), 'conflict')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], 'LOCAL')

    def test_legacy_baseline_cannot_prove_a_changed_upstream_supplier_part_is_safe(self):
        self.initial_import()
        original = json.loads(self.link.target_field_hashes)
        original.pop('supplier')
        self.link.target_field_hashes = json.dumps(original)
        self.assertEqual(self.update(supplier_id='NEW-HERO-PART'), 'conflict')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], '0151231')

    def test_removed_supplier_mapping_leaves_local_supplier_rows_alone(self):
        self.initial_import()
        self.item['supplier_items'][0]['supplier_part_no'] = 'LOCAL'
        self.read = own_read(drop=('supplier_items',))
        self.assertEqual(self.update(), 'updated')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], 'LOCAL')
        # Retain the original proof; a later re-enabled supplier rule must not adopt LOCAL.
        self.assertEqual(json.loads(self.fake.links[-1][2]['target_field_hashes'])['supplier']['supplier_part_no'], '0151231')

    def test_no_uom_or_price_rules_create_with_fallback_and_update_using_current_uom(self):
        self.initial_import(own_read(drop=('stock_uom', pm.BUYING, pm.SELLING)))
        self.assertNotIn('stock_uom', pm.item_values(SUPPLIER_ROW, self.read))
        self.assertEqual(self.item['stock_uom'], pm.REQUIRED_FALLBACKS['stock_uom'])
        self.item['stock_uom'] = 'Box'  # not mapped, so this remains locally owned
        with patch.object(pm, 'plan_price', wraps=pm.plan_price) as prices:
            self.assertEqual(self.update(), 'updated')
        self.assertEqual(self.item['stock_uom'], 'Box')
        self.assertEqual([call.args[3] for call in prices.call_args_list], ['Box', 'Box'])
        self.assertTrue(all(call.args[2] is None for call in prices.call_args_list))


if __name__ == '__main__':
    unittest.main()
