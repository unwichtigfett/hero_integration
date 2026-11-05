"""Read/lock checks only; these tests never activate or update a mapping."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller
from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as locks


class TestWritebackActiveGuard(unittest.TestCase):
    def invoke(self, rows):
        sql_calls, lock_calls = [], []
        db = SimpleNamespace(sql=lambda query, values, as_dict: (sql_calls.append((query, values, as_dict)) or rows))
        fake = SimpleNamespace(_=lambda message: message, db=db, throw=lambda msg: (_ for _ in ()).throw(ValueError(msg)))
        lock = lambda *args: lock_calls.append(args)
        source = {"doctype": "Customer", "field": "customer_name"}
        with patch.object(controller, "frappe", fake), patch.object(locks, "_lock_active_target", side_effect=lock):
            controller._check_active_writeback_conflicts("SNAPSHOT", source, "MY-DRAFT")
        return lock_calls, sql_calls

    def test_shared_api_key_and_locking_read(self):
        locks_used, sql = self.invoke([])
        self.assertEqual(locks_used, [("writeback:SNAPSHOT", "Customer", "FRAPPE_TO_MUTATION:customer_name")])
        self.assertIn("FOR UPDATE", sql[0][0])
        self.assertEqual(sql[0][1], ("FRAPPE_TO_MUTATION", "Active", "SNAPSHOT", "MY-DRAFT"))

    def test_existing_source_metadata_is_not_ignored(self):
        mapping = {"source": {"doctype": "Customer", "field": "customer_name", "legacy": False}}
        with self.assertRaisesRegex(ValueError, "already owns"):
            self.invoke([SimpleNamespace(mapping_json=json.dumps(mapping))])

    def test_invalid_or_duplicate_source_fails_closed(self):
        duplicate = json.dumps({"source": {"doctype": "Customer", "field": "customer_name"}}).replace("Customer", "Customer\", \"doctype\": \"Other")
        for payload in ("{bad", "{}", duplicate):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "cannot be safely inspected"):
                self.invoke([SimpleNamespace(mapping_json=payload)])

    def test_other_source_is_allowed(self):
        self.invoke([SimpleNamespace(mapping_json=json.dumps({"source": {"doctype": "Customer", "field": "other"}}))])


class TestNativeUpdateProfileOwnership(unittest.TestCase):
    def send_profile(self, mode='UPDATE', doctype='Task'):
        return {'usage': 'SEND_TO_HERO', 'source': {'doctype': doctype}, 'send_options': {'mode': mode}}

    def update_profile(self, area='HeroTasks'):
        from hero_integration.tests.test_mapped_update_v2 import mapping
        return mapping(area)

    def invoke(self, incoming, existing, *, source='source-a'):
        events = []
        def sql(query, params, as_dict=True):
            self.assertEqual(events[0], ('lock', ('native-update:' + source, incoming['source']['doctype'], 'UPDATE'))
                             if incoming.get('usage') == 'MAPPED_UPDATE_V2' or incoming['send_options']['mode'] == 'UPDATE'
                             else ('lock', ('native-send:' + source, incoming['source']['doctype'], 'SEND_TO_HERO')))
            self.assertIn('FOR UPDATE', query)
            self.assertEqual(params[:2], ('FRAPPE_TO_MUTATION', 'Active'))
            self.assertEqual(params[-1], 'new')
            events.append(('query', params))
            return [SimpleNamespace(mapping_json=payload if isinstance(payload, str) else json.dumps(payload))
                    for owner, payload in existing if owner == params[2]]
        fake = SimpleNamespace(db=SimpleNamespace(sql=sql), _=lambda message: message,
                               throw=lambda message: (_ for _ in ()).throw(ValueError(message)))
        with patch.object(controller, 'frappe', fake), \
             patch.object(locks, '_lock_active_target', side_effect=lambda *args: events.append(('lock', args))), \
             patch.object(controller, '_check_active_writeback_conflicts'):
            if incoming['usage'] == 'SEND_TO_HERO':
                controller._check_active_send_conflicts(source, incoming['source']['doctype'], 'new', incoming['send_options']['mode'])
            else:
                controller._check_active_mapped_update_conflicts(source, 'snapshot', incoming, 'new')
        return events

    def test_both_activation_orders_reject_the_other_update_runtime(self):
        send, update = self.send_profile(), self.update_profile()
        for incoming, other in ((send, update), (update, send)):
            with self.subTest(usage=incoming['usage']), self.assertRaisesRegex(ValueError, 'already owns'):
                self.invoke(incoming, [('source-a', other)])

    def test_both_runtimes_take_the_same_lock_before_current_membership_read(self):
        for incoming in (self.send_profile(), self.update_profile()):
            events = self.invoke(incoming, [])
            self.assertEqual(events[0], ('lock', ('native-update:source-a', 'Task', 'UPDATE')))
            self.assertEqual(events[1][0], 'query')

    def test_create_does_not_conflict_with_update_in_either_activation_order(self):
        create, update = self.send_profile('CREATE'), self.update_profile()
        self.invoke(create, [('source-a', update)])
        self.invoke(update, [('source-a', create)])

    def test_other_sources_and_document_types_keep_independent_slots(self):
        self.invoke(self.send_profile(), [('source-b', self.update_profile())])
        self.invoke(self.update_profile(), [('source-b', self.send_profile())])
        self.invoke(self.send_profile(), [('source-a', self.update_profile('HeroProjectMatches'))])
        self.invoke(self.update_profile(), [('source-a', self.send_profile(doctype='Project'))])

    def test_project_logbook_can_coexist_with_both_project_update_profiles(self):
        from hero_integration.tests.test_project_logbook_writeback import content
        for incoming in (self.send_profile(doctype='Project'), self.update_profile('HeroProjectMatches')):
            self.invoke(incoming, [('source-a', content())])

    def test_uninspectable_existing_profiles_remain_blocked(self):
        malformed = ('{', '[]', '{}', {'usage': 'SEND_TO_HERO', 'source': None},
                     {'usage': 'UNKNOWN_RUNTIME', 'source': {'doctype': 'Task'}},
                     {'usage': 'SEND_TO_HERO', 'source': {'doctype': 'Task'}, 'send_options': {}},
                     {'usage': 'MAPPED_UPDATE_V2', 'source': {'doctype': 'Project'}, 'area': 'HeroTasks'},
                     '{"source":{"doctype":"Task","doctype":"Project"}}')
        for incoming in (self.send_profile(), self.update_profile()):
            for other in malformed:
                with self.subTest(usage=incoming['usage'], other=other), self.assertRaisesRegex(ValueError, 'safely inspected'):
                    self.invoke(incoming, [('source-a', other)])
