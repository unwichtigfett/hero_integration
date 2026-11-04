"""Document, Picture and Full Migration run control."""

from __future__ import annotations

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import file_migration as fm


class _DB:
    def __init__(self):
        self.runs, self.defaults = {}, {}

    def get_value(self, doctype, name, field):
        return self.runs.get(name, {}).get(field)

    def set_value(self, doctype, name, values):
        self.runs.setdefault(name, {}).update(values)

    def get_default(self, key):
        return self.defaults.get(key)

    def set_default(self, key, value):
        self.defaults[key] = value

    def commit(self):
        pass

    def rollback(self):
        pass


class TestFullMigrationChain(unittest.TestCase):
    def setUp(self):
        self.db = _DB()
        self.db.runs["FULL"] = {"state": "Running"}
        self.started, self.queued = [], []
        fake = SimpleNamespace(db=self.db, log_error=lambda **k: None)

        def start_step(action, parent):
            if action == "Schedule Migration" and self.no_events:
                return ""
            name = f"RUN-{len(self.started) + 1}"
            self.started.append(action)
            self.db.runs[name] = {"state": "Queued"}
            return name

        self.no_events = False
        self.addCleanup(patch.stopall)
        patch("frappe.utils.synchronization.filelock", return_value=contextlib.nullcontext()).start()
        patch.object(fm, "frappe", fake).start()
        patch.object(fm, "_start_step", side_effect=start_step).start()
        patch.object(fm, "_enqueue", side_effect=self.queued.append).start()
        patch.object(fm, "now_datetime", lambda: "now").start()

    def finish_current(self, state="Completed"):
        child = fm._full("FULL")["child"]
        self.db.runs[child]["state"] = state
        fm.advance_full("FULL")

    def test_new_full_run_keeps_services_after_product_links_and_before_files(self):
        steps = [fm.EMPLOYEE_ACTION, *fm.FULL_STEPS[:4], fm.SERVICE_ACTION, *fm.FULL_STEPS[4:]]
        fm._save_full("FULL", {"step": 0, "child": "", "notes": [], "steps": steps})
        fm.advance_full("FULL")
        for _ in steps:
            self.finish_current()
        self.assertEqual(self.started, steps)
        self.assertEqual(self.started.index(fm.SERVICE_ACTION), self.started.index(fm.PRODUCT_ACTION) + 1)
        self.assertEqual(self.db.runs["FULL"]["state"], "Completed")

    def test_steps_run_one_after_another_and_complete(self):
        fm.advance_full("FULL")
        self.assertEqual(self.started, ["Project Migration"])
        fm.advance_full("FULL")  # child still queued: nothing new starts
        self.assertEqual(len(self.started), 1)
        for _ in range(len(fm.FULL_STEPS) - 1):
            self.finish_current()
        self.assertEqual(self.started, list(fm.FULL_STEPS))
        self.finish_current("Completed With Conflicts")
        self.assertEqual(self.db.runs["FULL"]["state"], "Completed With Conflicts")
        self.assertIn("Picture Migration: conflicts", self.db.runs["FULL"]["error_message"])

    def test_area_without_read_service_is_skipped(self):
        self.no_events = True
        fm.advance_full("FULL")
        self.finish_current()
        self.assertEqual(self.started, ["Project Migration", "Projection Migration"])
        self.assertIn("Schedule Migration: skipped", "; ".join(fm._full("FULL")["notes"]))

    def test_failed_step_blocks_the_chain(self):
        fm.advance_full("FULL")
        self.finish_current("Failed")
        self.assertEqual(self.db.runs["FULL"]["state"], "Blocked")
        self.assertEqual(self.started, ["Project Migration"])

    def test_a_finished_child_queues_its_parent(self):
        self.db.defaults[fm.CHILD_KEY.format("RUN-9")] = "FULL"
        fm.on_run_finished("RUN-9")
        self.assertEqual(self.queued, ["FULL"])
        self.db.runs["FULL"]["state"] = "Cancelled"
        fm.on_run_finished("RUN-9")
        self.assertEqual(self.queued, ["FULL"])


