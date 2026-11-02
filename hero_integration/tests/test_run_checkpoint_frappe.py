"""Opt-in checkpoint transactions use a connection-local temporary Run table and unique fixtures; no migration or HERO transport."""
import json
import os
from datetime import timedelta
from threading import Event, Thread
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import frappe
from hero_integration.services import migration
from hero_integration.services.mapping_runtime import effective_rule_fingerprint


@skipUnless(os.environ.get('HERO_CHECKPOINT_TEST_SITE'), 'requires explicitly authorized development site')
class TestCheckpointMariaDB(TestCase):
    def setUp(self):
        self.assertEqual(frappe.local.site, os.environ['HERO_CHECKPOINT_TEST_SITE'])
        self.db = frappe.db
        self.token = 'checkpoint-08a-' + uuid4().hex
        self.rule = self.token + '-rule'
        self.customers = []
        self.started = frappe.utils.now_datetime()
        self.db.sql('CREATE TEMPORARY TABLE `hero_checkpoint_08a_fixture` '
                    '(name varchar(140) primary key, checkpoint_contract_json longtext, resume_record_index int) ENGINE=InnoDB')
        self.db.sql('INSERT INTO `tabHERO Frappe Mapping` '
                    '(name, definition_id, operation, status, source_path, personal_graphql_mapping) '
                    'VALUES (%s,%s,%s,%s,%s,%s)',
                    (self.rule, self.rule, self.token, 'Active', 'nr', ''))
        def contract(_run, *, lock=False):
            return {'version': 1, 'rules': effective_rule_fingerprint((self.rule,), 'synthetic-root', lock=lock)}
        self.contract = contract
        self.bound = json.dumps(contract(None), sort_keys=True)
        self.db.sql('INSERT INTO `hero_checkpoint_08a_fixture` VALUES (%s,%s,0)', (self.token, self.bound))
        self.db.commit()
        self.run = SimpleNamespace(name=self.token, checkpoint_contract_json=self.bound, resume_record_index=0,
            save=lambda: self.db.sql('UPDATE `hero_checkpoint_08a_fixture` SET resume_record_index=%s WHERE name=%s',
                                    (self.run.resume_record_index, self.token)))
        actual_get_doc = frappe.get_doc
        def get_doc(doctype, name=None, **kwargs):
            if doctype == 'HERO Integration Run' and name == self.token:
                rows = self.db.sql('SELECT checkpoint_contract_json FROM `hero_checkpoint_08a_fixture` '
                                   'WHERE name=%s FOR UPDATE', (self.token,))
                return SimpleNamespace(state='Running', owner=frappe.session.user, started_on=self.started,
                    lease_expires_on=self.started + timedelta(hours=1), checkpoint_contract_json=rows[0][0])
            return actual_get_doc(doctype, **kwargs) if name is None else actual_get_doc(doctype, name, **kwargs)
        self.patches = [patch.object(migration, '_run_checkpoint_contract', side_effect=contract),
                        patch.object(migration, '_locked_active_run', return_value=self.token),
                        patch.object(frappe, 'get_doc', side_effect=get_doc)]
        for p in self.patches: p.start()

    def tearDown(self):
        self.db.rollback()
        for p in reversed(self.patches): p.stop()
        for name in self.customers:
            if self.db.exists('Customer', name): frappe.delete_doc('Customer', name, force=False)
        self.db.sql('DELETE FROM `tabHERO Frappe Mapping` WHERE name=%s AND definition_id=%s', (self.rule, self.rule))
        self.db.commit()
        self.db.sql('DROP TEMPORARY TABLE `hero_checkpoint_08a_fixture`')

    def insert_customer(self, suffix):
        name = self.token + suffix
        self.customers.append(name)
        frappe.get_doc({'doctype': 'Customer', 'customer_name': name, 'customer_type': 'Individual'}).insert(set_name=name)
        self.run.resume_record_index += 1
        self.run.save()
        return name

    def test_acknowledged_customer_survives_later_contract_conflict_and_interruption(self):
        with migration._checkpoint_transaction(self.run, self.started):
            first = self.insert_customer('-first')
        with self.assertRaises(migration.MappingPlanError):
            with migration._checkpoint_transaction(self.run, self.started):
                second = self.insert_customer('-second')
                self.db.sql('UPDATE `tabHERO Frappe Mapping` SET source_path=%s WHERE name=%s', ('id', self.rule))
        self.assertTrue(self.db.exists('Customer', first))
        self.assertFalse(self.db.exists('Customer', second))
        self.assertEqual(self.db.sql('SELECT resume_record_index FROM `hero_checkpoint_08a_fixture`')[0][0], 1)
        with self.assertRaises(RuntimeError):
            with migration._checkpoint_transaction(self.run, self.started):
                third = self.insert_customer('-third')
                raise RuntimeError('synthetic interruption')
        self.assertFalse(self.db.exists('Customer', third))
        self.assertTrue(self.db.exists('Customer', first))
        self.run.resume_record_index = 1  # A recovered worker reloads the durable checkpoint.
        with migration._checkpoint_transaction(self.run, self.started):
            continued = self.insert_customer('-continued')
        self.assertTrue(self.db.exists('Customer', continued))
        self.assertEqual(self.db.sql('SELECT resume_record_index FROM `hero_checkpoint_08a_fixture`')[0][0], 2)

    def test_concurrent_field_change_waits_for_checkpoint_then_rejects_stale_continuation(self):
        site, sites_path = frappe.local.site, frappe.local.sites_path
        ready, changed = Event(), Event()
        errors = []
        def writer():
            try:
                frappe.init(site=site, sites_path=sites_path)
                frappe.connect(); frappe.set_user('Administrator')
                ready.set()
                frappe.db.sql('UPDATE `tabHERO Frappe Mapping` SET source_path=%s WHERE name=%s', ('id', self.rule))
                frappe.db.commit(); changed.set()
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                frappe.destroy()
        thread = Thread(target=writer)
        with migration._checkpoint_transaction(self.run, self.started):
            thread.start()
            self.assertTrue(ready.wait(5))
            self.assertFalse(changed.wait(.2), 'rule changed while acknowledgement held its row lock')
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(changed.is_set())
        with self.assertRaises(migration.MappingPlanError):
            with migration._checkpoint_transaction(self.run, self.started): self.fail('stale work admitted')

    def test_actual_run_reader_releases_database_locks_before_transport(self):
        from hero_integration.client.graphql import HeroEndpoint
        from hero_integration.services.introspection import endpoint_fingerprint
        from hero_integration.services.read_contract import ReadContract
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _target_lock_name
        from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
        endpoint = HeroEndpoint('https://example.invalid/graphql', 'example.invalid')
        config = SimpleNamespace(endpoint=endpoint, api_token='synthetic', timeout_seconds=1,
            source_instance='synthetic', source_binding_revision=1, source_binding_confirmed=True)
        self.run.graphql_service, self.run.graphql_mapping = 'SERVICE', 'MAP'
        self.run.service_revision = self.run.mapping_revision = self.run.source_binding_revision = 1
        self.run.source_instance = 'synthetic'
        contract = ReadContract('SERVICE', 1, 'MAP', 1, 'HeroCustomers', 'contacts', 'id',
                                (self.rule,), {'offset': '$offset', 'first': '$page_size'}, 1)
        docs = {
            ('HERO GraphQL Service', 'SERVICE'): SimpleNamespace(schema_snapshot='SNAP', query_document='query { contacts { id } }'),
            ('HERO GraphQL Mapping', 'MAP'): SimpleNamespace(),
            ('HERO GraphQL Schema Snapshot', 'SNAP'): SimpleNamespace(source_instance='synthetic',
                endpoint_fingerprint=endpoint_fingerprint(endpoint.url, 'synthetic')),
        }
        delegated = frappe.get_doc
        def get_doc(doctype, name=None, **kwargs):
            if isinstance(doctype, str) and (doctype, name) in docs: return docs[(doctype, name)]
            return delegated(doctype, name, **kwargs)
        lock_exchange_context(self.token, self.token, 'QUERY_TO_FRAPPE')
        lock_name = _target_lock_name('personal-draft-exchange', self.token,
                                     json.dumps([self.token, 'QUERY_TO_FRAPPE'], separators=(',', ':')))
        self.assertEqual(self.db.sql('SELECT IS_USED_LOCK(%s) = CONNECTION_ID()', (lock_name,))[0][0], 1)
        def transport(*args):
            self.assertEqual(self.db.sql('SELECT IS_FREE_LOCK(%s)', (lock_name,))[0][0], 1)
            self.assertFalse(getattr(self.db, '_hero_mapping_target_locks', None))
            return {'contacts': []}
        with patch.object(frappe, 'get_doc', side_effect=get_doc), \
             patch('hero_integration.services.read_contract.from_documents', return_value=contract), \
             patch.object(migration, 'HeroGraphQLClient', return_value=SimpleNamespace(query=transport)), \
             patch.object(migration, '_request_rate', return_value=10000):
            source = migration._source_for_run(self.run, config)
            self.assertEqual(source.customers().records, ())

    def test_mapping_publication_uses_same_transaction_scoped_root_lock(self):
        from hero_integration.hero_integration.doctype.hero_graphql_mapping.hero_graphql_mapping import lock_exchange_context
        site, sites_path = frappe.local.site, frappe.local.sites_path
        ready, acquired = Event(), Event()
        errors = []
        lock_exchange_context(self.token, self.token, 'QUERY_TO_FRAPPE')
        def publisher():
            try:
                frappe.init(site=site, sites_path=sites_path)
                frappe.connect(); frappe.set_user('Administrator')
                ready.set()
                lock_exchange_context(self.token, self.token, 'QUERY_TO_FRAPPE')
                acquired.set()
                frappe.db.rollback()
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                frappe.destroy()
        thread = Thread(target=publisher)
        thread.start()
        self.assertTrue(ready.wait(5))
        self.assertFalse(acquired.wait(.2))
        self.db.commit()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertTrue(acquired.is_set())
