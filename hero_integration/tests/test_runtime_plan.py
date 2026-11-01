from __future__ import annotations

import unittest
from dataclasses import replace

from hero_integration.services.runtime_plan import (
    FrappeRuntimePlanReferences,
    InMemoryRuntimePlanAudit,
    InMemoryRuntimePlanStorage,
    RuntimePlanConflict,
    RuntimePlanError,
    RuntimePlanService,
    RuntimePlanState,
    RuntimePlanSelection,
)
from types import SimpleNamespace


FINGERPRINT = "a" * 64


class References:
    def __init__(self):
        self.checked = []
        self.rejected = False

    def validate_current(self, selection):
        self.checked.append(selection)
        if self.rejected:
            raise RuntimePlanError("selected plan evidence is stale")


class Authority:
    def __init__(self, allowed=("manager",)):
        self.allowed = set(allowed)
        self.calls = []

    def require_system_manager(self, actor, site_scope):
        self.calls.append((actor, site_scope))
        if actor not in self.allowed:
            raise PermissionError("System Manager authority is required.")


class FrappeReferences:
    def __init__(self):
        self.local = SimpleNamespace(site="b9-test.localhost")
        self.documents = {
            ("HERO GraphQL Schema Snapshot", "snapshot-a"): SimpleNamespace(
                source_instance="hero-one", schema_fingerprint=FINGERPRINT,
                analysis_status="Validated", execution_contract_status="Compatible",
            ),
            ("HERO GraphQL Service", "HERO-GQL-00001"): SimpleNamespace(
                service_revision=1, schema_snapshot="snapshot-a", schema_fingerprint=FINGERPRINT,
                operation_name="contacts", status="Validated", execution_status="Approved",
                read_mapping="HERO-GQL-MAP-00001", approved_service_revision=1,
                approved_mapping_revision=1,
            ),
            ("HERO GraphQL Mapping", "HERO-GQL-MAP-00001"): SimpleNamespace(
                mapping_revision=1, schema_snapshot="snapshot-a", schema_fingerprint=FINGERPRINT,
                source_instance="hero-one", operation_name="contacts", operation_kind="QUERY",
                direction="QUERY_TO_FRAPPE", status="Active",
            ),
        }

    def get_doc(self, doctype, name):
        return self.documents[(doctype, name)]


def selection(*, source="hero-one", mapping_revision=1, operation_revision=1, operation="contacts", execution_mode="AUTO_SYNC"):
    return RuntimePlanSelection(
        site_scope="b9-test.localhost",
        source_instance=source,
        graphql_service="HERO-GQL-00001",
        service_revision=1,
        operation_revision=operation_revision,
        graphql_mapping="HERO-GQL-MAP-00001",
        mapping_revision=mapping_revision,
        schema_snapshot="snapshot-a",
        schema_fingerprint=FINGERPRINT,
        operation_name=operation,
        execution_mode=execution_mode,
    )


