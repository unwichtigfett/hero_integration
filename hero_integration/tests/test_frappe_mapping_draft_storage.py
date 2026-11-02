"""Opt-in storage-04 tests require HERO_DRAFT_DB_TEST=1 and this worktree on PYTHONPATH.
Workers spawn independent connections and commit separate transactions.
"""
from __future__ import annotations

import json
import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
from queue import Empty
import unittest
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import DraftConflict

SITES = '/srv/hero-native/implementation-results/HERO_MAPPING_FRAPPE_STORAGE_IMPLEMENT_04_20261025/sites'
SITE = 'draftstorage04.localhost'
DB = 'herodraftstorage04'
ONE = 'draft04-one@example.invalid'
TWO = 'draft04-two@example.invalid'
EMPLOYEE = 'draft04-employee@example.invalid'


def connect(user='Administrator'):
    assert os.environ.get('HERO_DRAFT_DB_TEST') == '1'
    frappe.init(SITE, sites_path=SITES)
    assert frappe.conf.db_name == DB and int(frappe.conf.db_port) == 13328
    frappe.connect()
    frappe.set_user(user)
    frappe.db.sql('SET SESSION innodb_lock_wait_timeout=5')
    frappe.flags.mute_emails = True
    return frappe.db.sql('select connection_id()')[0][0]


def worker(definition, actor, operation, barrier, results):
    try:
        connection_id = connect(actor)
        service = FrappeMappingDraftStorage().service()
        barrier.wait(timeout=10)
        if operation == 'open':
            value = service.open_draft(definition, actor)
            result = ('OPEN', value.key)
        else:
            value = service.save(definition, actor, 0, 1, {'record_path': operation}, expected_generation=1)
            result = ('SAVED', value.draft.content_hash)
        frappe.db.commit()
        results.put((connection_id, *result))
    except DraftConflict:
        frappe.db.rollback()
        results.put((connection_id, 'CONFLICT', ''))
    except Exception as error:
        if getattr(frappe.local, 'db', None):
            frappe.db.rollback()
        results.put((0, 'ERROR', type(error).__name__ + ': ' + str(error)))
    finally:
        frappe.destroy()


def lock_worker(definition, ready, release, attempted, results, holding):
    class ObservedStorage(FrappeMappingDraftStorage):
        def _authorize(self, actor):
            super()._authorize(actor)
            if not holding:
                attempted.set()

        @contextmanager
        def transaction(self, definition_id, actor):
            with super().transaction(definition_id, actor):
                if holding:
                    ready.set()
                    if not release.wait(10):
                        raise TimeoutError('test lock release timed out')
                yield

    try:
        connection_id = connect(ONE)
        value = ObservedStorage().service().open_draft(definition, ONE)
        frappe.db.commit()
        results.put((connection_id, 'OPEN', value.key))
    except Exception as error:
        if getattr(frappe.local, 'db', None): frappe.db.rollback()
        results.put((0, 'ERROR', repr(error)))
    finally:
        frappe.destroy()


