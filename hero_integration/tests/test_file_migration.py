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
        patch.object(fm, "frappe", fake).start()
        patch.object(fm, "_start_step", side_effect=start_step).start()
        patch.object(fm, "_enqueue", side_effect=self.queued.append).start()
        patch.object(fm, "now_datetime", lambda: "now").start()

    def finish_current(self, state="Completed"):
        child = fm._full("FULL")["child"]
        self.db.runs[child]["state"] = state
        fm.advance_full("FULL")

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

    def test_unfinished_phase_queues_the_next_slice(self):
        run, queued = self.run_slice({"done": False, "counts": {"added": 3, "unchanged": 2}})
        self.assertEqual(queued, ["R"])
        self.assertEqual((run["created_records"], run["skipped_records"], run["processed"]), (3, 2, 5))
        self.assertEqual(run["state"], "Running")

    def test_finished_phase_completes_and_failures_mark_conflicts(self):
        run, queued = self.run_slice({"done": True, "counts": {"added": 1, "failed": 2}})
        self.assertEqual((run["state"], queued), ("Completed With Conflicts", []))

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

        fake = SimpleNamespace(get_doc=lambda values: _Run(name="RUN-1", **values),
                               db=SimpleNamespace(set_default=lambda *a: None, commit=lambda: None))
        with patch.object(fm, "frappe", fake), patch.object(fm, "_configuration", return_value=_config()), \
                patch.object(fm, "_enqueue"), patch("hero_integration.services.file_transfer.reset_phase"):
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


if __name__ == "__main__":
    unittest.main()
