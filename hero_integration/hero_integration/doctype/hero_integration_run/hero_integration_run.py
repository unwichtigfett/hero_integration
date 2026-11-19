import json

from frappe.model.document import Document


class HEROIntegrationRun(Document):
    def validate(self):
        before = self.get_doc_before_save()
        if before and (before.page_overlap or 0) != (self.page_overlap or 0):
            raise ValueError("Page Overlap is fixed when a run is created.")

    def before_insert(self):
        import frappe
        from hero_integration.services.offset_pagination import validate_overlap

        parent = self.flags.get("parent_execution_run")
        if parent:
            parent_run = frappe.get_doc("HERO Integration Run", parent)
            self.page_overlap = validate_overlap(parent_run.page_overlap, 1000)
        else:
            settings = frappe.get_single("HERO Integration Settings")
            self.page_overlap = validate_overlap(settings.polling_page_overlap, int(settings.polling_page_size or 100))
        if self.page_overlap and getattr(self, "checkpoint_contract_json", None):
            contract = json.loads(self.checkpoint_contract_json)
            contract["page_overlap"] = self.page_overlap
            self.checkpoint_contract_json = json.dumps(contract, sort_keys=True)
        self.page_checkpoint_json = None
