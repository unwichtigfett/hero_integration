from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services.runtime_plan import (
    FrappeRuntimePlanAudit,
    FrappeRuntimePlanAuthority,
    FrappeRuntimePlanStorage,
    RuntimePlanConflict,
    RuntimePlanSelection,
    RuntimePlanService,
)


class _References:
    def validate_current(self, selection):
        return None


class TestRuntimePlanFrappeIntegration(IntegrationTestCase):
    fingerprint = "b" * 64
    source_instance = "b8a-test-source"

    def setUp(self):
        frappe.set_user("Administrator")
        for doctype in ("HERO Runtime Plan Audit", "HERO Runtime Plan"):
            frappe.db.delete(doctype, {"source_instance": self.source_instance})

    def tearDown(self):
        for doctype in ("HERO Runtime Plan Audit", "HERO Runtime Plan"):
            frappe.db.delete(doctype, {"source_instance": self.source_instance})

    def _selection(self, mapping_revision=1):
        return RuntimePlanSelection(
            site_scope=frappe.local.site,
            source_instance=self.source_instance,
            graphql_service="B8A-TEST-SERVICE",
            service_revision=1,
            operation_revision=1,
            graphql_mapping="B8A-TEST-MAPPING",
            mapping_revision=mapping_revision,
            schema_snapshot="B8A-TEST-SNAPSHOT",
            schema_fingerprint=self.fingerprint,
            operation_name="b8aTestRead",
            execution_mode="AUTO_SYNC",
        )

    def _service(self):
        return RuntimePlanService(
            FrappeRuntimePlanStorage(),
            _References(),
            FrappeRuntimePlanAudit(),
            FrappeRuntimePlanAuthority(),
        )

    def test_real_mariadb_lifecycle_cas_and_audit(self):
        service = self._service()
        draft = service.create_draft(self._selection(), actor="Administrator")
        self.assertEqual(frappe.db.count("HERO Runtime Plan", {"plan_key": draft.plan_key}), 1)
        self.assertEqual(frappe.db.count("HERO Runtime Plan Audit", {"runtime_plan": draft.plan_key}), 1)

        unchanged = service.update_draft(
            draft.plan_key, self._selection(), expected_revision=draft.revision, actor="Administrator"
        )
        self.assertEqual((unchanged.revision, unchanged.generation), (1, 1))
        changed = service.update_draft(
            draft.plan_key, self._selection(mapping_revision=2), expected_revision=1, actor="Administrator"
        )
        self.assertEqual((changed.revision, changed.generation), (2, 2))
        with self.assertRaises(RuntimePlanConflict):
            service.update_draft(draft.plan_key, self._selection(), expected_revision=1, actor="Administrator")

        approved = service.approve(changed.plan_key, expected_revision=2, actor="Administrator")
        stored = FrappeRuntimePlanStorage().read(approved.plan_key)
        self.assertEqual((stored.state.value, stored.revision, stored.generation), ("APPROVED", 3, 3))
        self.assertEqual(frappe.db.count("HERO Runtime Plan Audit", {"runtime_plan": approved.plan_key}), 3)
