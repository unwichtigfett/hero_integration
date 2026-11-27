"""Execution-free B6 object and field binding registry."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
import re
from threading import RLock
from typing import Protocol

from hero_integration.services.mapping_registry_model import MappingDirection

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,139}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")

NATIVE_DELETED = "ERPNext document deleted."
RETIRED_LINK_SYSTEM = "HERO-Retired"


class BindingRegistryError(ValueError): pass
class BindingConflict(BindingRegistryError): pass


class PendingOutbound(BindingConflict):
    """An external delivery must be verified before its baseline can be used."""

class BindingState(StrEnum):
    INACTIVE = "INACTIVE"
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    SYNCHRONIZED = "SYNCHRONIZED"
    LOCAL_CHANGED = "LOCAL_CHANGED"
    REMOTE_CHANGED = "REMOTE_CHANGED"
    MAPPING_CHANGED = "MAPPING_CHANGED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value): raise BindingRegistryError(f"{label} must be a stable non-secret identifier.")
    return value
def _native_name(value, label):
    if not isinstance(value, str) or not 0 < len(value) <= 140 or value.strip() != value or not value.isprintable():
        raise BindingRegistryError(f"Invalid {label}.")
    return value

def _target_id(value: object, doctype: str) -> str:
    if doctype == "User" and (not isinstance(value, str) or not _USER_ID.fullmatch(value)):
        raise BindingRegistryError("Invalid native User name.")
    return _native_name(value, "native target document name")
def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value): raise BindingRegistryError(f"{label} must be a SHA-256 fingerprint.")
    return value
def _key(kind: str, **parts: object) -> str:
    return sha256(json.dumps({"kind": kind, **parts}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
def state_fingerprint(value: object, *, present: bool) -> str:
    """Typed integrity token; it is not a confidentiality mechanism."""
    try: encoded = json.dumps({"present": present, "value": value if present else None}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error: raise BindingRegistryError("State must be JSON-serializable.") from error
    return sha256(encoded.encode()).hexdigest()

def native_fingerprint(value, fieldtype):
    """Fingerprint a projected or stored scalar in its native field domain."""
    from datetime import date, datetime, time, timedelta
    from decimal import Decimal

    if value is not None:
        if fieldtype == "Check" and value in (0, 1, "0", "1"):
            value = int(value)
        elif fieldtype in {"Date", "Datetime", "Time"}:
            if fieldtype == "Time" and isinstance(value, timedelta) and timedelta() <= value < timedelta(days=1):
                value = (datetime.min + value).time()
            try:
                parsed = {"Date": date, "Datetime": datetime, "Time": time}[fieldtype].fromisoformat(str(value))
            except ValueError as error:
                raise BindingRegistryError("Invalid native temporal field representation.") from error
            if getattr(parsed, "tzinfo", None) is not None:
                raise BindingRegistryError("Native temporal values must use the approved local time conversion.")
            value = str(parsed)
        elif fieldtype in {"Float", "Currency", "Int"} and (type(value) in (float, int, Decimal) or
                isinstance(value, str) and len(value) <= 128 and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value)):
            number = Decimal(str(value))
            if not number.is_finite() or (fieldtype == "Int" and number != number.to_integral_value()):
                raise BindingRegistryError("Invalid value for the native numeric field.")
            if fieldtype == "Int":
                value = int(number)
            else:
                # Retain the existing Frappe float fingerprint when lossless.
                approximate = float(number) if number else 0.0
                if Decimal(str(approximate)) == number:
                    value = approximate
                else:
                    exact = format(number, "f")
                    value = {"decimal": exact.rstrip("0").rstrip(".") if "." in exact else exact}
    return state_fingerprint(value, present=True)


@dataclass(frozen=True, slots=True)
class ObjectBinding:
    identity_key: str; site_scope: str; source_instance: str; object_type: str; external_object_id: str; target_app: str; target_doctype: str; target_name: str; mapping_definition_id: str; mapping_revision: int; revision: int = 1; lifecycle: str = "VALID"; reconciliation: str = ""
    target_incarnation: str = ""
    identity_mapping_revision: int = 0
    @classmethod
    def create(cls, *, site_scope: str, source_instance: str, object_type: str, external_object_id: str, target_app: str, target_doctype: str, target_name: str, mapping_definition_id: str, mapping_revision: int) -> "ObjectBinding":
        key = cls.key_for(site_scope, source_instance, object_type, external_object_id, target_app,
                          target_doctype, target_name, mapping_definition_id, mapping_revision)
        return cls(key, site_scope, source_instance, object_type, external_object_id, target_app,
                   target_doctype, target_name, mapping_definition_id, mapping_revision)

    @staticmethod
    def key_for(site, source, object_type, external, app, doctype, name, definition, revision):
        return _key("hero-object-binding-v1", site=site, source=source, object_type=object_type,
                    external=external, app=app, doctype=doctype, name=name, definition=definition,
                    mapping_revision=revision)

    def incarnation_key(self, revision):
        return _key("hero-object-binding-v2", binding=self.legacy_key(revision), incarnation=self.target_incarnation)

    def canonical_key(self, revision):
        birth = self.identity_mapping_revision or self.mapping_revision
        if self.target_incarnation and self.identity_key == self.incarnation_key(birth):
            return self.incarnation_key(revision)
        return self.legacy_key(revision)

    def legacy_key(self, revision):
        return self.key_for(self.site_scope, self.source_instance, self.object_type, self.external_object_id,
                            self.target_app, self.target_doctype, self.target_name, self.mapping_definition_id, revision)

    def __post_init__(self) -> None:
        for label, value in zip(("identity", "site", "source", "object type", "target app", "mapping definition"), (self.identity_key, self.site_scope, self.source_instance, self.object_type, self.target_app, self.mapping_definition_id)): _id(value, label)
        _native_name(self.external_object_id, "external identity")
        _native_name(self.target_doctype, "native target DocType")
        _target_id(self.target_name, self.target_doctype)
        if not isinstance(self.mapping_revision, int) or self.mapping_revision < 1 or not isinstance(self.revision, int) or self.revision < 1: raise BindingRegistryError("Binding revisions must be positive.")

@dataclass(frozen=True, slots=True)
class FieldBinding:
    identity_key: str; object_binding_key: str; site_scope: str; source_instance: str; source_field: str; target_app: str; target_doctype: str; target_name: str; target_field: str; mapping_definition_id: str; mapping_revision: int; direction: MappingDirection; revision: int = 1; state: BindingState = BindingState.UNACKNOWLEDGED; acknowledged_source_fingerprint: str | None = None; acknowledged_target_fingerprint: str | None = None; acknowledged_version: int = 0; reconciliation: str = ""
    acknowledged_value_json: str = ""
    identity_mapping_revision: int = 0
    @classmethod
    def create(cls, parent: ObjectBinding, *, source_field: str, target_field: str, mapping_definition_id: str, mapping_revision: int, direction: MappingDirection) -> "FieldBinding":
        key = cls.key_for(parent.identity_key, source_field, target_field, mapping_definition_id, mapping_revision, direction)
        return cls(key, parent.identity_key, parent.site_scope, parent.source_instance, source_field, parent.target_app,
                   parent.target_doctype, parent.target_name, target_field, mapping_definition_id, mapping_revision, direction)

    @staticmethod
    def key_for(parent, source, target, definition, revision, direction):
        return _key("hero-field-binding-v1", parent=parent, source_field=source, target_field=target,
                    definition=definition, mapping_revision=revision, direction=direction.value)

    def canonical_key(self, revision):
        return self.key_for(self.object_binding_key, self.source_field, self.target_field,
                            self.mapping_definition_id, revision, self.direction)

    def __post_init__(self) -> None:
        for label, value in zip(("identity", "object binding", "site", "source", "source field", "target app", "target field", "mapping definition"), (self.identity_key, self.object_binding_key, self.site_scope, self.source_instance, self.source_field, self.target_app, self.target_field, self.mapping_definition_id)): _id(value, label)
        _native_name(self.target_doctype, "native target DocType")
        _target_id(self.target_name, self.target_doctype)
        if not isinstance(self.mapping_revision, int) or self.mapping_revision < 1 or not isinstance(self.revision, int) or self.revision < 1 or self.acknowledged_version < 0: raise BindingRegistryError("Field binding revisions are invalid.")
        for value in (self.acknowledged_source_fingerprint, self.acknowledged_target_fingerprint):
            if value is not None: _hash(value, "Acknowledged state")

class BindingStorage(Protocol):
    def object(self, site: str, key: str, *, for_update=False) -> ObjectBinding | None: ...
    def field(self, site: str, key: str, *, for_update=False) -> FieldBinding | None: ...
    def fields_for_object(self, site: str, parent: str) -> tuple[FieldBinding, ...]: ...
    def fields_for_source(self, site: str, source_instance: str) -> tuple[FieldBinding, ...]: ...
    def put_object(self, value: ObjectBinding) -> ObjectBinding: ...
    def put_field(self, value: FieldBinding) -> FieldBinding: ...
    def update_field(self, value: FieldBinding, expected_revision: int) -> FieldBinding: ...

class InMemoryBindingStorage:
    """Deterministic provider; not evidence of MariaDB locking or uniqueness."""
    def __init__(self) -> None: self._lock = RLock(); self._objects = {}; self._fields = {}
    def object(self, site, key, *, for_update=False): return self._objects.get((site, key))
    def field(self, site, key, *, for_update=False): return self._fields.get((site, key))
    def fields_for_object(self, site, parent): return tuple(sorted((v for (s, _), v in self._fields.items() if s == site and v.object_binding_key == parent), key=lambda v: v.identity_key))
    def fields_for_source(self, site, source_instance): return tuple(sorted((v for (s, _), v in self._fields.items() if s == site and v.source_instance == source_instance and self.object(site, v.object_binding_key).lifecycle != "LEGACY"), key=lambda v: v.identity_key))
    def put_object(self, value):
        with self._lock:
            for prior in self._objects.values():
                if prior.site_scope != value.site_scope or prior.target_doctype != value.target_doctype:
                    continue
                same_source = (prior.source_instance, prior.object_type, prior.external_object_id) == (
                    value.source_instance, value.object_type, value.external_object_id)
                if prior.lifecycle == "LEGACY" and prior.reconciliation == NATIVE_DELETED:
                    continue
                if same_source or (prior.lifecycle != "LEGACY" and prior.target_name == value.target_name):
                    return compatible_binding(prior, replace(value, target_incarnation=prior.target_incarnation))
            if (value.site_scope, value.identity_key) in self._objects:
                # Model a new native creation in the deterministic storage provider.
                value = replace(value, target_incarnation=f"memory-{len(self._objects)}")
                value = replace(value, identity_key=value.incarnation_key(value.mapping_revision))
            self._objects[(value.site_scope, value.identity_key)] = value
            return value
    def put_field(self, value):
        with self._lock:
            if self.object(value.site_scope, value.object_binding_key) is None:
                raise BindingRegistryError("Field binding parent does not exist.")
            for prior in self.fields_for_object(value.site_scope, value.object_binding_key):
                if prior.direction == value.direction and prior.target_field == value.target_field:
                    return compatible_binding(prior, value)
            self._fields[(value.site_scope, value.identity_key)] = value
            return value
    def update_field(self, value, expected_revision):
        with self._lock:
            old = self.field(value.site_scope, value.identity_key)
            if old is None or old.revision != expected_revision: raise BindingConflict("Field binding revision is stale.")
            self._fields[(value.site_scope, value.identity_key)] = value; return value

_KEEP_SECTION = object()


class BindingRegistryService:
    def __init__(self, storage: BindingStorage) -> None: self._storage = storage
    def create_or_resolve_object_binding(self, **kwargs) -> ObjectBinding:
        candidate = ObjectBinding.create(**kwargs)
        persisted = self._storage.put_object(candidate)
        return self._compatible(persisted, replace(candidate, target_incarnation=persisted.target_incarnation))
    def create_or_resolve_field_binding(self, parent: ObjectBinding, **kwargs) -> FieldBinding:
        if self._storage.object(parent.site_scope, parent.identity_key, for_update=True) != parent: raise BindingRegistryError("Field binding requires its canonical persisted parent.")
        candidate = FieldBinding.create(parent, **kwargs)
        if (parent.lifecycle != "VALID" or parent.reconciliation or
                (candidate.direction == MappingDirection.HERO_TO_FRAPPE and candidate.mapping_revision != parent.mapping_revision)):
            raise BindingConflict("Field binding requires a current parent and mapping.")
        return self._compatible(self._storage.put_field(candidate), candidate)
    @staticmethod
    def _compatible(old, candidate):
        return compatible_binding(old, candidate)
    def get_object_binding(self, site_scope: str, identity_key: str): return self._storage.object(site_scope, identity_key)
    def get_field_bindings(self, site_scope: str, object_binding_key: str): return self._storage.fields_for_object(site_scope, object_binding_key)
    def get_field_bindings_for_source(self, site_scope: str, source_instance: str): return self._storage.fields_for_source(site_scope, source_instance)
    def get_inbound_field_bindings_for_source(self, site_scope: str, source_instance: str): return tuple(binding for binding in self._storage.fields_for_source(site_scope, source_instance) if binding.direction is MappingDirection.HERO_TO_FRAPPE)
    def record_verified_acknowledgment(self, binding: FieldBinding, *, expected_revision: int, mapping_definition_id: str, mapping_revision: int, source_fingerprint: str, target_fingerprint: str, evidence_id: str, version: int, value_json: str = "", task_section=_KEEP_SECTION) -> FieldBinding:
        _id(evidence_id, "acknowledgment evidence")
        if mapping_definition_id != binding.mapping_definition_id or mapping_revision != binding.mapping_revision: raise BindingRegistryError("Acknowledgment does not bind the current mapping.")
        _hash(source_fingerprint, "source acknowledgment"); _hash(target_fingerprint, "target acknowledgment")
        if expected_revision != binding.revision or version < binding.acknowledged_version: raise BindingConflict("Acknowledgment is stale.")
        if version == binding.acknowledged_version and (binding.acknowledged_source_fingerprint, binding.acknowledged_target_fingerprint) not in {(None, None), (source_fingerprint, target_fingerprint)}: raise BindingConflict("Acknowledgment conflicts with recorded evidence.")
        parent = self._storage.object(binding.site_scope, binding.object_binding_key, for_update=True)
        if (not parent or parent.lifecycle != "VALID" or parent.reconciliation
                or binding.direction == MappingDirection.HERO_TO_FRAPPE and parent.mapping_revision != mapping_revision):
            raise BindingConflict("The parent binding cannot acknowledge a field.")
        if version == binding.acknowledged_version and binding.state == BindingState.SYNCHRONIZED:
            current = self._storage.field(binding.site_scope, binding.identity_key, for_update=True)
            if current != binding:
                raise BindingConflict("Acknowledgment was superseded.")
            return current
        from hero_integration.services.task_hierarchy import confirmed, pack
        section = confirmed(binding) if task_section is _KEEP_SECTION else task_section
        if section:
            value_json = pack(json.loads(value_json) if value_json else None, section)
        return self._storage.update_field(replace(binding, revision=binding.revision + 1, state=BindingState.SYNCHRONIZED, acknowledged_source_fingerprint=source_fingerprint, acknowledged_target_fingerprint=target_fingerprint, acknowledged_version=version, acknowledged_value_json=value_json, reconciliation=""), expected_revision)
    @staticmethod
    def change_state(binding: FieldBinding, *, active_mapping_revision: int | None, mapping_available: bool, source_fingerprint: str | None, target_fingerprint: str | None) -> BindingState:
        if binding.state == BindingState.INACTIVE: return BindingState.INACTIVE
        if not mapping_available: return BindingState.RECONCILIATION_REQUIRED
        if active_mapping_revision != binding.mapping_revision: return BindingState.MAPPING_CHANGED
        if binding.acknowledged_source_fingerprint is None or binding.acknowledged_target_fingerprint is None: return BindingState.UNACKNOWLEDGED
        if source_fingerprint != binding.acknowledged_source_fingerprint and target_fingerprint != binding.acknowledged_target_fingerprint: return BindingState.RECONCILIATION_REQUIRED
        if source_fingerprint != binding.acknowledged_source_fingerprint: return BindingState.REMOTE_CHANGED
        if target_fingerprint != binding.acknowledged_target_fingerprint: return BindingState.LOCAL_CHANGED
        return BindingState.SYNCHRONIZED

    @staticmethod
    def inbound_reconciliation(binding: FieldBinding, *, active_mapping_revision: int | None, source_fingerprint: str | None, target_fingerprint: str) -> str:
        """Classify normalized inbound values without changing state or acknowledgments.

        Fingerprints must use the same active mapping. None means omitted, not NULL;
        unknown null semantics must be held. The writer owns the atomic write and ack.
        """
        if binding.state == BindingState.INACTIVE:
            raise BindingConflict("An inactive field cannot be reconciled.")
        if binding.direction is not MappingDirection.HERO_TO_FRAPPE:
            raise BindingRegistryError("Inbound comparison requires an inbound field binding.")
        if active_mapping_revision != binding.mapping_revision:
            return "MAPPING_CHANGED"
        return BindingRegistryService.compare_inbound_values(source_fingerprint, target_fingerprint,
            binding.acknowledged_source_fingerprint, binding.acknowledged_target_fingerprint)

    @staticmethod
    def compare_inbound_values(source_fingerprint, target_fingerprint, source_base, target_base) -> str:
        """Compare values after the caller has verified the binding and its mapping."""
        if source_fingerprint is None:
            return "OMITTED"
        _hash(source_fingerprint, "source state")
        _hash(target_fingerprint, "target state")
        if source_base is None or target_base is None:
            return "UNACKNOWLEDGED"
        if source_fingerprint == target_fingerprint:
            return "CONFIRM_EQUAL"
        if source_fingerprint == source_base:
            return "UNCHANGED" if target_fingerprint == target_base else "KEEP_LOCAL"
        if target_fingerprint == target_base:
            return "APPLY_SOURCE"
        return "CONFLICT"


class FrappeBindingStorage:
    """Narrow source-only adapter; it uses no DDL, commits, or public API."""
    OBJECT = "HERO Object Binding"
    FIELD = "HERO Field Binding"
    def __init__(self, frappe_module=None) -> None: self._frappe_module = frappe_module
    def _frappe(self):
        if self._frappe_module is not None: return self._frappe_module
        import frappe
        return frappe
    def _scope(self, site):
        frappe = self._frappe()
        if getattr(getattr(frappe, "local", None), "site", None) != site: raise BindingRegistryError("Binding access cannot cross site scope.")
        return frappe
    def _write(self, frappe, doctype, permission="write"):
        user = getattr(getattr(frappe, "session", None), "user", None)
        if user != "Administrator" and "System Manager" not in frappe.get_roles(): raise PermissionError("System Manager or Administrator authority is required.")
        frappe.has_permission(doctype, permission, throw=True)
    def object(self, site, key, *, for_update=False):
        frappe = self._scope(site)
        rows = frappe.db.get_values(self.OBJECT, {"site_scope": site, "identity_key": key}, "*",
                                    as_dict=True, limit=2, for_update=for_update)
        if len(rows) > 1:
            raise BindingConflict("Object binding identity is not unique.")
        return self._object(rows[0]) if rows else None

    def field(self, site, key, *, for_update=False):
        frappe = self._scope(site)
        rows = frappe.db.get_values(self.FIELD, {"site_scope": site, "identity_key": key}, "*",
                                    as_dict=True, limit=2, for_update=for_update)
        if len(rows) > 1:
            raise BindingConflict("Field binding identity is not unique.")
        return self._field(rows[0]) if rows else None

    def fields_for_object(self, site, parent, *, for_update=False):
        frappe = self._scope(site)
        rows = frappe.db.get_values(self.FIELD, {"site_scope": site, "object_binding": parent}, "*",
                                    as_dict=True, order_by="identity_key asc", for_update=for_update)
        return tuple(self._field(row) for row in rows)
    def fields_for_source(self, site, source_instance):
        frappe = self._scope(site)
        rows = frappe.get_all(self.FIELD, filters={"site_scope": site, "source_instance": source_instance,
                              "sync_state": ["!=", BindingState.INACTIVE.value]}, fields=["*"], order_by="identity_key asc")
        return tuple(self._field(row) for row in rows)
    def fields_for_target(self, site, source, doctype, name, field):
        frappe = self._scope(site)
        rows = frappe.get_all(self.FIELD, filters={"site_scope": site, "source_instance": source,
            "target_doctype": doctype, "target_name": name, "target_field": field}, fields=["*"], limit_page_length=3)
        return tuple(self._field(row) for row in rows)


    def target_claim(self, site, doctype, name, *, for_update=False):
        rows = self._scope(site).db.get_values(self.OBJECT, {"site_scope": site, "target_doctype": doctype,
            "target_name": name}, "*", as_dict=True, limit=2, for_update=for_update)
        if len(rows) > 1:
            raise BindingConflict("Multiple object bindings claim this identity or destination.")
        return self._object(rows[0]) if rows else None


    def lock_source_object(self, site, source, object_type, doctype, identity):
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
        frappe = self._scope(site)
        scope = [site, source, object_type, doctype]
        _lock_active_target("object-binding", "source", _key("source", scope=scope, identifier=identity))
        rows = frappe.db.sql("""SELECT * FROM `tabHERO Object Binding`
            WHERE site_scope=%s AND source_instance=%s AND object_type=%s AND target_doctype=%s
            AND external_object_id=%s AND BINARY external_object_id=BINARY %s
            AND NOT (lifecycle <=> 'LEGACY' AND reconciliation <=> %s)
            LIMIT 2 FOR UPDATE""", (*scope, identity, identity, NATIVE_DELETED), as_dict=True)
        if len(rows) > 1:
            raise BindingConflict("Multiple object bindings claim this source identity.")
        return self._object(rows[0]) if rows else None

    def put_object(self, value):
        frappe = self._scope(value.site_scope)
        self._write(frappe, self.OBJECT, "create")
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
        scope = [value.site_scope, value.source_instance, value.object_type, value.target_doctype]
        _lock_active_target("object-binding", "source", _key("source", scope=scope, identifier=value.external_object_id))
        _lock_native_target(value.site_scope, value.target_doctype, value.target_name)
        native = frappe.db.get_value(value.target_doctype, value.target_name, ["name", "creation"], as_dict=True, for_update=True)
        if not native or native.name != value.target_name:
            raise BindingConflict("Binding requires the exact current native document identity.")
        value = replace(value, target_incarnation=incarnation(native.creation))
        rows = frappe.db.sql("""SELECT * FROM `tabHERO Object Binding`
            WHERE site_scope=%s AND target_doctype=%s AND
            ((target_name=%s AND lifecycle != 'LEGACY') OR
             (source_instance=%s AND object_type=%s AND external_object_id=%s AND BINARY external_object_id=BINARY %s
              AND NOT (lifecycle <=> 'LEGACY' AND reconciliation <=> %s)))
            LIMIT 2 FOR UPDATE""", (value.site_scope, value.target_doctype, value.target_name,
                value.source_instance, value.object_type, value.external_object_id, value.external_object_id, NATIVE_DELETED), as_dict=True)
        if len(rows) > 1:
            raise BindingConflict("Multiple object bindings claim this identity or destination.")
        if rows:
            return compatible_binding(self._object(rows[0]), value)
        value = replace(value, identity_key=value.incarnation_key(value.mapping_revision))
        try:
            frappe.get_doc({"doctype": self.OBJECT, "identity_key": value.identity_key, "site_scope": value.site_scope, "source_instance": value.source_instance, "object_type": value.object_type, "external_object_id": value.external_object_id, "target_app": value.target_app, "target_doctype": value.target_doctype, "target_name": value.target_name, "target_incarnation": value.target_incarnation, "mapping_definition_id": value.mapping_definition_id, "mapping_revision": value.mapping_revision, "binding_revision": value.revision, "lifecycle": value.lifecycle, "reconciliation": value.reconciliation}).insert()
            return value
        except Exception as error:
            return self._after_duplicate(error, lambda: self.object(value.site_scope, value.identity_key), value)
    def put_field(self, value):
        frappe = self._scope(value.site_scope); self._write(frappe, self.FIELD, "create")
        parent = self._object(frappe.get_doc(self.OBJECT, value.object_binding_key, for_update=True))
        if parent.site_scope != value.site_scope or parent.lifecycle != "VALID" or parent.reconciliation:
            raise BindingConflict("Field binding parent is invalid.")
        if (value.source_instance, value.target_app, value.target_doctype, value.target_name) != (parent.source_instance, parent.target_app, parent.target_doctype, parent.target_name): raise BindingRegistryError("Field binding does not match its object binding identity.")
        rows = frappe.db.get_values(self.FIELD, {"site_scope": value.site_scope, "object_binding": parent.identity_key,
            "direction": value.direction.value, "target_field": value.target_field}, "*", as_dict=True, limit=2, for_update=True)
        if len(rows) > 1:
            raise BindingConflict("Multiple field bindings claim this destination.")
        if rows:
            return compatible_binding(self._field(rows[0]), value)
        try:
            frappe.get_doc({"doctype": self.FIELD, "identity_key": value.identity_key, "object_binding": value.object_binding_key, "site_scope": value.site_scope, "source_instance": value.source_instance, "source_field": value.source_field, "target_app": value.target_app, "target_doctype": value.target_doctype, "target_name": value.target_name, "target_field": value.target_field, "mapping_definition_id": value.mapping_definition_id, "mapping_revision": value.mapping_revision, "direction": value.direction.value, "binding_revision": value.revision, "sync_state": value.state.value, "acknowledged_source_fingerprint": value.acknowledged_source_fingerprint, "acknowledged_target_fingerprint": value.acknowledged_target_fingerprint, "acknowledged_version": value.acknowledged_version, "reconciliation": value.reconciliation}).insert()
            return value
        except Exception as error:
            return self._after_duplicate(error, lambda: self.field(value.site_scope, value.identity_key), value)
    def update_field(self, value, expected_revision):
        frappe = self._scope(value.site_scope); self._write(frappe, self.FIELD)
        parent = self.object(value.site_scope, value.object_binding_key, for_update=True)
        if not parent or parent.lifecycle != "VALID" or parent.reconciliation:
            raise BindingConflict("The parent binding cannot acknowledge a field.")
        doc = frappe.get_doc(self.FIELD, value.identity_key, for_update=True)
        if doc.site_scope != value.site_scope or int(doc.binding_revision) != expected_revision:
            raise BindingConflict("Field binding revision is stale.")
        for key, item in {"binding_revision": value.revision, "sync_state": value.state.value, "acknowledged_source_fingerprint": value.acknowledged_source_fingerprint, "acknowledged_target_fingerprint": value.acknowledged_target_fingerprint, "acknowledged_version": value.acknowledged_version, "acknowledged_value_json": value.acknowledged_value_json, "reconciliation": value.reconciliation}.items(): setattr(doc, key, item)
        doc.save(); return value
    @staticmethod
    def _object(doc): return ObjectBinding(doc.identity_key, doc.site_scope, doc.source_instance, doc.object_type, doc.external_object_id, doc.target_app, doc.target_doctype, doc.target_name, doc.mapping_definition_id, int(doc.mapping_revision), int(doc.binding_revision), doc.lifecycle or "VALID", doc.reconciliation or "", getattr(doc,"target_incarnation","") or "", int(getattr(doc, "identity_mapping_revision", 0) or 0))
    @staticmethod
    def _field(doc): return FieldBinding(doc.identity_key, doc.object_binding, doc.site_scope, doc.source_instance, doc.source_field, doc.target_app, doc.target_doctype, doc.target_name, doc.target_field, doc.mapping_definition_id, int(doc.mapping_revision), MappingDirection(doc.direction), int(doc.binding_revision), BindingState(doc.sync_state or BindingState.UNACKNOWLEDGED), doc.acknowledged_source_fingerprint or None, doc.acknowledged_target_fingerprint or None, int(doc.acknowledged_version or 0), doc.reconciliation or "", getattr(doc,"acknowledged_value_json","") or "", int(getattr(doc, "identity_mapping_revision", 0) or 0))
    @staticmethod
    def _after_duplicate(error, reread, candidate):
        """Only Frappe's explicit duplicate exception is safe to retry by reread."""
        if error.__class__.__name__ != "DuplicateEntryError": raise error
        existing = reread()
        if existing is None: raise BindingConflict("Duplicate insertion did not expose a canonical binding.") from error
        return compatible_binding(existing, candidate)


