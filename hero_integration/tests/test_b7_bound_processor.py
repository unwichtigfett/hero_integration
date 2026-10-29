from __future__ import annotations

import unittest

from hero_integration.services.binding_registry import BindingRegistryService, InMemoryBindingStorage
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.sync_engine import BoundField, FrappeBoundProcessor, InMemorySyncLedger, PollingConfiguration, SyncEngine, SyncState, SyncStatus


class _Writer:
    def __init__(self, result=True): self.result, self.calls = result, []
    def update_existing_bound(self, site, app, doctype, name, values):
        self.calls.append((site, app, doctype, name, values)); return self.result


class TestB7BoundProcessor(unittest.TestCase):
    def setUp(self):
        bindings = BindingRegistryService(InMemoryBindingStorage())
        parent = bindings.create_or_resolve_object_binding(site_scope="site", source_instance="hero", object_type="Contact", external_object_id="hero-1", target_app="erpnext", target_doctype="Customer", target_name="CUST-1", mapping_definition_id="name", mapping_revision=4)
        binding = bindings.create_or_resolve_field_binding(parent, source_field="profile.name", target_field="customer_name", mapping_definition_id="name", mapping_revision=4, direction=MappingDirection.HERO_TO_FRAPPE)
        self.field = BoundField("hero-1", binding, ("profile", "name"))

    def test_bound_write_requires_atomic_local_baseline_contract(self):
        writer = _Writer()
        processor = FrappeBoundProcessor(4, ("name",), document_adapter=writer)
        self.assertFalse(processor.process({"id": "hero-1", "profile": {"name": "Ada"}}, (self.field,)))
        self.assertEqual(writer.calls, [])

    def test_unacknowledged_field_never_overwrites_local_target(self):
        writer = _Writer()
        processor = FrappeBoundProcessor(4, ('name',), document_adapter=writer)
        self.assertFalse(processor.process({'id':'hero-1', 'profile':{'name':'Remote'}}, (self.field,)))
        self.assertEqual(writer.calls, [])

    def test_synchronized_binding_still_requires_atomic_target_comparison(self):
        from dataclasses import replace
        from hero_integration.services.binding_registry import BindingState
        acknowledged = replace(self.field.binding, state=BindingState.SYNCHRONIZED,
                               acknowledged_source_fingerprint='a'*64, acknowledged_target_fingerprint='b'*64)
        field = BoundField('hero-1', acknowledged, ('profile', 'name'))
        writer = _Writer()
        self.assertFalse(FrappeBoundProcessor(4, ('name',), document_adapter=writer).process(
            {'id':'hero-1', 'profile':{'name':'Remote'}}, (field,)))
        self.assertEqual(writer.calls, [])

    def test_stale_unbound_or_failed_update_never_onboards(self):
        writer = _Writer(False)
        self.assertFalse(FrappeBoundProcessor(5, ("name",), document_adapter=writer).process({"id": "hero-1", "profile": {"name": "Ada"}}, (self.field,)))
        self.assertFalse(FrappeBoundProcessor(4, ("name",), document_adapter=writer).process({"id": "other", "profile": {"name": "Ada"}}, (self.field,)))
        self.assertFalse(FrappeBoundProcessor(4, ("name",), document_adapter=writer).process({"id": "hero-1", "profile": {"name": "Ada"}}, (self.field,)))
        self.assertEqual(len(writer.calls), 0)


class TestIdempotentSyncConfiguration(unittest.TestCase):
    def test_identical_configuration_preserves_cursor_coverage_and_generation(self):
        ledger = InMemorySyncLedger()
        engine = SyncEngine(ledger, object(), object(), object(), object())
        configuration = PollingConfiguration("site", False, 60, "hero", 4, "a" * 64)
        first = engine.configure(configuration)
        done = ledger.save_state(SyncState(configuration, "cursor", ("bound",), SyncStatus.SUCCEEDED, first.generation + 1), first.generation)
        self.assertEqual(engine.configure(configuration), done)