class TestFileSlice(unittest.TestCase):
    def run_slice(self, state):
        db = _DB()
        db.runs["R"] = {"state": "Running"}
        queued = []
        fake = SimpleNamespace(db=db, log_error=lambda **k: None)
        lock = SimpleNamespace(filelock=lambda *a, **k: contextlib.nullcontext())
        config = SimpleNamespace(source_instance="source-a")
        with patch.object(fm, "frappe", fake), patch.object(fm, "_enqueue", side_effect=queued.append), \
                patch.object(fm, "now_datetime", lambda: "now"), \
                patch.dict("sys.modules", {"frappe.utils.synchronization": lock}), \
                patch("hero_integration.services.file_transfer._client_and_downloader",
                      return_value=("client", "downloader", "source-a")) as build, \
                patch("hero_integration.services.file_transfer.transfer", return_value=state) as transfer:
            fm._file_slice(SimpleNamespace(name="R", action="Document Migration"), config)
        build.assert_called_once_with(config)
        self.assertEqual(transfer.call_args.kwargs["source_instance"], "source-a")
        self.assertEqual((transfer.call_args.kwargs["client"], transfer.call_args.kwargs["downloader"]),
                         ("client", "downloader"))
        return db.runs["R"], queued

    def test_employee_action_dispatches_to_employee_importer(self):
        db=_DB();db.runs['R']={'state':'Running'}
        fake=SimpleNamespace(db=db,log_error=lambda **k:None)
        lock=SimpleNamespace(filelock=lambda *a,**k:contextlib.nullcontext())
        with patch.object(fm,'frappe',fake),patch.object(fm,'now_datetime',lambda:'now'), \
                patch.dict('sys.modules',{'frappe.utils.synchronization':lock}), \
                patch('hero_integration.services.file_transfer._client_and_downloader',return_value=('c','d','default')), \
                patch('hero_integration.services.employee_migration.sync_employees',return_value={'done':True,'counts':{'added':1}}) as importer:
            fm._file_slice(SimpleNamespace(name='R',action=fm.EMPLOYEE_ACTION),SimpleNamespace(source_instance='default'))
        self.assertEqual(importer.call_args.kwargs['run_name'],'R')
        self.assertEqual(db.runs['R']['state'],'Completed')

    def test_unfinished_phase_queues_the_next_slice(self):
        run, queued = self.run_slice({"done": False, "counts": {"added": 3, "unchanged": 2}})
        self.assertEqual(queued, ["R"])
        self.assertEqual((run["created_records"], run["skipped_records"], run["processed"]), (3, 2, 5))
        self.assertEqual(run["state"], "Running")

    def test_finished_phase_completes_and_failures_mark_conflicts(self):
        run, queued = self.run_slice({"done": True, "counts": {"added": 1, "failed": 2}})
        self.assertEqual((run["state"], queued), ("Completed With Conflicts", []))

    def test_counts_are_written_after_every_page_while_the_slice_runs(self):
        db = _DB()
        db.runs["R"] = {"state": "Running"}
        seen = []

        def transfer(phase, **kwargs):
            kwargs["progress"]({"added": 200})
            seen.append(dict(db.runs["R"]))  # the run already shows the first page
            kwargs["progress"]({"added": 300, "unchanged": 100})
            seen.append(dict(db.runs["R"]))
            return {"done": False, "counts": {"added": 300, "unchanged": 100}}

        fake = SimpleNamespace(db=db, log_error=lambda **k: None)
        lock = SimpleNamespace(filelock=lambda *a, **k: contextlib.nullcontext())
        with patch.object(fm, "frappe", fake), patch.object(fm, "_enqueue"), patch.object(fm, "now_datetime", lambda: "now"), \
                patch.dict("sys.modules", {"frappe.utils.synchronization": lock}), \
                patch("hero_integration.services.file_transfer._client_and_downloader", return_value=("c", "d", "s")), \
                patch("hero_integration.services.file_transfer.transfer", side_effect=transfer):
            fm._file_slice(SimpleNamespace(name="R", action="Document Migration"), SimpleNamespace(source_instance="s"))
        self.assertEqual([(row["created_records"], row["processed"]) for row in seen], [(200, 200), (300, 400)])

    def test_disk_pause_blocks_the_run(self):
        run, _ = self.run_slice({"done": False, "last_error": "Not enough free disk space; transfer paused.", "counts": {}})
        self.assertEqual(run["state"], "Blocked")
        self.assertIn("disk", run["error_message"])