def compatible_binding(existing, candidate):
    """Resolve immutable identity without resetting acknowledgment or lifecycle state."""
    if type(existing) is not type(candidate):
        raise BindingConflict("Canonical binding identity collides with incompatible state.")
    if isinstance(existing, ObjectBinding) and (existing.lifecycle != "VALID" or existing.reconciliation):
        raise BindingConflict("Object binding requires explicit lifecycle reconciliation.")
    mutable = ("revision",) if isinstance(candidate, ObjectBinding) else (
        "revision", "state", "acknowledged_source_fingerprint", "acknowledged_target_fingerprint",
        "acknowledged_version", "reconciliation", "acknowledged_value_json",
    )
    if (isinstance(existing, ObjectBinding) and existing.target_incarnation
            and existing.identity_key == existing.incarnation_key(existing.identity_mapping_revision or existing.mapping_revision)
            and candidate.identity_key == candidate.legacy_key(candidate.mapping_revision)):
        candidate = replace(candidate, identity_key=candidate.incarnation_key(candidate.mapping_revision))
    if existing.identity_mapping_revision and not candidate.identity_mapping_revision:
        birth = existing.identity_mapping_revision
        if (not 0 < birth < existing.mapping_revision or existing.identity_key != candidate.canonical_key(birth)
                or candidate.identity_key != candidate.canonical_key(candidate.mapping_revision)):
            raise BindingConflict("The original binding identity cannot be verified.")
        candidate = replace(candidate, identity_key=existing.identity_key, identity_mapping_revision=birth)
    identity_only = replace(existing, **{name: getattr(candidate, name) for name in mutable})
    if identity_only != candidate:
        from dataclasses import fields
        differing = [field.name for field in fields(candidate)
                     if getattr(identity_only, field.name) != getattr(candidate, field.name)]
        raise BindingConflict("Canonical binding identity has incompatible fields: " + ", ".join(differing))
    return existing


