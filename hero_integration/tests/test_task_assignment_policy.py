import unittest

from hero_integration.services.task_assignment_policy import PolicyError, validate_policy_payload


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
