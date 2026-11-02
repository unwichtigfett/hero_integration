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

    def test_run_summary_identifies_each_record_and_escapes_error_text(self):
        self.records = [
            SimpleNamespace(name="REC-1", source_type="project", source_id="41", result="PENDING_REVIEW", detail="Address <missing>"),
            SimpleNamespace(name="REC-2", source_type="project", source_id="42", result="PENDING_REVIEW", detail="Address <missing>"),
        ]
        self.assertTrue(notices.notify_run_summary("HERO-RUN-1"))
        (notice,) = self.sent
        self.assertIn("2 records were not imported", notice["subject"])
        self.assertEqual(notice["email_content"],
                         "REC-1 | project | 41 | PENDING_REVIEW | Address &lt;missing&gt;<br>"
                         "REC-2 | project | 42 | PENDING_REVIEW | Address &lt;missing&gt;")

    def test_run_without_failures_sends_nothing(self):
        self.assertFalse(notices.notify_run_summary("HERO-RUN-2"))
        self.assertEqual(self.sent, [])

    def test_summary_insertion_failure_is_logged_after_prior_recipient_attempt(self):
        from hero_integration.services import migration

        self.records = [SimpleNamespace(source_type="project", result="CONFLICT", detail="synthetic")]
        logged = []

        def get_doc(values):
            if values["for_user"] == "second@example.invalid":
                def fail_insert(**_kwargs):
                    raise RuntimeError("synthetic insert failure")
                return SimpleNamespace(insert=fail_insert)
            return _Inserted(self.sent, values)

        self.fake.get_doc = get_doc
        self.fake.log_error = lambda **kwargs: logged.append(kwargs["title"])
        with patch.object(migration, "frappe", self.fake), patch.object(
            notices, "configured_recipients", return_value=["first@example.invalid", "second@example.invalid"]
        ):
            migration._notify_not_imported(SimpleNamespace(name="RUN-1", mode="Execute"))

        self.assertEqual([row["for_user"] for row in self.sent], ["first@example.invalid"])
        self.assertEqual(logged, ["HERO run summary notification failed"])

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