def incarnation(creation):
    from frappe.utils import get_datetime

    if not creation:
        raise BindingConflict("Native document incarnation is missing.")
    return get_datetime(creation).isoformat(timespec="microseconds")


def matches_incarnation(stored, creation):
    return bool(stored and creation and stored == incarnation(creation))


def verify_incarnation(parent, doc):
    if (parent.lifecycle != "VALID" or parent.target_doctype != doc.doctype or parent.target_name != doc.name
            or not matches_incarnation(parent.target_incarnation, doc.creation)):
        raise BindingConflict("Binding does not prove the current native document incarnation.")


def _lock_native_target(site, doctype, name):
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    _lock_active_target("object-binding", "target", _key("target", site=site, doctype=doctype, name=name))


def hold_object(parent, reason, *, deleted=False):
    """Record a proven lifecycle transition within the caller's transaction."""
    import frappe
    current = frappe.get_doc("HERO Object Binding", parent.identity_key, for_update=True)
    if current.lifecycle == "LEGACY" and (reason != NATIVE_DELETED or current.reconciliation == NATIVE_DELETED):
        return
    if current.site_scope != frappe.local.site or int(current.binding_revision) != parent.revision:
        raise BindingConflict("Object binding changed during its lifecycle transition.")
    frappe.db.set_value("HERO Object Binding", current.name, {
        "lifecycle": "LEGACY" if deleted else "REVIEW", "reconciliation": reason,
        "binding_revision": parent.revision + 1})
    if deleted:
        frappe.db.sql("""UPDATE `tabHERO Field Binding` SET sync_state='INACTIVE', reconciliation=%s,
            binding_revision=binding_revision+1 WHERE object_binding=%s AND NOT (sync_state <=> 'INACTIVE')""",
            (reason, current.name))


