"""Confirming one sent Item field must not acknowledge unrelated local edits."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import mapped_update_v2 as update
from hero_integration.services import product_migration as products
from hero_integration.tests.test_product_mapping import _Frappe, SUPPLIER_ROW, own_read


class TestProductPartialWriteback(TestCase):
    def setUp(self):
        self.site = _Frappe()
        self.site.db.set_value = lambda doctype, name, values, **kwargs: self.site.set_value(doctype, name, values)
        self.site.db.get_value = lambda doctype, filters, field: next((
            row['supplier_name'] for row in self.site.masters
            if doctype == 'Supplier' and row['doctype'] == doctype
            and row['supplier_name'] == filters['supplier_name']), None)
        self.read = own_read()
        self.row = deepcopy(SUPPLIER_ROW)
        self.row['base_data']['description'] = 'Original description'
        self.patches = [patch.object(products, 'frappe', self.site), patch.object(update, 'frappe', self.site)]
        for pending in self.patches:
            pending.start()
            self.addCleanup(pending.stop)
        self.assertEqual(products.upsert(self.row, None, products._Masters(), 'source-a', read=self.read), 'added')
        self.item = self.site.items['PV-1001']
        self.link = NS(name='L1', source_field_hashes='{}', **self.site.links[-1][1])
        state = json.loads(self.link.target_field_hashes)
        state['prices'] = {products.BUYING: {'name': 'BUY-1', 'rate': 145.55, 'uom': 'Stk'},
                           products.SELLING: {'name': 'SELL-1', 'rate': 98, 'uom': 'Stk'}}
        self.link.target_field_hashes = json.dumps(state)
        self.baseline = deepcopy(state)
        self.original_name = self.item['item_name']

    def acknowledge_description(self):
        assignment = {'id': 'description', 'source': {'doctype': 'Item', 'field': 'description'}, 'resolver': 'FIELD'}
        ctx = NS(content={'area': 'HeroProducts', 'assignments': [assignment]},
                 source_links={'description': self.link}, document=self.item, link=self.link, query_plan=self.read.plan)
        self.item['description'] = 'Sent description'
        after = deepcopy(self.row)
        after['base_data']['description'] = 'Sent description'
        prepared = NS(changes=[{'id': 'description', 'local': 'Sent description'}])
        with patch.object(update, '_canonical', return_value='Sent description'):
            update._acknowledge(ctx, prepared, after)
        values = self.site.links[-1][2]
        for key, value in values.items():
            setattr(self.link, key, value)
        self.row = after
        return values

    def import_changed_price(self):
        row = {**self.row, 'base_price': self.row['base_price'] + 1}
        return products.upsert(row, self.link, products._Masters(), 'source-a', read=self.read)

    def test_partial_send_preserves_unsent_local_name_and_supplier_and_price_proofs(self):
        self.item['item_name'] = 'Unsent local name'
        values = self.acknowledge_description()
        state = json.loads(self.link.target_field_hashes)
        self.assertEqual(state['fields']['item_name'], self.baseline['fields']['item_name'])
        self.assertEqual(state['fields']['description'], products.digest('Sent description'))
        self.assertEqual(state['supplier'], self.baseline['supplier'])
        self.assertEqual(state['prices'], self.baseline['prices'])
        self.assertNotIn('target_payload_hash', values)
        self.assertEqual(self.import_changed_price(), 'conflict')
        self.assertEqual(self.item['item_name'], 'Unsent local name')
        self.assertEqual(self.item['description'], 'Sent description')

    def test_field_proofs_reject_an_aggregate_already_renewed_by_the_old_bug(self):
        self.item['item_name'] = 'Unsent local name'
        self.acknowledge_description()
        self.link.target_payload_hash = products.owned_digest(self.item, tuple(self.baseline['fields']))
        self.assertEqual(self.import_changed_price(), 'conflict')
        self.assertEqual(self.item['item_name'], 'Unsent local name')

    def test_only_confirmed_local_change_can_refresh_aggregate_and_import_next_price(self):
        values = self.acknowledge_description()
        self.assertEqual(values['target_payload_hash'], products.owned_digest(self.item, tuple(self.baseline['fields'])))
        self.assertEqual(self.import_changed_price(), 'updated')
        self.assertEqual(self.item['description'], 'Sent description')

    def test_reverting_unsent_change_uses_field_proofs_despite_old_aggregate(self):
        self.item['item_name'] = 'Unsent local name'
        original_aggregate = self.link.target_payload_hash
        self.acknowledge_description()
        self.item['item_name'] = self.original_name
        self.assertEqual(self.link.target_payload_hash, original_aggregate)
        self.assertEqual(self.import_changed_price(), 'updated')

    def test_legacy_link_without_field_proofs_keeps_aggregate_conflict_protection(self):
        legacy = deepcopy(self.baseline)
        legacy.pop('fields')
        self.link.target_field_hashes = json.dumps(legacy)
        self.item['item_name'] = 'Unsent local name'
        self.assertEqual(self.import_changed_price(), 'conflict')
        self.assertEqual(self.item['item_name'], 'Unsent local name')

    def test_description_send_does_not_acknowledge_an_unimported_remote_name(self):
        self.row['base_data']['name'] = 'New remote name'
        self.acknowledge_description()
        self.assertEqual(self.item['item_name'], self.original_name)
        self.assertEqual(products.upsert(self.row, self.link, products._Masters(), 'source-a', read=self.read), 'updated')
        self.assertEqual(self.item['item_name'], 'New remote name')
        self.assertEqual(self.item['description'], 'Sent description')

    def test_remote_name_after_partial_send_still_conflicts_with_unsent_local_name(self):
        self.row['base_data']['name'] = 'New remote name'
        self.item['item_name'] = 'Unsent local name'
        self.acknowledge_description()
        self.assertEqual(products.upsert(self.row, self.link, products._Masters(), 'source-a', read=self.read), 'conflict')
        self.assertEqual(self.item['item_name'], 'Unsent local name')
