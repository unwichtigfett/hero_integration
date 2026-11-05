"""Run the shared hook-registry contract offline as well as on the CI site."""
import unittest

from hero_integration.tests import test_deletion_hook as integration_contract


class TestDeletionHookRegistry(unittest.TestCase):
    def test_hook_contract_without_site_setup(self):
        name = 'test_listed_hero_doctypes_use_deletion_guard_without_replacing_native_hooks'
        case = integration_contract.TestDeletionHook(name)
        # This specific contract reads only hooks; its IntegrationTestCase setup
        # changes site settings and is intentionally reserved for the site suite.
        getattr(case, name)()
