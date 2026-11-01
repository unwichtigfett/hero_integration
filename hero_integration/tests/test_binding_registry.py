from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from hero_integration.services.binding_registry import (
    BindingConflict, BindingRegistryError, BindingRegistryService, BindingState,
    FrappeBindingStorage, InMemoryBindingStorage, state_fingerprint,
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
        self.assertNotEqual(first.identity_key, self.obj(site_scope="two.test").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(source_instance="hero-b").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(object_type="Company").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(external_object_id="43").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(target_app="other-app").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(target_name="CUST-2").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(mapping_definition_id="other-rule").identity_key)
        self.assertNotEqual(first.identity_key, self.obj(mapping_revision=2).identity_key)

    def test_user_binding_accepts_canonical_email_identifier(self):
        user = self.obj(object_type='User', target_app='frappe', target_doctype='User', target_name='person@example.invalid')
        self.assertEqual(user.target_name, 'person@example.invalid')
        self.assertEqual(self.field(user).target_name, user.target_name)
        with self.assertRaises(BindingRegistryError):
            self.obj(object_type='User', target_app='frappe', target_doctype='User', target_name='not a user')

    def test_object_and_field_creation_are_idempotent_and_site_scoped(self):
        parent = self.obj()
        self.assertIs(parent, self.obj())
        field = self.field(parent)
        self.assertIs(field, self.field(parent))
        self.assertEqual(self.service.get_field_bindings("one.test", parent.identity_key), (field,))
        self.assertIsNone(self.service.get_object_binding("two.test", parent.identity_key))

    def test_frappe_storage_rejects_cross_site_reads_and_unprivileged_writes_before_access(self):
        frappe = SimpleNamespace(
            local=SimpleNamespace(site="one.test"), session=SimpleNamespace(user="employee@example.invalid"),
            get_roles=lambda: ("Employee",), get_all=Mock(), get_doc=Mock(), has_permission=Mock(),
        )
        storage = FrappeBindingStorage(frappe)
        with self.assertRaises(BindingRegistryError):
            storage.object("two.test", "some-key")
        with self.assertRaises(PermissionError):
            storage.put_object(self.obj())
        frappe.get_all.assert_not_called()
        frappe.get_doc.assert_not_called()
        frappe.has_permission.assert_not_called()

    def test_field_requires_persisted_matching_parent_and_multiple_fields_are_allowed(self):
        parent = self.obj()
        first = self.field(parent)
        second = self.field(parent, source_field="email", target_field="email_id", mapping_definition_id="contact-email")
        self.assertEqual(self.service.get_field_bindings(parent.site_scope, parent.identity_key), tuple(sorted((first, second), key=lambda item: item.identity_key)))
        with self.assertRaises(BindingRegistryError): self.field(type(parent).create(site_scope="one.test", source_instance="hero-a", object_type="Contact", external_object_id="missing", target_app="erpnext", target_doctype="Customer", target_name="CUST-X", mapping_definition_id="x", mapping_revision=1))

    def test_source_field_enumeration_is_site_and_source_scoped(self):
        first_parent = self.obj()
        first = self.field(first_parent)
        outbound = self.field(first_parent, source_field="email", target_field="email_id", mapping_definition_id="contact-email", direction=MappingDirection.FRAPPE_TO_HERO)
        other_parent = self.obj(source_instance="hero-b", external_object_id="43", target_name="CUST-2")
        other = self.field(other_parent)
        self.assertEqual(
            self.service.get_field_bindings_for_source("one.test", "hero-a"),
            tuple(sorted((first, outbound), key=lambda item: item.identity_key)),
        )
        self.assertEqual(self.service.get_inbound_field_bindings_for_source("one.test", "hero-a"), (first,))
        self.assertEqual(self.service.get_field_bindings_for_source("one.test", "hero-b"), (other,))
        self.assertEqual(self.service.get_field_bindings_for_source("two.test", "hero-a"), ())

    def test_acknowledgment_is_guarded_idempotent_and_stale_safe(self):
        field = self.field(self.obj())
        source, target = state_fingerprint(None, present=True), state_fingerprint(False, present=True)
        ack = self.service.record_verified_acknowledgment(field, expected_revision=1, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=source, target_fingerprint=target, evidence_id="migration-1", version=1)
        self.assertEqual(ack.state, BindingState.SYNCHRONIZED)
        self.assertIs(ack, self.service.record_verified_acknowledgment(ack, expected_revision=2, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=source, target_fingerprint=target, evidence_id="migration-1", version=1))
        with self.assertRaises(BindingConflict): self.service.record_verified_acknowledgment(ack, expected_revision=1, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=source, target_fingerprint=target, evidence_id="migration-2", version=0)

    def test_same_acknowledgment_version_rejects_different_fingerprints(self):
        field = self.field(self.obj())
        source = state_fingerprint("source", present=True)
        target = state_fingerprint("target", present=True)
        ack = self.service.record_verified_acknowledgment(
            field, expected_revision=1, mapping_definition_id="contact-name", mapping_revision=1,
            source_fingerprint=source, target_fingerprint=target, evidence_id="migration-1", version=1,
        )
        with self.assertRaisesRegex(BindingConflict, "conflicts with recorded evidence"):
            self.service.record_verified_acknowledgment(
                ack, expected_revision=2, mapping_definition_id="contact-name", mapping_revision=1,
                source_fingerprint=source, target_fingerprint=state_fingerprint("other", present=True),
                evidence_id="migration-2", version=1,
            )
        self.assertEqual(self.service.get_field_bindings("one.test", ack.object_binding_key), (ack,))

    def test_change_state_distinguishes_missing_null_empty_false_zero_and_mapping_change(self):
        field = self.field(self.obj())
        self.assertNotEqual(state_fingerprint(None, present=False), state_fingerprint(None, present=True))
        self.assertEqual(len({state_fingerprint("", present=True), state_fingerprint(False, present=True), state_fingerprint(0, present=True)}), 3)
        self.assertEqual(self.service.change_state(field, active_mapping_revision=1, mapping_available=True, source_fingerprint=None, target_fingerprint=None), BindingState.UNACKNOWLEDGED)
        self.assertEqual(self.service.change_state(field, active_mapping_revision=2, mapping_available=True, source_fingerprint=None, target_fingerprint=None), BindingState.MAPPING_CHANGED)
        self.assertEqual(self.service.change_state(field, active_mapping_revision=1, mapping_available=False, source_fingerprint=None, target_fingerprint=None), BindingState.RECONCILIATION_REQUIRED)

    def test_inbound_reconciliation_preserves_local_baseline_and_omissions(self):
        field = self.field(self.obj())
        base = state_fingerprint("base", present=True)
        field = self.service.record_verified_acknowledgment(field, expected_revision=1, mapping_definition_id="contact-name", mapping_revision=1, source_fingerprint=base, target_fingerprint=base, evidence_id="initial", version=1)
        local = state_fingerprint("local", present=True)
        remote = state_fingerprint("remote", present=True)
        compare = self.service.inbound_reconciliation
        self.assertEqual(compare(field, active_mapping_revision=1, source_fingerprint=base, target_fingerprint=local), "KEEP_LOCAL")
        self.assertEqual(compare(field, active_mapping_revision=1, source_fingerprint=remote, target_fingerprint=local), "CONFLICT")
        self.assertEqual(compare(field, active_mapping_revision=1, source_fingerprint=None, target_fingerprint=local), "OMITTED")
        self.assertEqual(compare(field, active_mapping_revision=1, source_fingerprint=remote, target_fingerprint=base), "APPLY_SOURCE")
        self.assertEqual(compare(field, active_mapping_revision=1, source_fingerprint=local, target_fingerprint=local), "CONFIRM_EQUAL")
        self.assertEqual(compare(field, active_mapping_revision=2, source_fingerprint=base, target_fingerprint=base), "MAPPING_CHANGED")
        self.assertEqual(field.acknowledged_target_fingerprint, base)

    def test_inbound_reconciliation_requires_confirmed_base_and_direction(self):
        parent = self.obj()
        field = self.field(parent)
        value = state_fingerprint("same", present=True)
        self.assertEqual(self.service.inbound_reconciliation(field, active_mapping_revision=1, source_fingerprint=value, target_fingerprint=value), "UNACKNOWLEDGED")
        outbound = self.field(parent, source_field="other", target_field="other", mapping_definition_id="outbound", direction=MappingDirection.FRAPPE_TO_HERO)
        with self.assertRaises(BindingRegistryError):
            self.service.inbound_reconciliation(outbound, active_mapping_revision=1, source_fingerprint=value, target_fingerprint=value)

    def test_concurrent_same_request_creates_one_logical_binding(self):
        values = []
        threads = [threading.Thread(target=lambda: values.append(self.obj())) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual({value.identity_key for value in values}, {values[0].identity_key})

    def test_duplicate_reread_accepts_only_exactly_compatible_record(self):
        candidate = self.obj()
        class DuplicateEntryError(Exception): pass
        self.assertIs(candidate, FrappeBindingStorage._after_duplicate(DuplicateEntryError(), lambda: candidate, candidate))
        with self.assertRaises(BindingConflict): FrappeBindingStorage._after_duplicate(DuplicateEntryError(), lambda: None, candidate)
        with self.assertRaises(BindingConflict): FrappeBindingStorage._after_duplicate(DuplicateEntryError(), lambda: self.obj(mapping_revision=2), candidate)

    def test_non_duplicate_insert_failure_is_not_retried(self):
        candidate = self.obj()
        with self.assertRaises(RuntimeError): FrappeBindingStorage._after_duplicate(RuntimeError("db unavailable"), lambda: candidate, candidate)

if __name__ == "__main__": unittest.main()
