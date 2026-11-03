from __future__ import annotations

import unittest
from contextlib import nullcontext
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

    def test_calendar_partial_values_preserve_unconfirmed_clears_and_falsy_scalars(self):
        from hero_integration.services.native_projections import values as native_values
        from hero_integration.services.projection_contracts import CONTRACTS
        from hero_integration.tests.test_projection_contracts import native_options

        plan = plan_from_definitions("HeroCalendarEvents", definitions("HeroCalendarEvents"))
        row = record("HeroCalendarEvents")
        row.update(description="", full_address="", resources=[], partners=[], is_done=False,
                   category_id=0, color="")
        native = native_values(CONTRACTS["HeroCalendarEvents"], row, "9", "a",
                               native_options("HeroCalendarEvents"), lookup=lambda *args: {}, allow_partial=True)
        result = plan.values(row, native_value=lambda field: native[field], allow_partial=True)
        for field in ("description", "location", "custom_hero_resources", "custom_hero_partners"):
            self.assertNotIn(field, result)
        self.assertEqual(result["status"], "Open")  # HERO "not done" is the native open Event
        self.assertEqual(result["custom_hero_category_id"], "0")
        self.assertEqual(result["color"], "")
        self.assertEqual(result["all_day"], 0)
        done = native_values(CONTRACTS["HeroCalendarEvents"], {**row, "is_done": True}, "9", "a",
                             native_options("HeroCalendarEvents"), lookup=lambda *args: {}, allow_partial=True)
        self.assertEqual(done["status"], "Completed")
        with self.assertRaises(ValueError):
            plan.values({**row, "category_id": False}, native_value=lambda field: native[field], allow_partial=True)

    def test_event_upsert_preserves_omitted_baselines_and_detects_later_local_conflict(self):
        from hero_integration.services.change_detection import encode, field_hashes

        before = {"subject": "old", "location": "acknowledged"}
        link = SimpleNamespace(name="LINK", target_doctype="Event", target_name="EV-1", mapping_version="v1",
                               source_field_hashes=encode(field_hashes(before)), target_field_hashes=encode(field_hashes(before)))
        class Target:
            name = "EV-1"
            def __init__(self): self.fields = {**before, "location": "local edit"}; self.saves = 0
            def get(self, field): return self.fields.get(field)
            def set(self, field, value): self.fields[field] = value
            def save(self): self.saves += 1
        target = Target()
        fake = SimpleNamespace(db=SimpleNamespace(exists=lambda *args: True, savepoint=lambda *args: None),
                               get_doc=lambda *args: target)
        def baseline(_link, _source_hash, _target_hash, source, target_hashes, _version):
            link.source_field_hashes = encode(source)
            link.target_field_hashes = encode(target_hashes)
        with patch.object(migration, "frappe", fake), patch.object(migration, "_target_available", return_value=True), \
             patch.object(migration, "_link", return_value=link), patch.object(migration, "_observe_link"), \
             patch.object(migration, "_update_link_baseline", side_effect=baseline), \
             patch.object(migration, "managed_target_update", return_value=nullcontext()), \
             patch.object(migration, "notify_conflict_once"), patch.object(migration, "_mark_link_conflict"):
            def apply(values, version="v1"):
                return migration._upsert("CalendarEvent", "9", {"id": 9}, values, "Event", "custom_hero_key",
                                         version, SimpleNamespace(mode="Execute"), preserve_omitted=True)
            self.assertEqual(apply({"subject": "new"}).code, "UPDATED")
            self.assertEqual(target.fields, {"subject": "new", "location": "local edit"})
            self.assertEqual(apply({"subject": "new"}).code, "SKIPPED")
            self.assertEqual(apply({"subject": "new", "location": "source edit"}).code, "CONFLICT")
            self.assertEqual(apply({"subject": "new"}, "v2").code, "CONFLICT")
            self.assertEqual(target.saves, 1)
