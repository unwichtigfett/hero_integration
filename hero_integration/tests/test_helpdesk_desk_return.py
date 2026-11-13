"""Helpdesk's way back to the Desk: the workspace stop is a setting and stays the default."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hero_integration.integrations.helpdesk import desk_return
from hero_integration.services import spa_theme


def _frappe(value=1, has_field=True, fails=False):
    def single(doctype, field):
        if fails:
            raise RuntimeError("no database")
        return value
    return SimpleNamespace(get_meta=lambda doctype: SimpleNamespace(has_field=lambda field: has_field),
                           db=SimpleNamespace(get_single_value=single))


class TestDeskReturn(TestCase):
    def _direct(self, **fake):
        with patch.object(desk_return, "frappe", _frappe(**fake)):
            return desk_return.opens_desk_directly()

    def test_only_a_switched_off_setting_opens_the_desk_directly(self):
        self.assertTrue(self._direct(value=0))
        self.assertFalse(self._direct(value=1))
        self.assertFalse(self._direct(value=0, has_field=False))
        self.assertFalse(self._direct(fails=True))

    def test_setting_is_created_once_and_keeps_the_stop_by_default(self):
        created, stored = [], []
        existing = set()
        fake = SimpleNamespace(db=SimpleNamespace(
            exists=lambda doctype, value=None: doctype == "DocType" or bool(existing),
            set_single_value=lambda *args: stored.append(args)))
        with patch.object(desk_return, "frappe", fake), patch(
                "frappe.custom.doctype.custom_field.custom_field.create_custom_fields",
                side_effect=lambda fields, update: created.append(fields) or self.assertFalse(update)):
            desk_return.ensure_setting()
            existing.add(desk_return.FIELD)
            desk_return.ensure_setting()
        self.assertEqual(len(created), 1)
        self.assertEqual(stored, [("HD Settings", desk_return.FIELD, 1)])
        field = created[0]["HD Settings"][0]
        self.assertEqual((field["fieldname"], field["fieldtype"], field["default"]),
                         (desk_return.FIELD, "Check", "1"))

    def test_script_reaches_helpdesk_only_while_the_stop_is_switched_off(self):
        with patch.object(desk_return, "frappe", _frappe(value=0)):
            self.assertIn("helpdesk_desk_return.js", spa_theme.markup(SimpleNamespace(path="/helpdesk/tickets")))
            self.assertNotIn("helpdesk_desk_return.js", spa_theme.markup(SimpleNamespace(path="/crm")))
        with patch.object(desk_return, "frappe", _frappe(value=1)):
            self.assertNotIn("helpdesk_desk_return.js", spa_theme.markup(SimpleNamespace(path="/helpdesk")))
