"""Pure typed catalog of effective Frappe field metadata."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class FrappeFieldRegistryError(ValueError):
    """Provider metadata cannot be represented without inference."""


class OwnershipState(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"


class FieldProvenance(StrEnum):
    STANDARD = "STANDARD"
    CUSTOM = "CUSTOM"
    PROPERTY_SETTER_EFFECTIVE = "PROPERTY_SETTER_EFFECTIVE"
    FRAMEWORK = "FRAMEWORK"


class ResolutionCode(StrEnum):
    RESOLVED = "RESOLVED"
    MISSING_APP = "MISSING_APP"
    MISSING_DOCTYPE = "MISSING_DOCTYPE"
    MISSING_DOCFIELD = "MISSING_DOCFIELD"
    AMBIGUOUS_OWNERSHIP = "AMBIGUOUS_OWNERSHIP"
    UNRESOLVED_OWNERSHIP = "UNRESOLVED_OWNERSHIP"
    UNSUPPORTED_FIELD = "UNSUPPORTED_FIELD"
    PROTECTED_FIELD = "PROTECTED_FIELD"
    READ_ONLY_FIELD = "READ_ONLY_FIELD"
    MISSING_SITE_CONTEXT = "MISSING_SITE_CONTEXT"
    METADATA_PROVIDER_FAILURE = "METADATA_PROVIDER_FAILURE"
    INVALID_REFERENCE = "INVALID_REFERENCE"


@dataclass(frozen=True)
class RegistryDiagnostic:
    code: ResolutionCode
    message: str


@dataclass(frozen=True)
class FrappeFieldReference:
    app: str
    doctype: str
    fieldname: str

    def __post_init__(self) -> None:
        if any(not isinstance(v, str) or not v or "." in v for v in (self.app, self.doctype, self.fieldname)):
            raise FrappeFieldRegistryError("Field references require three non-empty, dot-free segments.")

    @property
    def identity(self) -> str:
        return f"{self.app}.{self.doctype}.{self.fieldname}"

    @classmethod
    def parse(cls, value: str) -> "FrappeFieldReference":
        if not isinstance(value, str) or len(value.split(".")) != 3:
            raise FrappeFieldRegistryError("Field reference must be <app>.<doctype>.<field>.")
        return cls(*value.split("."))


@dataclass(frozen=True)
class ModuleMetadata:
    module: str
    app: str


@dataclass(frozen=True)
class DocTypeMetadata:
    name: str
    module: str
    custom: bool = False
    istable: bool = False
    issingle: bool = False
    is_virtual: bool = False


@dataclass(frozen=True)
class EffectiveFieldMetadata:
    fieldname: str
    label: str
    fieldtype: str
    reqd: bool = False
    read_only: bool = False
    hidden: bool = False
    permlevel: int = 0
    custom: bool = False
    property_setter: bool = False
    framework: bool = False
    options: str | tuple[str, ...] = ""
    precision: int | None = None
    virtual: bool = False
    computed: bool = False


@runtime_checkable
class FrappeFieldMetadataProvider(Protocol):
    """Read-only, site-scoped source of effective metadata."""

    def installed_apps(self, site: str) -> tuple[str, ...]: ...
    def module_definitions(self, site: str) -> tuple[ModuleMetadata, ...]: ...
    def doctypes(self, site: str) -> tuple[DocTypeMetadata, ...]: ...
    def effective_fields(self, site: str, doctype: DocTypeMetadata) -> tuple[EffectiveFieldMetadata, ...]: ...


@dataclass(frozen=True)
class FrappeFieldTypeDescriptor:
    name: str
    family: str
    stored_value: bool
    direct_mapping_candidate: bool
    sensitive: bool = False


@dataclass(frozen=True)
class FrappeDocTypeDescriptor:
    app: str | None
    name: str
    module: str
    custom: bool
    istable: bool
    issingle: bool
    is_virtual: bool
    ownership: OwnershipState
    diagnostics: tuple[RegistryDiagnostic, ...] = ()


@dataclass(frozen=True)
class FrappeFieldDescriptor:
    reference: FrappeFieldReference
    module: str
    label: str
    fieldtype: FrappeFieldTypeDescriptor
    required: bool
    read_only: bool
    hidden: bool
    permlevel: int
    provenance: FieldProvenance
    property_setter_effective: bool
    link_target: str | None
    dynamic_link_field: str | None
    child_table_target: str | None
    select_options: tuple[str, ...]
    precision: int | None
    virtual: bool
    computed: bool
    protected: bool
    mapping_ready: bool
    diagnostics: tuple[RegistryDiagnostic, ...] = ()

    @property
    def identity(self) -> str:
        return self.reference.identity


@dataclass(frozen=True)
class FieldResolution:
    code: ResolutionCode
    reference: str
    field: FrappeFieldDescriptor | None = None
    diagnostics: tuple[RegistryDiagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return self.code == ResolutionCode.RESOLVED


@dataclass(frozen=True)
class FrappeFieldRegistry:
    site: str
    installed_apps: tuple[str, ...]
    doctypes: tuple[FrappeDocTypeDescriptor, ...]
    fields: tuple[FrappeFieldDescriptor, ...]
    diagnostics: tuple[RegistryDiagnostic, ...] = ()
    execution_authority: str = "NONE"

    def resolve(self, reference: str) -> FieldResolution:
        try:
            parsed = FrappeFieldReference.parse(reference)
        except FrappeFieldRegistryError as error:
            return FieldResolution(ResolutionCode.INVALID_REFERENCE, str(reference), diagnostics=(RegistryDiagnostic(ResolutionCode.INVALID_REFERENCE, str(error)),))
        if parsed.app not in self.installed_apps:
            return FieldResolution(ResolutionCode.MISSING_APP, reference, diagnostics=(RegistryDiagnostic(ResolutionCode.MISSING_APP, "Application is not installed on this site."),))
        docs = [item for item in self.doctypes if item.name == parsed.doctype and item.app == parsed.app]
        if not docs:
            unresolved = [item for item in self.doctypes if item.name == parsed.doctype and item.ownership != OwnershipState.RESOLVED]
            if unresolved:
                code = ResolutionCode.AMBIGUOUS_OWNERSHIP if any(item.ownership == OwnershipState.AMBIGUOUS for item in unresolved) else ResolutionCode.UNRESOLVED_OWNERSHIP
                return FieldResolution(code, reference, diagnostics=unresolved[0].diagnostics)
            return FieldResolution(ResolutionCode.MISSING_DOCTYPE, reference, diagnostics=(RegistryDiagnostic(ResolutionCode.MISSING_DOCTYPE, "DocType is not owned by this installed application."),))
        item = next((field for field in self.fields if field.reference == parsed), None)
        if item is None:
            return FieldResolution(ResolutionCode.MISSING_DOCFIELD, reference, diagnostics=(RegistryDiagnostic(ResolutionCode.MISSING_DOCFIELD, "DocField is not in effective metadata."),))
        return FieldResolution(_resolution_code(item), reference, item, item.diagnostics)


_FIELD_TYPES = {
    "Data": ("TEXT", True, True, False), "Small Text": ("TEXT", True, True, False),
    "Long Text": ("TEXT", True, True, False), "Text": ("TEXT", True, True, False),
    "Text Editor": ("TEXT", True, False, False), "Code": ("TEXT", True, False, False),
    "Int": ("NUMBER", True, True, False), "Long Int": ("NUMBER", True, True, False),
    "Float": ("NUMBER", True, True, False), "Currency": ("NUMBER", True, True, False),
    "Percent": ("NUMBER", True, True, False), "Check": ("BOOLEAN", True, True, False),
    "Date": ("DATE_TIME", True, True, False), "Datetime": ("DATE_TIME", True, True, False),
    "Time": ("DATE_TIME", True, False, False), "Duration": ("NUMBER", True, False, False),
    "Select": ("ENUM", True, True, False), "Link": ("REFERENCE", True, False, False),
    "Dynamic Link": ("REFERENCE", True, False, False), "Table": ("CHILD_TABLE", True, False, False),
    "Table MultiSelect": ("CHILD_TABLE", True, False, False), "Attach": ("FILE", True, False, True),
    "Attach Image": ("FILE", True, False, True), "Password": ("SECRET", True, False, True),
}
_NON_DATA = frozenset({"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold", "Image"})
_FRAMEWORK = frozenset({"doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent", "parentfield", "parenttype"})


def build_frappe_field_registry(site: str, provider: FrappeFieldMetadataProvider) -> FrappeFieldRegistry:
    """Build an immutable, discovery-only catalog from a read-only provider."""
    if not isinstance(site, str) or not site:
        return _failure(site if isinstance(site, str) else "", ResolutionCode.MISSING_SITE_CONTEXT, "A non-empty site context is required.")
    if not isinstance(provider, FrappeFieldMetadataProvider):
        return _failure(site, ResolutionCode.METADATA_PROVIDER_FAILURE, "Metadata provider does not implement the read-only protocol.")
    try:
        apps = _unique(provider.installed_apps(site), "installed application")
        modules = tuple(provider.module_definitions(site))
        doctypes = tuple(provider.doctypes(site))
        return _build(site, apps, modules, doctypes, provider)
    except Exception:
        return _failure(site, ResolutionCode.METADATA_PROVIDER_FAILURE, "Metadata provider failed; no partial catalog was retained.")


def _build(site: str, apps: tuple[str, ...], modules: tuple[ModuleMetadata, ...], raw_docs: tuple[DocTypeMetadata, ...], provider: FrappeFieldMetadataProvider) -> FrappeFieldRegistry:
    owners = _owners(modules, set(apps))
    docs: list[FrappeDocTypeDescriptor] = []
    fields: list[FrappeFieldDescriptor] = []
    seen_docs: set[tuple[str, str]] = set()
    seen_fields: set[str] = set()
    for raw in sorted(raw_docs, key=lambda item: (item.module.casefold(), item.name.casefold())):
        _valid_doc(raw)
        if (raw.module, raw.name) in seen_docs:
            raise FrappeFieldRegistryError("Duplicate DocType metadata identity.")
        seen_docs.add((raw.module, raw.name))
        candidates = owners.get(raw.module, ())
        if len(candidates) != 1:
            state = OwnershipState.AMBIGUOUS if len(candidates) > 1 else OwnershipState.UNRESOLVED
            code = ResolutionCode.AMBIGUOUS_OWNERSHIP if state == OwnershipState.AMBIGUOUS else ResolutionCode.UNRESOLVED_OWNERSHIP
            docs.append(FrappeDocTypeDescriptor(None, raw.name, raw.module, raw.custom, raw.istable, raw.issingle, raw.is_virtual, state, (RegistryDiagnostic(code, "Module ownership cannot be resolved to one installed application."),)))
            continue
        doc = FrappeDocTypeDescriptor(candidates[0], raw.name, raw.module, raw.custom, raw.istable, raw.issingle, raw.is_virtual, OwnershipState.RESOLVED)
        docs.append(doc)
        for metadata in sorted(tuple(provider.effective_fields(site, raw)), key=lambda item: item.fieldname.casefold()):
            descriptor = _field(doc, metadata)
            if descriptor.identity in seen_fields:
                raise FrappeFieldRegistryError("Duplicate canonical Frappe field identity.")
            seen_fields.add(descriptor.identity)
            fields.append(descriptor)
    return FrappeFieldRegistry(site, apps, tuple(sorted(docs, key=lambda item: ((item.app or "").casefold(), item.name.casefold()))), tuple(sorted(fields, key=lambda item: item.identity.casefold())))


def _owners(modules: tuple[ModuleMetadata, ...], apps: set[str]) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for item in modules:
        if not isinstance(item, ModuleMetadata) or not item.module or not item.app:
            raise FrappeFieldRegistryError("Module metadata is malformed.")
        if item.app in apps:
            result.setdefault(item.module, set()).add(item.app)
    return {module: tuple(sorted(values, key=str.casefold)) for module, values in result.items()}


def _valid_doc(value: DocTypeMetadata) -> None:
    if not isinstance(value, DocTypeMetadata) or not isinstance(value.name, str) or not value.name or "." in value.name or not isinstance(value.module, str) or not value.module:
        raise FrappeFieldRegistryError("DocType metadata is malformed or ambiguous.")


def _field(doc: FrappeDocTypeDescriptor, value: EffectiveFieldMetadata) -> FrappeFieldDescriptor:
    if not isinstance(value, EffectiveFieldMetadata) or not value.fieldname or "." in value.fieldname:
        raise FrappeFieldRegistryError("DocField metadata is malformed or ambiguous.")
    if not isinstance(value.permlevel, int) or isinstance(value.permlevel, bool) or value.permlevel < 0:
        raise FrappeFieldRegistryError("DocField permlevel must be a non-negative integer.")
    kind = _type(value.fieldtype)
    provenance = FieldProvenance.FRAMEWORK if value.framework else FieldProvenance.CUSTOM if value.custom else FieldProvenance.PROPERTY_SETTER_EFFECTIVE if value.property_setter else FieldProvenance.STANDARD
    options = value.options if isinstance(value.options, str) else ""
    protected = value.fieldname in _FRAMEWORK
    diagnostics: list[RegistryDiagnostic] = []
    if protected:
        diagnostics.append(RegistryDiagnostic(ResolutionCode.PROTECTED_FIELD, "Framework system field is not a mapping target."))
    if doc.istable:
        diagnostics.append(RegistryDiagnostic(ResolutionCode.UNSUPPORTED_FIELD, "Child Table fields require a later child-row resolver."))
    if doc.issingle:
        diagnostics.append(RegistryDiagnostic(ResolutionCode.UNSUPPORTED_FIELD, "Single DocType fields are configuration metadata."))
    if doc.is_virtual or value.virtual or value.computed:
        diagnostics.append(RegistryDiagnostic(ResolutionCode.UNSUPPORTED_FIELD, "Virtual or computed fields are not stored mapping targets."))
    if value.read_only:
        diagnostics.append(RegistryDiagnostic(ResolutionCode.READ_ONLY_FIELD, "Read-only metadata does not permit a mapping target."))
    if not kind.direct_mapping_candidate:
        diagnostics.append(RegistryDiagnostic(ResolutionCode.UNSUPPORTED_FIELD, "Field type requires a later explicit mapping resolver."))
    return FrappeFieldDescriptor(FrappeFieldReference(doc.app or "", doc.name, value.fieldname), doc.module, value.label or value.fieldname, kind, bool(value.reqd), bool(value.read_only), bool(value.hidden), value.permlevel, provenance, bool(value.property_setter), options or None if value.fieldtype == "Link" else None, options or None if value.fieldtype == "Dynamic Link" else None, options or None if value.fieldtype in {"Table", "Table MultiSelect"} else None, _select(value.options) if value.fieldtype == "Select" else (), value.precision if isinstance(value.precision, int) else None, bool(value.virtual), bool(value.computed), protected, not diagnostics, tuple(diagnostics))


def _type(name: str) -> FrappeFieldTypeDescriptor:
    if not isinstance(name, str) or not name:
        raise FrappeFieldRegistryError("DocField fieldtype is required.")
    if name in _NON_DATA:
        return FrappeFieldTypeDescriptor(name, "NON_DATA", False, False)
    return FrappeFieldTypeDescriptor(name, *_FIELD_TYPES.get(name, ("UNKNOWN", False, False, False)))


def _select(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in (value if isinstance(value, tuple) else value.splitlines()) if isinstance(item, str) and item)


def _resolution_code(field: FrappeFieldDescriptor) -> ResolutionCode:
    if field.protected:
        return ResolutionCode.PROTECTED_FIELD
    if field.read_only:
        return ResolutionCode.READ_ONLY_FIELD
    return ResolutionCode.UNSUPPORTED_FIELD if field.diagnostics else ResolutionCode.RESOLVED


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values) or len(values) != len(set(values)):
        raise FrappeFieldRegistryError(f"{label} metadata is malformed.")
    return tuple(sorted(values, key=str.casefold))


def _failure(site: str, code: ResolutionCode, message: str) -> FrappeFieldRegistry:
    return FrappeFieldRegistry(site, (), (), (), (RegistryDiagnostic(code, message),))
