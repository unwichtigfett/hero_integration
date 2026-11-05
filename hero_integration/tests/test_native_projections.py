from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import migration
from hero_integration.services.native_projections import creation_defaults, key_for, validate_options, values
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
    def test_v2_selected_status_reaches_native_event_creation_and_update(self):
        from hero_integration.tests.test_query_mapping_v2 import rule, plan
        operation = 'HeroCalendarEvents'
        rules = [
            rule('id', 'id', 'Event', 'custom_hero_source_id', operation=operation,
                 kind='int', purpose='Source Identity', transform='Positive Int To Text'),
            rule('title', 'title', 'Event', 'subject', operation=operation, purpose='Record Name'),
            rule('start', 'start', 'Event', 'starts_on', operation=operation, transform='Native Adapter'),
            rule('end', 'end', 'Event', 'ends_on', operation=operation, transform='Native Adapter'),
            rule('all', 'all_day', 'Event', 'all_day', operation=operation, kind='boolean', transform='Native Adapter'),
            rule('done', 'is_done', 'Event', 'status', operation=operation, kind='boolean', transform='Native Adapter'),
        ]
        selected = plan(rules, operation=operation)
        fake = SimpleNamespace(
            get_doc=lambda *args: SimpleNamespace(mapping_json=json.dumps({'native_options': EVENT_OPTIONS})),
            db=SimpleNamespace(get_single_value=lambda *args: 'UTC'))
        for existing in (None, SimpleNamespace(target_doctype='Event', target_name='EV-1', sync_status='Synchronized')):
            for done, expected in ((True, 'Completed'), (False, 'Open')):
                with self.subTest(existing=bool(existing), done=done):
                    row = event(); row.update(is_done=done, deleted=False)
                    with patch.object(migration, 'frappe', fake), patch.object(migration, '_link', return_value=existing), \
                         patch.object(migration, '_native_lookup', return_value={'event_type': 'Private'}), \
                         patch.object(migration, '_upsert', return_value=migration.Result('PREVIEW_OK', 'test')) as write:
                        result = migration._migrate_projection(row, SimpleNamespace(mode='Preview', graphql_mapping='M'), selected)
                    self.assertEqual(result.code, 'PREVIEW_OK', result)
                    self.assertEqual(write.call_args.args[3]['status'], expected)

        from hero_integration.services.query_mapping import event_record
        unselected = plan(rules[:-1], operation=operation)
        self.assertNotIn('is_done', event_record(unselected, {**event(), 'is_done': True}))
        missing = event(); missing.pop('is_done', None)
        self.assertNotIn('is_done', event_record(selected, missing))

    def test_native_identity_key_binds_source_type_and_exact_identity(self):
        key = key_for("source-a", "product", "0022")
        self.assertEqual(key, key_for("source-a", "product", "0022"))
        for source, kind, identity in (("source-b", "product", "0022"),
                                       ("source-a", "service", "0022"),
                                       ("source-a", "product", "22")):
            with self.subTest(source=source, kind=kind, identity=identity):
                self.assertNotEqual(key, key_for(source, kind, identity))

    def test_event_times_and_child_rows(self):
        out = values(CONTRACTS["HeroCalendarEvents"], event(), "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})
        self.assertEqual(out["starts_on"], "2026-10-22 08:00:00")
        self.assertEqual(out["ends_on"], "2026-10-22 09:00:00")
        self.assertEqual(out["custom_hero_resources"][0]["custom_hero_source_id"], "2")
        self.assertEqual(out["custom_hero_partners"][0]["custom_hero_partner_name"], "Example partner")

    def test_event_description_treats_hero_markup_as_text(self):
        row = event()
        row["description"] = '<img src="https://synthetic.invalid/x" onerror="run()">\nA & B'
        out = values(CONTRACTS["HeroCalendarEvents"], row, "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})
        self.assertEqual(out["description"],
                         '<p>&lt;img src=&quot;https://synthetic.invalid/x&quot; onerror=&quot;run()&quot;&gt;<br>A &amp; B</p>')

    def test_event_dst_recurrence_and_null_boolean_block(self):
        for change in ({"start": "2026-10-25T02:30:00"}, {"start": "2026-03-29T02:30:00"},
                       {"is_recurring": True}, {"is_recurring": None}, {"all_day": None}):
            row = event()
            row.update(change)
            with self.assertRaises(ValueError):
                values(CONTRACTS["HeroCalendarEvents"], row, "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})

    def test_existing_event_holds_the_whole_time_group_for_review(self):
        for change in ({'is_recurring': True}, {'is_recurring': None}, {'end': None}):
            row = event(); row.update(change)
            review = []
            out = values(CONTRACTS['HeroCalendarEvents'], row, '9', 'a', EVENT_OPTIONS,
                         lookup=lambda *args: {}, allow_partial=True, review=review)
            self.assertTrue(review)
            self.assertEqual(out['subject'], 'Installation')
            for field in ('starts_on', 'ends_on', 'all_day', 'custom_hero_original_start', 'custom_hero_original_end'):
                self.assertNotIn(field, out)

    def test_new_recurring_event_imports_the_occurrence_for_review(self):
        row = event(); row['is_recurring'] = True
        review = []
        out = values(CONTRACTS['HeroCalendarEvents'], row, '9', 'a', EVENT_OPTIONS, lookup=lambda *args: {}, review=review)
        self.assertEqual(out['starts_on'], '2026-10-22 08:00:00')
        self.assertEqual(len(review), 1)
        self.assertIn('recurring', review[0])

    def test_new_recurring_event_is_created_pending_review(self):
        plan = __import__("hero_integration.services.mapping_runtime", fromlist=["plan_from_definitions"]).plan_from_definitions(
            "HeroCalendarEvents", definitions("HeroCalendarEvents"))
        fake = SimpleNamespace(
            get_doc=lambda *args: SimpleNamespace(mapping_json=json.dumps({"native_options": EVENT_OPTIONS})),
            db=SimpleNamespace(get_single_value=lambda *args: "UTC"))
        row = event(); row['is_recurring'] = True
        with patch.object(migration, "frappe", fake), patch.object(migration, "_link", return_value=None), patch.object(
            migration, "_upsert", return_value=migration.Result("PENDING_REVIEW", "test")) as write:
            result = migration._migrate_projection(row, SimpleNamespace(mode="Execute", graphql_mapping="M"), plan)
        self.assertEqual(result.code, "PENDING_REVIEW")
        self.assertIn("recurring", write.call_args.kwargs["review_reason"])
        self.assertEqual(write.call_args.kwargs["creation_values"]["repeat_this_event"], 0)

    def test_events_wait_while_their_record_is_held_or_rejected(self):
        for status, code in (("Pending Review", "PENDING_REVIEW"), ("Rejected", "SKIPPED_REJECTED")):
            link = SimpleNamespace(sync_status=status, conflict_detail="x", target_doctype="Event", target_name="EV-1")
            plan = __import__("hero_integration.services.mapping_runtime", fromlist=["plan_from_definitions"]).plan_from_definitions(
                "HeroCalendarEvents", definitions("HeroCalendarEvents"))
            with patch.object(migration, "_link", return_value=link), patch.object(migration, "_upsert") as write:
                result = migration._migrate_projection(event(), SimpleNamespace(mode="Execute", graphql_mapping="M"), plan)
            self.assertEqual(result.code, code)
            write.assert_not_called()

    def test_blank_title_keeps_the_subject_of_an_existing_event(self):
        plan = __import__("hero_integration.services.mapping_runtime", fromlist=["plan_from_definitions"]).plan_from_definitions(
            "HeroCalendarEvents", definitions("HeroCalendarEvents"))
        for title in ("", "   "):
            row = event(); row["title"] = title
            native = values(CONTRACTS["HeroCalendarEvents"], row, "9", "a", EVENT_OPTIONS, lookup=lambda *a: {},
                            allow_partial=True, review=[])
            out = plan.values(row, project_link=lambda value: None, native_value=lambda target: native[target],
                              allow_partial=True)
            self.assertNotIn("subject", out)
            created = values(CONTRACTS["HeroCalendarEvents"], row, "9", "a", EVENT_OPTIONS, lookup=lambda *a: {})
            self.assertTrue(created["subject"])  # a new event is named after its type

    def test_optional_end_is_not_invented_for_a_new_event(self):
        row = event(); row['end'] = None
        out = values(CONTRACTS['HeroCalendarEvents'], row, '9', 'a', EVENT_OPTIONS, lookup=lambda *args: {})
        self.assertNotIn('ends_on', out)
        self.assertNotIn('custom_hero_original_end', out)

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
