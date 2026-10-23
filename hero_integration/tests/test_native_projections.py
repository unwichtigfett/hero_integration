from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import migration
from hero_integration.services.native_projections import creation_defaults, validate_options, values
from hero_integration.services.projection_contracts import CONTRACTS
from hero_integration.tests.test_projection_contracts import definitions, documents, record
from hero_integration.services.read_contract import from_documents


EVENT_OPTIONS = {"confirmed": True, "source_timezone": "Europe/Berlin",
                 "target_timezone": "UTC", "all_day_end": "exclusive"}


def event():
    row = record("HeroCalendarEvents")
    row.update(title="Installation", start="2026-10-22T10:00:00+02:00",
               end="2026-10-22T11:00:00+02:00", all_day=False, is_recurring=False,
               resources=[{"id": 2, "name": "Vehicle A", "type": "vehicle", "type_id": 1}],
               partners=[{"id": 3, "full_name": "Example partner"}])
    return row


class TestNativeProjections(unittest.TestCase):
    def test_event_times_and_child_rows(self):
        out = values(CONTRACTS["HeroCalendarEvents"], event(), "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})
        self.assertEqual(out["starts_on"], "2026-10-22 08:00:00")
        self.assertEqual(out["ends_on"], "2026-10-22 09:00:00")
        self.assertEqual(out["custom_hero_resources"][0]["custom_hero_source_id"], "2")
        self.assertEqual(out["custom_hero_partners"][0]["custom_hero_partner_name"], "Example partner")

    def test_event_dst_recurrence_and_null_boolean_block(self):
        for change in ({"start": "2026-10-25T02:30:00"}, {"start": "2026-03-29T02:30:00"},
                       {"is_recurring": True}, {"all_day": None}):
            row = event()
            row.update(change)
            with self.assertRaises(ValueError):
                values(CONTRACTS["HeroCalendarEvents"], row, "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})

    def test_all_day_exclusive_end_becomes_native_inclusive_date(self):
        row = event()
        row.update(start="2026-10-22T00:00:00", end="2026-10-24T00:00:00", all_day=True)
        out = values(CONTRACTS["HeroCalendarEvents"], row, "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})
        self.assertEqual(out["starts_on"], "2026-10-22 00:00:00")
        self.assertEqual(out["ends_on"], "2026-10-23 23:59:59")

    def test_event_defaults_never_share_or_notify(self):
        out = creation_defaults(CONTRACTS["HeroCalendarEvents"], EVENT_OPTIONS)
        self.assertEqual(out["event_type"], "Private")
        self.assertEqual(out["event_participants"], [])
        for field in ("send_reminder", "sync_with_google_calendar", "add_video_conferencing", "repeat_this_event"):
            self.assertEqual(out[field], 0)

    def test_event_requires_closed_and_confirmed_options(self):
        with self.assertRaises(ValueError):
            validate_options(CONTRACTS["HeroCalendarEvents"], {**EVENT_OPTIONS, "unknown": True})
        with self.assertRaises(ValueError):
            validate_options(CONTRACTS["HeroCalendarEvents"], {**EVENT_OPTIONS, "confirmed": False})

    def test_read_contract_and_execution_pipeline_require_timezone_match(self):
        service, mapping, snapshot = documents("HeroCalendarEvents")
        self.assertEqual(from_documents(service, mapping, snapshot).target_operation, "HeroCalendarEvents")
        plan = __import__("hero_integration.services.mapping_runtime", fromlist=["plan_from_definitions"]).plan_from_definitions(
            "HeroCalendarEvents", definitions("HeroCalendarEvents")
        )
        fake = SimpleNamespace(
            get_doc=lambda *args: SimpleNamespace(mapping_json=json.dumps({"native_options": EVENT_OPTIONS})),
            db=SimpleNamespace(get_single_value=lambda *args: "UTC"),
        )
        with patch.object(migration, "frappe", fake), patch.object(migration, "_link", return_value=None), patch.object(
            migration, "_upsert", return_value=migration.Result("PREVIEW_OK", "test")
        ) as write:
            result = migration._migrate_projection(event(), SimpleNamespace(mode="Preview", graphql_mapping="M"), plan)
        self.assertEqual(result.code, "PREVIEW_OK")
        self.assertEqual(write.call_args.kwargs["creation_values"]["event_type"], "Private")

    def test_child_table_hash_ignores_frappe_generated_row_metadata(self):
        contract = CONTRACTS["HeroCalendarEvents"]
        field = "custom_hero_resources"
        row = {"custom_hero_source_id": "2", "custom_hero_resource_name": "Vehicle A",
               "custom_hero_resource_type": "vehicle", "custom_hero_type_id": "1"}
        first = SimpleNamespace(get=lambda name: [row] if name == field else [])
        second = SimpleNamespace(get=lambda name: [{**row, "name": "child-random-name", "idx": 1, "owner": "admin"}] if name == field else [])
        self.assertEqual(
            migration._projection_target_values(first, {field: [row]}, contract),
            migration._projection_target_values(second, {field: [row]}, contract),
        )
