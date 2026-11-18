"""Administrator-owned classification; native Links protect referenced categories."""
from frappe.model.document import Document
import frappe


class ProjectImageCategory(Document):
    def validate(self):
        previous = self.get_doc_before_save()
        old = previous.get("upload_folder") if previous else None
        if (self.get("upload_folder") or "") != (old or ""):
            frappe.throw(frappe._("The image upload folder is managed by the application."))

    def after_insert(self):
        from hero_integration.services.project_image_categories import ensure_upload_folder
        ensure_upload_folder(self)
