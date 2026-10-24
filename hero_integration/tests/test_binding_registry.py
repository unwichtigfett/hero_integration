from __future__ import annotations

import threading
import unittest

from hero_integration.services.binding_registry import (
    BindingConflict, BindingRegistryError, BindingRegistryService, BindingState,
    InMemoryBindingStorage, state_fingerprint,
)
from hero_integration.services.mapping_registry_model import MappingDirection


class TestBindingRegistry(unittest.TestCase):
    def setUp(self):
        self.service = BindingRegistryService(InMemoryBindingStorage())

    def obj(self, **changes):
        values = dict(site_scope="one.test", source_instance="hero-a", object_type="Contact", external_object_id="42", target_app="erpnext", target_doctype="Customer", target_name="CUST-1", mapping_definition_id="contact-name", mapping_revision=1)
        values.update(changes)
        return self.service.create_or_resolve_object_binding(**values)

    def field(self, parent, **changes):
        values = dict(source_field="name", target_field="customer_name", mapping_definition_id="contact-name", mapping_revision=1, direction=MappingDirection.HERO_TO_FRAPPE)
        values.update(changes)
        return self.service.create_or_resolve_field_binding(parent, **values)

    def test_identity_separates_source_type_target_and_mapping_revision(self):
        first = self.obj()
        self.assertNotEqual(first.identity_key, self.obj(source_instance="hero-b").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(object_type="Company").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(target_name="CUST-2").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(mapping_revision=2).identity_key)

    def test_object_and_field_creation_are_idempotent_and_site_scoped(self):
        parent = self.obj()
        self.assertIs(parent, self.obj())
        field = self.field(parent)
        self.assertIs(field, self.field(parent))
        self.assertEqual(self.service.get_field_bindings("one.test", parent.identity_key), (field,))
        self.assertIsNone(self.service.get_object_binding("two.test", parent.identity_key))

    def test_field_requires_persisted_matching_parent_and_multiple_fields_are_allowed(self):
        parent = self.obj()
        first = self.field(parent)
        second = self.field(parent, source_field="email", target_field="email_id", mapping_definition_id="contact-email")
        self.assertEqual(self.service.get_field_bindings(parent.site_scope, parent.identity_key), tuple(sorted((first, second), key=lambda item: item.identity_key)))
        with self.assertRaises(BindingRegistryError): self.field(type(parent).create(site_scope="one.test", source_instance="hero-a", object_type="Contact", external_object_id="missing", target_app="erpnext", target_doctype="Customer", target_name="CUST-X", mapping_definition_id="x", mapping_revision=1))

    def test_acknowledgment_is_guarded_idempotent_and_stale_safe(self):
        field = self.field(self.obj())
        source, target = state_fingerprint(None, present=True), state_fingerprint(False, present=True)
        ack = self.service.record_verified_acknowledgment(field, expected_revision=1, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=source, target_fingerprint=target, evidence_id="migration-1", version=1)
        self.assertEqual(ack.state, BindingState.SYNCHRONIZED)
        self.assertIs(ack, self.service.record_verified_acknowledgment(ack, expected_revision=2, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=source, target_fingerprint=target, evidence_id="migration-1", version=1))
        with self.assertRaises(BindingConflict): self.service.record_verified_acknowledgment(ack, expected_revision=1, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=source, target_fingerprint=target, evidence_id="migration-2", version=0)

    def test_change_state_distinguishes_missing_null_empty_false_zero_and_mapping_change(self):
        field = self.field(self.obj())
        self.assertNotEqual(state_fingerprint(None, present=False), state_fingerprint(None, present=True))
        self.assertEqual(len({state_fingerprint("", present=True), state_fingerprint(False, present=True), state_fingerprint(0, present=True)}), 3)
        self.assertEqual(self.service.change_state(field, active_mapping_revision=1, mapping_available=True, source_fingerprint=None, target_fingerprint=None), BindingState.UNACKNOWLEDGED)
        self.assertEqual(self.service.change_state(field, active_mapping_revision=2, mapping_available=True, source_fingerprint=None, target_fingerprint=None), BindingState.MAPPING_CHANGED)
        self.assertEqual(self.service.change_state(field, active_mapping_revision=1, mapping_available=False, source_fingerprint=None, target_fingerprint=None), BindingState.RECONCILIATION_REQUIRED)

    def test_concurrent_same_request_creates_one_logical_binding(self):
        values = []
        threads = [threading.Thread(target=lambda: values.append(self.obj())) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual({value.identity_key for value in values}, {values[0].identity_key})

if __name__ == "__main__": unittest.main()
