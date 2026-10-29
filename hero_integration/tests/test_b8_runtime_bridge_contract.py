from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

try:
    import frappe  # noqa: F401
except ModuleNotFoundError:
    frappe = types.ModuleType("frappe")
    frappe.whitelist = lambda *args, **kwargs: lambda function: function
    sys.modules["frappe"] = frappe

from hero_integration import api
from hero_integration.services import sync_runtime_adapter


class TestB8RuntimeBridgeContract(unittest.TestCase):
    def test_auto_sync_hands_off_only_to_the_canonical_runtime_adapter(self):
        frappe_module = types.ModuleType("frappe")
        frappe_module.get_single = lambda _doctype: SimpleNamespace(
            polling_mode="AUTO_SYNC",
            scheduled_sync_enabled=1,
            polling_interval_minutes=60,
            last_successful_sync=None,
        )
        frappe_module.utils = SimpleNamespace(
            now_datetime=lambda: datetime(2026, 10, 25, 10, 0)
        )
        adapter = types.ModuleType("hero_integration.services.sync_runtime_adapter")
        calls: list[str] = []
        adapter.run_scheduled_auto_sync = lambda: calls.append("canonical")

        previous_frappe = sys.modules.get("frappe")
        previous_adapter = sys.modules.get("hero_integration.services.sync_runtime_adapter")
        sys.modules.pop("hero_integration.tasks", None)
        sys.modules["frappe"] = frappe_module
        sys.modules["hero_integration.services.sync_runtime_adapter"] = adapter
        try:
            tasks = importlib.import_module("hero_integration.tasks")
            tasks.run_scheduled_sync()
        finally:
            sys.modules.pop("hero_integration.tasks", None)
            if previous_frappe is None:
                sys.modules.pop("frappe", None)
            else:
                sys.modules["frappe"] = previous_frappe
            if previous_adapter is None:
                sys.modules.pop("hero_integration.services.sync_runtime_adapter", None)
            else:
                sys.modules["hero_integration.services.sync_runtime_adapter"] = previous_adapter

        self.assertEqual(calls, ["canonical"])

    def test_preflight_revalidates_the_current_plan_before_any_runtime_execution(self):
        selection = SimpleNamespace(
            source_instance="hero-one", execution_mode="AUTO_SYNC", operation_revision=3, service_revision=3
        )
        executable = SimpleNamespace(plan_key="a" * 64, generation=7, selection=selection)
        approved = SimpleNamespace(generation=7)
        calls = []

        class Service:
            def current_approved(self, site_scope, source_instance):
                calls.append(("current", site_scope, source_instance))
                return approved

            def resolve_executable_plan(self, *args):
                calls.append(("resolve", args))
                return executable

        frappe_module = SimpleNamespace(
            local=SimpleNamespace(site="b9-test.localhost"),
            session=SimpleNamespace(user="Administrator"),
            get_single=lambda _doctype: SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=1, hero_source_instance="hero-one"),
        )
        expected = sync_runtime_adapter.RuntimePreflight("INCOMPLETE", executable.plan_key, executable.generation, "hero-one")
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=Service()), patch.object(
            sync_runtime_adapter, "_run", return_value=expected
        ) as runtime:
            result = sync_runtime_adapter.run_scheduled_auto_sync(frappe_module=frappe_module)

        self.assertIs(result, expected)
        runtime.assert_called_once_with(executable, frappe_module, automatic=True)
        self.assertEqual(calls, [
            ("current", "b9-test.localhost", "hero-one"),
            ("resolve", ("b9-test.localhost", "hero-one", 7, True, "Administrator")),
        ])

    def test_preflight_rejects_missing_approved_plan_before_resolution(self):
        class Service:
            def current_approved(self, _site_scope, _source_instance):
                return None

            def resolve_executable_plan(self, *_args):
                raise AssertionError("must not resolve a missing plan")

        frappe_module = SimpleNamespace(
            local=SimpleNamespace(site="b9-test.localhost"),
            session=SimpleNamespace(user="Administrator"),
            get_single=lambda _doctype: SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=1, hero_source_instance="hero-one"),
        )
        with patch.object(sync_runtime_adapter, "_runtime_plan_service", return_value=Service()):
            with self.assertRaises(sync_runtime_adapter.RuntimeBridgeError):
                sync_runtime_adapter.run_scheduled_auto_sync(frappe_module=frappe_module)

    def test_preflight_rejects_an_unauthenticated_scheduler_before_plan_access(self):
        frappe_module = SimpleNamespace(
            local=SimpleNamespace(site="b9-test.localhost"),
            session=SimpleNamespace(user="Guest"),
            get_single=lambda _doctype: SimpleNamespace(polling_mode="AUTO_SYNC", scheduled_sync_enabled=1, hero_source_instance="hero-one"),
        )
        with patch.object(sync_runtime_adapter, "_runtime_plan_service") as service:
            with self.assertRaises(PermissionError):
                sync_runtime_adapter.run_scheduled_auto_sync(frappe_module=frappe_module)

        service.assert_not_called()

    def test_pending_sync_api_requires_system_manager_and_uses_canonical_adapter(self):
        calls = []
        fake_frappe = SimpleNamespace(
            only_for=lambda role: calls.append(("only_for", role)),
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )
        result = sync_runtime_adapter.RuntimePreflight("INCOMPLETE", "a" * 64, 8, "hero-one")
        with patch.object(api, "frappe", fake_frappe), patch.object(
            sync_runtime_adapter, "run_pending_sync", return_value=result
        ) as pending:
            response = api.pending_sync("8")

        self.assertEqual(calls, [("only_for", "System Manager")])
        pending.assert_called_once_with(8)
        self.assertEqual(response, {
            "status": "INCOMPLETE", "plan_key": "a" * 64,
            "generation": 8, "source_instance": "hero-one",
        })

    def test_pending_sync_api_rejects_invalid_generation_before_adapter(self):
        fake_frappe = SimpleNamespace(
            only_for=lambda _role: None,
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )
        with patch.object(api, "frappe", fake_frappe), patch.object(sync_runtime_adapter, "run_pending_sync") as pending:
            with self.assertRaises(ValueError):
                api.pending_sync("0")
        pending.assert_not_called()

    def _sync_hero_context(self, *, synchronized=False):
        from hero_integration.services.binding_registry import BindingRegistryService, InMemoryBindingStorage
        from hero_integration.services.mapping_registry_model import MappingDirection
        from hero_integration.services.sync_engine import OutboundIntent

        bindings = BindingRegistryService(InMemoryBindingStorage())
        parent = bindings.create_or_resolve_object_binding(
            site_scope="b9-test.localhost", source_instance="hero-one", object_type="Contact",
            external_object_id="1", target_app="erpnext", target_doctype="Customer", target_name="CUST-1",
            mapping_definition_id="definition", mapping_revision=3,
        )
        binding = bindings.create_or_resolve_field_binding(
            parent, source_field="name", target_field="customer_name", mapping_definition_id="definition",
            mapping_revision=3, direction=MappingDirection.HERO_TO_FRAPPE,
        )
        local = "b" * 64 if synchronized else "a" * 64
        if synchronized:
            binding = bindings.record_verified_acknowledgment(
                binding, expected_revision=1, mapping_definition_id="definition", mapping_revision=3,
                source_fingerprint="c" * 64, target_fingerprint=local, evidence_id="evidence", version=1,
            )
        intent = OutboundIntent(binding, 3, "d" * 64, local, True, "mutation X { update { id } }", {})
        return {
            "bindings": bindings, "binding": binding, "parent": parent,
            "executable": SimpleNamespace(generation=4),
            "snapshot": SimpleNamespace(schema_fingerprint="d" * 64),
            "mapping": {"target": {"operation": "update"}}, "intent": intent,
            "fingerprint": "f" * 64,
        }

    def test_sync_hero_rejects_missing_confirmation_without_audit_or_transport(self):
        context = self._sync_hero_context()
        calls = []
        fake_frappe = SimpleNamespace(
            only_for=lambda role: calls.append(("only_for", role)),
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )
        with patch.object(api, "frappe", fake_frappe), patch.object(
            api, "_sync_hero_context", return_value=context
        ), patch.object(api, "_assert_mutation_execution_released") as release, patch.object(
            api, "_reserve_mutation"
        ) as reserve:
            with self.assertRaises(ValueError):
                api.sync_hero_execute("binding", "mapping", 4, 1, "wrong", 1)

        self.assertEqual(calls, [("only_for", "System Manager")])
        release.assert_not_called()
        reserve.assert_not_called()

    def test_sync_hero_rejects_unchanged_binding_before_audit_or_transport(self):
        context = self._sync_hero_context(synchronized=True)
        fake_frappe = SimpleNamespace(
            only_for=lambda _role: None,
            throw=lambda message: (_ for _ in ()).throw(ValueError(message)),
        )
        with patch.object(api, "frappe", fake_frappe), patch.object(
            api, "_sync_hero_context", return_value=context
        ), patch.object(api, "_assert_mutation_execution_released") as release, patch.object(
            api, "_reserve_mutation"
        ) as reserve:
            with self.assertRaises(ValueError):
                api.sync_hero_execute("binding", "mapping", 4, context["binding"].revision, "f" * 64, 1)

        release.assert_not_called()
        reserve.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
