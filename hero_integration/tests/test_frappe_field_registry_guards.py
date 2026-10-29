from __future__ import annotations

import ast
import inspect

from hero_integration.services.frappe_field_registry import (
    DocTypeMetadata,
    EffectiveFieldMetadata,
    FrappeFieldMetadataProvider,
    ModuleMetadata,
    OwnershipState,
    ResolutionCode,
    build_frappe_field_registry,
)


class SyntheticProvider(FrappeFieldMetadataProvider):
    def __init__(self, apps, modules, doctypes, fields, *, fail=False):
        self.apps = apps
        self.modules = modules
        self.doc_types = doctypes
        self.field_map = fields
        self.fail = fail
        self.field_calls = []

    def installed_apps(self, site):
        if self.fail:
            raise RuntimeError("do not expose provider detail")
        return self.apps

    def module_definitions(self, site):
        return self.modules

    def doctypes(self, site):
        return self.doc_types

    def effective_fields(self, site, doctype):
        self.field_calls.append(doctype.name)
        return self.field_map[doctype.name]


def test_uses_site_installed_apps_and_never_infers_missing_module_owner():
    provider = SyntheticProvider(
        ("erpnext",),
        (ModuleMetadata("Selling", "erpnext"), ModuleMetadata("Bench Only", "bench_app")),
        (
            DocTypeMetadata("Customer", "Selling"),
            DocTypeMetadata("Orphan", "No Module", custom=True),
            DocTypeMetadata("Bench Doc", "Bench Only"),
        ),
        {"Customer": (EffectiveFieldMetadata("email_id", "Email", "Data"),)},
    )

    registry = build_frappe_field_registry("site-a", provider)

    assert tuple(field.identity for field in registry.fields) == ("erpnext.Customer.email_id",)
    assert provider.field_calls == ["Customer"]
    orphan = next(item for item in registry.doctypes if item.name == "Orphan")
    assert orphan.ownership == OwnershipState.UNRESOLVED
    assert registry.resolve("erpnext.Orphan.any").code == ResolutionCode.UNRESOLVED_OWNERSHIP
    assert registry.resolve("bench_app.Bench Doc.any").code == ResolutionCode.MISSING_APP


def test_preserves_effective_custom_relation_type_and_readiness_metadata():
    provider = SyntheticProvider(
        ("erpnext",),
        (ModuleMetadata("Selling", "erpnext"),),
        (DocTypeMetadata("Customer", "Selling"),),
        {
            "Customer": (
                EffectiveFieldMetadata("name", "Name", "Data", framework=True),
                EffectiveFieldMetadata(
                    "custom_status",
                    "Status",
                    "Select",
                    custom=True,
                    options="Open\nClosed",
                ),
                EffectiveFieldMetadata("account", "Account", "Link", options="Account"),
                EffectiveFieldMetadata(
                    "reference_name", "Reference", "Dynamic Link", options="reference_doctype"
                ),
                EffectiveFieldMetadata("items", "Items", "Table", options="Customer Item"),
                EffectiveFieldMetadata(
                    "effective_date",
                    "Effective Date",
                    "Date",
                    property_setter=True,
                    read_only=True,
                    hidden=True,
                    precision=2,
                ),
                EffectiveFieldMetadata("section", "Section", "Section Break"),
            )
        },
    )

    registry = build_frappe_field_registry("site-a", provider)
    by_id = {item.identity: item for item in registry.fields}

    assert by_id["erpnext.Customer.custom_status"].select_options == ("Open", "Closed")
    assert by_id["erpnext.Customer.custom_status"].provenance.value == "CUSTOM"
    assert by_id["erpnext.Customer.account"].link_target == "Account"
    assert by_id["erpnext.Customer.reference_name"].dynamic_link_field == "reference_doctype"
    assert by_id["erpnext.Customer.items"].child_table_target == "Customer Item"
    assert by_id["erpnext.Customer.effective_date"].provenance.value == "PROPERTY_SETTER_EFFECTIVE"
    assert by_id["erpnext.Customer.effective_date"].property_setter_effective
    assert by_id["erpnext.Customer.effective_date"].hidden
    assert by_id["erpnext.Customer.effective_date"].precision == 2
    assert registry.resolve("erpnext.Customer.name").code == ResolutionCode.PROTECTED_FIELD
    assert registry.resolve("erpnext.Customer.effective_date").code == ResolutionCode.READ_ONLY_FIELD
    assert registry.resolve("erpnext.Customer.account").code == ResolutionCode.UNSUPPORTED_FIELD
    assert registry.resolve("erpnext.Customer.section").code == ResolutionCode.UNSUPPORTED_FIELD


def test_catalogs_child_single_and_virtual_doctypes_without_granting_mapping_authority():
    provider = SyntheticProvider(
        ("app",),
        (ModuleMetadata("Core", "app"),),
        (
            DocTypeMetadata("Child", "Core", istable=True),
            DocTypeMetadata("Settings", "Core", issingle=True),
            DocTypeMetadata("Virtual", "Core", is_virtual=True),
        ),
        {
            "Child": (EffectiveFieldMetadata("value", "Value", "Data"),),
            "Settings": (EffectiveFieldMetadata("enabled", "Enabled", "Check"),),
            "Virtual": (EffectiveFieldMetadata("value", "Value", "Data", virtual=True),),
        },
    )

    registry = build_frappe_field_registry("site-a", provider)

    assert len(registry.fields) == 3
    assert all(not field.mapping_ready for field in registry.fields)
    assert registry.execution_authority == "NONE"


def test_reports_missing_targets_ambiguous_ownership_and_provider_failures_without_partial_data():
    ambiguous = SyntheticProvider(
        ("app_a", "app_b"),
        (ModuleMetadata("Shared", "app_a"), ModuleMetadata("Shared", "app_b")),
        (DocTypeMetadata("Shared Doc", "Shared"),),
        {},
    )
    registry = build_frappe_field_registry("site-a", ambiguous)
    assert registry.resolve("app_a.Shared Doc.value").code == ResolutionCode.AMBIGUOUS_OWNERSHIP
    assert registry.resolve("app_a.Unknown.value").code == ResolutionCode.MISSING_DOCTYPE
    assert registry.resolve("app_a.Shared Doc").code == ResolutionCode.INVALID_REFERENCE

    failed = build_frappe_field_registry(
        "site-a",
        SyntheticProvider((), (), (), {}, fail=True),
    )
    assert not failed.fields
    assert failed.diagnostics[0].code == ResolutionCode.METADATA_PROVIDER_FAILURE
    missing_site = build_frappe_field_registry("", ambiguous)
    assert missing_site.diagnostics[0].code == ResolutionCode.MISSING_SITE_CONTEXT


def test_is_deterministic_and_core_has_no_frappe_database_or_transport_import():
    provider = SyntheticProvider(
        ("app",),
        (ModuleMetadata("Core", "app"),),
        (DocTypeMetadata("Thing", "Core"),),
        {"Thing": (EffectiveFieldMetadata("value", "Value", "Float"),)},
    )

    assert build_frappe_field_registry("site-a", provider) == build_frappe_field_registry("site-a", provider)

    from hero_integration.services import frappe_field_registry

    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(inspect.getsource(frappe_field_registry)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "frappe" not in imports
    assert "requests" not in imports
    assert "http" not in imports
