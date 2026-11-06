"""Retired fixed Customer operations must not remain reachable."""
import unittest
from hero_integration.services import customer_send


class TestCustomerSendFields(unittest.TestCase):
    def test_fixed_operation_backend_is_removed(self):
        for name in ('legacy_preview', 'legacy_send', 'READ_DOCUMENT', 'MUTATION_DOCUMENT', '_read_hero'):
            with self.subTest(name=name):
                self.assertFalse(hasattr(customer_send, name))
