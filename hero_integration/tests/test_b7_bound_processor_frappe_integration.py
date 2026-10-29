from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from hero_integration.services.binding_registry import (
    BindingRegistryService,
    FrappeBindingStorage,
)
from hero_integration.services.mapping_registry_model import MappingDirection
from hero_integration.services.sync_engine import FrappeBoundIndex, FrappeBoundProcessor


class TestB7BoundProcessorFrappeIntegration(IntegrationTestCase):
    source = "b9-b7-bound-source"

    def setUp(self):
        frappe.set_user("Administrator")
        self._remove()
        self.bound = frappe.get_doc({"doctype": "Customer", "customer_name": "B9BoundOld", "customer_type": "Company"}).insert()
        self.unbound = frappe.get_doc({"doctype": "Customer", "customer_name": "B9UnboundOld", "customer_type": "Company"}).insert()
        self.bindings = BindingRegistryService(FrappeBindingStorage())
        parent = self.bindings.create_or_resolve_object_binding(
            site_scope=frappe.local.site, source_instance=self.source, object_type="Contact",
            external_object_id="bound-1", target_app="erpnext", target_doctype="Customer",
            target_name=self.bound.name, mapping_definition_id="b9-name", mapping_revision=7,
        )
        self.bindings.create_or_resolve_field_binding(
            parent, source_field="name", target_field="customer_name", mapping_definition_id="b9-name",
            mapping_revision=7, direction=MappingDirection.HERO_TO_FRAPPE,
        )

    def tearDown(self):
        self._remove()

    def _remove(self):
        frappe.db.delete("HERO Field Binding", {"source_instance": self.source})
        frappe.db.delete("HERO Object Binding", {"source_instance": self.source})
        frappe.db.delete("Customer", {"customer_name": ["in", ["B9BoundOld", "B9BoundNew", "B9UnboundOld"]]})

    def test_blocks_bound_target_without_atomic_conflict_contract(self):
        fields = FrappeBoundIndex().fields(frappe.local.site, self.source)
        processor = FrappeBoundProcessor(7, ("b9-name",))
        self.assertFalse(processor.process({"id": "bound-1", "name": "B9BoundNew"}, fields))
        self.assertEqual(frappe.get_doc("Customer", self.bound.name).customer_name, "B9BoundOld")
        self.assertEqual(frappe.get_doc("Customer", self.unbound.name).customer_name, "B9UnboundOld")
        self.assertFalse(FrappeBoundProcessor(8, ("b9-name",)).process({"id": "bound-1", "name": "blocked"}, fields))
        self.assertEqual(frappe.get_doc("Customer", self.bound.name).customer_name, "B9BoundOld")