def native_target_lifecycle(doc, method=None, old=None, new=None, merge=False):
    """A native delete retires claims atomically. A bound rename needs explicit reconciliation."""
    import frappe
    if not frappe.db.table_exists("HERO Object Binding"):
        return
    name = old or doc.name
    # Native deletion/rename already owns the target row. Query the same current
    # claims as admission; absent registrations must not affect other Frappe apps.
    claims = frappe.db.sql("""SELECT * FROM `tabHERO Object Binding`
        WHERE site_scope=%s AND target_doctype=%s AND target_name=%s
          AND (lifecycle != 'LEGACY' OR (lifecycle='LEGACY' AND reconciliation='DELETED'
               AND target_incarnation=%s)) FOR UPDATE""",
        (frappe.local.site, doc.doctype, name, incarnation(doc.creation) if doc.creation else ""), as_dict=True)
    if method == "before_rename":
        if any(row.lifecycle != "LEGACY" for row in claims):
            raise BindingConflict("Renaming or merging this bound document requires identity reconciliation.")
        return
    for row in claims:
        hold_object(FrappeBindingStorage._object(row), NATIVE_DELETED, deleted=True)
    if frappe.db.table_exists("HERO Integration Link"):
        frappe.db.sql("""UPDATE `tabHERO Integration Link` SET source_system=%s,
            source_key=CONCAT('retired:', name), sync_status='Source Deleted',
            conflict_detail='ERPNext document deleted; historical link.'
            WHERE source_system='HERO' AND target_doctype=%s AND target_name=%s""",
            (RETIRED_LINK_SYSTEM, doc.doctype, name))
