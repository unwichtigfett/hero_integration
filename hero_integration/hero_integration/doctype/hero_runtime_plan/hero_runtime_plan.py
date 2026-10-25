import frappe
from frappe.model.document import Document


class HERORuntimePlan(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        if not getattr(self.flags, "runtime_plan_lifecycle_write", False):
            frappe.throw("Runtime plans may only be changed through the canonical lifecycle service.")
