"""Task dates, completion and assignee from HERO."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from hero_integration.services import task_lifecycle as life

TODAY = date(2026, 11, 2)


def local(value):
    return datetime.fromisoformat(value.replace("Z", "")) if value else None


def values(**record):
    return life.source_values(record, local, {7: "Anna@Example.invalid "}, TODAY)


class _Task(dict):
    def set(self, field, value):
        self[field] = value

    def __getattr__(self, name):
        return self.get(name)

    def __setattr__(self, name, value):
        self[name] = value


class TestTaskLifecycleSource(unittest.TestCase):
    def test_start_is_the_creation_date_and_end_the_due_date(self):
        result = values(created="2026-10-01T08:00:00", due_date="2026-10-20T23:00:00")
        self.assertEqual((result[life.START], result[life.END]), ("2026-10-01", "2026-10-20"))

    def test_a_task_created_after_its_due_date_starts_on_the_due_date(self):
        result = values(created="2026-10-21T08:00:00", due_date="2026-10-20T08:00:00")
        self.assertEqual(result[life.START], "2026-10-20")

    def test_done_is_the_tick_date_and_never_in_the_future(self):
        self.assertEqual(values(done_date="2026-10-05T10:00:00")[life.DONE], "2026-10-05")
        self.assertEqual(values(done_date="2027-01-01T10:00:00")[life.DONE], "2026-11-02")
        self.assertIsNone(values(done_date=None)[life.DONE])

    def test_the_assignee_is_the_hero_users_email(self):
        self.assertEqual(values(target_user_id=7)[life.ASSIGNEE], "anna@example.invalid")
        self.assertEqual(values(target_user_id=8)[life.ASSIGNEE], "hero user 8")
        self.assertIsNone(values(target_user_id=None)[life.ASSIGNEE])

    def test_paths_hero_did_not_return_stay_omitted(self):
        self.assertEqual(values(), {})


class TestTaskLifecycleDecision(unittest.TestCase):
    def test_without_baseline_an_untouched_default_takes_hero(self):
        self.assertEqual(life.decide("f", "s", "t", None, None, None), "APPLY")
        self.assertEqual(life.decide("f", "s", "t", None, None, "2026-10-01"), "CONFLICT")
        self.assertEqual(life.decide("f", "s", "s", None, None, "x"), "CONFIRM")

    def test_three_way_rules(self):
        self.assertEqual(life.decide("f", "s1", "t2", "s1", "t1", "x"), "KEEP_LOCAL")
        self.assertEqual(life.decide("f", "s2", "t1", "s1", "t1", "x"), "APPLY")
        self.assertEqual(life.decide("f", "s2", "t2", "s1", "t1", "x"), "CONFLICT")


class TestTaskLifecycleApply(unittest.TestCase):
    def test_hero_done_completes_and_reopening_clears(self):
        task = _Task(status="Open")
        life.apply(task, {life.DONE: "2026-10-05"})
        self.assertEqual((task.status, task.completed_on), ("Completed", "2026-10-05"))
        self.assertEqual(life.target_values(task)[life.DONE], "2026-10-05")
        life.apply(task, {life.DONE: None})
        self.assertEqual(task.status, "Open")

    def test_local_overdue_status_is_not_a_completion_change(self):
        self.assertIsNone(life.target_values(_Task(status="Overdue"))[life.DONE])


if __name__ == "__main__":
    unittest.main()
