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
            db=SimpleNamespace(get_value=Mock(return_value="owner@example.invalid")),
        )
        patcher = patch.object(access, "frappe", self.frappe)
        patcher.start()
        self.addCleanup(patcher.stop)

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
