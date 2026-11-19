"""Scheduled AUTO_SYNC is a bound-only operation, independent of manual area imports."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration import api, tasks
from hero_integration.services import execution_runtime, sync_runtime_adapter


class TestAutoSyncRouting(unittest.TestCase):
    def test_scheduled_sync_cannot_enqueue_initial_imports(self):
        record = Mock()
        result = sync_runtime_adapter.RuntimePreflight("INCOMPLETE", "plan", 3, "source", "SYNC-1")
        with patch.object(sync_runtime_adapter, "run_scheduled_auto_sync", return_value=result) as sync, \
             patch.object(api, "_start_migration", side_effect=AssertionError):
            tasks._run("AUTO_SYNC", record)
        sync.assert_called_once_with()
        record.assert_called_once_with("AUTO_SYNC", "Incomplete", "INCOMPLETE", source_instance="source", sync_run="SYNC-1")

    def test_off_or_disabled_scheduler_suppresses_automatic_execution(self):
        fake = Mock(local=SimpleNamespace(site="isolated.localhost"))
        with patch.object(tasks, "frappe", fake), patch.object(tasks, "_run") as run, \
             patch("frappe.utils.scheduler.is_scheduler_inactive", return_value=True) as inactive:
            fake.get_single.return_value = SimpleNamespace(polling_mode="OFF")
            tasks.run_scheduled_sync()
            inactive.assert_not_called()
            fake.get_single.return_value.polling_mode = "AUTO_SYNC"
            tasks.run_scheduled_sync()
            inactive.assert_called_once_with(verbose=False)
            run.assert_not_called()

    def test_manual_import_dispatch_uses_persisted_action_without_polling_settings(self):
        from hero_integration.services import migration, file_migration
        fake = Mock()
        with patch.object(execution_runtime, "frappe", fake), patch.object(migration, "_execute_run") as native, \
             patch.object(file_migration, "_execute") as file:
            fake.db.get_value.return_value = "Project Migration"
            execution_runtime.execute("migration", run_name="M1")
            native.assert_called_once_with("M1")
            fake.db.get_value.return_value = "Full Migration"
            execution_runtime.execute("migration", run_name="M2")
            file.assert_called_once_with("M2")
            fake.get_single.assert_not_called()
