from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import migration
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.tests.test_projection_contracts import definitions, record


class TestProjectionRuntime(unittest.TestCase):
    def test_task_identity_is_source_instance_scoped(self):
        plan = plan_from_definitions("HeroTasks", definitions("HeroTasks"))
        calls = []
        with patch.object(
            migration,
            "_upsert",
            side_effect=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or migration.Result("PREVIEW_OK", "synthetic")
            ),
        ):
            for instance in ("one", "two"):
                row = record()
                row["_hero_source_instance"] = instance
                self.assertEqual(
                    migration._migrate_projection(row, SimpleNamespace(mode="Preview"), plan).code,
                    "PREVIEW_OK",
                )
        keys = [call[0][3]["custom_hero_key"] for call in calls]
        self.assertNotEqual(*keys)
        self.assertEqual(calls[0][0][4], "HERO External Task")

    def test_deleted_source_only_updates_provenance_link(self):
        plan = plan_from_definitions("HeroTasks", definitions("HeroTasks"))
        row = record()
        row["is_deleted"] = True
        with patch.object(migration, "_link", return_value=None), patch.object(
            migration, "_mark_source_deleted"
        ) as mark, patch.object(migration, "_upsert") as write:
            result = migration._migrate_projection(row, SimpleNamespace(mode="Execute"), plan)
        self.assertEqual(result.code, "SKIPPED_SOURCE_DELETED")
        mark.assert_called_once()
        write.assert_not_called()

    def test_unresolved_project_blocks_task_write(self):
        plan = plan_from_definitions("HeroTasks", definitions("HeroTasks"))
        row = record()
        row["target_project_match_id"] = 4
        with patch.object(migration, "_link", return_value=None), patch.object(
            migration, "_upsert"
        ) as write:
            result = migration._migrate_projection(row, SimpleNamespace(mode="Execute"), plan)
        self.assertEqual(result.code, "BLOCKED_RECORD")
        write.assert_not_called()

    def test_calendar_private_or_notification_change_conflicts(self):
        plan = plan_from_definitions("HeroCalendarEvents", definitions("HeroCalendarEvents"))
        row = record("HeroCalendarEvents")
        bound = SimpleNamespace(target_doctype="Event", target_name="EV-1")
        with (
            patch.object(migration, "_link", return_value=bound),
            patch.object(
                migration,
                "_native_lookup",
                return_value=SimpleNamespace(get=lambda field: "Public" if field == "event_type" else 0),
            ),
        ):
            result = migration._migrate_projection(row, SimpleNamespace(mode="Preview"), plan)
        self.assertEqual(result.code, "CONFLICT")
