"""Administrator notices for held and not imported HERO records (user decision 2026-10-31)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import conflict_notifications as notices


class _Inserted:
    def __init__(self, sink, values):
        self.sink, self.values = sink, values

    def insert(self, ignore_permissions=False):
        self.sink.append(self.values)


class TestReviewNotifications(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.records = []
        self.fake = SimpleNamespace(
            get_doc=lambda values: _Inserted(self.sent, values),
            get_all=lambda doctype, **kwargs: list(self.records),
        )
        patcher = patch.object(notices, "frappe", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        recipients = patch.object(notices, "configured_recipients", return_value=["admin@example.invalid"])
        recipients.start()
        self.addCleanup(recipients.stop)

    def test_held_record_asks_for_approval_or_rejection(self):
        link = SimpleNamespace(name="LINK-1", target_doctype="Project", target_name="PV-1")
        self.assertTrue(notices.notify_review(link, "The project address is missing."))
        (notice,) = self.sent
        self.assertEqual((notice["document_type"], notice["document_name"]), ("HERO Integration Link", "LINK-1"))
        self.assertIn("approval or rejection", notice["subject"])
        self.assertEqual(notice["email_content"], "The project address is missing.")

    def test_run_summary_lists_every_not_imported_record_by_reason(self):
        row = lambda source, result, detail: SimpleNamespace(source_type=source, result=result, detail=detail)
        self.records = [row("Task", "BLOCKED_RECORD", "No project binding.")] * 3 + [
            row("project", "PENDING_REVIEW", "Address missing.")]
        self.assertTrue(notices.notify_run_summary("HERO-RUN-1"))
        (notice,) = self.sent
        self.assertEqual(notice["document_name"], "HERO-RUN-1")
        self.assertIn("4 records were not imported", notice["subject"])
        self.assertEqual(notice["email_content"].splitlines(), [
            "3 × Task BLOCKED_RECORD: No project binding.",
            "1 × project PENDING_REVIEW: Address missing.",
        ])

    def test_run_without_failures_sends_nothing(self):
        self.assertFalse(notices.notify_run_summary("HERO-RUN-2"))
        self.assertEqual(self.sent, [])

    def test_conflict_notice_is_only_created_for_changed_evidence(self):
        link = SimpleNamespace(name="LINK-1", conflict_detail="existing evidence")
        self.assertFalse(notices.notify_conflict_once(link, {"email_id"}, "existing evidence"))
        self.assertEqual(self.sent, [])

        self.assertTrue(notices.notify_conflict_once(link, {"email_id", "customer_name"}, "new evidence"))
        (notice,) = self.sent
        self.assertEqual(notice["subject"], "Field conflict: customer_name, email_id")
        self.assertEqual(notice["email_content"], "new evidence")
        self.assertEqual((notice["document_type"], notice["document_name"]), ("HERO Integration Link", "LINK-1"))


if __name__ == "__main__":
    unittest.main()
