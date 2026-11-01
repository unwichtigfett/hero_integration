"""Offline tests for the shared deletion gate; no Frappe site is opened."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import guards


class TestDeletionGuard(unittest.TestCase):
    def setUp(self):
        self.get_setting = Mock(return_value=0)

        def throw(message, error):
            raise error(message)

        self.frappe = SimpleNamespace(
            db=SimpleNamespace(get_single_value=self.get_setting),
            PermissionError=PermissionError,
            throw=Mock(side_effect=throw),
        )
        patcher = patch.object(guards, "frappe", self.frappe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_or_missing_setting_rejects_app_document_deletion(self):
        document = SimpleNamespace(doctype="HERO GraphQL Service", name="SYNTHETIC")
        for value in (0, None):
            with self.subTest(value=value):
                self.get_setting.return_value = value
                with self.assertRaisesRegex(PermissionError, "deletion is disabled"):
                    guards.assert_app_document_deletion_allowed(document)
        self.get_setting.assert_called_with("HERO Integration Settings", "allow_deletion")

    def test_enabled_setting_permits_guard_and_read_failure_propagates(self):
        self.get_setting.return_value = 1
        guards.assert_deletion_allowed(action="synthetic deletion")
        self.frappe.throw.assert_not_called()
        self.get_setting.side_effect = RuntimeError("setting unavailable")
        with self.assertRaisesRegex(RuntimeError, "setting unavailable"):
            guards.assert_deletion_allowed(action="synthetic deletion")


if __name__ == "__main__":
    unittest.main()
