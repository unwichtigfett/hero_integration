"""A mixed-result run may replay only a Customer's own successful writer receipt."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import deferred_references as refs


class TestAcceptedCustomerReceipt(unittest.TestCase):
    def test_receipt_must_belong_to_the_exact_source_run_and_native_target(self):
        pending = SimpleNamespace(origin_run='run-a', source_id='71')
        proof = {'__hero_origin_binding': {'target_doctype': 'Customer', 'target_name': 'C-71'}}
        with patch.object(refs, 'frappe') as frappe:
            frappe.db.exists.return_value = None
            self.assertFalse(refs._accepted_customer_receipt(pending, proof))
            frappe.db.exists.return_value = 'receipt'
            self.assertTrue(refs._accepted_customer_receipt(pending, proof))
            dt, criteria = frappe.db.exists.call_args.args
            self.assertEqual(dt, 'HERO Integration Record')
            self.assertEqual(criteria, {'run': 'run-a', 'source_type': 'customer', 'source_id': '71',
                'technical_action': 'hero_to_frappe', 'target_doctype': 'Customer', 'target_name': 'C-71',
                'result': ['in', ['CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED']]})

    def test_incomplete_origin_claim_never_queries_for_a_receipt(self):
        pending = SimpleNamespace(origin_run='run-a', source_id='71')
        for proof in ({}, {'target_doctype': 'Contact', 'target_name': 'C-71'}, {'target_doctype': 'Customer'}):
            with patch.object(refs, 'frappe') as frappe:
                self.assertFalse(refs._accepted_customer_receipt(pending, {'__hero_origin_binding': proof}))
                frappe.db.exists.assert_not_called()
