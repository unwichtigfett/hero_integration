"""Product imports and pending native prices on a disposable MariaDB site."""
from dataclasses import replace
import os
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration.services import product_migration as product
from hero_integration.tests.test_mapped_collection_relations import PRICES
from hero_integration.tests.test_product_migration import BUILTIN, ROW


class TestProductPendingMariaDB(TestCase):
    def setUp(self):
        if (os.environ.get('HERO_TEST_PRIVATE_SITE') != getattr(frappe.local, 'site', None)
                or os.environ.get('HERO_SYNTHETIC_PRODUCT_TEST') != '1'):
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        frappe.set_user('Administrator')
        self.identity = 'synthetic-' + uuid4().hex[:12]
        self.row = {**ROW, 'product_id': self.identity, 'nr': self.identity, 'base_price': None, 'list_price': None,
                    'sales_prices': [{'id': None, 'supply_sales_price_id': 10, 'net_price_per_unit': 12.5,
                                      'supply_sales_price': {'is_default': False}}]}
        self.read = replace(BUILTIN, plan=replace(BUILTIN.plan,
            adapter_options={'version': 2, 'native_relations': [PRICES]}))

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_messages()

    def test_article_is_saved_and_missing_price_can_be_reconciled_later(self):
        warnings = []
        result = product.upsert(self.row, None, product._Masters(), 'synthetic', self.read, relation_warnings=warnings)
        self.assertEqual(result, 'added')
        link = frappe.get_doc('HERO Integration Link', {'source_type': 'supply_product',
                              'source_instance': 'synthetic', 'source_id': self.identity})
        self.assertTrue(frappe.db.exists('Item', link.target_name))
        self.assertEqual(link.sync_status, 'Pending Review')
        self.assertIn('stable source identity', warnings[0])
        self.assertFalse(frappe.db.exists('Item Price', {'item_code': link.target_name}))

        price_list = frappe.db.exists('Price List', 'Standard Selling')
        if not price_list:
            frappe.get_doc({'doctype': 'Price List', 'price_list_name': 'Standard Selling',
                            'currency': 'EUR', 'selling': 1, 'buying': 0, 'enabled': 1}).insert()
        self.row['sales_prices'][0]['id'] = 991234
        outcome = product.upsert(self.row, link, product._Masters(), 'synthetic', self.read)
        self.assertEqual(outcome, 'updated')
        link.reload()
        self.assertEqual((link.sync_status, link.conflict_detail), ('Synchronized', ''))
        self.assertEqual(frappe.db.count('Item Price', {'item_code': link.target_name, 'price_list': 'Standard Selling'}), 1)

    def test_bounded_run_reports_import_and_pending_price_separately(self):
        from hero_integration.services import migration_testing

        run = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': 'Product Migration',
                              'mode': 'Execute', 'state': 'Running', 'source_instance': 'synthetic'}).insert()
        saved = {}
        with patch.object(product, 'fetch_page', return_value=[self.row]), \
             patch.object(migration_testing, 'test_state', return_value={}), \
             patch.object(migration_testing, 'save_test_state', side_effect=lambda _run, _op, state: saved.update(state)), \
             patch.object(frappe.db, 'commit'):
            state = product.sync_products(object(), source_instance='synthetic', read=self.read,
                                          record_limit=1, run_name=run.name)
        self.assertEqual((state['counts']['added'], state['counts']['pending_review']), (1, 1))
        link = frappe.get_doc('HERO Integration Link', {'source_type': 'supply_product',
                              'source_instance': 'synthetic', 'source_id': self.identity})
        self.assertTrue(frappe.db.exists('HERO Integration Record', {'run': run.name,
            'target_name': link.target_name, 'result': 'PENDING_REVIEW', 'technical_action': 'product_relation_review'}))
