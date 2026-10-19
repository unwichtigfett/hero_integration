from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda: lambda function: function
    sys.modules["frappe"] = frappe

from hero_integration import api
from hero_integration.client import settings as client_settings


class _Settings:
    def __init__(
        self,
        token: str | None = None,
        password_error: Exception | None = None,
        stored_credential: bool = False,
        allow_graphql_mutation_execution: int | str = 0,
    ):
        self.hero_api_url = "https://hero.synthetic.invalid/graphql"
        self.allowed_hero_hosts = "hero.synthetic.invalid"
        self.hero_source_instance = "default"
        self.timeout_seconds = 30
        self.token = token
        self.password_error = password_error
        self.stored_credential = stored_credential
        self.allow_graphql_mutation_execution = allow_graphql_mutation_execution
        self.password_calls: list[tuple[str, bool]] = []
        self.save_calls = 0

    def get_password(self, fieldname: str, raise_exception: bool = True) -> str | None:
        self.password_calls.append((fieldname, raise_exception))
        if self.password_error:
            raise self.password_error
        return self.token

    def get(self, fieldname: str) -> str | None:
        if fieldname != "hero_api_token":
            raise AssertionError(fieldname)
        return "********" if self.token or self.stored_credential else None

    def save(self) -> None:
        self.save_calls += 1


class _FrappeSettingsMock:
    def __init__(self, settings: _Settings, permission_error: Exception | None = None):
        self.settings = settings
        self.permission_error = permission_error
        self.permission_calls: list[str] = []
        self.get_single_calls: list[str] = []

    def only_for(self, role: str) -> None:
        self.permission_calls.append(role)
        if self.permission_error:
            raise self.permission_error

    def get_single(self, doctype: str) -> _Settings:
        self.get_single_calls.append(doctype)
        return self.settings

    def throw(self, message: str) -> None:
        raise ValueError(message)


class TestWorkspaceSettingsContract(unittest.TestCase):
    def _call(self, settings: _Settings, permission_error: Exception | None = None):
        frappe_mock = _FrappeSettingsMock(settings, permission_error)
        with patch.object(api, "frappe", frappe_mock):
            result = api.workspace_settings_get()
        return result, frappe_mock

    def test_missing_token_is_reported_as_not_configured_without_transport(self):
        result, frappe_mock = self._call(_Settings())

        self.assertEqual(result["api_token_configured"], False)
        self.assertEqual(result["api_token_status"], "NOT_CONFIGURED")
        self.assertEqual(frappe_mock.settings.password_calls, [("hero_api_token", False)])
        self.assertEqual(frappe_mock.permission_calls, ["System Manager"])
        self.assertEqual(frappe_mock.get_single_calls, ["HERO Integration Settings"])

    def test_missing_mutation_configuration_is_disabled(self):
        result, _ = self._call(_Settings())

        self.assertFalse(result["mutation_execution_requested"])
        self.assertFalse(result["mutation_execution_released"])
        self.assertFalse(result["mutation_execution_enabled"])
        self.assertEqual(result["mutation_execution_status"], "DISABLED")

    def test_manipulated_mutation_setting_remains_disabled_before_release(self):
        result, _ = self._call(_Settings(allow_graphql_mutation_execution=1))

        self.assertTrue(result["mutation_execution_requested"])
        self.assertFalse(result["mutation_execution_enabled"])
        self.assertEqual(result["mutation_execution_status"], "DISABLED")

    def test_admin_can_persist_a_safe_off_mutation_request(self):
        settings = _Settings(allow_graphql_mutation_execution=1)
        frappe_mock = _FrappeSettingsMock(settings)

        with patch.object(api, "frappe", frappe_mock):
            result = api.workspace_mutation_execution_set(False)

        self.assertEqual(frappe_mock.permission_calls, ["System Manager"])
        self.assertEqual(settings.allow_graphql_mutation_execution, 0)
        self.assertEqual(settings.save_calls, 1)
        self.assertFalse(result["mutation_execution_enabled"])

    def test_admin_activation_request_is_rejected_before_settings_write(self):
        settings = _Settings()
        frappe_mock = _FrappeSettingsMock(settings)

        with patch.object(api, "frappe", frappe_mock), self.assertRaisesRegex(ValueError, api.MUTATION_EXECUTION_DISABLED):
            api.workspace_mutation_execution_set(True)

        self.assertEqual(frappe_mock.permission_calls, ["System Manager"])
        self.assertEqual(frappe_mock.get_single_calls, [])
        self.assertEqual(settings.save_calls, 0)

    def test_non_admin_cannot_change_the_mutation_setting(self):
        settings = _Settings()
        frappe_mock = _FrappeSettingsMock(settings, PermissionError("denied"))

        with patch.object(api, "frappe", frappe_mock), self.assertRaisesRegex(PermissionError, "denied"):
            api.workspace_mutation_execution_set(False)

        self.assertEqual(frappe_mock.get_single_calls, [])
        self.assertEqual(settings.save_calls, 0)

    def test_valid_token_is_configured_but_never_returned(self):
        token = "synthetic-secret-that-must-not-leak"
        result, _ = self._call(_Settings(token=token))

        self.assertEqual(result["api_token_configured"], True)
        self.assertEqual(result["api_token_status"], "CONFIGURED")
        self.assertNotIn("hero_api_token", result)
        self.assertNotIn(token, json.dumps(result))

    def test_missing_settings_permission_is_denied_before_reading(self):
        settings = _Settings()

        with self.assertRaisesRegex(PermissionError, "denied"):
            self._call(settings, PermissionError("denied"))

        self.assertEqual(settings.password_calls, [])

    def test_defective_stored_credential_remains_fail_closed(self):
        settings = _Settings(password_error=RuntimeError("credential decryption failed"))

        with self.assertRaisesRegex(RuntimeError, "credential decryption failed"):
            self._call(settings)

        self.assertEqual(settings.password_calls, [("hero_api_token", False)])

    def test_undecryptable_stored_credential_is_not_reported_as_configured(self):
        result, _ = self._call(_Settings(stored_credential=True))

        self.assertEqual(result["api_token_configured"], False)
        self.assertEqual(result["api_token_status"], "CREDENTIAL_ERROR")

    def test_missing_token_blocks_transport_configuration(self):
        frappe_mock = _FrappeSettingsMock(_Settings())

        with patch.object(client_settings, "frappe", frappe_mock), patch(
            "hero_integration.client.graphql.urllib.request.build_opener"
        ) as build_opener:
            with self.assertRaisesRegex(client_settings.HeroConfigurationError, "not configured"):
                client_settings.get_configuration()

        build_opener.assert_not_called()
