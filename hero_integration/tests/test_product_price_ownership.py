"""A price list has exactly one writer within the selected product mapping."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import product_migration as products
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.tests.test_product_mapping import BUILTIN
from hero_integration.tests.test_mapped_collection_relations import PRICES


class TestProductPriceOwnership(TestCase):
    def make_plan(self, *, without=(), collection=None):
        definitions = [spec for spec in BUILTIN.plan.definitions
                       if spec.target_doctype != 'Item Price' or spec.relation not in without]
        return plan_from_definitions('HeroProducts', definitions, target_operation='HeroProducts',
            adapter_options={'version': 2, 'native_relations': [deepcopy(collection or PRICES)]})

    def test_scalar_and_collection_selling_overlap_is_rejected_before_any_import(self):
        with patch.object(products, 'frappe') as database, patch.object(products, 'apply_price') as writer:
            with self.assertRaisesRegex(ValueError, 'Each price list'):
                self.make_plan()
        database.get_doc.assert_not_called()
        database.db.set_value.assert_not_called()
        writer.assert_not_called()

    def test_scalar_buying_and_collection_selling_have_distinct_owners(self):
        plan = self.make_plan(without=('Standard Selling',))
        self.assertEqual([spec.relation for spec in plan.definitions if spec.target_doctype == 'Item Price'],
                         ['Standard Buying'])
        self.assertEqual(plan.adapter_options['native_relations'][0]['price_lists']['10']['name'], 'Standard Selling')

    def test_collection_overlap_is_detected_by_target_price_list_not_source_identity(self):
        collection = deepcopy(PRICES)
        collection['price_lists'] = {'98765': {'name': 'Standard Buying', 'currency': 'EUR', 'create': False}}
        with self.assertRaisesRegex(ValueError, 'Each price list'):
            self.make_plan(collection=collection)

    def test_scalar_selling_can_coexist_with_a_distinct_collection_price_list(self):
        collection = deepcopy(PRICES)
        collection['price_lists'].pop('10')
        self.make_plan(collection=collection)

    def test_collection_can_own_all_selling_prices_without_scalar_rules(self):
        self.make_plan(without=('Standard Buying', 'Standard Selling'))
