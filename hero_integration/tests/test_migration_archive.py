"""Archive selection is local to migration. Source lifecycle remains authoritative for sync."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import file_transfer, migration, migration_archive as archive, migration_testing as testing
from hero_integration.services.migration_testing_selection import prepare_native
from hero_integration.sources.hero import SourcePage


class MigrationArchiveTests(TestCase):
    def test_schema_flags_are_type_and_area_specific(self):
        with archive.selection(False):
            for operation, flag in archive.FIELDS.items():
                with self.subTest(operation=operation):
                    self.assertTrue(archive.excluded(operation, {flag: True}))
                    for value in (False, None, "false"):
                        self.assertFalse(archive.excluded(operation, {flag: value}))
            self.assertFalse(archive.excluded("HeroEmployees", {"status": "inactive", "deleted": True}))
            self.assertFalse(archive.excluded("HeroProducts", {"is_deleted": False, "base_data": {"is_deleted": True}}))

    def test_archived_dependency_never_reaches_existing_binding_or_native_writer(self):
        row = {"id": 4, "is_deleted": True}
        write, budget = Mock(), Mock()
        run = SimpleNamespace(mode="Execute")
        with archive.selection(False), patch.object(migration, "_migrate_isolated") as fence:
            migration._reserve_mapped_record(budget, "customer", row, None)
            result = migration._migrate_with_binding(row, run, None, "customer", write)
            self.assertEqual(result.code, "SKIPPED_SOURCE_DELETED")
            fence.assert_not_called()
            self.assertFalse(budget.mock_calls)
        with archive.selection(True), patch.object(migration, "_migrate_isolated", side_effect=lambda fn, **kw: fn()):
            with patch("hero_integration.services.execution_runtime.process_migration_record", side_effect=lambda *a, **k: a[4]()) as fence:
                migration._migrate_with_binding(row, SimpleNamespace(mode="Execute", name="run"), None, "customer", write)
                self.assertIs(fence.call_args.args[1], row)
                self.assertIs(row["is_deleted"], True)
                write.assert_called_once()

    def test_standalone_test_fills_limit_across_an_all_archived_page(self):
        rows = {0: SourcePage(({"id": 1, "is_deleted": True},), 1),
                1: SourcePage(({"id": 2, "is_deleted": False},), None)}
        reader = Mock(side_effect=rows.__getitem__)
        source = SimpleNamespace(contract=SimpleNamespace(target_operation="HeroTasks"), projection=reader)
        plan = SimpleNamespace(source_identity=lambda row: str(row["id"]))
        budget = Mock(version=3, limit=1)
        budget.selection.return_value = None
        budget.incremental_ids.side_effect = lambda op, kind, pages: [key for page in pages for key in page][:1]
        for include, expected in ((False, 2), (True, 1)):
            with archive.selection(include):
                replay = prepare_native(source, budget, "test-contract", plan)
                self.assertEqual([r["id"] for r in replay.projection().records], [expected])
                self.assertEqual(budget.freeze.call_args.args[1], [str(expected)])
        self.assertIn(1, [call.args[0] for call in reader.call_args_list])

    def test_discovery_does_not_admit_assignees_from_excluded_tasks(self):
        row = {"id": 1, "target_project_match_id": 8, "author_user_id": 5,
               "target_user_id": 6, "is_deleted": True}
        for include in (False, True):
            cohort = {"project_ids": ["8"], "user_ids": [], "partner_ids": [], "task_ids": [], "event_ids": []}
            with archive.selection(include):
                testing.collect_related_projection(cohort, "HeroTasks", [row])
            self.assertEqual(cohort["user_ids"], ["5", "6"] if include else [])
            self.assertEqual(cohort["task_ids"], ["1"] if include else [])

    def test_file_detail_and_shared_primary_address_evidence(self):
        row = {"downloadUrl": "synthetic"}
        rel = {"address": {"id": 6}, "is_deleted": True}
        with archive.selection(False):
            self.assertEqual(file_transfer.skip_reason(row, {"is_deleted": True}), "skipped_archived")
            self.assertTrue(archive.primary_address_excluded({"customer_addresses": [rel]}, 6))
            self.assertFalse(archive.primary_address_excluded({"customer_addresses": [rel, {**rel, "is_deleted": False}]}, 6))
            self.assertEqual(testing.processed({"added": 2, "skipped_archived": 3}), 2)
        with archive.selection(True):
            self.assertIsNone(file_transfer.skip_reason(row, {"is_deleted": True}))
            self.assertEqual(file_transfer.skip_reason({}, {"is_deleted": True}), "no_url")

    def test_sync_recovery_keeps_original_lifecycle_including_in_nested_context(self):
        with archive.selection(True):
            with archive.run_selection(SimpleNamespace(inventory_sync_run="sync", migrate_archived_records=1)):
                self.assertFalse(archive.active())
                self.assertEqual(file_transfer.skip_reason({}, {"is_deleted": True}), "deleted")
            self.assertTrue(archive.includes_archived())
        self.assertFalse(archive.active())

    def test_setting_round_trips_through_workspace_and_config_exchange(self):
        from hero_integration import api
        from hero_integration.services import config_exchange as cx
        from hero_integration.tests.test_workspace_settings_contract import _Settings, _FrappeSettingsMock
        from hero_integration.tests.test_config_exchange import payload

        settings = _Settings()
        with patch.object(api, "frappe", _FrappeSettingsMock(settings)):
            self.assertFalse(api.workspace_settings_get()["migrate_archived_records"])
            for value in (1, 0):
                saved = api.workspace_settings_save(True, settings.hero_api_url, migrate_archived_records=value)
                self.assertEqual(saved["migrate_archived_records"], bool(value))
                self.assertEqual(api.workspace_settings_get()["migrate_archived_records"], bool(value))
                local = {"migrate_archived_records": value}
                exported = cx._export_settings(local)
                parsed = cx._parse(payload(settings=exported))
                target = {"migrate_archived_records": 1 - value}
                db = Mock()
                with patch.object(cx, "frappe", SimpleNamespace(get_single=lambda *_: target, db=db)):
                    cx._apply_settings(cx._settings_changes(parsed))
                    db.set_single_value.assert_called_once_with("HERO Integration Settings", "migrate_archived_records", value)
                    self.assertEqual(cx._settings_changes(cx._parse(payload(settings={}))), {})
        with self.assertRaises(cx.ConfigExchangeError):
            cx._parse(payload(settings={"migrate_archived_records": "false"}))
