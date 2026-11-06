import unittest
import sys
import types
from unittest.mock import Mock, patch

from hero_integration.services.task_assignment_policy import (
    PolicyError, _fingerprint, approve_policy, create_policy, revoke_policy, validate_policy_payload,
)


class TestTaskAssignmentPolicy(unittest.TestCase):
    def setUp(self):
        self.payload = {
            'source_instance': 'synthetic', 'issuer': 'Administrator', 'company': 'Synthetic Company',
            'user_bindings': ['binding-1'], 'allow_initial': True, 'allow_transfer': False,
        }

    def test_valid_policy_has_explicit_scope_and_bounded_actions(self):
        policy = validate_policy_payload(self.payload, 'isolated.test')
        self.assertEqual(policy['site_scope'], 'isolated.test')
        self.assertEqual(policy['source_type'], 'Task')
        self.assertEqual(policy['user_bindings'], ['binding-1'])
        self.assertFalse(policy['allow_transfer'])

    def test_ambiguity_extra_fields_and_missing_authority_fail(self):
        for changes in ({'company': ''}, {'issuer': ''}, {'source_instance': ''},
                        {'user_bindings': []}, {'user_bindings': ['binding-1', 'binding-1']},
                        {'allow_initial': 1}, {'allow_transfer': 'true'}, {'approved': True}):
            with self.subTest(changes=changes):
                with self.assertRaises(PolicyError):
                    validate_policy_payload({**self.payload, **changes}, 'isolated.test')

    def test_policy_fingerprint_is_stable_for_binding_order_and_bound_to_scope(self):
        first = validate_policy_payload({**self.payload, 'user_bindings': ['b', 'a']}, 'site-a')
        reordered = validate_policy_payload({**self.payload, 'user_bindings': ['a', 'b']}, 'site-a')
        other_site = validate_policy_payload({**self.payload, 'user_bindings': ['a', 'b']}, 'site-b')
        transfer = validate_policy_payload({**self.payload, 'user_bindings': ['a', 'b'], 'allow_transfer': True}, 'site-a')
        self.assertEqual(_fingerprint(first), _fingerprint(reordered))
        self.assertNotEqual(_fingerprint(first), _fingerprint(other_site))
        self.assertNotEqual(_fingerprint(first), _fingerprint(transfer))

    def test_native_effects_require_explicit_bounded_decisions(self):
        effects = {'date_rule': 'LOCAL_TODAY', 'allow_notifications': True, 'allow_follow': False}
        self.assertEqual(validate_policy_payload({**self.payload, 'native_effects': effects}, 'isolated.test')['native_effects'], effects)
        for invalid in ({**effects, 'date_rule': 'HERO_DUE_DATE'},
                        {**effects, 'allow_follow': 1}, {'date_rule': 'LOCAL_TODAY'},
                        {**effects, 'unexpected': True}):
            with self.subTest(invalid=invalid), self.assertRaises(PolicyError):
                validate_policy_payload({**self.payload, 'native_effects': invalid}, 'isolated.test')

    def test_silent_policy_explicitly_forbids_transfer_and_side_effects(self):
        effects = {'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False}
        policy = {**self.payload, 'native_effects': effects, 'silent_initial': True}
        self.assertTrue(validate_policy_payload(policy, 'isolated.test')['silent_initial'])
        for change in ({'allow_transfer': True}, {'native_effects': {**effects, 'allow_notifications': True}},
                       {'native_effects': {**effects, 'allow_follow': True}}, {'silent_initial': 1}):
            with self.subTest(change=change), self.assertRaises(PolicyError):
                validate_policy_payload({**policy, **change}, 'isolated.test')

    def test_policy_writes_deny_non_manager_before_document_access(self):
        frappe = types.ModuleType('frappe')
        frappe.only_for = Mock(side_effect=PermissionError('denied'))
        frappe.get_doc = Mock(side_effect=AssertionError('document access before authorization'))
        frappe.session = types.SimpleNamespace(user='synthetic-employee')
        with patch.dict(sys.modules, {'frappe': frappe}):
            for action in (lambda: create_policy(self.payload),
                           lambda: approve_policy('policy', 1),
                           lambda: revoke_policy('policy', 1)):
                with self.subTest(action=action), self.assertRaises(PermissionError):
                    action()
        self.assertEqual(frappe.only_for.call_count, 3)
        frappe.get_doc.assert_not_called()
