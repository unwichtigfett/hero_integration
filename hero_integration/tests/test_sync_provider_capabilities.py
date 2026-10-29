from __future__ import annotations

from hashlib import sha256
import unittest

from hero_integration.graphql_protocol.graphql_operation_materializer import (
    GraphQLOperationRequest,
    GraphQLResultBinding,
)
from hero_integration.graphql_protocol.operation_kind import GraphQLOperationKind
from hero_integration.services.binding_registry import BindingRegistryService, InMemoryBindingStorage
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.read_contract import ReadContract
from hero_integration.services.sync_engine import (
    BoundField,
    ChangePage,
    InMemorySyncLedger,
    MaterializedReadPlan,
    PollingConfiguration,
    SyncEngine,
    SyncRun,
    SyncStatus,
)
from hero_integration.services.sync_provider_capabilities import (
    ProviderCapability,
    ProviderCapabilityError,
    ProviderContract,
    guarded_provider,
    offset_provider,
)


FP = "a" * 64
QUERY = "query Contacts { contacts { id name } }"


def _plan() -> MaterializedReadPlan:
    contract = ReadContract("service", 1, "mapping", 1, "Projection", "contacts", "id", ("definition",), {}, 10)
    sealed = SealedRead(contract, QUERY, FP, sha256(QUERY.encode()).hexdigest())
    request = GraphQLOperationRequest(
        "binding", FP, "node", GraphQLOperationKind.QUERY, "contacts", "Contacts", "contacts", QUERY, (),
        (GraphQLResultBinding(("id",), ("name",), ("data", "contacts", "id"), "ID!"),), "request", None, "{}",
    )
    return MaterializedReadPlan(sealed, request, "hero")


class _Index:
    def __init__(self, fields):
        self._fields = fields

    def fields(self, site_scope, source_instance):
        return self._fields


class _Processor:
    def __init__(self):
        self.rows = []

    def process(self, record, fields):
        self.rows.append((record, fields))
        return True


class TestProviderCapabilityContracts(unittest.TestCase):
    def test_contracts_describe_only_proven_discovery(self):
        self.assertEqual(
            ProviderContract(ProviderCapability.COMPLETE_DELTA_CURSOR).change_capabilities().complete_change_discovery,
            True,
        )
        self.assertEqual(
            ProviderContract(ProviderCapability.VERIFIED_TUPLE_WATERMARK).change_capabilities().cursor,
            True,
        )
        bounded = ProviderContract(ProviderCapability.BOUND_OBJECT_RECONCILIATION).change_capabilities()
        self.assertEqual((bounded.cursor, bounded.complete_change_discovery, bounded.bounded_comparison), (False, False, True))
        unsupported = ProviderContract(ProviderCapability.UNSUPPORTED).change_capabilities()
        self.assertEqual((unsupported.cursor, unsupported.complete_change_discovery, unsupported.bounded_comparison), (False, False, False))

    def test_offset_reader_is_never_adapted_as_a_change_provider(self):
        calls = []
        provider = offset_provider(lambda offset: calls.append(offset))
        self.assertEqual(provider.contract.capability, ProviderCapability.UNSUPPORTED)
        with self.assertRaises(ProviderCapabilityError):
            provider.changes(None)
        self.assertEqual(calls, [])

    def test_verified_tuple_watermark_rejects_unverified_final_page_before_processing(self):
        provider = guarded_provider(
            ProviderContract(ProviderCapability.VERIFIED_TUPLE_WATERMARK),
            lambda _cursor: ChangePage(({"id": "1"},), None, True, "watermark"),
            tuple_watermark_verified=lambda _page: False,
        )
        with self.assertRaises(ProviderCapabilityError):
            provider.changes(None)

    def test_pending_sync_is_bound_only_and_records_completed_coverage(self):
        registry = BindingRegistryService(InMemoryBindingStorage())
        parent = registry.create_or_resolve_object_binding(
            site_scope="site", source_instance="hero", object_type="Contact", external_object_id="1",
            target_app="erpnext", target_doctype="Customer", target_name="C1", mapping_definition_id="definition", mapping_revision=1,
        )
        binding = registry.create_or_resolve_field_binding(
            parent, source_field="name", target_field="customer_name", mapping_definition_id="definition", mapping_revision=1,
            direction=MappingDirection.HERO_TO_FRAPPE,
        )
        provider = guarded_provider(
            ProviderContract(ProviderCapability.COMPLETE_DELTA_CURSOR),
            lambda _cursor: ChangePage(({"id": "1", "name": "Ada"}, {"id": "unbound", "name": "Ignored"}), None, True, "watermark"),
        )
        ledger = InMemorySyncLedger()
        processor = _Processor()
        engine = SyncEngine(ledger, _plan(), provider, _Index((BoundField("1", binding, ("name",)),)), processor)
        state = engine.configure(PollingConfiguration("site", False, 60, "hero", 1, FP))

        done = engine.pending_sync(SyncRun("manual", "site", "manager", state.generation, False))

        self.assertEqual(done.status, SyncStatus.SUCCEEDED)
        self.assertEqual(done.coverage, (binding.identity_key,))
        self.assertEqual([row[0]["id"] for row in processor.rows], ["1"])

    def test_pending_sync_reports_reconciliation_or_incomplete_without_reading_unproven_provider(self):
        for capability, expected in (
            (ProviderCapability.BOUND_OBJECT_RECONCILIATION, SyncStatus.RECONCILIATION_REQUIRED),
            (ProviderCapability.UNSUPPORTED, SyncStatus.INCOMPLETE),
        ):
            with self.subTest(capability=capability):
                calls = []
                provider = guarded_provider(
                    ProviderContract(capability),
                    lambda cursor: calls.append(cursor) or ChangePage((), None, True, "watermark"),
                )
                ledger = InMemorySyncLedger()
                engine = SyncEngine(ledger, _plan(), provider, _Index(()), _Processor())
                state = engine.configure(PollingConfiguration("site", False, 60, "hero", 1, FP))

                done = engine.pending_sync(SyncRun("manual", "site", "manager", state.generation, False))

                self.assertEqual((done.status, done.coverage), (expected, ()))
                self.assertEqual(calls, [])
