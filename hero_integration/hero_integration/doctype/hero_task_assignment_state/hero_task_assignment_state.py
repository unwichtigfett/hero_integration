import frappe
from frappe.model.document import Document
from hero_integration.services.task_assignment_state import managed_assignment_active


class HEROTaskAssignmentState(Document):
    def validate(self):
        if not managed_assignment_active():
            frappe.throw('Task assignment state is managed by the integration.', frappe.PermissionError)

    def on_trash(self):
        frappe.throw('Task assignment state must be retained.', frappe.PermissionError)

    def before_rename(self, *_args, **_kwargs):
        frappe.throw('Task assignment state identity is immutable.', frappe.PermissionError)
