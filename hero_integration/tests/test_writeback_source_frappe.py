"""Committed source changes must be visible despite an older request snapshot.

Requires an explicitly selected disposable MariaDB site. All HERO clients are
mocked; only test-owned Settings values are temporarily committed and restored.
"""
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
import frappe
from frappe.database import get_db
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from hero_integration import api
from hero_integration.client.settings import get_configuration
from hero_integration.services.binding_registry import BindingRegistryService, InMemoryBindingStorage, state_fingerprint
from hero_integration.services.frappe_writeback import WritebackContractError
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.sync_engine import OutboundApproval, OutboundIntent, execute_sync_hero
from hero_integration.tests.test_verified_writeback import MUTATION, context, mutation_snapshot

SITE = os.environ.get('HERO_WRITEBACK_SOURCE_TEST_SITE')
SETTINGS = 'HERO Integration Settings'
FIELDS = ('allowed_hero_hosts', 'hero_api_url', 'hero_source_instance', 'source_binding_revision',
          'source_binding_confirmed_revision', 'timeout_seconds', 'hero_api_token')
AUTH_KEY = (SETTINGS, SETTINGS, 'hero_api_token')


@unittest.skipUnless(SITE, 'requires HERO_WRITEBACK_SOURCE_TEST_SITE set to a disposable MariaDB site')
class TestWritebackSourceSnapshotMariaDB(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(frappe.local.site, SITE)
        if frappe.conf.get('db_type', 'mariadb') != 'mariadb':
            self.skipTest('requires MariaDB REPEATABLE READ')
        self.primary = frappe.local.db
        self.writer = None
        self.original_isolation = self.primary.sql('SELECT @@SESSION.tx_isolation')[0][0]
        self.saved_singles = self.primary.sql(
            f"SELECT field, value FROM `tabSingles` WHERE doctype=%s AND field IN ({','.join(['%s'] * len(FIELDS))})",
            (SETTINGS, *FIELDS), as_dict=True,
        )
        self.saved_auth = self.primary.sql(
            'SELECT doctype, name, fieldname, password, encrypted FROM `__Auth` '
            'WHERE doctype=%s AND name=%s AND fieldname=%s', AUTH_KEY,
        )
        # A temporary in-memory key avoids writing site_config or decrypting real credentials.
        encryption = patch.dict(frappe.local.conf, {'encryption_key': Fernet.generate_key().decode()})
        encryption.start()
        self.addCleanup(encryption.stop)
        self.addCleanup(self._restore_settings)
        self._write_values(self.primary, {
            'allowed_hero_hosts': 'example.invalid', 'hero_api_url': 'https://example.invalid/graphql',
            'hero_source_instance': 'hero', 'source_binding_revision': '7',
            'source_binding_confirmed_revision': '7', 'timeout_seconds': '1', 'hero_api_token': '********',
        })
        set_encrypted_password(SETTINGS, SETTINGS, 'synthetic-snapshot-token', 'hero_api_token')
        self.primary.commit()
        self.primary.sql('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        self.primary.rollback()
        conf = frappe.local.conf
        self.writer = get_db(socket=conf.get('db_socket'), host=conf.get('db_host'), port=conf.get('db_port'),
            user=conf.get('db_user') or conf.db_name, password=conf.get('db_password'), cur_db_name=conf.db_name)
        self.writer.sql('SET SESSION innodb_lock_wait_timeout=2')
        self.assertNotEqual(self.primary.sql('SELECT CONNECTION_ID()')[0][0],
                            self.writer.sql('SELECT CONNECTION_ID()')[0][0])
        self.connection_id = self.primary.sql('SELECT CONNECTION_ID()')[0][0]
        original = get_configuration()  # Establish request A's old consistent snapshot.
        self.assertEqual((original.source_instance, original.source_binding_revision, original.timeout_seconds), ('hero', 7, 1))

    @staticmethod
    def _write_values(db, values):
        for field, value in values.items():
            db.sql('DELETE FROM `tabSingles` WHERE doctype=%s AND field=%s', (SETTINGS, field))
            db.sql('INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)',
                   (SETTINGS, field, value))

    def _restore_settings(self):
        if self.writer is not None:
            try:
                self.writer.rollback()
            finally:
                self.writer.close()
        self.primary.rollback()
        self.primary.sql(
            f"DELETE FROM `tabSingles` WHERE doctype=%s AND field IN ({','.join(['%s'] * len(FIELDS))})",
            (SETTINGS, *FIELDS),
        )
        for row in self.saved_singles:
            self.primary.sql('INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, %s, %s)',
                             (SETTINGS, row.field, row.value))
        self.primary.sql('DELETE FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname=%s', AUTH_KEY)
        for row in self.saved_auth:
            self.primary.sql('INSERT INTO `__Auth` (doctype, name, fieldname, password, encrypted) '
                             'VALUES (%s, %s, %s, %s, %s)', row)
        self.primary.commit()
        isolation = self.original_isolation.replace('-', ' ').upper()
        self.assertIn(isolation, {'READ UNCOMMITTED', 'READ COMMITTED', 'REPEATABLE READ', 'SERIALIZABLE'})
        self.primary.sql('SET SESSION TRANSACTION ISOLATION LEVEL ' + isolation)
        self.primary.rollback()
        frappe.clear_document_cache(SETTINGS)

    def _verified_context(self):
        bindings = BindingRegistryService(InMemoryBindingStorage())
        parent = bindings.create_or_resolve_object_binding(site_scope=SITE, source_instance='hero',
            object_type='Contact', external_object_id='42', target_app='erpnext', target_doctype='Customer',
            target_name='C1', mapping_definition_id='d', mapping_revision=1)
        field = bindings.create_or_resolve_field_binding(parent, source_field='full_name', target_field='customer_name',
            mapping_definition_id='d', mapping_revision=1, direction=MappingDirection.HERO_TO_FRAPPE)
        old = state_fingerprint('Old', present=True)
        field = bindings.record_verified_acknowledgment(field, expected_revision=1, mapping_definition_id='d',
            mapping_revision=1, source_fingerprint=old, target_fingerprint=old, evidence_id='initial', version=1)
        result = context()
        result['snapshot'] = mutation_snapshot(include_id=True)
        result['binding'], result['bindings'] = field, bindings
        result['intent'] = OutboundIntent(field, 1, 'a' * 64, state_fingerprint('Expected', present=True),
            True, MUTATION, {'contact': {'id': 42, 'full_name': 'Expected'}})
        return result

    def _run_with_committed_change(self, change, *, succeeds):
        verified = self._verified_context()
        before = verified['binding']
        reads = []
        def read(document, variables):
            reads.append((document, variables))
            if len(reads) == 1:
                # Request B can commit while request A is inside its simulated HTTP read.
                # A source reader holding FOR UPDATE across HTTP would time out here.
                self._write_values(self.writer, change)
                self.writer.commit()
                stale = get_configuration()
                self.assertEqual((stale.source_instance, stale.source_binding_revision, stale.timeout_seconds), ('hero', 7, 1))
                return {'contacts': [{'id': '42', 'full_name': 'Old'}]}
            return {'contacts': [{'id': '42', 'full_name': 'Expected'}]}
        with patch('hero_integration.client.graphql.HeroGraphQLClient') as reader, \
                patch('hero_integration.client.graphql_mutation.HeroMutationClient') as mutation:
            reader.return_value.query.side_effect = read
            mutation.return_value.execute_once.return_value = {'update_contact': {'id': '42'}}
            transport = api._SyncHeroTransport(verified, SimpleNamespace(correlation_id='snapshot-test'))
            approval = OutboundApproval(True, True, True, True, True, 2)
            if succeeds:
                updated = execute_sync_hero(verified['intent'], approval, transport, verified['bindings'])
                self.assertEqual(updated.acknowledged_source_fingerprint, state_fingerprint('Expected', present=True))
                mutation.return_value.execute_once.assert_called_once()
                self.assertEqual(mutation.call_args.args[2], 2)
                self.assertEqual(len(reads), 2)
            else:
                with self.assertRaises(WritebackContractError):
                    execute_sync_hero(verified['intent'], approval, transport, verified['bindings'])
                mutation.assert_not_called()
                self.assertEqual(len(reads), 1)
                self.assertEqual(verified['bindings'].get_field_bindings(SITE, before.object_binding_key), (before,))
        # The transport must not replace, commit or roll back the caller's transaction.
        self.assertIs(frappe.local.db, self.primary)
        self.assertEqual(self.primary.sql('SELECT CONNECTION_ID()')[0][0], self.connection_id)
        stale = get_configuration()
        self.assertEqual((stale.source_instance, stale.source_binding_revision, stale.timeout_seconds), ('hero', 7, 1))

    def test_committed_foreign_source_is_seen_before_mutation_despite_old_snapshot(self):
        self._run_with_committed_change({'hero_source_instance': 'other', 'source_binding_revision': '8',
                                        'source_binding_confirmed_revision': '8'}, succeeds=False)

    def test_committed_unconfirmed_revision_is_seen_before_mutation_despite_old_snapshot(self):
        self._run_with_committed_change({'source_binding_revision': '8'}, succeeds=False)

    def test_unchanged_source_can_succeed_without_restarting_the_caller_transaction(self):
        self._run_with_committed_change({'timeout_seconds': '2'}, succeeds=True)
