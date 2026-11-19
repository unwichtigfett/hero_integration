"""Exercise native Contact validation and persistence after promoting existing children."""
import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.integrations.erpnext.contact import _set_primary_child


class TestContactPrimary(IntegrationTestCase):
    def test_existing_secondary_children_pass_native_validation(self):
        contact = frappe.get_doc({
            "doctype": "Contact", "first_name": "Synthetic Primary Switch",
            "email_ids": [{"email_id": "old@example.invalid", "is_primary": 1},
                          {"email_id": "new@example.invalid"}],
            "phone_nos": [{"phone": "+12025550101", "is_primary_phone": 1},
                          {"phone": "+12025550102", "is_primary_mobile_no": 1}],
        }).insert()
        original = [row.name for row in contact.email_ids + contact.phone_nos]
        for table, field, flag, new in (
            ("email_ids", "email_id", "is_primary", "new@example.invalid"),
            ("phone_nos", "phone", "is_primary_phone", "+12025550102"),
            ("phone_nos", "phone", "is_primary_mobile_no", "+12025550101"),
        ):
            self.assertTrue(_set_primary_child(contact, table, field, flag, new))
            contact.save()
            contact.reload()
        self.assertEqual((contact.email_id, contact.phone, contact.mobile_no),
                         ("new@example.invalid", "+12025550102", "+12025550101"))
        self.assertEqual([row.name for row in contact.email_ids + contact.phone_nos], original)