def _config(instance="source-a", revision=3, confirmed=True):
    return SimpleNamespace(source_instance=instance, source_binding_revision=revision, source_binding_confirmed=confirmed)


class TestSourceBinding(unittest.TestCase):
    def bound(self, run, config):
        with patch.object(fm, "_configuration", return_value=config):
            return fm.bound_configuration(run)

    def test_run_uses_only_the_confirmed_binding_it_was_queued_under(self):
        run = SimpleNamespace(source_instance="source-a", source_binding_revision=3)
        self.assertEqual(self.bound(run, _config()).source_instance, "source-a")
        for config in (_config(instance="source-b"), _config(revision=4), _config(confirmed=False)):
            with self.subTest(config=config), self.assertRaises(fm.SourceBindingError):
                self.bound(run, config)
        with self.assertRaises(fm.SourceBindingError):  # legacy run without a recorded source
            self.bound(SimpleNamespace(source_instance="", source_binding_revision=3), _config())

    def test_start_records_source_and_binding_revision(self):
        created = []

        class _Run(SimpleNamespace):
            def insert(self, **kwargs):
                created.append(self)

        fake = SimpleNamespace(get_single=lambda dt: SimpleNamespace(testing_enabled=0), get_doc=lambda values: _Run(name="RUN-1", **values),
                               db=SimpleNamespace(set_default=lambda *a: None, commit=lambda: None))
        with patch.object(fm, "frappe", fake), patch.object(fm, "_configuration", return_value=_config()), \
                patch.object(fm, "_enqueue"), patch.object(fm.direct_runtime, "resolve_read"), \
                patch("hero_integration.services.file_transfer.reset_phase"):
            fm.start("Document Migration")
        self.assertEqual((created[0].source_instance, created[0].source_binding_revision), ("source-a", 3))
        with patch.object(fm, "frappe", fake), patch.object(fm, "_configuration", return_value=_config(confirmed=False)), \
                self.assertRaises(fm.SourceBindingError):
            fm.start("Document Migration")

    def test_source_change_before_execution_blocks_without_reading_hero(self):
        db = _DB()
        db.runs["R"] = {"state": "Running"}

        class _Run(SimpleNamespace):
            def save(self, **kwargs):
                pass

        run = _Run(name="R", state="Running", owner="admin", action="Document Migration",
                   source_instance="source-a", source_binding_revision=3)
        fake = SimpleNamespace(db=db, get_doc=lambda *a: run, set_user=lambda user: None, log_error=lambda **k: None)
        with patch.object(fm, "frappe", fake), patch.object(fm, "now_datetime", lambda: "now"), \
                patch.object(fm, "_configuration", return_value=_config(instance="source-b")), \
                patch.object(fm, "on_run_finished"), patch.object(fm, "_file_slice") as file_slice:
            fm.execute("R")
        file_slice.assert_not_called()
        self.assertEqual(db.runs["R"]["state"], "Blocked")
        self.assertIn("another HERO source", db.runs["R"]["error_message"])


