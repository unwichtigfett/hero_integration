from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hero_integration.services import mapping_rule_write
from hero_integration.services import schema_provisioning
from hero_integration import api
from hero_integration.services.mapping_draft_service import DraftConflict
from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping


class Document:
    def __init__(self, values, events):
        self.__dict__.update(values)
        self.events = events
    def get(self, key): return getattr(self, key, None)
    def check_permission(self, permission): self.events.append(('permission', permission))
    def save(self):
        self.events.append(('save', self.status, self.target_doctype, self.target_field))
        self.mapping_revision += 1


class TestMappingRuleWrite(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.parent = dict(name='PARENT', source_instance='source', schema_snapshot='snapshot',
            direction='QUERY_TO_FRAPPE', status='Active', definition_id='parent', mapping_revision=3,
            mapping_json='{"target_operation":"HeroCustomers","mapping_definition_ids":["local-rule"]}', operation_name='customers', schema_fingerprint='fingerprint')
        self.rule = dict(name='RULE', schema_snapshot='snapshot', personal_graphql_mapping='',
            status='Draft', owner='alice', modified_by='alice', definition_id='local-rule',
            portable_definition_id='09a2bc52-4b21-46b7-9a11-3a62bf5a6348', operation='HeroCustomers',
            target_doctype='Customer', target_field='old_field', purpose='Field', mapping_revision=2, schema_fingerprint='fingerprint')
        self.session = SimpleNamespace(user='alice')
        self.collisions = []
        def get_doc(doctype, name, for_update=False):
            self.events.append(('get', doctype, for_update))
            return Document(deepcopy(self.parent if doctype == 'HERO GraphQL Mapping' else self.rule), self.events)
        def only_for(role):
            if self.session.user != 'alice': raise PermissionError('System Manager required')
        self.frappe = SimpleNamespace(session=self.session, local=SimpleNamespace(site='fixture'),
                                      db=SimpleNamespace(sql=lambda *args, **kwargs: self.collisions),
                                      get_doc=get_doc, only_for=only_for)
        for item in (patch.object(mapping_rule_write, 'frappe', self.frappe),
                     patch.object(api, '_validated_snapshot', return_value=(SimpleNamespace(source_instance='source', schema_fingerprint='fingerprint'), {})),
                     patch.object(mapping_rule_write, 'assert_portable_identity_schema'),
                     patch.object(hero_graphql_mapping, 'lock_exchange_context',
                                  side_effect=lambda *scope: self.events.append(('context', *scope))),
                     patch.object(hero_frappe_mapping, '_lock_active_target',
                                  side_effect=lambda *target: self.events.append(('lock', *target)))):
            item.start(); self.addCleanup(item.stop)

    def activate(self, **kwargs):
        return mapping_rule_write.activate_draft_rule('PARENT', 'RULE', 'source', 'snapshot', 3, 2, **kwargs)

    def test_activation_and_draft_retarget_lock_both_targets_before_identity_and_rows(self):
        with self.assertRaisesRegex(ValueError, 'global consumer reconciliation'):
            self.activate(target_doctype='Customer', target_field='new_field')
        targets = [event for event in self.events if event[0] == 'lock']
        self.assertEqual(targets[:2], [('lock','HeroCustomers','Customer','new_field'),
                                      ('lock','HeroCustomers','Customer','old_field')])
        self.assertEqual(targets[2][1], 'personal-new-definition')
        self.assertEqual(targets[3][1], 'portable-field-rule')
        self.assertLess(self.events.index(targets[-1]), self.events.index(('get','HERO GraphQL Mapping',True)))
        self.assertFalse(any(event[0] == 'save' for event in self.events))

    def test_stale_scope_actor_and_revision_are_rejected_before_write(self):
        self.parent['mapping_revision'] = 4
        with self.assertRaises(DraftConflict): self.activate()
        self.assertFalse(any(event[0] == 'save' for event in self.events))
        self.parent['mapping_revision'] = 3
        self.rule['modified_by'] = 'foreign'
        with self.assertRaises(PermissionError): self.activate()
        self.rule['modified_by'] = 'alice'
        self.parent['source_instance'] = 'wrong'
        with self.assertRaises(DraftConflict): self.activate()

    def test_denied_manager_authority_stops_before_loading_or_locking_records(self):
        self.session.user = 'non-manager'
        with self.assertRaisesRegex(PermissionError, 'System Manager required'):
            self.activate()
        self.assertEqual(self.events, [])

    def test_active_rule_and_missing_portable_identity_are_not_overwritten(self):
        self.rule['status'] = 'Active'
        with self.assertRaises(PermissionError): self.activate()
        self.rule['status'] = 'Draft'
        self.rule['portable_definition_id'] = ''
        with self.assertRaisesRegex(ValueError, 'Enroll'): self.activate()
        self.assertFalse(any(event[0] == 'save' for event in self.events))

    def test_portable_identity_collision_rejects_before_write(self):
        self.collisions.append({'name':'other-rule'})
        with self.assertRaisesRegex(DraftConflict, 'collides'): self.activate()
        self.assertFalse(any(event[0] == 'save' for event in self.events))

    def test_unselected_or_foreign_owned_rule_is_rejected(self):
        self.rule['owner'] = 'foreign'
        with self.assertRaises(PermissionError): self.activate()
        self.rule['owner'] = 'alice'
        self.parent['mapping_json'] = '{"target_operation":"HeroCustomers"}'
        with self.assertRaisesRegex(DraftConflict, 'not selected'):
            self.activate()
        self.assertFalse(any(event[0] == 'save' for event in self.events))

    def test_field_rule_deletion_is_fail_closed(self):
        def fail(message, *args): raise PermissionError(message)
        fake = SimpleNamespace(throw=fail, PermissionError=PermissionError, _=lambda message: message)
        doc = SimpleNamespace(personal_graphql_mapping='')
        with patch.object(hero_frappe_mapping, 'frappe', fake):
            with self.assertRaisesRegex(PermissionError, 'references are reconciled'):
                hero_frappe_mapping.HEROFrappeMapping.on_trash(doc)

    def test_unenrolled_uuid_draft_uses_its_persistent_identity(self):
        self.rule['portable_definition_id'] = ''
        self.rule['definition_id'] = '09a2bc524b2146b79a113a62bf5a6348'
        self.parent['mapping_json'] = '{"target_operation":"HeroCustomers","mapping_definition_ids":["09a2bc524b2146b79a113a62bf5a6348"]}'
        with self.assertRaisesRegex(ValueError, 'global consumer reconciliation'):
            self.activate()

    def test_direct_active_update_and_foreign_draft_are_denied_by_controller(self):
        from hero_integration.services import frappe_mapping_draft_storage
        def fail(message, *args): raise PermissionError(message)
        fake = SimpleNamespace(only_for=lambda role: None, session=self.session, throw=fail, _=lambda message: message,
                               PermissionError=PermissionError)
        for status, owner, expected in [('Active', 'alice', 'cannot be changed directly'),
                                        ('Draft', 'foreign', 'Only the owner')]:
            previous = SimpleNamespace(status=status, owner=owner, personal_graphql_mapping='')
            doc = SimpleNamespace(is_new=lambda: False, get_doc_before_save=lambda: previous,
                                  has_value_changed=lambda field: field == 'target_field', personal_graphql_mapping='')
            with self.subTest(status=status), patch.object(hero_frappe_mapping, 'frappe', fake), \
                 patch.object(frappe_mapping_draft_storage, 'personal_publication_parent', return_value=None):
                with self.assertRaisesRegex(PermissionError, expected):
                    hero_frappe_mapping.HEROFrappeMapping.validate(doc)

    def test_adoption_locks_context_and_sorted_targets_before_identity(self):
        identity = '09a2bc52-4b21-46b7-9a11-3a62bf5a6348'
        package = {'mappings':[{'definition_id':identity, 'operation':'HeroCustomers',
            'target_doctype':'Customer', 'target_field':'email_id', 'purpose':'Field'}]}
        with patch.object(schema_provisioning, 'frappe', self.frappe):
            schema_provisioning._lock_adoption_mappings(SimpleNamespace(source_instance='source'), 'snapshot',
                                                       package, {'mapping:' + identity})
        steps = [event for event in self.events if event[0] in {'context', 'lock'}]
        self.assertEqual(steps, [('context','source','snapshot','QUERY_TO_FRAPPE'),
                                 ('lock','HeroCustomers','Customer','email_id'),
                                 ('lock','portable-field-rule','fixture',identity)])
