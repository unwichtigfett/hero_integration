"""Historical fixed-adapter compatibility tests; public Send uses grouped Mutation mappings."""

from __future__ import annotations

import unittest

from hero_integration.services import task_send
from hero_integration.services.native_projections import _html


class TestTaskSendValues(unittest.TestCase):
    def test_description_text_is_the_inverse_of_the_import(self):
        text = "Zeile 1 & <2>\nZeile 2"
        self.assertEqual(task_send.plain_text(_html(text)), text)
        self.assertEqual(task_send.plain_text('<div class="ql-editor"><p>A</p><p>B</p></div>'), "A\nB")
        self.assertIsNone(task_send.plain_text("<p></p>"))
        self.assertIsNone(task_send.plain_text(None))

    def test_every_sent_field_is_offered_by_hero_task_input(self):
        self.assertEqual({key for _, key, _ in task_send.FIELDS} | {"id", "target_project_match_id"},
                         task_send.SENT_INPUTS)

    def test_baseline_fields_are_the_inbound_import_fields(self):
        from hero_integration.services import task_lifecycle as life
        self.assertEqual({base for _, _, base in task_send.FIELDS},
                         {"subject", "description", life.END, life.DONE, life.ASSIGNEE})

    def test_the_assignee_is_shown_by_email(self):
        self.assertEqual(task_send._show("target_user_id", 7, {7: "a@example.invalid"}), "a@example.invalid")
        self.assertEqual(task_send._show("target_user_id", 8, {}), "HERO user 8")


if __name__ == "__main__":
    unittest.main()
