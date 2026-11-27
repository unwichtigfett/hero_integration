import json

from frappe.model.document import Document


class HEROIntegrationRun(Document):
    def validate(self):
        before = self.get_doc_before_save()
        if before and (before.page_overlap or 0) != (self.page_overlap or 0):
            raise ValueError("Page Overlap is fixed when a run is created.")

        if before and getattr(before, "execution_budget_run", None) and self.execution_budget_run != before.execution_budget_run:
            raise ValueError("The execution budget root is fixed at acceptance.")
        if before and int(getattr(before, "migrate_archived_records", 0) or 0) != int(getattr(self, "migrate_archived_records", 0) or 0):
            raise ValueError("Archive selection is fixed when a migration run starts.")
        if before and any((getattr(before, key, None) or 0) != (getattr(self, key, None) or 0)
                          for key in ('testing_enabled', 'testing_max_actions', 'budget_contract_version')):
            raise ValueError("The run's test budget is fixed at creation.")

    def before_insert(self):
        import frappe
        from hero_integration.services.offset_pagination import validate_overlap

        from hero_integration.services.test_environment import guard_run
        guard_run(self)
        if getattr(self, "budget_contract_version", 0) in (2, 3) and not (
                self.meta.has_field("budget_contract_version") and
                frappe.get_meta("HERO Execution Budget").has_field("selection_json") and
                frappe.get_meta("HERO Budget Outcome").has_field("phase")):
            raise ValueError("Apply the primary record budget schema before accepting a bounded run.")
        if getattr(self, "budget_contract_version", 0) == 3 and not (
                frappe.get_meta("HERO Execution Budget").has_field("incremental_json") and
                frappe.get_meta("HERO Test Cohort Series").has_field("source_binding_revision")):
            raise ValueError("Apply the incremental cohort schema before accepting a test migration.")
        parent = self.flags.get("parent_execution_run")
        if parent:
            parent_run = frappe.get_doc("HERO Integration Run", parent)
            self.migrate_archived_records = int(getattr(parent_run, "migrate_archived_records", 0) or 0)
            self.page_overlap = validate_overlap(parent_run.page_overlap, 1000)
        else:
            settings = frappe.get_single("HERO Integration Settings")
            self.migrate_archived_records = int(getattr(settings, "migrate_archived_records", 0) or 0)
            self.page_overlap = validate_overlap(settings.polling_page_overlap, int(settings.polling_page_size or 100))
        if self.page_overlap and getattr(self, "checkpoint_contract_json", None):
            contract = json.loads(self.checkpoint_contract_json)
            contract["page_overlap"] = self.page_overlap
            self.checkpoint_contract_json = json.dumps(contract, sort_keys=True)
        self.page_checkpoint_json = None
