"""Offline tests of the real claim decision and Document admission boundary."""
from copy import deepcopy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_identity_claim as claims
from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
from hero_integration.hero_integration.doctype.hero_mapping_identity_claim import hero_mapping_identity_claim


ROOT = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
REVISION = '9d8fe02d-9eb0-49b1-a599-e0630f421010'


def claim():
    return {'site_scope':'test.local', 'source_instance':'source', 'direction':'QUERY_TO_FRAPPE',
            'schema_fingerprint':'a'*64, 'operation':'HeroCustomers', 'identity_kind':'RULE_REVISION',
            'portable_id':REVISION, 'logical_id':ROOT, 'predecessor_id':ROOT, 'content_hash':'b'*64}


class FakeDocument:
    def __init__(self, fake, values):
        self.fake = fake
        self.__dict__.update(values)
    def is_new(self): return True
    def insert(self):
        self.fake.events.append(('insert', self.claim_key))
        hero_mapping_identity_claim.HEROMappingIdentityClaim.validate(self)
        if self.fake.fail_insert:
            raise RuntimeError('synthetic insert failure')
        if self.claim_key in self.fake.rows:
            raise ValueError('duplicate key')
        self.fake.rows[self.claim_key] = {'claim_key':self.claim_key, 'claim_json':self.claim_json,
                                          'claim_hash':self.claim_hash}
        return self


class Fake:
    def __init__(self):
        self.local, self.session = SimpleNamespace(site='test.local'), SimpleNamespace(user='owner')
        self.PermissionError = PermissionError
        key, encoded, digest = claims.reconciliation_marker('test.local')
        self.rows = {key:{'claim_key':key, 'claim_json':encoded, 'claim_hash':digest}}
        self.ready, self.unique, self.fail_insert = True, True, False
        self.snapshots, self.events = {}, []
        self.db = SimpleNamespace(sql=self.sql, table_exists=lambda *_:self.ready, savepoint=self.savepoint, rollback=self.rollback)
    def savepoint(self, name): self.snapshots[name] = deepcopy(self.rows)
    def rollback(self, save_point): self.rows = deepcopy(self.snapshots[save_point])
    def get_roles(self): return ['System Manager']
    def throw(self, message, exc=None): raise (exc or ValueError)(message)
    def get_doc(self, values): return FakeDocument(self, values)
    def sql(self, query, params=(), **kwargs):
        if query.startswith('show index'):
            return [{'Key_name':'claim_key', 'Column_name':'claim_key', 'Seq_in_index':1,
                     'Non_unique':0 if self.unique else 1, 'Sub_part':None}]
        if query.startswith('select claim_key'):
            return [dict(self.rows[params[0]])] if params[0] in self.rows else []
        raise AssertionError(query)


