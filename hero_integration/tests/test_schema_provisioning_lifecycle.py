from __future__ import annotations

from dataclasses import replace
import unittest

from hero_integration.services.controlled_schema_provisioning import (
    ModuleOwner,
    ProvisioningPlanner,
    ProvisioningRequest,
)
from hero_integration.services.frappe_field_registry import FrappeFieldReference, FrappeFieldRegistry
from hero_integration.services.mapping_registry_model import (
    GraphQLReference, GraphQLOperationKind, MappingDefinition, MappingDirection,
    MissingSemantics, MissingValuePolicy, TransformPolicy,
)
from hero_integration.services.schema_provisioning_lifecycle import (
    InMemoryProvisioningApprovalAuthority, InMemoryProvisioningAudit,
    ProvisioningApproval, ProvisioningExecutor,
    ProvisioningLifecycleError,
)
from hero_integration.services.schema_registry import SchemaTypeReference


class FakeAdapter:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.writes = []
        self.sites = []

    def metadata_fingerprint(self, site):
        self.sites.append(site)
        return self.fingerprint

    def apply(self, site, operation):
        self.sites.append(site)
        self.writes.append(operation.operation_id)

    def verify(self, site, operation):
        self.sites.append(site)
        return True


def plan():
    mapping = MappingDefinition(
        "mapping-1", "source", MappingDirection.HERO_TO_FRAPPE,
        GraphQLReference(GraphQLOperationKind.QUERY, "lookup", ("value",)),
        SchemaTypeReference("String", "String", "SCALAR", ()),
        FrappeFieldReference("app", "New Target", "custom_value"), "Data",
        TransformPolicy.IDENTITY,
        MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.EXPLICIT_NULL),
        "snapshot", "a" * 64, 4,
    )
    fields = FrappeFieldRegistry("site.test", ("app",), (), ())
    return ProvisioningPlanner().prepare("site.test", (ProvisioningRequest(mapping, "Module", "meta-1"),), fields, (ModuleOwner("Module", "app"),))


def approval(value):
    return ProvisioningApproval(
        "approval-1", value.plan_id, value.fingerprint, value.site_scope,
        value.mapping_revision, value.metadata_fingerprint, "admin",
        ("System Manager",), tuple(item.operation_id for item in value.operations), "corr-1", 100.0,
    )


class TestProvisioningLifecycle(unittest.TestCase):
    def test_authorization_and_audit_precede_mocked_schema_write(self):
        value = plan()
        adapter, audit = FakeAdapter("meta-1"), InMemoryProvisioningAudit()
        result = ProvisioningExecutor(audit, adapter, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1).apply_authorized(value, approval(value))
        self.assertEqual(result.state.value, "APPLIED")
        self.assertEqual(audit.events[0][1], "RESERVED")
        self.assertEqual(len(adapter.writes), 1)
        self.assertEqual(set(adapter.sites), {"site.test"})

    def test_rejects_stale_forged_expired_actor_or_replayed_approval_before_writes(self):
        value = plan()
        adapter = FakeAdapter("meta-1")
        executor = ProvisioningExecutor(InMemoryProvisioningAudit(), adapter, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1)
        for forged in (
            replace(approval(value), mapping_revision=5),
            replace(approval(value), expires_at=1.0),
            replace(approval(value), actor="other"),
        ):
            with self.assertRaises(Exception):
                executor.apply_authorized(value, forged)
        self.assertEqual(adapter.writes, [])
        executor.apply_authorized(value, approval(value))
        with self.assertRaises(ProvisioningLifecycleError):
            executor.apply_authorized(value, approval(value))

    def test_stale_metadata_or_audit_reservation_failure_prevents_writes(self):
        value = plan()
        stale = FakeAdapter("changed")
        with self.assertRaises(ProvisioningLifecycleError):
            ProvisioningExecutor(InMemoryProvisioningAudit(), stale, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1).apply_authorized(value, approval(value))
        self.assertEqual(stale.writes, [])
        audit = InMemoryProvisioningAudit()
        audit.fail = "reserve"
        adapter = FakeAdapter("meta-1")
        with self.assertRaises(ProvisioningLifecycleError):
            ProvisioningExecutor(audit, adapter, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1).apply_authorized(value, approval(value))
        self.assertEqual(adapter.writes, [])

    def test_record_failure_preserves_reconciliation_requirement_and_prevents_replay(self):
        value = plan()
        audit, adapter = InMemoryProvisioningAudit(), FakeAdapter("meta-1")
        audit.fail = "record"
        executor = ProvisioningExecutor(audit, adapter, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1)
        with self.assertRaises(ProvisioningLifecycleError):
            executor.apply_authorized(value, approval(value))
        self.assertEqual(len(adapter.writes), 1)
        self.assertIn("PARTIAL", [event[1] for event in audit.events])
        with self.assertRaises(ProvisioningLifecycleError):
            executor.apply_authorized(value, approval(value))
        self.assertTrue(executor.reconciliation_status("audit-1").reconciliation_required)


    def test_waiting_application_never_reaches_audit_or_adapter(self):
        mapping = MappingDefinition(
            "waiting-app", "source", MappingDirection.HERO_TO_FRAPPE,
            GraphQLReference(GraphQLOperationKind.QUERY, "lookup", ("value",)),
            SchemaTypeReference("String", "String", "SCALAR", ()),
            FrappeFieldReference("missing", "Target", "value"), "Data",
            TransformPolicy.IDENTITY,
            MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.EXPLICIT_NULL),
            "snapshot", "a" * 64, 4,
        )
        value = ProvisioningPlanner().prepare(
            "site.test", (ProvisioningRequest(mapping, "Module", "meta-1"),),
            FrappeFieldRegistry("site.test", (), (), ()), (),
        )
        adapter, audit = FakeAdapter("meta-1"), InMemoryProvisioningAudit()
        with self.assertRaises(ProvisioningLifecycleError):
            ProvisioningExecutor(audit, adapter, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1).apply_authorized(value, approval(value))
        self.assertEqual(adapter.writes, [])
        self.assertEqual(audit.events, [])

    def test_audit_finalization_failure_never_replays_completed_operations(self):
        value = plan()
        audit, adapter = InMemoryProvisioningAudit(), FakeAdapter("meta-1")
        audit.fail = "finalize"
        executor = ProvisioningExecutor(audit, adapter, InMemoryProvisioningApprovalAuthority(), clock=lambda: 1)
        with self.assertRaises(ProvisioningLifecycleError):
            executor.apply_authorized(value, approval(value))
        with self.assertRaises(ProvisioningLifecycleError):
            executor.apply_authorized(value, approval(value))
        self.assertEqual(len(adapter.writes), 1)


if __name__ == "__main__":
    unittest.main()
