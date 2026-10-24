from __future__ import annotations

from hero_integration.services.frappe_field_registry import (
    DocTypeMetadata,
    EffectiveFieldMetadata,
    FrappeFieldMetadataProvider,
    ModuleMetadata,
    build_frappe_field_registry,
)


class FakeMetadataProvider(FrappeFieldMetadataProvider):
    def installed_apps(self, site: str) -> tuple[str, ...]:
        assert site == "test.local"
        return ("erpnext", "custom_app")

    def module_definitions(self, site: str) -> tuple[ModuleMetadata, ...]:
        return (
            ModuleMetadata("Selling", "erpnext"),
            ModuleMetadata("Custom", "custom_app"),
        )

    def doctypes(self, site: str) -> tuple[DocTypeMetadata, ...]:
        return (
            DocTypeMetadata("Customer", "Selling"),
            DocTypeMetadata("Customer Profile", "Custom", custom=True),
        )

    def effective_fields(
        self, site: str, doctype: DocTypeMetadata
    ) -> tuple[EffectiveFieldMetadata, ...]:
        return {
            "Customer": (
                EffectiveFieldMetadata("email_id", "Email", "Data"),
                EffectiveFieldMetadata("customer_name", "Customer Name", "Data", reqd=True),
            ),
            "Customer Profile": (
                EffectiveFieldMetadata(
                    "custom_code", "Code", "Data", custom=True
                ),
            ),
        }[doctype.name]


def test_builds_deterministic_catalog_for_site_installed_apps_only():
    registry = build_frappe_field_registry("test.local", FakeMetadataProvider())

    assert registry.site == "test.local"
    assert tuple(item.identity for item in registry.fields) == (
        "custom_app.Customer Profile.custom_code",
        "erpnext.Customer.customer_name",
        "erpnext.Customer.email_id",
    )
    resolved = registry.resolve("erpnext.Customer.email_id")
    assert resolved.ok
    assert resolved.field is not None
    assert resolved.field.fieldtype.name == "Data"
    assert registry.execution_authority == "NONE"
