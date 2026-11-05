"""Primary address backfill only adopts a unique, exclusively bound Billing address."""
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import primary_address as pa


class TestPrimaryAddress(TestCase):
    def run_fill(self, *, pointer=None, rows=None, bindings=None, links=None):
        customer=NS(customer_primary_address=pointer)
        address=NS(is_primary_address=0, links=links if links is not None else [NS(link_doctype='Customer',link_name='C')], save=Mock(),get_display=lambda:'Formatted address')
        db=NS(sql=Mock(return_value=rows if rows is not None else [NS(name='A',address_type='Billing',is_primary_address=0)]),set_value=Mock())
        fake=NS(db=db,get_doc=Mock(side_effect=lambda dt,*a,**k:customer if dt=='Customer' else address),get_all=Mock(return_value=['binding'] if bindings is None else bindings))
        with patch.object(pa,'frappe',fake),patch('hero_integration.services.change_detection.managed_target_update',return_value=nullcontext()):
            result=pa.fill_primary_address('C','default')
        return result,db,address

    def test_sets_both_native_relations_and_display(self):
        changed,db,address=self.run_fill()
        self.assertTrue(changed)
        self.assertEqual(address.is_primary_address,1)
        self.assertEqual(db.set_value.call_args.args,('Customer','C',{'customer_primary_address':'A','primary_address':'Formatted address'}))

    def test_preserves_manual_pointer_and_ambiguous_or_foreign_addresses(self):
        for options in ({'pointer':'manual'}, {'bindings':[]}, {'bindings':['one','two']}, {'rows':[]},
                        {'rows':[NS(name='A',address_type='Billing',is_primary_address=0),NS(name='B',address_type='Billing',is_primary_address=0)]},
                        {'links':[NS(link_doctype='Supplier',link_name='S')]}):
            with self.subTest(options=options):
                changed,db,address=self.run_fill(**options)
                self.assertFalse(changed)
                db.set_value.assert_not_called()
                address.save.assert_not_called()
