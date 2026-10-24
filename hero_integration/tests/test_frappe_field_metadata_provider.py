from __future__ import annotations

from types import SimpleNamespace

import pytest

from hero_integration.services.frappe_field_metadata_provider import (
    FrappeMetadataProviderError,
    FrappeV16FieldMetadataProvider,
)


class FakeFrappe:
    def __init__(self):
        self.local = SimpleNamespace(site="site-a")
        self.permissions = []
        self.calls = []

    def get_installed_apps(self):
        return ["erpnext"]

    def has_permission(self, doctype, action, throw=False):
        self.permissions.append((doctype, action, throw))
        return True

    def get_list(self, doctype, **kwargs):
        self.calls.append((doctype, kwargs))
        if doctype == "Module Def":
            return [{"module_name": "Selling", "app_name": "erpnext"}]
        if doctype == "DocType":
            return [{"name": "Customer", "module": "Selling", "custom": 0, "istable": 0, "issingle": 0, "is_virtual": 0}]
        if doctype == "Property Setter":
            return [{"field_name": "email_id"}]
        raise AssertionError(doctype)

    def get_meta(self, doctype):
        assert doctype == "Customer"
        return SimpleNamespace(fields=[
            SimpleNamespace(
                fieldname="email_id",
                label="Email",
                fieldtype="Data",
                reqd=0,
                read_only=0,
                hidden=0,
                permlevel=0,
                is_custom_field=0,
                is_system_generated=0,
                options="",
                precision=None,
                is_virtual=0,
                is_computed=0,
            )
        ])


def test_adapter_uses_site_scoped_permission_aware_metadata_apis():
    frappe = FakeFrappe()
    provider = FrappeV16FieldMetadataProvider(frappe)

    assert provider.installed_apps("site-a") == ("erpnext",)
    assert provider.module_definitions("site-a")[0].app == "erpnext"
    doc = provider.doctypes("site-a")[0]
    field = provider.effective_fields("site-a", doc)[0]

    assert field.property_setter
    assert field.fieldname == "email_id"
    assert all(action == "read" and throw for _, action, throw in frappe.permissions)
    assert [doctype for doctype, _ in frappe.calls] == ["Module Def", "DocType", "Property Setter"]
    assert all("limit_page_length" in kwargs for _, kwargs in frappe.calls)
    assert not hasattr(frappe, "get_all")


def test_adapter_rejects_cross_site_context_before_metadata_access():
    provider = FrappeV16FieldMetadataProvider(FakeFrappe())

    with pytest.raises(FrappeMetadataProviderError, match="does not match"):
        provider.doctypes("other-site")
