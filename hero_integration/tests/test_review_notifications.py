"""Administrator notices for held and not imported HERO records."""

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
        translation = patch.object(notices, "recipient_text", side_effect=lambda _user, text: text)
        translation.start()
        self.addCleanup(translation.stop)

    def test_each_recipient_gets_their_own_language_and_details_remain_escaped(self):
        self.records = [SimpleNamespace(name="REC-1", source_type="project", source_id="41",
                                        result="CONFLICT", detail="Known <problem>")]
        german = {
            "HERO run {0}: {1} records were not imported": "HERO-Lauf {0}: {1} Datensätze wurden nicht importiert",
            "Known <problem>": "Bekannt <Problem>",
        }
        with patch.object(notices, "configured_recipients", return_value=["de@example.invalid", "en@example.invalid"]), \
             patch.object(notices, "recipient_text", side_effect=lambda user, text: german.get(text, text) if user.startswith("de@") else text):
            self.assertTrue(notices.notify_run_summary("RUN-1"))
        de, en = self.sent
        self.assertEqual(de["subject"], "HERO-Lauf RUN-1: 1 Datensätze wurden nicht importiert")
        self.assertTrue(de["email_content"].endswith("Bekannt &lt;Problem&gt;"))
        self.assertEqual(en["subject"], "HERO run RUN-1: 1 records were not imported")
        self.assertTrue(en["email_content"].endswith("Known &lt;problem&gt;"))

    def test_broken_custom_translation_cannot_prevent_notification(self):
        link = SimpleNamespace(name="LINK-1", conflict_detail="")
        with patch.object(notices, "recipient_text", side_effect=lambda _user, text: "Fehler {" if text == "Field conflict: {0}" else text):
            self.assertTrue(notices.notify_conflict_once(link, {"customer_name"}, "new evidence"))
        self.assertEqual(self.sent[0]["subject"], "Field conflict: customer_name")

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

    def test_unmatched_task_assignees_are_one_notice_per_run(self):
        self.assertTrue(notices.notify_unmatched_assignees("HERO-RUN-3", {"b@example.invalid": 2, "a@example.invalid": 1}))
        (notice,) = self.sent
        self.assertIn("2 HERO assignees have no ERPNext user", notice["subject"])
        self.assertTrue(notice["email_content"].endswith("a@example.invalid | 1 task(s)<br>b@example.invalid | 2 task(s)"))
        self.assertFalse(notices.notify_unmatched_assignees("HERO-RUN-4", {}))

    def test_run_without_failures_sends_nothing(self):
        self.assertFalse(notices.notify_run_summary("HERO-RUN-2"))
        self.assertEqual(self.sent, [])

    def test_summary_insertion_failure_is_logged_after_prior_recipient_attempt(self):
        from hero_integration.services import migration

        self.records = [SimpleNamespace(name="REC-1", source_type="project", source_id="41", result="CONFLICT",
                                        detail="synthetic")]
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

    def test_conflict_notice_translates_dynamic_detail_for_each_recipient(self):
        link = SimpleNamespace(name="LINK-1", conflict_detail="")
        translations = {"Field conflict: {0}": "Feldkonflikt: {0}",
                        "Field conflict requires review: {0}.": "Feldkonflikt erfordert Prüfung: {0}."}
        with patch.object(notices, "configured_recipients", return_value=["de@example.invalid", "en@example.invalid"]), \
             patch.object(notices, "recipient_text", side_effect=lambda user, text: translations.get(text, text) if user.startswith("de@") else text):
            notices.notify_conflict_once(link, {"customer_name"}, "Field conflict requires review: customer_name.")
        de, en = self.sent
        self.assertEqual((de["subject"], de["email_content"]),
                         ("Feldkonflikt: customer_name", "Feldkonflikt erfordert Prüfung: customer_name."))
        self.assertEqual((en["subject"], en["email_content"]),
                         ("Field conflict: customer_name", "Field conflict requires review: customer_name."))


if __name__ == "__main__":
    unittest.main()
