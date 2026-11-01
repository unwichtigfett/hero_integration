"""Offline checks for personal mapping and Version permission hooks."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hero_integration.services import mapping_draft_access as access


class TestPersonalMappingAccess(unittest.TestCase):
    def setUp(self):
        self.frappe = SimpleNamespace(
            session=SimpleNamespace(user="owner@example.invalid"),
            get_roles=Mock(return_value=["System Manager"]),
            db=SimpleNamespace(get_value=Mock(return_value="owner@example.invalid"),
                               escape=Mock(return_value="<escaped-user>")),
        )
        patcher = patch.object(access, "frappe", self.frappe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_personal_list_filters_require_manager_and_escape_owner(self):
        self.frappe.get_roles.return_value = ["Employee"]
        self.assertEqual(access.personal_mapping_query(doctype="HERO Mapping Draft"), "1=0")
        self.frappe.db.escape.assert_not_called()
        self.frappe.get_roles.return_value = ["System Manager"]
        for doctype, column in (("HERO Mapping Draft", "owner_user"), ("HERO Mapping Revision", "actor")):
            with self.subTest(doctype=doctype):
                query = access.personal_mapping_query(user="owner'example", doctype=doctype)
                self.assertIn(f"`tab{doctype}`.`{column}` = <escaped-user>", query)
                self.frappe.db.escape.assert_called_with("owner'example")

    def test_personal_version_list_filter_escapes_owner_and_preserves_other_versions(self):
        query = access.personal_version_query(user="owner'example")
        self.frappe.db.escape.assert_called_once_with("owner'example")
        self.assertIn("`tabVersion`.`ref_doctype` not in", query)
        self.assertIn("d.owner_user=<escaped-user>", query)
        self.assertIn("r.actor=<escaped-user>", query)
        self.assertNotIn("owner'example", query)

    def test_unrelated_versions_leave_native_permission_decision_unchanged(self):
        doc = SimpleNamespace(ref_doctype="Customer", docname="CUST-1")
        self.assertIsNone(access.personal_version_permission(doc))
        self.frappe.db.get_value.assert_not_called()
        self.frappe.get_roles.assert_not_called()

    def test_personal_versions_require_both_owner_and_system_manager(self):
        for doctype, column in (("HERO Mapping Draft", "owner_user"), ("HERO Mapping Revision", "actor")):
            doc = SimpleNamespace(ref_doctype=doctype, docname="synthetic")
            with self.subTest(doctype=doctype):
                self.assertTrue(access.personal_version_permission(doc))
                self.frappe.db.get_value.assert_called_with(doctype, "synthetic", column)
                self.assertFalse(access.personal_version_permission(doc, user="other@example.invalid"))
                self.frappe.get_roles.return_value = ["Employee"]
                self.assertFalse(access.personal_version_permission(doc))
                self.frappe.get_roles.return_value = ["System Manager"]
