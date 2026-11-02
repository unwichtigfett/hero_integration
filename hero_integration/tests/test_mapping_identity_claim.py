"""Offline tests of the real claim decision and Document admission boundary."""
from copy import deepcopy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_identity_claim as claims
from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError
from hero_integration.services.mapping_rule_lineage import semantic_fingerprint
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping
from hero_integration.hero_integration.doctype.hero_mapping_identity_claim import hero_mapping_identity_claim


ROOT = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
REVISION = '9d8fe02d-9eb0-49b1-a599-e0630f421010'


def claim():
    fields = {'source_path':'email', 'source_type':'string|null', 'target_doctype':'Customer',
              'target_field':'custom_import_email', 'purpose':'Field', 'transformation':'Trim', 'relation':'', 'priority':5}
    return {'site_scope':'test.local', 'source_instance':'source', 'direction':'QUERY_TO_FRAPPE',
            'schema_fingerprint':'a'*64, 'operation':'HeroCustomers', 'identity_kind':'RULE_REVISION',
            'portable_id':REVISION, 'logical_id':ROOT, 'predecessor_id':ROOT,
            'mapping_id':'a16b15fa-7ca8-4722-b63c-ac60cc46629a',
            'rule_fields':fields, 'schema_manifest':{}, 'schema_dependencies':[],
            'content_hash':semantic_fingerprint('HeroCustomers', 'a'*64, fields)}