@unittest.skipUnless(os.environ.get('HERO_DRAFT_DB_TEST') == '1', 'Dedicated MariaDB site required')
class TestFrappeMappingDraftStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect()
        assert frappe.__version__ == '16.52.0'
        assert Path(__import__('hero_integration').__file__).resolve().is_relative_to(
            '/srv/hero-native/isolated-checkpoints/hero-personal-drafts-implement-03')
        for user, roles in ((ONE, ['System Manager']), (TWO, ['System Manager']), (EMPLOYEE, [])):
            if not frappe.db.exists('User', user):
                frappe.get_doc({'doctype':'User', 'email':user, 'first_name':'Synthetic Draft 04',
                                'send_welcome_email':0, 'roles':[{'role':role} for role in roles]}).insert()
        frappe.db.commit()
        print('DB_EVIDENCE', frappe.db.sql('select database(),version(),@@tx_isolation,@@port')[0], flush=True)
        frappe.destroy()

    def setUp(self):
        connect()
        self.definition = 'draft04-' + uuid4().hex
        schema = {'__schema': {'queryType':{'name':'Query'}, 'types':[
            {'kind':'OBJECT','name':'Query','description':self.definition,'fields':[{'name':'readThings','args':[],
              'type':{'kind':'SCALAR','name':'String'}}]}, {'kind':'SCALAR','name':'String'}]}}
        snapshot = frappe.get_doc({'doctype':'HERO GraphQL Schema Snapshot', 'source_instance':self.definition,
            'schema_fingerprint':'a'*64, 'analysis_status':'Validated', 'snapshot_json':json.dumps(schema)})
        # Synthetic snapshot fixture supplies all required source fields.
        for field in frappe.get_meta(snapshot.doctype).fields:
            if field.reqd and not snapshot.get(field.fieldname):
                if field.fieldtype in ('Int','Check'): snapshot.set(field.fieldname, 1)
                elif field.fieldtype == 'Select': snapshot.set(field.fieldname, (field.options or '').splitlines()[0])
                else: snapshot.set(field.fieldname, 'synthetic-draft04')
        snapshot.insert()
        mapping = frappe.get_doc({'doctype':'HERO GraphQL Mapping', 'definition_id':self.definition,
            'schema_snapshot':snapshot.name, 'mapping_json':json.dumps({'record_path':'readThings'}),
            'operation_kind':'QUERY','operation_name':'readThings','direction':'QUERY_TO_FRAPPE',
            'status':'Active','mapping_revision':1})
        mapping.insert()
        self.mapping_name = mapping.name
        self.active_before = frappe.db.sql('select * from `tabHERO GraphQL Mapping` where name=%s', (mapping.name,))
        frappe.db.commit()
        frappe.set_user(ONE)
        self.storage = FrappeMappingDraftStorage()
        self.service = self.storage.service()

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user('Administrator')
        self.assertEqual(self.active_before, frappe.db.sql('select * from `tabHERO GraphQL Mapping` where name=%s', (self.mapping_name,)))
        # Keep fixtures confined to the disposable DB; resource cleanup drops
        # that exact DB after the complete run, preserving immutable history.
        frappe.destroy()

    def open(self, actor=ONE):
        frappe.set_user(actor)
        return self.service.open_draft(self.definition, actor)

    def save(self, content, revision=0, generation=1):
        return self.service.save(self.definition, ONE, revision, 1, content, expected_generation=generation)

    def test_persist_reload_separation_and_denial(self):
        first = self.open()
        self.save({'source':{'doctype':'Customer'}})
        frappe.db.commit()
        self.assertEqual(1, self.open().content_revision)
        second = self.open(TWO)
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(0, second.content_revision)
        with self.assertRaises(PermissionError):
            self.service.save(self.definition, ONE, 1, 1, {}, expected_generation=1)
        with self.assertRaises(PermissionError):
            with self.storage.transaction(self.definition, TWO):
                self.storage.draft(first.key)
        frappe.set_user(EMPLOYEE)
        with self.assertRaises(PermissionError): self.service.open_draft(self.definition, EMPLOYEE)

    def test_noop_incomplete_stale_and_outer_rollback(self):
        draft = self.open()
        original = frappe.db.get_value('HERO Mapping Draft', draft.key, ['modified','modified_by'], as_dict=True)
        self.assertTrue(self.save({'record_path':'readThings'}).unchanged)
        self.assertEqual(original, frappe.db.get_value('HERO Mapping Draft', draft.key, ['modified','modified_by'], as_dict=True))
        saved = self.save({'source':{}})
        self.assertEqual(1, saved.draft.content_revision)
        with self.assertRaises(DraftConflict): self.save({'source':{}}, revision=0)
        self.assertEqual(1, frappe.db.count('HERO Mapping Revision', {'draft_key':draft.key}))
        frappe.db.rollback()
        self.assertFalse(frappe.db.exists('HERO Mapping Draft', draft.key), 'adapter committed caller transaction')

    def test_failure_rolls_back_draft_and_revision(self):
        draft = self.open()
        frappe.db.commit()
        original = self.storage.append_revision
        for after_insert in (False, True):
            def fail(value):
                if after_insert: original(value)
                raise RuntimeError('synthetic persistence failure')
            with patch.object(self.storage, 'append_revision', side_effect=fail):
                with self.assertRaises(RuntimeError): self.save({'record_path':'incomplete'})
            self.assertEqual(0, self.open().content_revision)
            self.assertEqual(0, frappe.db.count('HERO Mapping Revision', {'draft_key':draft.key}))
        frappe.db.commit()

    def test_immutability_raw_writes_discard_history_and_reopen(self):
        draft = self.open()
        self.save({'source':{}})
        foreign = self.open(TWO)
        frappe.set_user(ONE)
        revision = frappe.get_doc('HERO Mapping Revision', f'{draft.key}-1-1')
        revision.content_json = '{}'
        with self.assertRaises(Exception): revision.save()
        with self.assertRaises(Exception): revision.db_set('content_json','{}')
        with self.assertRaises(Exception): frappe.delete_doc(revision.doctype, revision.name, ignore_permissions=True)
        raw = frappe.get_doc('HERO Mapping Draft', draft.key)
        raw.content_json = '{}'
        with self.assertRaises(frappe.PermissionError): raw.save()
        with self.assertRaises(Exception): raw.db_set('content_json','{}')
        with self.assertRaises(Exception): frappe.delete_doc(raw.doctype, raw.name, ignore_permissions=True)
        with self.assertRaises(ValueError): self.service.discard(self.definition, ONE, 1, 1)
        self.service.discard(self.definition, ONE, 1, 1, confirmed=True)
        reopened = self.open()
        self.assertEqual(2, reopened.generation)
        self.assertEqual(1, frappe.db.count('HERO Mapping Revision', {'draft_key':draft.key}))
        with self.assertRaises(DraftConflict): self.save({}, generation=1)
        self.assertEqual(foreign, self.open(TWO))

    def test_reopen_discarded_draft_uses_changed_active_base_without_erasing_history(self):
        first = self.open()
        self.save({'record_path': 'personal-v1'})
        self.service.discard(self.definition, ONE, 1, 1, confirmed=True)
        # This disposable fixture simulates a separately changed active base.
        frappe.db.set_value('HERO GraphQL Mapping', self.mapping_name, {
            'mapping_json': json.dumps({'record_path': 'new-active'}), 'mapping_revision': 2,
        })
        reopened = self.open()
        self.assertEqual((reopened.generation, reopened.content_revision, reopened.base_active_revision), (2, 0, 2))
        self.assertEqual(reopened.content, {'record_path': 'new-active'})
        self.assertEqual(reopened.base_content, {'record_path': 'new-active'})
        self.assertEqual(1, frappe.db.count('HERO Mapping Revision', {'draft_key': first.key, 'generation': 1}))
        with self.assertRaises(DraftConflict):
            self.service.save(self.definition, ONE, 1, 1, {'record_path': 'stale'}, expected_generation=1)
        saved = self.service.save(self.definition, ONE, 0, 2, {'record_path': 'personal-v2'}, expected_generation=2)
        self.assertEqual(saved.draft.content_revision, 1)
        self.assertEqual(2, frappe.db.count('HERO Mapping Revision', {'draft_key': first.key}))

    def test_reopen_cannot_change_identity_or_bypass_generation_and_revision_guards(self):
        first = self.open()
        self.service.discard(self.definition, ONE, 1, 0, confirmed=True)
        with self.storage.transaction(self.definition, ONE):
            original_base = frappe.db.get_value('HERO Mapping Draft', first.key, 'base_content_json')
            for base in (original_base, '{"record_path":"changed"}'):
                for invalid in ({'generation': 3}, {'content_revision': 1},
                                {'reconciliation_revision': 1}, {'reconciliation_json': '{}'},
                                {'definition_id': 'different-definition'}, {'site_scope': 'other-site'},
                                {'owner_user': TWO}, {'draft_key': 'other-key'}):
                    raw = frappe.get_doc('HERO Mapping Draft', first.key)
                    raw.update({'state': 'OPEN', 'generation': 2, 'content_revision': 0,
                                'reconciliation_revision': 0, 'reconciliation_json': '',
                                'base_content_json': base})
                    raw.update(invalid)
                    with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
                        raw.save()
        self.assertEqual('DISCARDED', frappe.db.get_value('HERO Mapping Draft', first.key, 'state'))

    def concurrent(self, operations):
        frappe.db.commit()
        context = multiprocessing.get_context('spawn')
        barrier, results = context.Barrier(3), context.Queue()
        processes = [context.Process(target=worker, args=(self.definition, actor, operation, barrier, results))
                     for actor, operation in operations]
        try:
            for process in processes: process.start()
            barrier.wait(timeout=10)
            outcomes = [results.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(0, process.exitcode)
            self.assertNotEqual(outcomes[0][0], outcomes[1][0], 'same DB connection would conceal missing locks')
            print('INDEPENDENT_TRANSACTIONS', outcomes, flush=True)
            return outcomes
        finally:
            for process in processes:
                if process.is_alive(): process.terminate(); process.join(5)
            results.close()
            results.join_thread()
            frappe.db.rollback()

    def test_two_cas_writers_one_winner(self):
        draft = self.open()
        outcomes = self.concurrent(((ONE,'winner-a'), (ONE,'winner-b')))
        self.assertCountEqual(['SAVED','CONFLICT'], [value[1] for value in outcomes])
        self.assertEqual(1, self.open().content_revision)
        self.assertEqual(1, frappe.db.count('HERO Mapping Revision', {'draft_key':draft.key}))

    def test_simultaneous_first_insert_same_owner(self):
        outcomes = self.concurrent(((ONE,'open'), (ONE,'open')))
        self.assertEqual(['OPEN','OPEN'], [value[1] for value in outcomes])
        self.assertEqual(outcomes[0][2], outcomes[1][2])
        self.assertEqual(1, frappe.db.count('HERO Mapping Draft', {'definition_id':self.definition,'state':'OPEN'}))

    def test_simultaneous_first_insert_distinct_owners(self):
        outcomes = self.concurrent(((ONE,'open'), (TWO,'open')))
        self.assertEqual(['OPEN','OPEN'], [value[1] for value in outcomes])
        self.assertNotEqual(outcomes[0][2], outcomes[1][2])
        self.assertEqual(2, frappe.db.count('HERO Mapping Draft', {'definition_id':self.definition,'state':'OPEN'}))

    def test_parent_lock_blocks_independent_first_insert_until_commit(self):
        frappe.db.commit()
        context = multiprocessing.get_context('spawn')
        ready, release, attempted = context.Event(), context.Event(), context.Event()
        results = context.Queue()
        holder = context.Process(target=lock_worker, args=(self.definition, ready, release, attempted, results, True))
        contender = context.Process(target=lock_worker, args=(self.definition, ready, release, attempted, results, False))
        try:
            holder.start()
            self.assertTrue(ready.wait(10), 'holder never acquired real parent lock')
            contender.start()
            self.assertTrue(attempted.wait(10), 'independent contender never reached adapter')
            with self.assertRaises(Empty):
                results.get(timeout=0.5)
            release.set()
            outcomes = [results.get(timeout=15), results.get(timeout=15)]
            self.assertEqual(['OPEN','OPEN'], [item[1] for item in outcomes])
            self.assertNotEqual(outcomes[0][0], outcomes[1][0])
            self.assertEqual(outcomes[0][2], outcomes[1][2])
            print('REAL_LOCK_OVERLAP', outcomes, flush=True)
        finally:
            release.set()
            for process in (holder, contender):
                if process.pid:
                    process.join(10)
                    if process.is_alive(): process.terminate(); process.join(5)
            results.close()
            results.join_thread()
            frappe.db.rollback()
