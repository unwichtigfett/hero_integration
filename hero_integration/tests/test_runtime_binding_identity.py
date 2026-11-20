import unittest

from hero_integration.services.binding_registry import (
    BindingConflict, BindingRegistryService, InMemoryBindingStorage, state_fingerprint,
)
from hero_integration.services.mapping_registry_model import MappingDirection


class TestRuntimeBindingIdentity(unittest.TestCase):
    def setUp(self):
        self.registry = BindingRegistryService(InMemoryBindingStorage())
        self.identity = dict(site_scope="site.test", source_instance="hero-a", object_type="Contact",
                             external_object_id="42", target_app="erpnext", target_doctype="Customer",
                             target_name="CUST-42", mapping_definition_id="reviewed", mapping_revision=1)
        self.parent = self.registry.create_or_resolve_object_binding(**self.identity)

    def test_contradictory_target_claim_rejected_even_across_mapping_revisions(self):
        with self.assertRaises(BindingConflict):
            self.registry.create_or_resolve_object_binding(**dict(self.identity, target_name="CUST-99", mapping_revision=2))

    def test_other_external_id_cannot_adopt_bound_customer(self):
        with self.assertRaises(BindingConflict):
            self.registry.create_or_resolve_object_binding(**dict(self.identity, external_object_id="99"))

    def test_equal_ack_replay_cannot_return_superseded_baseline(self):
        binding = self.registry.create_or_resolve_field_binding(self.parent, source_field="category",
            target_field="customer_details", mapping_definition_id="rule", mapping_revision=1,
            direction=MappingDirection.HERO_TO_FRAPPE)
        def ack(binding, value, version):
            fingerprint = state_fingerprint(value, present=True)
            return self.registry.record_verified_acknowledgment(binding, expected_revision=binding.revision,
                mapping_definition_id="rule", mapping_revision=1, source_fingerprint=fingerprint,
                target_fingerprint=fingerprint, evidence_id="readback", version=version)
        first = ack(binding, "A", 1)
        ack(first, "B", 2)
        with self.assertRaises(BindingConflict):
            ack(first, "A", 1)

    def test_native_scalar_domains_preserve_dates_precision_and_text(self):
        from datetime import date, datetime
        from decimal import Decimal
        from hero_integration.services.binding_registry import native_fingerprint as fp
        self.assertEqual(fp(date(2026, 11, 20), "Date"), fp("2026-11-20", "Date"))
        self.assertEqual(fp(datetime(2026, 11, 20, 12), "Datetime"), fp("2026-11-20T12:00:00.000", "Datetime"))
        self.assertEqual(fp(False, "Check"), fp(0, "Check"))
        self.assertEqual(fp(Decimal("-0.00"), "Currency"), fp(0.0, "Currency"))
        self.assertNotEqual(fp(None, "Currency"), fp(0, "Currency"))
        self.assertNotEqual(fp(Decimal("10000000000000000.01"), "Currency"),
                            fp(Decimal("10000000000000000.02"), "Currency"))
        self.assertNotEqual(fp("01", "Data"), fp("1", "Data"))

    def test_new_rule_cannot_steal_previously_bound_destination(self):
        spec = dict(source_field="category", target_field="customer_details", mapping_definition_id="one",
                    mapping_revision=1, direction=MappingDirection.HERO_TO_FRAPPE)
        self.registry.create_or_resolve_field_binding(self.parent, **spec)
        with self.assertRaises(BindingConflict):
            self.registry.create_or_resolve_field_binding(self.parent, **dict(spec, source_field="name",
                                                                            mapping_definition_id="two"))

    def test_native_doctype_and_opaque_source_ids_roundtrip(self):
        claim = self.registry.create_or_resolve_object_binding(**dict(self.identity, object_type="SupplyProduct",
            external_object_id="P+a/7==", target_doctype="HERO External Task", target_name="Auftrag März"))
        self.assertEqual(self.registry.get_object_binding(claim.site_scope, claim.identity_key), claim)
