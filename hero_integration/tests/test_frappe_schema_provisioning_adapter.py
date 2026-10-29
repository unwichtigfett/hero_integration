from __future__ import annotations

from types import SimpleNamespace

import pytest

from hero_integration.services.controlled_schema_provisioning import (
    ProvisioningField,
    ProvisioningOperation,
    ProvisioningOperationKind,
)
from hero_integration.services.frappe_schema_provisioning_adapter import FrappeSchemaProvisioningAdapter
from hero_integration.services.schema_provisioning_lifecycle import ProvisioningLifecycleError


class FakeFrappe:
    def __init__(self):
        self.local = SimpleNamespace(site="site.test")
        self.session = SimpleNamespace(user="Administrator")
        self.created = []
        self.custom_field_type = "Data"

    def get_list(self, doctype, *args, **kwargs):
        if doctype == "Custom Field":
            return [SimpleNamespace(dt="Target", fieldname="value", fieldtype=self.custom_field_type, modified="now")]
        if doctype == "Property Setter":
            return [SimpleNamespace(doc_type="Target", field_name="value", property="label", value="Value", modified="now")]
        return [SimpleNamespace(name="DocType", module="Module", custom=0, modified="now")]

    def has_permission(self, *args, **kwargs):
        return True

    def get_doc(self, value):
        outer = self
        class Doc:
            def insert(self):
                outer.created.append(value)
        return Doc()

    def get_meta(self, doctype):
        return SimpleNamespace(get_field=lambda name: SimpleNamespace(fieldtype="Data"))


def test_mocked_adapter_uses_framework_documents_not_raw_ddl():
    api = FakeFrappe()
    adapter = FrappeSchemaProvisioningAdapter(api)
    op = ProvisioningOperation(
        "create", ProvisioningOperationKind.CREATE_CUSTOM_DOCTYPE, "app", "Module", "New Target",
        "value", "Data", initial_fields=(ProvisioningField("value", "Data"),),
    )
    before = adapter.metadata_fingerprint("site.test")
    adapter.apply("site.test", op)
    assert api.created[0]["doctype"] == "DocType"
    assert api.created[0]["custom"] == 1
    assert adapter.verify("site.test", op)
    api.custom_field_type = "Int"
    assert adapter.metadata_fingerprint("site.test") != before


def test_adapter_rejects_scope_mismatch_before_any_framework_write():
    api = FakeFrappe()
    adapter = FrappeSchemaProvisioningAdapter(api)
    op = ProvisioningOperation("create", ProvisioningOperationKind.CREATE_CUSTOM_FIELD, "app", "Module", "Target", "value", "Data")
    with pytest.raises(ProvisioningLifecycleError):
        adapter.apply("other.site", op)
    assert api.created == []
