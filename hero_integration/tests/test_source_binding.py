"""Administrative HERO source binding: no account identity is ever assumed."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace


class TestSourceBinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = importlib.import_module("hero_integration.client")
        cls._missing = object()
        cls._saved_alias = getattr(cls.package, "settings", cls._missing)
        cls._saved = {name: sys.modules.get(name) for name in ("frappe", "hero_integration.client.settings")}
        cls.frappe = types.ModuleType("frappe")
        sys.modules["frappe"] = cls.frappe
        sys.modules.pop("hero_integration.client.settings", None)
        cls.settings_module = importlib.import_module("hero_integration.client.settings")

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls._saved_alias is cls._missing:
            if getattr(cls.package, "settings", None) is cls.settings_module:
                delattr(cls.package, "settings")
        else:
            cls.package.settings = cls._saved_alias

    def test_any_endpoint_token_or_label_change_opens_an_unconfirmed_revision(self):
        rule = self.settings_module.next_source_binding
        self.assertEqual(rule(0, 0, binding_changed=False, confirm=False), (0, 0))
        self.assertEqual(rule(0, 0, binding_changed=True, confirm=False), (1, 0))
        self.assertEqual(rule(1, 0, binding_changed=False, confirm=True), (1, 1))
        self.assertEqual(rule(1, 1, binding_changed=True, confirm=True), (2, 2))
        self.assertEqual(rule(2, 2, binding_changed=True, confirm=False), (3, 2))
        self.assertEqual(self.settings_module.BINDING_FIELDS, ("hero_api_url", "hero_api_token", "hero_source_instance"))

    def _configuration(self, **fields):
        values = {
            "allowed_hero_hosts": "example.invalid", "hero_api_url": "https://example.invalid/graphql",
            "timeout_seconds": 30, "hero_source_instance": "account-a", **fields,
        }
        settings = SimpleNamespace(**values, get_password=lambda name, raise_exception=False: "token")
        self.frappe.get_single = lambda doctype: settings
        return self.settings_module.get_configuration()

    def test_configuration_reports_confirmation_without_exposing_identity_from_the_token(self):
        self.assertTrue(self._configuration().source_binding_confirmed)
        unconfirmed = self._configuration(source_binding_revision=3, source_binding_confirmed_revision=2)
        self.assertFalse(unconfirmed.source_binding_confirmed)
        self.assertEqual(unconfirmed.source_binding_revision, 3)
        self.assertNotIn("token", repr(unconfirmed))


if __name__ == "__main__":
    unittest.main()