def different_content(item):
    fields = {**item['rule_fields'], 'transformation':'Identity'}
    return {**item, 'rule_fields':fields, 'content_hash':semantic_fingerprint(item['operation'],
        item['schema_fingerprint'], fields, schema_manifest=item['schema_manifest'],
        schema_dependencies=item['schema_dependencies'])}


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
    _ = staticmethod(lambda message: message)

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

    def test_claim_schema_requires_full_single_column_unique_index(self):
        full = {'Key_name':'claim_key', 'Column_name':'claim_key', 'Seq_in_index':1,
                'Non_unique':0, 'Sub_part':None}
        cases = {
            'nonunique': [{**full, 'Non_unique':1}],
            'prefix': [{**full, 'Sub_part':16}],
            'composite': [full, {**full, 'Column_name':'site_scope', 'Seq_in_index':2}],
            'wrong_column': [{**full, 'Column_name':'site_scope'}],
        }
        for label, rows in cases.items():
            with self.subTest(index=label), patch.object(self.fake.db, 'sql', return_value=rows):
                with self.assertRaisesRegex(MappingDraftError, 'unique index'):
                    claims.assert_claim_schema(self.fake)
        with patch.object(self.fake.db, 'sql', return_value=[full]):
            claims.assert_claim_schema(self.fake)

    def batch(self):
        root = {**claim(), 'identity_kind':'LOGICAL_ROOT', 'portable_id':ROOT, 'logical_id':ROOT, 'predecessor_id':None, 'mapping_id':None}
        return [root, {**root, 'identity_kind':'RULE_REVISION'}, claim()]

    def test_claim_batch_is_idempotent_and_context_conflicts_fail_closed(self):
        fake = self.fake
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target') as lock:
            batch = self.batch()
            first = claims.reserve_claims(fake, batch)
            self.assertTrue(all(not item['unchanged'] for item in first))
            self.assertTrue(all(item['unchanged'] for item in claims.reserve_claims(fake, batch)))
            for change in ({'source_instance':'another'}, {'direction':'FRAPPE_TO_MUTATION'}):
                changed = [{**item, **change} for item in batch]
                with self.assertRaises(DraftConflict): claims.reserve_claims(fake, changed)
            with self.assertRaises(DraftConflict): claims.reserve_claims(fake, [different_content(item) for item in batch])
            self.assertEqual(len(fake.rows), 4)
            self.assertEqual([call.args[2] for call in lock.call_args_list[:2]], [ROOT, REVISION])
            record = fake.rows[first[0]['claim_key']]
            self.assertNotIn('custom_import_email', record['claim_json'])
            self.assertNotIn('rule_fields', record['claim_json'])
            with self.assertRaises(PermissionError):
                hero_mapping_identity_claim.HEROMappingIdentityClaim.validate(FakeDocument(fake, record))
            with self.assertRaises(PermissionError): claims._reserve_claim(fake, batch[0])

    def test_claim_fingerprint_must_match_canonical_rule_content(self):
        fake = self.fake
        batch = self.batch()
        batch[2]['content_hash'] = 'c'*64
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            with self.assertRaisesRegex(MappingDraftError, 'fingerprint'):
                claims.reserve_claims(fake, batch)
            self.assertEqual(len(fake.rows), 1)
            batch = self.batch()
            batch[2]['rule_fields']['priority'] = {'unexpected':'object'}
            with self.assertRaisesRegex(MappingDraftError, 'evidence'):
                claims.reserve_claims(fake, batch)
            batch = self.batch()
            for item in batch:
                item['schema_manifest'] = {'arbitrary':'invalid'}
                item['schema_dependencies'] = {'arbitrary':'invalid'}
                item['content_hash'] = semantic_fingerprint(item['operation'], item['schema_fingerprint'], item['rule_fields'],
                    schema_manifest=item['schema_manifest'], schema_dependencies=item['schema_dependencies'])
            with self.assertRaisesRegex(MappingDraftError, 'evidence'):
                claims.reserve_claims(fake, batch)

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

    def test_unauthorized_claim_stops_before_schema_check_or_identity_lock(self):
        fake = self.fake
        for user, roles in (('Guest', ['System Manager']), ('employee', ['Employee'])):
            with self.subTest(user=user), patch.object(fake, 'get_roles', return_value=roles), \
                 patch.object(claims, 'assert_claim_schema') as schema_check, \
                 patch.object(claims, 'assert_reconciliation_ready') as reconciliation, \
                 patch.object(hero_frappe_mapping, '_lock_active_target') as lock, \
                 patch.object(fake.db, 'savepoint') as savepoint:
                fake.session.user = user
                before = deepcopy(fake.rows)
                with self.assertRaises(PermissionError):
                    claims.reserve_claims(fake, self.batch())
                self.assertEqual(fake.rows, before)
                schema_check.assert_not_called()
                reconciliation.assert_not_called()
                lock.assert_not_called()
                savepoint.assert_not_called()

    def test_rollback_failure_requires_transaction_abort_and_clears_scopes(self):
        fake = self.fake
        fake.fail_insert = True
        def fail_rollback(**kwargs):
            raise RuntimeError('rollback unavailable')
        fake.db.rollback = fail_rollback
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            with self.assertRaisesRegex(RuntimeError, 'Claim rollback failed; abort the transaction') as caught:
                claims.reserve_claims(fake, self.batch())
        self.assertEqual(str(caught.exception.__cause__), 'rollback unavailable')
        self.assertFalse(claims._batch_scope.get())
        self.assertIsNone(claims._insert_scope.get())

    def test_late_conflict_rolls_back_inserted_claims(self):
        fake = self.fake
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            batch = self.batch()
            claims.reserve_claims(fake, [different_content(item) for item in batch])
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
                other = different_content(claim())
                encoded = claims._claim_encoded(other)
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

    def test_mapping_scoped_successors_cannot_cross_mapping_lineage(self):
        fake = self.fake
        root, first, leaf = self.batch()
        mapping_a = 'a16b15fa-7ca8-4722-b63c-ac60cc46629a'
        mapping_b = '21e99ce9-7e38-40f6-a36d-21aa33e43c98'
        root['mapping_id'] = first['mapping_id'] = None
        leaf['mapping_id'] = mapping_a
        descendant = {**leaf, 'portable_id':'4fc41897-6013-444e-8ce4-fb81dcb59205',
                      'predecessor_id':REVISION, 'mapping_id':mapping_b}
        with patch.dict(sys.modules, {'frappe':fake}), patch.object(hero_mapping_identity_claim, 'frappe', fake), \
             patch.object(hero_frappe_mapping, '_lock_active_target'):
            with self.assertRaisesRegex(MappingDraftError, 'mapping scope'):
                claims.reserve_claims(fake, [root, first, leaf, descendant])
            descendant['mapping_id'] = mapping_a
            self.assertEqual(len(claims.reserve_claims(fake, [root, first, leaf, descendant])), 4)
            sibling = {**descendant, 'portable_id':'65ec64ca-0e6a-4d85-b7f2-856f78fe7924',
                       'predecessor_id':ROOT, 'mapping_id':mapping_b}
            result = claims.reserve_claims(fake, [root, first, leaf, descendant, sibling])
            self.assertEqual(len(result), 5)
            self.assertEqual(sum(not item['unchanged'] for item in result), 1)

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
                claims.reserve_claims(fake, [root, different_content(first), leaf])
            with self.assertRaises(DraftConflict): claims.reserve_claims(fake, [root, first, leaf, different_content(leaf)])
