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

    def test_explicit_null_or_blank_clears_owned_number_and_keeps_other_supplier(self):
        self.initial_import()
        self.item['supplier_items'].append({'supplier': 'Other', 'supplier_part_no': 'KEEP'})
        for empty in (None, '', '   '):
            with self.subTest(empty=empty):
                self.item['supplier_items'][0]['supplier_part_no'] = '0151231'
                self.assertEqual(self.update(supplier_id=empty), 'updated')
                self.assertIsNone(self.item['supplier_items'][0]['supplier_part_no'])
                self.assertEqual(self.item['supplier_items'][1]['supplier_part_no'], 'KEEP')
                state = json.loads(self.fake.links[-1][2]['target_field_hashes'])
                self.assertIsNone(state['supplier']['supplier_part_no'])
                self.item['item_name'] = 'MODUL: AXIpremium'  # Restore the original baseline between cases.

    def test_explicit_clear_preserves_locally_changed_number_as_conflict(self):
        self.initial_import()
        self.item['supplier_items'][0]['supplier_part_no'] = 'LOCAL'
        self.assertEqual(self.update(supplier_id=None), 'conflict')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], 'LOCAL')

    def test_missing_part_preserves_number_and_does_not_acknowledge_local_change(self):
        self.initial_import()
        self.item['supplier_items'][0]['supplier_part_no'] = 'LOCAL'
        row = deepcopy(SUPPLIER_ROW)
        del row['base_data']['supplier_id']
        row['base_data']['name'] = 'New HERO name'
        self.assertNotIn('supplier_part_no', pm.item_values(row, self.read)['supplier'])
        self.assertEqual(pm.upsert(row, self.link, pm._Masters(), 'source-a', read=self.read), 'updated')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], 'LOCAL')
        state = json.loads(self.fake.links[-1][2]['target_field_hashes'])
        self.assertEqual(state['supplier']['supplier_part_no'], '0151231')

    def test_legacy_baseline_cannot_clear_an_unproven_number(self):
        self.initial_import()
        original = json.loads(self.link.target_field_hashes)
        original.pop('supplier')
        self.link.target_field_hashes = json.dumps(original)
        self.assertEqual(self.update(supplier_id=None), 'conflict')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], '0151231')

    def test_missing_part_cannot_adopt_legacy_local_number_for_a_later_clear(self):
        self.initial_import()
        original = json.loads(self.link.target_field_hashes)
        original.pop('supplier')
        self.link.target_field_hashes = json.dumps(original)
        self.item['supplier_items'][0]['supplier_part_no'] = 'LOCAL'
        row = deepcopy(SUPPLIER_ROW)
        del row['base_data']['supplier_id']
        row['base_data']['name'] = 'New HERO name'
        self.assertEqual(pm.upsert(row, self.link, pm._Masters(), 'source-a', read=self.read), 'updated')
        for key, value in self.fake.links[-1][2].items():
            setattr(self.link, key, value)
        self.assertNotIn('supplier', json.loads(self.link.target_field_hashes))
        row['base_data']['supplier_id'] = None
        self.assertEqual(pm.upsert(row, self.link, pm._Masters(), 'source-a', read=self.read), 'conflict')
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], 'LOCAL')

    def test_invalid_part_type_is_not_treated_as_an_explicit_clear(self):
        self.initial_import()
        with self.assertRaisesRegex(ValueError, 'must be text or null'):
            self.update(supplier_id=123)
        self.assertEqual(self.item['supplier_items'][0]['supplier_part_no'], '0151231')

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