class TestMappingReadiness(unittest.TestCase):
    def test_missing_mapping_blocks_before_run_insert(self):
        with patch.object(fm, "confirmed_configuration", return_value=_config()), \
                patch.object(fm.direct_runtime, "resolve_read", side_effect=ValueError("approve mapping")), \
                patch.object(fm.frappe, "get_doc") as insert:
            for action in (*fm.DIRECT_OPERATIONS, fm.FULL_ACTION):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "approve mapping"):
                    fm.start(action)
            insert.assert_not_called()

    def test_resume_preserves_blocked_run_when_mapping_is_unapproved(self):
        run = SimpleNamespace(action="Picture Migration", state="Blocked")
        with patch.object(fm, "bound_configuration", return_value=_config()), \
                patch.object(fm.direct_runtime, "resolve_read", side_effect=ValueError("approve mapping")), \
                self.assertRaisesRegex(ValueError, "approve mapping"):
            fm.resume(run)
        self.assertEqual(run.state, "Blocked")

    def test_scope_and_service_come_from_the_selected_mapping(self):
        from dataclasses import replace
        template = fm.direct_runtime.builtin_read("HeroProducts")
        read = replace(template, origin="approved-reader", mapping="current-mapping")
        fake = SimpleNamespace(get_all=lambda *a, **kw: [], db=SimpleNamespace(exists=lambda *a: False))
        with patch.object(fm, "frappe", fake), patch.object(fm.direct_runtime, "resolve_read", return_value=read):
            products = next(row for row in fm.page_areas([], "source-a") if row["key"] == "products")
        self.assertEqual(products["service"], {"service": "approved-reader", "mapping": "current-mapping"})
        item = next(row for row in products["scope"]["objects"] if row["doctype"] == "Item")
        self.assertIn("custom_hero_manufacturer_nr", item["fields"])
        self.assertEqual(products["scope"]["field_count"], len(read.plan.definitions) - 1)  # two price lists, one field

    def test_page_marks_direct_and_full_areas_blocked(self):
        fake = SimpleNamespace(get_all=lambda *a, **kw: [], db=SimpleNamespace(exists=lambda *a: False))
        with patch.object(fm, "frappe", fake), \
                patch.object(fm.direct_runtime, "resolve_read", side_effect=ValueError("approve mapping")):
            rows = {row["key"]: row for row in fm.page_areas([], "source-a")}
        for key in ("employees", "products", "services", "documents", "pictures", "full"):
            self.assertFalse(rows[key]["ready"])
            self.assertEqual(rows[key]["blocker"], "approve mapping")


class TestEveryStartPathIsSourceBound(unittest.TestCase):
    """The older file_transfer entry points share the bound run path."""

    def setUp(self):
        from hero_integration import api
        from hero_integration.services import file_transfer as ft

        self.api, self.ft = api, ft
        self.inserted, self.queued = [], []

        class _Run(SimpleNamespace):
            def insert(inner, **kwargs):
                self.inserted.append(inner)

        def throw(message, *args, **kwargs):
            raise RuntimeError(message)

        self.fake = SimpleNamespace(get_single=lambda dt: SimpleNamespace(testing_enabled=0), only_for=lambda *a: None, throw=throw, _=lambda text: text,
                                    get_doc=lambda values: _Run(name="RUN-1", **values),
                                    db=SimpleNamespace(exists=lambda *a, **k: None, set_default=lambda *a: None,
                                                       commit=lambda: None),
                                    log_error=lambda **kwargs: self.queued.append(("logged", kwargs)))
        for target in (patch.object(api, "frappe", self.fake), patch.object(ft, "frappe", self.fake),
                       patch.object(fm, "frappe", self.fake), patch.object(fm.direct_runtime, "resolve_read"),
                       patch.object(fm, "_enqueue", side_effect=lambda name: self.queued.append(name)),
                       patch("hero_integration.services.file_transfer.reset_phase")):
            target.start()
            self.addCleanup(target.stop)

    def test_unconfirmed_binding_blocks_both_public_starts(self):
        with patch.object(fm, "_configuration", return_value=_config(confirmed=False)):
            for start in (lambda: self.ft.start("documents"), lambda: self.api.sync_area_start("documents")):
                with self.subTest(start=start), self.assertRaisesRegex(RuntimeError, "confirm the source binding"):
                    start()
        self.assertEqual((self.inserted, self.queued), ([], []))

    def test_legacy_start_creates_a_bound_run(self):
        with patch.object(fm, "_configuration", return_value=_config()):
            self.assertEqual(self.ft.start("images"), "RUN-1")
        self.assertEqual((self.inserted[0].action, self.inserted[0].source_instance,
                          self.inserted[0].source_binding_revision), ("Picture Migration", "source-a", 3))
        self.assertEqual(self.queued, ["RUN-1"])

    def test_already_queued_legacy_job_transfers_nothing(self):
        with patch.object(self.ft, "transfer", side_effect=AssertionError("legacy job must not transfer")):
            self.ft.run_job("documents")
        self.assertEqual(self.queued[0][0], "logged")

    def test_transfer_without_a_bound_client_requires_a_confirmed_binding(self):
        with patch.object(fm, "_configuration", return_value=_config(confirmed=False)), \
                self.assertRaises(fm.SourceBindingError):
            self.ft.transfer("documents", dry_run=True)


if __name__ == "__main__":
    unittest.main()
