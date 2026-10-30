from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from hero_integration.services import schema_refresh

NOW = datetime(2026, 10, 30, 12, 0, 0)


def _settings(**values):
    defaults = dict(hero_api_url="https://hero.synthetic.invalid/graphql", source_binding_revision=2,
                    schema_verified_revision=2, schema_verified_on=NOW - timedelta(days=1),
                    schema_last_checked_on=NOW - timedelta(minutes=61), schema_refresh_interval_minutes=60,
                    schema_verified_by="admin@example.com", token="synthetic-token")
    defaults.update(values)
    token = defaults.pop("token")
    settings = SimpleNamespace(**defaults)
    settings.get_password = lambda fieldname, raise_exception=True: token
    return settings


class _Db:
    def __init__(self):
        self.values = {}

    def set_single_value(self, doctype, field, value, update_modified=True):
        assert doctype == "HERO Integration Settings" and update_modified is False
        self.values[field] = value


def _frappe(settings):
    return SimpleNamespace(get_single=lambda doctype: settings, db=_Db(), enqueue=MagicMock(),
                           local=SimpleNamespace(site=None))


class TestSchemaRefreshDue(unittest.TestCase):
    def test_new_or_changed_binding_is_checked_on_save_and_never_periodically(self):
        for settings in (_settings(schema_verified_on=None), _settings(schema_verified_revision=1)):
            with self.subTest(settings=settings):
                self.assertTrue(schema_refresh.check_due_after_save(settings))
                self.assertFalse(schema_refresh.periodic_due(settings, NOW))

    def test_disabled_or_tokenless_settings_never_contact_hero(self):
        for settings in (_settings(hero_api_url=""), _settings(token=None)):
            with self.subTest(settings=settings):
                self.assertFalse(schema_refresh.check_due_after_save(settings))
                self.assertFalse(schema_refresh.periodic_due(settings, NOW))

    def test_verified_binding_refreshes_after_the_configured_interval(self):
        self.assertFalse(schema_refresh.check_due_after_save(_settings()))
        self.assertTrue(schema_refresh.periodic_due(_settings(), NOW))
        self.assertFalse(schema_refresh.periodic_due(_settings(schema_last_checked_on=NOW - timedelta(minutes=59)), NOW))
        self.assertTrue(schema_refresh.periodic_due(
            _settings(schema_refresh_interval_minutes=10, schema_last_checked_on=NOW - timedelta(minutes=10)), NOW))

    def test_missing_interval_uses_the_default(self):
        for empty in (None, 0, ""):
            with self.subTest(empty=empty):
                self.assertEqual(schema_refresh.refresh_interval(_settings(schema_refresh_interval_minutes=empty)), 60)


class TestSchemaRefreshRecording(unittest.TestCase):
    def test_success_verifies_the_binding_that_was_current_at_start(self):
        settings = _settings(schema_verified_on=None, source_binding_revision=3)
        fake = _frappe(settings)
        result = {"status": "CAPTURED", "snapshot_name": "S1"}
        with patch.object(schema_refresh, "frappe", fake), patch.object(schema_refresh, "_capture", return_value=result):
            self.assertIs(schema_refresh.refresh("admin@example.com"), result)
        self.assertEqual(fake.db.values["schema_check_status"], "OK")
        self.assertEqual(fake.db.values["schema_verified_revision"], 3)
        self.assertEqual(fake.db.values["schema_verified_by"], "admin@example.com")
        self.assertIsNotNone(fake.db.values["schema_verified_on"])

    def test_unchanged_schema_reports_that_the_snapshot_stays(self):
        fake = _frappe(_settings())
        with patch.object(schema_refresh, "frappe", fake), \
                patch.object(schema_refresh, "_capture", return_value={"status": "UNCHANGED", "snapshot_name": "S1"}):
            schema_refresh.refresh("admin@example.com")
        self.assertIn("unchanged", fake.db.values["schema_check_message"])

    def test_failure_is_recorded_and_does_not_verify(self):
        settings = _settings(schema_verified_on=None)
        fake = _frappe(settings)
        error = schema_refresh.SchemaRefreshError("HERO rejected the API token. Check the token and save again.")
        with patch.object(schema_refresh, "frappe", fake), patch.object(schema_refresh, "_capture", side_effect=error), \
                self.assertRaises(schema_refresh.SchemaRefreshError):
            schema_refresh.refresh("admin@example.com")
        self.assertEqual(fake.db.values["schema_check_status"], "FAILED")
        self.assertIn("API token", fake.db.values["schema_check_message"])
        self.assertNotIn("schema_verified_revision", fake.db.values)

    def test_rejected_token_has_a_clear_message(self):
        from hero_integration.client.graphql import HeroClientError

        for code in ("HTTP_401", "HTTP_403"):
            with self.subTest(code=code):
                message = schema_refresh._client_error_message(HeroClientError("HERO request failed.", code=code))
                self.assertIn("rejected the API token", message)


class TestScheduledSchemaRefresh(unittest.TestCase):
    def test_tick_enqueues_one_deduplicated_job_only_when_due(self):
        fake = _frappe(_settings(schema_last_checked_on=None))
        with patch.object(schema_refresh, "frappe", fake):
            schema_refresh.run_scheduled_schema_refresh()
        fake.enqueue.assert_called_once()
        kwargs = fake.enqueue.call_args.kwargs
        self.assertEqual((kwargs["queue"], kwargs["job_id"], kwargs["deduplicate"]),
                         ("long", schema_refresh.REFRESH_LOCK, True))

        fake = _frappe(_settings(schema_verified_on=None))
        with patch.object(schema_refresh, "frappe", fake):
            schema_refresh.run_scheduled_schema_refresh()
        fake.enqueue.assert_not_called()

    def test_job_refreshes_as_the_verifying_user_and_swallows_recorded_failures(self):
        fake = _frappe(_settings(schema_last_checked_on=None))
        with patch.object(schema_refresh, "frappe", fake), \
                patch.object(schema_refresh, "refresh", side_effect=schema_refresh.SchemaRefreshError("x")) as refresh:
            schema_refresh.scheduled_refresh_job()
        refresh.assert_called_once_with("admin@example.com")


if __name__ == "__main__":
    unittest.main()
