from __future__ import annotations

import unittest
from dataclasses import replace

from hero_integration.services.runtime_plan import (
    FrappeRuntimePlanAudit,
    FrappeRuntimePlanReferences,
    InMemoryRuntimePlanAudit,
    InMemoryRuntimePlanStorage,
    RuntimePlanAuditEvent,
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


class TestRuntimePlanAuditBoundary(unittest.TestCase):
    def test_duplicate_with_changed_semantic_evidence_is_rejected(self):
        class DuplicateEntryError(Exception):
            pass

        event = RuntimePlanAuditEvent('a' * 64, 'site', 'source', 'APPROVED', 'manager', 2, 2, 'b' * 64)
        existing = SimpleNamespace(runtime_plan=event.plan_key, action=event.action,
                                   plan_revision=event.revision, generation=event.generation,
                                   semantic_hash=event.semantic_hash)

        def get_doc(*args):
            if len(args) == 1:
                def insert():
                    raise DuplicateEntryError()
                return SimpleNamespace(insert=insert)
            return existing

        fake = SimpleNamespace(local=SimpleNamespace(site='site'), get_doc=get_doc)
        audit = FrappeRuntimePlanAudit(fake)
        audit.record(event)
        existing.semantic_hash = 'c' * 64
        with self.assertRaises(RuntimePlanConflict):
            audit.record(event)


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

    def test_reader_change_requires_fresh_approval_and_invalidates_queued_generation(self):
        draft = self.service.create_draft(selection(), actor="manager")
        old = self.service.approve(draft.plan_key, expected_revision=1, actor="manager")
        change = replace(old.selection, graphql_service="reader-two", operation_name="tasks")
        self.assertEqual(self.service.update_draft(old.plan_key, old.selection,
                         expected_revision=2, actor="manager"), old)
        with self.assertRaises(RuntimePlanConflict):
            self.service.update_draft(old.plan_key, change, expected_revision=1, actor="manager")
        changed = self.service.update_draft(old.plan_key, change, expected_revision=2, actor="manager")
        self.assertEqual((changed.state, changed.approved_by, changed.plan_key, changed.revision,
                          changed.generation), (RuntimePlanState.DRAFT, "", old.plan_key, 3, 3))
        with self.assertRaises(RuntimePlanConflict):
            self.service.resolve_executable_plan(change.site_scope, change.source_instance, 3, True, "manager")
        approved = self.service.approve(old.plan_key, expected_revision=3, actor="manager")
        with self.assertRaises(RuntimePlanConflict):
            self.service.resolve_executable_plan(change.site_scope, change.source_instance, 2, True, "manager")
        self.assertEqual(approved.selection, change)
        self.assertEqual((approved.revision, approved.generation), (4, 4))
        self.assertEqual([e.action for e in self.audit.events], ["DRAFT_CREATED", "APPROVED", "DRAFT_UPDATED", "APPROVED"])

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

    def test_scope_delimiter_and_boolean_revisions_are_rejected(self):
        original = selection()
        for field in ("site_scope", "source_instance"):
            with self.subTest(field=field), self.assertRaises(RuntimePlanError):
                replace(original, **{field: "part|other"})
        for field in ("service_revision", "operation_revision", "mapping_revision"):
            with self.subTest(field=field), self.assertRaises(RuntimePlanError):
                replace(original, **{field: True})

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

    def test_revision_change_requires_new_approval(self):
        draft = self.service.create_draft(selection(), actor="manager")
        approved = self.service.approve(draft.plan_key, expected_revision=1, actor="manager")
        changed = self.service.update_draft(approved.plan_key, selection(mapping_revision=2), expected_revision=2, actor="manager")
        self.assertEqual(changed.state, RuntimePlanState.DRAFT)
        self.assertEqual(changed.generation, approved.generation + 1)
        with self.assertRaises(RuntimePlanConflict):
            self.service.approve(approved.plan_key, expected_revision=2, actor="manager")

    def test_multi_reader_approval_and_scope(self):
        from hero_integration.services.runtime_plan import FrappeRuntimePlanStorage as Storage
        first = selection()
        other = replace(first, graphql_service="reader-two", graphql_mapping="map-two")
        plan = self.service.create_draft(first, additional_readers=[other], actor="manager")
        plan = self.service.approve(plan.plan_key, expected_revision=1, actor="manager")
        self.assertEqual(self.references.checked, [first, other])
        self.assertEqual(Storage._decode(SimpleNamespace(**Storage._payload(plan))), plan)
        def resolve(reader):
            return self.service.resolve_executable_plan(first.site_scope, first.source_instance, 2, True, "manager", reader=reader)
        self.assertEqual(resolve(other.graphql_service).selection, other)
        with self.assertRaises(RuntimePlanConflict):
            resolve("unapproved")
        for wrong in [first, replace(other, source_instance="other")]:
            with self.assertRaises(RuntimePlanError):
                self.service.update_draft(plan.plan_key, first, additional_readers=[wrong], expected_revision=2, actor="manager")
        self.assertEqual(self.storage.read(plan.plan_key), plan)

    def test_reader_failure_does_not_spend_another_readers_quota(self):
        from unittest.mock import Mock, patch
        from hero_integration.services import sync_runtime_adapter as adapter
        readers = tuple(replace(selection(), graphql_service=f"reader-{i}") for i in range(4))
        plan = self.service.create_draft(readers[0], additional_readers=readers[1:], actor="manager")
        plan = self.service.approve(plan.plan_key, expected_revision=1, actor="manager")
        fake = Mock()
        fake.db.get_value.return_value = None
        fake.db.get_single_value.return_value = 5
        settings = SimpleNamespace(testing_enabled=0, allow_graphql_mutation_execution=0, source_binding_revision=5)
        def gate(_, cfg, reader):
            if reader == readers[2]:
                raise adapter.ReaderDisabled("Disabled area.")
        def run(executable, frappe, **kw):
            if executable.selection == readers[0]:
                raise ValueError("Simulated read failure")
            return adapter.RuntimePreflight("PARTIAL", plan.plan_key, plan.generation, "source", "completed", True)
        with patch.object(adapter, "_require_area_enabled", side_effect=gate), patch.object(
                adapter, "_require_latest_validated_snapshot"), patch.object(adapter, "_run", side_effect=run) as calls, patch(
                "hero_integration.services.polling.runtime_plan", return_value={"page_size": 4}):
            result = adapter._run_scheduled_readers(fake, settings, self.service, plan, "manager")
        self.assertEqual(result.status, "FAILED")
        self.assertEqual([c.args[0].selection for c in calls.call_args_list], [readers[0], readers[1], readers[3]])
        self.assertEqual([c.kwargs["allocation"] for c in calls.call_args_list], [2, 1, 1])
        self.assertEqual(len({c.kwargs["cycle"] for c in calls.call_args_list}), 1)
        fake.db.rollback.assert_called_once()

    def test_disabled_primary_does_not_mask_completed_inbound_readers(self):
        from unittest.mock import Mock, patch
        from hero_integration.services import sync_runtime_adapter as adapter

        primary = selection()
        other = replace(primary, graphql_service="reader-two", graphql_mapping="map-two")
        plan = self.service.create_draft(primary, additional_readers=[other], actor="manager")
        plan = self.service.approve(plan.plan_key, expected_revision=1, actor="manager")
        fake = Mock()
        fake.db.get_value.return_value = None
        fake.db.get_single_value.return_value = 5
        settings = SimpleNamespace(testing_enabled=0, allow_graphql_mutation_execution=1, source_binding_revision=5)

        def gate(_site, _settings, reader):
            if reader == primary:
                raise adapter.ReaderDisabled("Disabled area.")

        with patch.object(adapter, "_require_area_enabled", side_effect=gate), \
                patch.object(adapter, "_require_latest_validated_snapshot"), \
                patch.object(adapter, "_run", return_value=SimpleNamespace(status="SUCCEEDED", run_id="completed")) as run, \
                patch("hero_integration.services.polling.runtime_plan", return_value={"page_size": 4}), \
                patch("hero_integration.services.automatic_writeback.enqueue") as enqueue:
            result = adapter._run_scheduled_readers(fake, settings, self.service, plan, "manager")
        self.assertEqual((result.status, result.run_id), ("SUCCEEDED", "completed"))
        self.assertEqual(run.call_args.args[0].selection, other)
        enqueue.assert_not_called()

    def test_cycle_progress_requires_every_allocated_reader(self):
        from unittest.mock import Mock, patch
        from hero_integration.services import sync_runtime_adapter as adapter
        first = selection()
        other = replace(first, graphql_service="reader-two", graphql_mapping="map-two")
        plan = self.service.create_draft(first, additional_readers=[other], actor="manager")
        plan = self.service.approve(plan.plan_key, expected_revision=1, actor="manager")
        fake = Mock()
        fake.db.get_value.return_value = None
        fake.db.get_single_value.return_value = 5
        settings = SimpleNamespace(testing_enabled=0, allow_graphql_mutation_execution=0, source_binding_revision=5)
        progress = adapter.RuntimePreflight("PARTIAL", plan.plan_key, plan.generation, "source", "first", True)
        for status, proven, expected in [("PARTIAL", True, True), ("BOUNDED_COMPLETE", False, True),
                                         ("PARTIAL", False, False), ("INCOMPLETE", False, False)]:
            second = adapter.RuntimePreflight(status, plan.plan_key, plan.generation, "source", "second", proven)
            with patch.object(adapter, "_require_area_enabled"), patch.object(adapter, "_require_latest_validated_snapshot"), \
                    patch.object(adapter, "_run", side_effect=[progress, second]), \
                    patch("hero_integration.services.polling.runtime_plan", return_value={"page_size": 4}):
                result = adapter._run_scheduled_readers(fake, settings, self.service, plan, "manager")
            self.assertEqual(result.inventory_progress, expected)

    def test_shared_budget_and_small_budget_rotation(self):
        from hero_integration.services.sync_runtime_adapter import reader_allocations
        readers = [replace(selection(), graphql_service="reader-" + str(i)) for i in range(8)]
        for limit in (1, 3, 8, 100):
            last = None
            totals = dict.fromkeys(readers, 0)
            for _ in readers:
                allocations = reader_allocations(readers, limit, last)
                self.assertEqual(sum(n for _, n in allocations), limit)
                for reader, n in allocations:
                    totals[reader] += n
                last = allocations[-1 if limit < len(readers) else max(0, limit % len(readers) - 1)][0].graphql_service
            self.assertEqual(set(totals.values()), {limit})

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
