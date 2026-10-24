"""Lazy Frappe v16 adapter for already-authorized B5 operations."""
from __future__ import annotations

import json
import re
from hashlib import sha256

from hero_integration.services.controlled_schema_provisioning import (
    ProvisioningField,
    ProvisioningOperation,
    ProvisioningOperationKind,
)
from hero_integration.services.schema_provisioning_lifecycle import ProvisioningLifecycleError

_FIELDNAME = re.compile(r"^[a-z][a-z0-9_]{0,139}$")


class FrappeSchemaProvisioningAdapter:
    """No Frappe import or site access occurs until an authorized operation runs."""

    def __init__(self, frappe_module=None) -> None:
        self._frappe_module = frappe_module

    def metadata_fingerprint(self, site_scope: str) -> str:
        frappe = self._frappe(site_scope)
        payload = {
            doctype: _rows(frappe.get_list(doctype, fields=fields, order_by=order, limit_page_length=0), fields)
            for doctype, fields, order in (
                ("DocType", ("name", "modified", "module", "custom"), "name asc"),
                ("Custom Field", ("dt", "fieldname", "fieldtype", "modified"), "dt asc, fieldname asc"),
                ("Property Setter", ("doc_type", "field_name", "property", "value", "modified"), "doc_type asc, field_name asc, property asc"),
            )
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def apply(self, site_scope: str, operation: ProvisioningOperation) -> None:
        frappe = self._frappe(site_scope)
        self._require_authority(frappe, operation)
        if operation.kind == ProvisioningOperationKind.REUSE_EXISTING_COMPATIBLE_TARGET:
            return
        fields = _fields_for(operation)
        if operation.kind == ProvisioningOperationKind.CREATE_CUSTOM_DOCTYPE:
            frappe.get_doc({
                "doctype": "DocType", "name": operation.doctype, "module": operation.module,
                "custom": 1, "istable": 0, "issingle": 0, "is_virtual": 0,
                "fields": [_field_payload(field) for field in fields],
            }).insert()
            return
        if operation.kind == ProvisioningOperationKind.CREATE_CUSTOM_FIELD:
            field = fields[0]
            frappe.get_doc({
                "doctype": "Custom Field", "dt": operation.doctype, "fieldname": field.fieldname,
                "label": field.fieldname.replace("_", " ").title(), "fieldtype": field.fieldtype,
                "module": operation.module,
            }).insert()
            return
        raise ProvisioningLifecycleError("Operation is not approved for framework mutation.")

    def verify(self, site_scope: str, operation: ProvisioningOperation) -> bool:
        frappe = self._frappe(site_scope)
        meta = frappe.get_meta(operation.doctype)
        for field in _fields_for(operation):
            effective = meta.get_field(field.fieldname)
            if effective is None or getattr(effective, "fieldtype", None) != field.fieldtype:
                return False
        return True

    def _frappe(self, site_scope: str):
        if self._frappe_module is None:
            import frappe
            api = frappe
        else:
            api = self._frappe_module
        if getattr(getattr(api, "local", None), "site", None) != site_scope:
            raise ProvisioningLifecycleError("Frappe site does not match provisioning scope.")
        return api

    @staticmethod
    def _require_authority(frappe, operation: ProvisioningOperation) -> None:
        if getattr(getattr(frappe, "session", None), "user", None) != "Administrator" and not frappe.has_role("System Manager"):
            raise PermissionError("System Manager or Administrator authority is required.")
        target = "DocType" if operation.kind == ProvisioningOperationKind.CREATE_CUSTOM_DOCTYPE else "Custom Field"
        frappe.has_permission(target, "create", throw=True)
        if operation.kind == ProvisioningOperationKind.CREATE_CUSTOM_FIELD:
            frappe.has_permission(operation.doctype, "write", throw=True)


def _fields_for(operation: ProvisioningOperation) -> tuple[ProvisioningField, ...]:
    fields = operation.initial_fields or (
        ProvisioningField(operation.fieldname or "", operation.fieldtype or ""),
    )
    if not fields or any(not _FIELDNAME.fullmatch(field.fieldname) or not field.fieldtype for field in fields):
        raise ProvisioningLifecycleError("Provisioning fields must be validated lower-case Frappe fieldnames.")
    return fields


def _field_payload(field: ProvisioningField) -> dict[str, str]:
    return {
        "fieldname": field.fieldname,
        "label": field.fieldname.replace("_", " ").title(),
        "fieldtype": field.fieldtype,
    }


def _rows(rows, fields: tuple[str, ...]) -> list[list[str | None]]:
    normalized: list[list[str | None]] = []
    for item in rows:
        normalized.append([
            _value(item, field)
            for field in fields
        ])
    return sorted(normalized, key=lambda item: tuple("" if value is None else value for value in item))


def _value(item, field: str) -> str | None:
    value = item.get(field) if isinstance(item, dict) else getattr(item, field, None)
    return None if value is None else str(value)