class TestRuntimePlan(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryRuntimePlanStorage()
        self.references = References()
        self.audit = InMemoryRuntimePlanAudit()
        self.authority = Authority()
        self.service = RuntimePlanService(self.storage, self.references, self.audit, self.authority)

    def test_draft_approve_disable_lifecycle_records_semantic_generations(self):
        draft = self.service.create_draft(selection(), actor="manager")
        self.assertEqual((draft.state, draft.revision, draft.generation), (RuntimePlanState.DRAFT, 1, 1))
        self.assertIs(self.service.update_draft(draft.plan_key, selection(), expected_revision=1, actor="manager"), draft)

        updated = self.service.update_draft(
            draft.plan_key, selection(mapping_revision=2), expected_revision=1, actor="manager"
        )
        self.assertEqual((updated.state, updated.revision, updated.generation), (RuntimePlanState.DRAFT, 2, 2))
        approved = self.service.approve(updated.plan_key, expected_revision=2, actor="manager")
        self.assertEqual((approved.state, approved.revision, approved.generation), (RuntimePlanState.APPROVED, 3, 3))
        self.assertEqual(self.references.checked, [approved.selection])
        self.assertIs(self.service.current_approved("b9-test.localhost", "hero-one"), approved)

        disabled = self.service.disable(approved.plan_key, expected_revision=3, actor="manager")
        self.assertEqual((disabled.state, disabled.revision, disabled.generation), (RuntimePlanState.DISABLED, 4, 4))
        self.assertIsNone(self.service.current_approved("b9-test.localhost", "hero-one"))
        self.assertEqual(
            [event.action for event in self.audit.events],
            ["DRAFT_CREATED", "DRAFT_UPDATED", "APPROVED", "DISABLED"],
        )

    def test_cas_and_site_source_scope_fail_closed(self):
        first = self.service.create_draft(selection(), actor="manager")
        with self.assertRaises(RuntimePlanConflict):
            self.service.update_draft(first.plan_key, selection(mapping_revision=2), expected_revision=2, actor="manager")
        with self.assertRaises(RuntimePlanConflict):
            self.service.create_draft(selection(), actor="manager")

        second = self.service.create_draft(selection(source="hero-two"), actor="manager")
        self.assertNotEqual(first.plan_key, second.plan_key)
        second = self.service.approve(second.plan_key, expected_revision=1, actor="manager")
        self.assertIsNone(self.service.current_approved("b9-test.localhost", "hero-one"))
        self.assertIs(self.service.current_approved("b9-test.localhost", "hero-two"), second)
        self.assertIsNone(self.service.current_approved("other.localhost", "hero-two"))

    def test_selection_fingerprint_binds_each_changeable_contract_dimension(self):
        original = selection()
        changes = {
            "site_scope": "other.localhost", "source_instance": "hero-two",
            "graphql_service": "service-two", "service_revision": 2,
            "operation_revision": 2, "graphql_mapping": "mapping-two", "mapping_revision": 2,
            "schema_snapshot": "snapshot-b", "schema_fingerprint": "b" * 64, "operation_name": "projects",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = replace(original, **{field: value})
                self.assertNotEqual(changed.semantic_hash, original.semantic_hash)
                if field in {"site_scope", "source_instance"}:
                    self.assertNotEqual(changed.plan_key, original.plan_key)
                else:
                    self.assertEqual(changed.plan_key, original.plan_key)

    def test_approval_checks_authority_and_current_references_before_transition(self):
        draft = self.service.create_draft(selection(), actor="manager")
        with self.assertRaises(PermissionError):
            self.service.approve(draft.plan_key, expected_revision=1, actor="guest")
        self.assertEqual(self.references.checked, [])

        self.references.rejected = True
        with self.assertRaises(RuntimePlanError):
            self.service.approve(draft.plan_key, expected_revision=1, actor="manager")
        self.assertEqual(self.storage.read(draft.plan_key).state, RuntimePlanState.DRAFT)
        self.assertEqual([event.action for event in self.audit.events], ["DRAFT_CREATED"])

    def test_only_a_draft_may_be_edited_or_approved(self):
        draft = self.service.create_draft(selection(), actor="manager")
        approved = self.service.approve(draft.plan_key, expected_revision=1, actor="manager")
        with self.assertRaises(RuntimePlanConflict):
            self.service.update_draft(approved.plan_key, selection(mapping_revision=2), expected_revision=2, actor="manager")
        with self.assertRaises(RuntimePlanConflict):
            self.service.approve(approved.plan_key, expected_revision=2, actor="manager")

    def test_frappe_reference_adapter_rechecks_all_saved_identities(self):
        frappe = FrappeReferences()
        references = FrappeRuntimePlanReferences(frappe)
        references.validate_current(selection())
        frappe.documents[("HERO GraphQL Mapping", "HERO-GQL-MAP-00001")].mapping_revision = 2
        with self.assertRaises(RuntimePlanError):
            references.validate_current(selection())

    def test_resolution_revalidates_approved_generation_before_runtime_construction(self):
        draft = self.service.create_draft(selection(), actor="manager")
        approved = self.service.approve(draft.plan_key, expected_revision=1, actor="manager")
        resolved = self.service.resolve_executable_plan(
            "b9-test.localhost", "hero-one", approved.generation, False, "manager"
        )
        self.assertEqual((resolved.plan_key, resolved.generation, resolved.selection.graphql_service), (
            approved.plan_key, approved.generation, "HERO-GQL-00001"
        ))
        with self.assertRaises(RuntimePlanConflict):
            self.service.resolve_executable_plan("b9-test.localhost", "hero-one", 1, False, "manager")

    def test_frappe_style_email_actor_is_a_valid_approval_identity(self):
        actor = "manager@example.invalid"
        self.authority.allowed.add(actor)
        draft = self.service.create_draft(selection(), actor=actor)
        approved = self.service.approve(draft.plan_key, expected_revision=1, actor=actor)
        self.assertEqual(approved.approved_by, actor)

    def test_plan_requires_auto_sync_and_operation_revision_is_revalidated(self):
        with self.assertRaises(RuntimePlanError):
            selection(execution_mode="MONITOR")
        frappe = FrappeReferences()
        references = FrappeRuntimePlanReferences(frappe)
        with self.assertRaises(RuntimePlanError):
            references.validate_current(selection(operation_revision=2))


if __name__ == "__main__":
    unittest.main()
