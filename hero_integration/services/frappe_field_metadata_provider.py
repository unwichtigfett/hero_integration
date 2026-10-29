"""Thin Frappe v16 adapter for the pure field registry.

Frappe is imported only when this production adapter is actually constructed.
All list operations remain permission-aware and read-only.
"""
from __future__ import annotations

from typing import Any

from hero_integration.services.frappe_field_registry import (
    DocTypeMetadata,
    EffectiveFieldMetadata,
    ModuleMetadata,
)


class FrappeMetadataProviderError(RuntimeError):
    """The current Frappe request cannot safely provide site metadata."""


class FrappeV16FieldMetadataProvider:
    """Read effective v16 metadata without granting document-data access."""

    def __init__(self, frappe_api: Any | None = None) -> None:
        self._frappe_api = frappe_api

    def installed_apps(self, site: str) -> tuple[str, ...]:
        frappe = self._frappe(site)
        return tuple(frappe.get_installed_apps())

    def module_definitions(self, site: str) -> tuple[ModuleMetadata, ...]:
        frappe = self._frappe(site)
        self._require_read(frappe, "Module Def")
        return tuple(
            ModuleMetadata(_value(row, "module_name"), _value(row, "app_name"))
            for row in frappe.get_list(
                "Module Def",
                fields=["module_name", "app_name"],
                order_by="module_name asc",
                limit_page_length=0,
            )
        )

    def doctypes(self, site: str) -> tuple[DocTypeMetadata, ...]:
        frappe = self._frappe(site)
        self._require_read(frappe, "DocType")
        return tuple(
            DocTypeMetadata(
                _value(row, "name"),
                _value(row, "module"),
                bool(_value(row, "custom")),
                bool(_value(row, "istable")),
                bool(_value(row, "issingle")),
                bool(_value(row, "is_virtual")),
            )
            for row in frappe.get_list(
                "DocType",
                fields=["name", "module", "custom", "istable", "issingle", "is_virtual"],
                order_by="module asc, name asc",
                limit_page_length=0,
            )
        )

    def effective_fields(
        self, site: str, doctype: DocTypeMetadata
    ) -> tuple[EffectiveFieldMetadata, ...]:
        frappe = self._frappe(site)
        self._require_read(frappe, doctype.name)
        self._require_read(frappe, "Property Setter")
        property_setters = {
            _value(row, "field_name")
            for row in frappe.get_list(
                "Property Setter",
                filters={"doc_type": doctype.name, "field_name": ["!=", ""]},
                fields=["field_name"],
                limit_page_length=0,
            )
        }
        meta = frappe.get_meta(doctype.name)
        return tuple(
            EffectiveFieldMetadata(
                _value(field, "fieldname"),
                _value(field, "label") or _value(field, "fieldname"),
                _value(field, "fieldtype"),
                bool(_value(field, "reqd")),
                bool(_value(field, "read_only")),
                bool(_value(field, "hidden")),
                _integer(_value(field, "permlevel")),
                bool(_value(field, "is_custom_field")),
                _value(field, "fieldname") in property_setters,
                bool(_value(field, "is_system_generated")),
                _value(field, "options"),
                _optional_integer(_value(field, "precision")),
                bool(_value(field, "is_virtual")),
                bool(_value(field, "is_computed")),
            )
            for field in meta.fields
        )

    def _frappe(self, site: str):
        if not isinstance(site, str) or not site:
            raise FrappeMetadataProviderError("A site context is required.")
        if self._frappe_api is None:
            import frappe

            api = frappe
        else:
            api = self._frappe_api
        active_site = str(getattr(getattr(api, "local", None), "site", "") or "")
        if active_site != site:
            raise FrappeMetadataProviderError("Current Frappe site does not match requested site context.")
        return api

    @staticmethod
    def _require_read(frappe: Any, doctype: str) -> None:
        frappe.has_permission(doctype, "read", throw=True)


def _value(item: object, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _integer(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