class TestMappingIdentityClaim(unittest.TestCase):
    def setUp(self): self.fake = Fake()

    def batch(self):
        root = {**claim(), 'identity_kind':'LOGICAL_ROOT', 'portable_id':ROOT, 'logical_id':ROOT, 'predecessor_id':None}
        return [root, {**root, 'identity_kind':'RULE_REVISION'}, claim()]

    def test_claim_batch_is_idempotent_and_context_conflicts_fail_closed(self):
        fake = self.fake
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target') as lock:
            batch = self.batch()
            first = claims.reserve_claims(fake, batch)
            self.assertTrue(all(not item['unchanged'] for item in first))
            self.assertTrue(all(item['unchanged'] for item in claims.reserve_claims(fake, batch)))
            for change in ({'content_hash':'c'*64}, {'source_instance':'another'}, {'direction':'FRAPPE_TO_MUTATION'}):
                changed = [{**item, **change} for item in batch]
                with self.assertRaises(DraftConflict): claims.reserve_claims(fake, changed)
            self.assertEqual(len(fake.rows), 4)
            self.assertEqual([call.args[2] for call in lock.call_args_list[:2]], [ROOT, REVISION])
            record = fake.rows[first[0]['claim_key']]
            with self.assertRaises(PermissionError):
                hero_mapping_identity_claim.HEROMappingIdentityClaim.validate(FakeDocument(fake, record))
            with self.assertRaises(PermissionError): claims._reserve_claim(fake, batch[0])

    def test_prerequisites_and_failure_roll_back(self):
        fake = self.fake
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            fake.ready = False
            with self.assertRaisesRegex(MappingDraftError, 'schema'): claims.reserve_claims(fake, self.batch())
            fake.ready, fake.unique = True, False
            with self.assertRaisesRegex(MappingDraftError, 'unique index'): claims.reserve_claims(fake, self.batch())
            fake.unique = True
            marker = fake.rows.pop(claims.reconciliation_marker('test.local')[0])
            with self.assertRaisesRegex(MappingDraftError, 'reconciliation'): claims.reserve_claims(fake, self.batch())
            fake.rows[marker['claim_key']] = marker
            fake.fail_insert = True
            with self.assertRaisesRegex(RuntimeError, 'synthetic'): claims.reserve_claims(fake, self.batch())
            self.assertEqual(len(fake.rows), 1)
            fake.fail_insert = False
            fake.session.user = 'Guest'
            with self.assertRaises(PermissionError): claims.reserve_claims(fake, self.batch())

    def test_late_conflict_rolls_back_inserted_claims(self):
        fake = self.fake
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            batch = self.batch()
            claims.reserve_claims(fake, [{**item, 'content_hash':'c'*64} for item in batch])
            root_keys = {claims.claim_key(batch[0]), claims.claim_key(batch[1])}
            for key in root_keys: fake.rows.pop(key)
            before = deepcopy(fake.rows)
            with self.assertRaises(DraftConflict): claims.reserve_claims(fake, batch)
            self.assertEqual(fake.rows, before)

    def test_all_uuid_locks_are_acquired_before_the_first_insert(self):
        fake = self.fake
        def lock(_operation, _site, identity): fake.events.append(('lock', identity))
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target', side_effect=lock):
            claims.reserve_claims(fake, self.batch())
        first_insert = next(index for index, event in enumerate(fake.events) if event[0] == 'insert')
        self.assertEqual(fake.events[:2], [('lock', ROOT), ('lock', REVISION)])
        self.assertGreaterEqual(first_insert, 2)

    def test_authoritative_recheck_rejects_claim_arriving_at_identity_lock(self):
        fake = self.fake
        inserted = []
        def competitor(_operation, _site, identity):
            if identity == REVISION and not inserted:
                other = {**claim(), 'content_hash':'c'*64}
                encoded = claims._encoded(other)
                key = claims.claim_key(other)
                from hashlib import sha256
                fake.rows[key] = {'claim_key':key, 'claim_json':encoded,
                                  'claim_hash':sha256(encoded.encode()).hexdigest()}
                inserted.append(key)
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target', side_effect=competitor):
            with self.assertRaises(DraftConflict): claims.reserve_claims(fake, self.batch())
            self.assertTrue(inserted)
            self.assertIn(inserted[0], fake.rows)

    def test_missing_cycle_and_conflicting_root_identity_are_rejected(self):
        fake = self.fake
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            root, first, leaf = self.batch()
            with self.assertRaisesRegex(MappingDraftError, 'root'): claims.reserve_claims(fake, [leaf])
            cyclic = {**leaf, 'predecessor_id':'21e99ce9-7e38-40f6-a36d-21aa33e43c98'}
            other = {**leaf, 'portable_id':cyclic['predecessor_id'], 'predecessor_id':REVISION}
            with self.assertRaisesRegex(MappingDraftError, 'cycle'): claims.reserve_claims(fake, [root, first, cyclic, other])
            with self.assertRaisesRegex(MappingDraftError, 'matching root'):
                claims.reserve_claims(fake, [root, {**first, 'content_hash':'c'*64}, leaf])
            with self.assertRaises(DraftConflict): claims.reserve_claims(fake, [root, first, leaf, {**leaf, 'content_hash':'c'*64}])
