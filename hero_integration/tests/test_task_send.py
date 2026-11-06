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

    def test_fixed_operation_backend_is_removed(self):
        for name in ('legacy_preview', 'legacy_send', 'READ_DOCUMENT', 'MUTATION_DOCUMENT', '_read_hero'):
            with self.subTest(name=name):
                self.assertFalse(hasattr(task_send, name))
