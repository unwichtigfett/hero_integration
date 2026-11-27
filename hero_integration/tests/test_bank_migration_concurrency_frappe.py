"""Two real MariaDB sessions migrate the same IBAN under different customer identities."""
import os
import threading
from queue import Queue
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration.services import deferred_references, mapped_relations
from hero_integration.tests.test_mapped_relations import BANK


class TestConcurrentBankMigration(TestCase):
    def test_stale_worker_preserves_the_first_owner_and_finishes_with_one_account(self):
        site = os.environ.get('HERO_TEST_PRIVATE_SITE')
        if not site or site != frappe.local.site or os.environ.get('HERO_SYNTHETIC_BINDING_TEST') != '1':
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        path = frappe.local.sites_path
        tag = uuid4().hex
        source = 'bank-race-' + tag
        bank = 'Synthetic bank ' + tag
        bic = 'TESTDE' + tag[:5].upper()
        bban = '10000000' + str(int(tag[:8],16)).zfill(10)
        iban = 'DE' + str(98 - int(bban + '131400') % 97).zfill(2) + bban
        parties = []
        ready, snapshot = threading.Event(), threading.Event()
        outcomes = Queue()
        plan = SimpleNamespace(adapter_options={'native_relations': [BANK]}, mapping_version='synthetic')
        apply = mapped_relations._apply

        def hold_first(plan, item, prepared, parent_type, parent_name, parent):
            if parent_name == parties[0]:
                ready.set()
                if not snapshot.wait(10):
                    raise TimeoutError('Second connection did not open its read view')
            return apply(plan,item,prepared,parent_type,parent_name,parent)

        def worker(index):
            try:
                frappe.init(site=site,sites_path=path)
                frappe.connect()
                frappe.set_user('Administrator')
                if index == 1:
                    if not ready.wait(10):
                        raise TimeoutError('First connection did not finish preflight')
                    frappe.db.get_value('Bank Account',{'iban':iban},'name')
                    snapshot.set()
                row = {'_hero_source_instance':source, 'accounting':{'id':index+1,'bank_name':bank,'bic':bic,'iban':iban}}
                deferred_references._resolve_committed((row, parties[index]))
                outcomes.put('created')
            except Exception as error:
                outcomes.put(str(error))
            finally:
                if frappe.db:
                    frappe.db.rollback()
                frappe.destroy()

        def resolve(args, **kwargs):
            row, party = args
            return mapped_relations.sync(plan, row, 'Customer', party)

        try:
            frappe.get_doc({'doctype':'Bank','bank_name':bank,'swift_number':bic}).insert()
            for index in range(2):
                customer = frappe.get_doc({'doctype':'Customer','customer_type':'Company',
                                           'customer_name':source + str(index)}).insert()
                parties.append(customer.name)
            frappe.db.commit()
            with patch.object(mapped_relations,'_apply',hold_first), patch.object(deferred_references,'resolve_one',resolve):
                threads = [threading.Thread(target=worker,args=(index,)) for index in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            result = sorted(outcomes.get(timeout=1) for _ in range(2))
            self.assertEqual(result, ['An existing IBAN requires explicit account reconciliation; no account was adopted.',
                                      'created'])
            # Read the other sessions' committed records from a fresh snapshot.
            frappe.db.rollback()
            self.assertEqual(frappe.get_all('Bank Account',filters={'bank':bank},pluck='party'),parties[:1])
        finally:
            frappe.db.rollback()
            frappe.db.delete('HERO Integration Link',{'source_instance':source})
            frappe.db.delete('Bank Account',{'bank':bank})
            frappe.db.delete('Bank',{'name':bank})
            for party in parties:
                frappe.db.delete('Customer',{'name':party})
            frappe.db.commit()
