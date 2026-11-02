import frappe
from frappe.model.document import Document

from hero_integration.services.task_assignment_policy import policy_write_active


class HEROTaskAssignmentPolicy(Document):
    def validate(self):
        if not policy_write_active():
            frappe.throw(frappe._('Task assignment policies are managed by the authorized policy service.'), frappe.PermissionError)

    def on_trash(self):
        frappe.throw(frappe._('Task assignment policies must be retained and revoked.'), frappe.PermissionError)

    def before_rename(self, *_args, **_kwargs):
        frappe.throw(frappe._('Task assignment policy identity is immutable.'), frappe.PermissionError)
