"""Read projections over real, synthetic checkpoint rows. No worker or transport."""
import json
from uuid import uuid4
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from hero_integration import api
from hero_integration.services import file_migration, run_progress
from hero_integration.services.execution_budget import BudgetReached, ExecutionBudget
from hero_integration.services.integration_events import record_processed_page


class TestRunProgress(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.root = uuid4().hex
        self.site = frappe.local.site
        self.addCleanup(frappe.db.rollback)
        self.addCleanup(frappe.set_user, "Administrator")

    def row(self, dt, name, **values):
        now = now_datetime()
        fields = ["name", "creation", "modified", *values]
        frappe.db.bulk_insert(dt, fields, [(name, now, now, *values.values())])

    def run_row(self, name, action, **values):
        self.row(run_progress.RUN, name, action=action, state=values.pop("state", "Running"),
                 source_instance="fixture", **values)

    def view(self):
        with patch("hero_integration.client.graphql.HeroGraphQLClient.query", side_effect=AssertionError), \
                patch.object(frappe.db, "set_value", side_effect=AssertionError):
            rows = api.sync_page_runs(live=1, watched=json.dumps([self.root]))["rows"]
        return next(row for row in rows if row["name"] == self.root)

    def test_distinct_progress_after_replay_and_changed_overlap(self):
        self.run_row(self.root, "Customer Migration", resume_phase="customer", resume_offset=40)
        for identity, result in (("1", "CREATED"), ("1", "UPDATED"), ("1", "UPDATED"),
                                 ("2", "CONFLICT"), ("3", "BLOCKED_RECORD")):
            self.row("HERO Integration Record", uuid4().hex, run=self.root, result=result, source_type="customer",
                     source_id=identity, technical_action="hero_to_frappe", target_doctype="Customer", target_name=identity)
        card = self.view()
        self.assertEqual(card["checkpoint"], [40, 0])
        self.assertEqual(card["counts"], dict(processed=3, created=1, updated=1, unchanged=0,
                                              skipped=0, conflicts=1, errors=1, rows_read=None))
        self.assertEqual(card["counts"], self.view()["counts"])

    def test_full_parent_uses_children_and_one_budget(self):
        names = [uuid4().hex for _ in file_migration.NEW_FULL_STEPS]
        self.run_row(self.root, file_migration.FULL_ACTION, state="Completed", execution_budget_run=self.root,
                     testing_enabled=1, testing_max_actions=50)
        for name, action in zip(names, file_migration.NEW_FULL_STEPS):
            self.run_row(name, action, state="Completed", progress_counts_json=json.dumps(
                dict.fromkeys(run_progress.OUTCOMES, 0)))
        file_migration._save_full(self.root, {"steps": list(file_migration.NEW_FULL_STEPS), "step": len(names),
                                              "completed_children": names, "reconciliation_done": True})
        record_processed_page(names[-2], "supply_product", ["1", "2"])
        record_processed_page(names[-2], "supply_product", ["2"])
        for index in (5, 6):
            record_processed_page(names[index], "file_upload", ["same-file"])
        key = ExecutionBudget("migration", self.root, "fixture").budget_id
        self.row("HERO Execution Budget", key, run_id=self.root, source_instance="fixture", record_limit=50, used=32)
        self.row("HERO Pending Reference", self.root, origin_run=names[0], status="Waiting", identity_key=self.root)
        card = self.view()
        self.assertEqual(card["budget"], {"used": 32, "limit": 50, "remaining": 18})
        self.assertEqual(card["counts"]["processed"], 3)
        self.assertEqual((len(card["children"]),card["active"],card["pending"]), (len(names),True,1))
        self.assertEqual(card["state"], "Waiting for dependencies")
        frappe.db.set_value("HERO Pending Reference", self.root, "status", "Resolved")
        self.assertFalse(self.view()["active"])

    def test_sync_and_monitor_are_visible_without_migration(self):
        self.row(run_progress.SYNC, self.root, sync_run_id=self.root, site_scope=self.site,
                 state="RUNNING", source_instance="fixture", execution_scope="BOUND_OBJECTS",
                 confirmed_position="1a2b3c", skipped_count=1, rows_read=2)
        self.assertTrue(run_progress.has_activity())
        card = self.view()
        self.assertEqual((card["kind"], card["phase"], card["counts"]["rows_read"]), ("sync","BOUND_OBJECTS",2))
        self.assertEqual(card["budget"]["remaining"], 50)
        self.root = uuid4().hex
        self.row(run_progress.MONITOR,self.root,mode="MONITOR",status="Started")
        self.assertTrue(self.view()["active"])
        frappe.db.set_value(run_progress.MONITOR,self.root,{"status": "Incomplete", "rows_read": 7})
        self.assertEqual(self.view()["counts"]["rows_read"], 7)
        self.assertFalse(self.view()["active"])

    def test_midpage_budget_stop_retains_only_settled_outcomes(self):
        self.run_row(self.root, file_migration.PRODUCT_ACTION)
        def capped(*args, observe_identity, **kwargs):
            observe_identity("accepted", {"added": 1, "rows_read": 2})
            raise BudgetReached("test cap")

        def cleanup():
            frappe.db.rollback()
            frappe.db.delete("HERO Integration Record", {"run": self.root})
            frappe.db.delete(run_progress.RUN, self.root)
            frappe.db.commit()

        self.addCleanup(cleanup)
        frappe.db.commit()
        row = frappe.get_doc(run_progress.RUN, self.root)
        with patch.object(file_migration, "_direct_reader"), \
                patch("hero_integration.services.file_transfer._client_and_downloader", return_value=(None,None,"fixture")), \
                patch("hero_integration.services.product_migration.sync_products", side_effect=capped), \
                self.assertRaises(BudgetReached):
            file_migration._process_file_slice(row, object())
        card = self.view()
        self.assertEqual((card["counts"]["processed"], card["counts"]["created"]), (1,1))
        self.assertIsNone(card["checkpoint"])

    def test_guest_cannot_observe_known_receipt(self):
        self.run_row(self.root, "Customer Migration")
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            self.view()
