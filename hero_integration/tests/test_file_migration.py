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
        for _ in range(4):
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
        with patch.object(fm, "frappe", fake), patch.object(fm, "_enqueue", side_effect=queued.append), \
                patch.object(fm, "now_datetime", lambda: "now"), \
                patch.dict("sys.modules", {"frappe.utils.synchronization": lock}), \
                patch("hero_integration.services.file_transfer.transfer", return_value=state):
            fm._file_slice(SimpleNamespace(name="R", action="Document Migration"))
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


if __name__ == "__main__":
    unittest.main()
