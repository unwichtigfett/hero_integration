"""Runtime Frappe metadata catalog for draft-only mapping proposals.

The catalog deliberately does not grant a write path to another app.  It makes
site metadata discoverable for a System Manager, but leaves non-canonical
targets as Draft proposals until that app supplies an explicit integration.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from hero_integration.services.direct_mapping import (
    ALLOWED_TARGET_TYPES, PROTECTED_FIELDS, PURPOSE_FIELD, SOURCE_CONTRACTS, SYSTEM_FIELDS,
)


@dataclass(frozen=True)
class CatalogDocType:
    app_name: str
    doctype: str
    module: str
    selectable: bool
    reason: str


@dataclass(frozen=True)
class CatalogField:
    app_name: str
    doctype: str
    path: str
    label: str
    fieldtype: str
    nullable: bool
    relation: str
    selectable: bool
    reason: str
    is_custom: bool
    required: bool
    select_options: tuple[str, ...]
    activation_allowed: bool
    description: str = ""


CANONICAL_TARGETS = frozenset({"Customer", "Project"})


class FrappePickerMetadata:
    """Site metadata adapter shared by both mapping picker directions."""

    def doctypes(self):
        import frappe
        from frappe.modules.utils import get_module_app

        installed = set(frappe.get_installed_apps())
        rows = frappe.get_all(
            "DocType", fields=["name", "module", "istable", "issingle", "is_virtual", "custom"],
            order_by="module asc, name asc",
        )
        result = []
        for row in rows:
            item = dict(row)
            module = str(item.get("module") or "")
            try:
                app_name = get_module_app(module) if module else ""
            except Exception:
                app_name = "Custom" if item.get("custom") else ""
            if app_name and app_name not in installed and app_name != "Custom":
                continue
            item["app_name"] = app_name or ("Custom" if item.get("custom") else "")
            result.append(item)
        return result

    def can_write(self, doctype: str) -> bool:
        import frappe

        from hero_integration.services.doctype_permissions import can_doctype_write

        return can_doctype_write(doctype, frappe)

    def fields(self, doctype: str):
        import frappe

        from hero_integration.services.doctype_permissions import ensure_doctype_write

        ensure_doctype_write(doctype, frappe)
        return frappe.get_meta(doctype).fields


def validate_draft_spec(spec, metadata) -> CatalogField:
    """Validate a non-executable proposal without widening the active mapper."""
    contract = SOURCE_CONTRACTS.get(spec.operation)
    if not contract or spec.source_path not in contract["paths"]:
        raise ValueError("Source path is not selected by the approved HERO operation.")
    if spec.source_type != contract["paths"][spec.source_path]:
        raise ValueError("Source type does not match the approved HERO contract.")
    if spec.purpose != PURPOSE_FIELD:
        raise ValueError("Only ordinary field proposals may target a non-canonical app.")
    return validate_draft_target(spec.operation, spec.source_type, spec.target_doctype, spec.target_field, metadata)


def doctypes(metadata, app_name: str = "", search: str = "") -> list[CatalogDocType]:
    """Return installed-app DocTypes, including disabled explanations."""
    needle = search.strip().lower()
    result = []
    for item in metadata.doctypes():
        app = str(item.get("app_name") or "")
        name = str(item.get("name") or "")
        module = str(item.get("module") or "")
        if app_name and app != app_name:
            continue
        if needle and needle not in f"{app} {module} {name}".lower():
            continue
        if not name:
            continue
        selectable, reason = _doctype_state(item, metadata)
        result.append(CatalogDocType(app, name, module, selectable, reason))
    return sorted(result, key=lambda item: (item.app_name.lower(), item.doctype.lower()))


def fields(metadata, app_name: str = "", doctype: str = "", search: str = "", *, targets=None) -> list[CatalogField]:
    """Return target fields from installed apps without bypassing permissions."""
    needle = search.strip().lower()
    result = []
    for target in doctypes(metadata, app_name) if targets is None else targets:
        if doctype and target.doctype != doctype:
            continue
        if not target.selectable:
            continue
        for field in metadata.fields(target.doctype):
            fieldname = str(getattr(field, "fieldname", "") or "")
            label = str(getattr(field, "label", "") or fieldname)
            fieldtype = str(getattr(field, "fieldtype", "") or "")
            description = str(getattr(field, "description", "") or "")
            if not fieldname or (needle and needle not in f"{fieldname} {label} {fieldtype} {description}".lower()):
                continue
            result.append(_field(target, field, metadata))
    return sorted(result, key=lambda item: (item.app_name.lower(), item.doctype.lower(), item.path.lower()))


def catalog(metadata, app_name: str = "", search: str = "") -> dict[str, object]:
    all_entries = doctypes(metadata)
    needle = search.strip().lower()
    entries = [item for item in all_entries
               if (not app_name or item.app_name == app_name)
               and (not needle or needle in f"{item.app_name} {item.module} {item.doctype}".lower())]
    apps = sorted({item.app_name for item in all_entries if item.app_name})
    return {"apps": apps, "doctypes": [item.__dict__ for item in entries]}


def validate_draft_target(operation: str, source_type: str, target_doctype: str, target_field: str, metadata) -> CatalogField:
    """Validate only a Draft proposal; active runtime mappings remain canonical."""
    contract = SOURCE_CONTRACTS.get(operation)
    if not contract or source_type not in contract["paths"].values():
        raise ValueError("Source type is not part of the approved HERO operation.")
    if not target_doctype or not target_field:
        raise ValueError("A target DocType and field are required.")
    target = next((item for item in fields(metadata, doctype=target_doctype) if item.path == target_field), None)
    if target is None:
        doctype = next((item for item in doctypes(metadata) if item.doctype == target_doctype), None)
        if doctype is not None and not doctype.selectable:
            raise ValueError(doctype.reason)
        raise ValueError("The target field is not available in the current site metadata.")
    if not target.selectable:
        raise ValueError(target.reason)
    if not types_compatible(source_type, target.fieldtype):
        raise ValueError("Source and target field types are incompatible.")
    if target_doctype not in CANONICAL_TARGETS and target.activation_allowed:
        raise AssertionError("Only canonical targets may be activated by this app.")
    return target


def validate_draft_writeback_source(doctype: str, fieldname: str, metadata) -> CatalogField:
    """Validate a source without relaxing any incoming target protection."""
    source = next((item for item in writeback_fields(metadata, doctype=doctype) if item.path == fieldname), None)
    if source is None:
        raise ValueError("The Frappe source field is not available in the current site metadata.")
    if not source.selectable:
        raise ValueError(source.reason)
    return source


def writeback_fields(metadata, doctype: str = '', *, targets=None) -> list[CatalogField]:
    """Expose reviewed readable source fields separately from inbound targets.

    Only explicit existing writeback definitions can cross the inbound
    protected-target boundary, and only at the public field permission level.
    This grants neither mutation execution nor access to a specific record.
    """
    from hero_integration.services.frappe_writeback import DEFINITIONS
    candidates = doctypes(metadata) if targets is None else targets
    result = []
    for target in candidates:
        if doctype and target.doctype != doctype:
            continue
        if not target.selectable:
            continue
        reviewed = DEFINITIONS.get(target.doctype)
        for field in metadata.fields(target.doctype):
            item = _field(target, field, metadata)
            if (not item.selectable and reviewed and item.path in reviewed.fields
                    and item.path in PROTECTED_FIELDS.get(target.doctype, set())
                    and item.path not in SYSTEM_FIELDS and item.fieldtype in ALLOWED_TARGET_TYPES | {'Long Text', 'Text', 'Code', 'Select'}
                    and item.fieldtype not in {'Link', 'Table'}
                    and not getattr(field, 'read_only', 0) and not getattr(field, 'hidden', 0)
                    and not getattr(field, 'is_virtual', 0) and not int(getattr(field, 'permlevel', 0) or 0)):
                item = replace(item, selectable=True, reason='Reviewed writeback source; execution requires separate approval.')
            result.append(item)
    return sorted(result, key=lambda item: (item.app_name.lower(), item.doctype.lower(), item.path.lower()))


def types_compatible(source_type: str, target_type: str) -> bool:
    if source_type.startswith("string"):
        return target_type in {"Data", "Small Text", "Long Text", "Text", "Code", "Date", "Datetime", "Select"}
    if source_type.startswith("int"):
        return target_type in {"Int", "Float", "Data"}
    return source_type.startswith("boolean") and target_type == "Check"


def _doctype_state(item: dict[str, object], metadata) -> tuple[bool, str]:
    name = str(item.get("name") or "")
    if item.get("istable"):
        return False, "Child Table DocType: use an app-owned child-row resolver."
    if item.get("issingle"):
        return False, "Single DocType: it is configuration, not a record target."
    if item.get("is_virtual"):
        return False, "Virtual DocType: direct persistence is not available."
    if not metadata.can_write(name):
        return False, "No write permission for this DocType."
    if name not in CANONICAL_TARGETS:
        return True, "Draft proposal only; activation requires an app-owned integration."
    return True, "Canonical HERO migration target."


def _field(target: CatalogDocType, field, metadata) -> CatalogField:
    fieldname = str(getattr(field, "fieldname", "") or "")
    fieldtype = str(getattr(field, "fieldtype", "") or "")
    relation = str(getattr(field, "options", "") or "") if fieldtype in {"Link", "Table"} else ""
    options = tuple(option for option in str(getattr(field, "options", "") or "").splitlines() if option) if fieldtype == "Select" else ()
    selectable = target.selectable
    reason = target.reason
    if fieldname in SYSTEM_FIELDS | PROTECTED_FIELDS.get(target.doctype, set()):
        selectable, reason = False, "System field: it cannot be a mapping target."
    elif getattr(field, "read_only", 0):
        selectable, reason = False, "Read-only field: it cannot be a mapping target."
    elif fieldtype == "Table":
        selectable, reason = False, "Child Table field: an app-owned child-row resolver is required."
    elif fieldtype == "Link":
        selectable, reason = False, "Link field: an approved entity resolver is required."
    elif fieldtype not in ALLOWED_TARGET_TYPES | {"Long Text", "Text", "Code", "Select"}:
        selectable, reason = False, "Unsupported field type for a direct Draft proposal."
    elif selectable and target.doctype not in CANONICAL_TARGETS:
        reason = "Draft proposal only; activation requires an app-owned integration."
    return CatalogField(
        target.app_name, target.doctype, fieldname, str(getattr(field, "label", "") or fieldname), fieldtype,
        not bool(getattr(field, "reqd", 0)), relation, selectable, reason, bool(getattr(field, "is_custom_field", False)),
        bool(getattr(field, "reqd", 0)), options, target.doctype in CANONICAL_TARGETS,
        str(getattr(field, "description", "") or ""),
    )
