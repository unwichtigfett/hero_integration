"""Field-level, redacted comparison state for a linked HERO target."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Mapping

_managed_target = ContextVar("hero_integration_managed_target", default=())


@contextmanager
def record_transaction_callbacks(db):
    """Isolate native hooks registered by one record, including nested savepoints."""
    names = ("before_commit", "after_commit", "before_rollback", "after_rollback")
    outer = {name: getattr(db, name, None) for name in names}
    if not all(callable(getattr(group, "run", None)) for group in outer.values()):
        yield lambda **kwargs: db.rollback(**kwargs)  # Database adapter without Frappe hooks.
        return
    from frappe.utils import CallbackManager
    inner = {name: CallbackManager() for name in names}
    transfer = names
    def rollback(*, save_point):
        nonlocal transfer
        transfer = ("before_rollback", "after_rollback")
        inner["before_rollback"].run()
        db.rollback(save_point=save_point)
        inner["after_rollback"].run()
        transfer = ()
    try:
        for name in names:
            setattr(db, name, inner[name])
        yield rollback
    except BaseException:
        if transfer:
            transfer = ("before_rollback", "after_rollback")
        raise
    finally:
        for name in names:
            setattr(db, name, outer[name])
        for name in transfer:
            if len(inner[name]):
                outer[name].add(inner[name].run)


@contextmanager
def managed_target_update(doctype: str, name: str, link_name: str):
    """Assign one inbound writer ownership of one link for one target save."""
    token = _managed_target.set((*_managed_target.get(), (doctype, name, link_name)))
    try:
        yield
    finally:
        _managed_target.reset(token)


def managed_target_update_active(doctype: str, name: str | None) -> bool:
    return any(current[0] == doctype and current[1] in (None, name) for current in _managed_target.get())


def protect_external_task_deadline(doc, method=None) -> None:
    """Only the guarded inbound adapter may change the separate HERO deadline."""
    del method
    import frappe
    from frappe.utils import get_datetime

    if frappe.flags.in_migrate or managed_target_update_active('Task', doc.name):
        return
    current = doc.get('custom_hero_due_date')
    previous = doc.get_doc_before_save()
    old = previous.get('custom_hero_due_date') if previous else None
    normalize = lambda value: get_datetime(value) if value else None
    if normalize(current) != normalize(old):
        frappe.throw(frappe._('The external HERO deadline is managed by HERO Integration.'), frappe.PermissionError)


def protect_employee_source_observations(doc, method=None) -> None:
    """HERO observations are written by the inbound adapter, not local saves."""
    del method
    import frappe
    from hero_integration.services.employee_mapping import OBSERVATIONS

    if frappe.flags.in_migrate or managed_target_update_active(doc.doctype, doc.name):
        return
    previous = doc.get_doc_before_save()
    for field, _label in OBSERVATIONS.get(doc.doctype, ()):
        old = previous.get(field) if previous else None
        if not _same_mapped_value(doc.get(field) or None, old or None):
            frappe.throw(frappe._('HERO source observations are managed by HERO Integration.'), frappe.PermissionError)


def field_hashes(values: Mapping[str, object]) -> dict[str, str]:
    """Hash normalised field values without retaining unrestricted payloads."""

    return {
        str(field): hashlib.sha256(
            json.dumps(check_value(value), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        for field, value in sorted(values.items())
    }


def check_value(value: object) -> object:
    """A Check field is False/True on a new document but 0/1 once read back; compare both alike."""
    return int(value) if isinstance(value, bool) else value


def migration_storage_value(doc, field, value):
    """Project a blank migration value through Frappe's numeric column coercion."""
    if value is not None and value != "":
        return value
    meta = getattr(doc, "meta", None)
    df = meta.get_field(field) if meta else None
    if not df or getattr(df, "is_virtual", False):
        return value
    from frappe.utils import cint, flt
    if df.fieldtype in {"Int", "Check"}:
        return cint(value)
    if df.fieldtype in {"Float", "Currency", "Percent"}:
        return flt(value)
    return value


def native_table_rows(doc, field):
    """Ordered persisted business columns of a native child table, excluding row metadata."""
    meta = getattr(doc, "meta", None)
    column = meta.get_field(field) if meta else None
    if not column or column.fieldtype != "Table":
        return None
    import frappe
    child = frappe.get_meta(column.options)
    stored = set(child.get_valid_columns())
    fields = [df.fieldname for df in child.fields if df.fieldname in stored]
    return [{key: row.get(key) for key in fields} for row in doc.get(field) or []]


def mapped_table_matches(doc, field, proposed, *, complete=False):
    native_rows = native_table_rows(doc, field)
    if native_rows is None or not isinstance(proposed, (tuple, list)) or len(native_rows) != len(proposed):
        return False
    return all(isinstance(mapped, dict)
               and (native.keys() == mapped.keys() if complete else mapped.keys() <= native.keys())
               and all(_same_table_value(child, key, native[key], value)
                       for key, value in mapped.items())
               for native, child, mapped in zip(native_rows, doc.get(field) or [], proposed))


def _same_table_value(child, field, current, incoming):
    """Recognize only a projection into the actual fixed-point storage column."""
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
    import re

    projected = migration_storage_value(child, field, incoming)
    if _same_mapped_value(current, projected):
        return True
    numeric = (int, float, Decimal)
    if type(current) not in numeric or type(projected) not in numeric:
        return False
    column = child.meta.get_field(field)
    if not column or column.fieldtype not in {"Float", "Currency", "Percent"}:
        return False
    import frappe
    if frappe.db.db_type != "mariadb" or not getattr(child, "doctype", None):
        return False
    sql_type = frappe.db.get_column_type(child.doctype, field)
    spec = re.fullmatch(r"decimal\((\d+),(\d+)\)", sql_type or "", re.IGNORECASE)
    if not spec:
        return False
    precision, scale = map(int, spec.groups())
    with localcontext() as ctx:
        ctx.prec = precision
        quantum = Decimal(1).scaleb(-scale)
        try:
            source, stored = Decimal(str(projected)), Decimal(str(current))
            if not source.is_finite() or not stored.is_finite() or abs(source) >= Decimal(10) ** (precision - scale):
                return False
            rounded = source.quantize(quantum, rounding=ROUND_HALF_UP)
            if isinstance(projected, float):
                # A binary float close to a rounding tie can be serialized differently by drivers.
                if Decimal.from_float(projected).quantize(quantum, rounding=ROUND_HALF_UP) != rounded:
                    return False
            return stored == rounded
        except InvalidOperation:
            return False


def _same_mapped_value(current, incoming):
    """Compare native storage representations without treating text as numbers."""
    from datetime import date, datetime
    from decimal import Decimal, InvalidOperation
    from numbers import Number

    current, incoming = check_value(current), check_value(incoming)
    if isinstance(current, (Number, Decimal)) or isinstance(incoming, (Number, Decimal)):
        if not (isinstance(current, (Number, Decimal)) and isinstance(incoming, (Number, Decimal))):
            return False
        try:
            left, right = Decimal(str(current)), Decimal(str(incoming))
            return left.is_finite() and right.is_finite() and left == right
        except (InvalidOperation, ValueError):
            return False
    if field_hashes({'value': current}) == field_hashes({'value': incoming}):
        return True
    if isinstance(current, (date, datetime)) or isinstance(incoming, (date, datetime)):
        try:
            if isinstance(current, datetime) or isinstance(incoming, datetime):
                left = current if isinstance(current, datetime) else datetime.fromisoformat(str(current))
                right = incoming if isinstance(incoming, datetime) else datetime.fromisoformat(str(incoming))
            else:
                left = current if isinstance(current, date) else date.fromisoformat(str(current))
                right = incoming if isinstance(incoming, date) else date.fromisoformat(str(incoming))
            return left == right
        except (TypeError, ValueError):
            return False
    return False


def new_mapped_field_conflicts(doc, values, knownfields) -> set[str]:
    """New ownership never overwrites a differing occupied native field.

    Existing baseline keys remain the responsibility of the normal three-way
    comparison. Zero and False are occupied values, not missing defaults.
    """
    known = set(knownfields)
    return {field for field, incoming in values.items() if field not in known
            and (current := doc.get(field)) is not None and current != ''
            and not _same_mapped_value(current, incoming)}


def native_values_changed(before: Mapping[str, object], after: Mapping[str, object]) -> bool:
    """Compare owned native values, including child rows, before deciding to save."""
    return set(before) != set(after) or any(not _same_mapped_value(before[key], after[key]) for key in before)


def encode(hashes: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(hashes.items())), sort_keys=True, separators=(",", ":"))


def decode(value: object) -> dict[str, str] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in decoded.items()):
        return None
    return decoded


def changed_fields(previous: Mapping[str, str], current: Mapping[str, str]) -> set[str]:
    return {
        field for field in set(previous) | set(current)
        if previous.get(field) != current.get(field)
    }


EMPTY_HASH = field_hashes({"value": None})["value"]


@dataclass(frozen=True)
class Reconciliation:
    """Field-wise three-way outcome against the last acknowledged baseline.

    ``apply`` may be written to Frappe; ``local`` keeps the local value and its
    old baseline; ``converged`` and ``acknowledged`` (HERO cleared a value that
    the no-clear policy does not write) advance the baseline without a write.
    """

    apply: frozenset[str]
    local: frozenset[str]
    conflicts: frozenset[str]
    converged: frozenset[str]
    acknowledged: frozenset[str]
    unchanged: frozenset[str]

    @property
    def advances_baseline(self) -> bool:
        return bool(self.apply or self.converged or self.acknowledged)


def reconcile(
    prior_source: Mapping[str, str],
    prior_target: Mapping[str, str],
    source: Mapping[str, str],
    target: Mapping[str, str],
    *,
    tracked: set[str] | frozenset[str],
    missing: set[str] | frozenset[str] = frozenset(),
    source_empty: set[str] | frozenset[str] = frozenset(),
    target_empty: set[str] | frozenset[str] = frozenset(),
    unwritable: set[str] | frozenset[str] = frozenset(),
) -> Reconciliation:
    """Classify tracked fields against acknowledged baselines.

    Absent fields preserve baselines. Without a baseline, only nonempty values
    count as changes. HERO changes blocked by the no-clear policy are acknowledged
    without writing.
    """

    groups: dict[str, set[str]] = {name: set() for name in ("apply", "local", "conflicts", "converged", "acknowledged", "unchanged")}
    for field in sorted(set(tracked) - set(missing)):
        current_source = source.get(field, EMPTY_HASH)
        current_target = target.get(field, EMPTY_HASH)
        # Both maps describe one common acknowledged value.  When only one side
        # recorded a field (older links, relation snapshots), use it for both.
        base_source = prior_source.get(field, prior_target.get(field))
        base_target = prior_target.get(field, prior_source.get(field))
        source_changed = base_source != current_source if base_source is not None else field not in source_empty
        target_changed = base_target != current_target if base_target is not None else field not in target_empty
        if not source_changed and not target_changed:
            groups["unchanged"].add(field)
        elif source_changed and target_changed:
            groups["converged" if current_source == current_target else "conflicts"].add(field)
        elif source_changed:
            groups["acknowledged" if field in unwritable else "apply"].add(field)
        else:
            groups["local"].add(field)
    return Reconciliation(**{name: frozenset(fields) for name, fields in groups.items()})


def next_baseline(
    prior: Mapping[str, str],
    current: Mapping[str, str],
    outcome: Reconciliation,
    *,
    tracked: set[str] | frozenset[str],
) -> dict[str, str]:
    """Advance only reconciled fields; local-only and missing fields keep theirs."""

    advanced = outcome.apply | outcome.converged | outcome.acknowledged | outcome.unchanged
    result = {field: value for field, value in prior.items() if field in tracked and field not in advanced}
    for field in advanced:
        result[field] = current.get(field, EMPTY_HASH)
    return dict(sorted(result.items()))


def _managed_owner(doc, owner):
    return any(kind == doc.doctype and name in (None, doc.name) and current in (None, owner)
               for kind, name, current in _managed_target.get())


def _observe_bound_changes(frappe, doc, previous):
    """Capture a native save once per bound object in the document transaction."""
    from hero_integration.services.binding_registry import incarnation, native_fingerprint

    if not getattr(doc, "creation", None):
        return {}  # Singles have no native row incarnation to bind.
    parent = frappe.qb.DocType("HERO Object Binding")
    field = frappe.qb.DocType("HERO Field Binding")
    rows = (frappe.qb.from_(parent).join(field).on(field.object_binding == parent.name)
            .select(parent.name, parent.source_instance, parent.external_object_id, parent.object_type, field.target_field)
            .where((parent.site_scope == frappe.local.site) & (parent.target_doctype == doc.doctype)
                   & (parent.target_name == doc.name) & (parent.lifecycle == "VALID")
                   & (parent.target_incarnation == incarnation(doc.creation)) & (field.site_scope == parent.site_scope)
                   & (field.source_instance == parent.source_instance) & (field.target_name == parent.target_name)
                   & (field.target_doctype == parent.target_doctype))).run(as_dict=True)
    groups = {}
    covered = {}
    for row in rows:
        group = (row.name, row.source_instance, row.external_object_id, row.object_type)
        covered.setdefault((row.source_instance, row.external_object_id), set()).add(row.target_field)
        if _managed_owner(doc, row.name):
            continue
        meta = doc.meta.get_field(row.target_field)
        if meta is None:
            continue
        before = native_fingerprint(previous.get(row.target_field), meta.fieldtype)
        after = native_fingerprint(doc.get(row.target_field), meta.fieldtype)
        if before != after:
            groups.setdefault(group, {})[row.target_field] = (before, after)
    for (binding, instance, identity, kind), changes in groups.items():
        record_local_transition(doc, binding, instance, identity, kind, changes)
    return covered


def record_local_transition(doc, binding, instance, identity, kind, changes, *, recovered=False):
    """Save the same redacted, idempotent observation from a hook or a bound comparison."""
    from hero_integration.services.integration_events import record_output

    if recovered:
        import frappe
        from frappe.query_builder.functions import Coalesce
        event = frappe.qb.DocType("HERO Integration Record")
        pending = (frappe.qb.from_(event).select(event.native_state_json)
            .where((event.correlation_id == binding) & (event.technical_action == "frappe_change_observed")
                   & Coalesce(event.outbound_status, "").isin(("", "Deferred"))).run(pluck=True))
        remaining = dict(changes)
        for raw in pending:
            observed = json.loads(raw or "{}")
            for field in tuple(remaining):
                if observed.get(field) == remaining[field][1]:
                    del remaining[field]
        changes = remaining
        if not changes:
            return
    key = hashlib.sha256(json.dumps([binding, str(doc.modified), changes], sort_keys=True).encode()).hexdigest()
    record_output(source_type=kind, source_id=identity, source_instance=instance, result="OBSERVED",
                  action="frappe_change_observed", origin="frappe", correlation_id=binding,
                  target_doctype=doc.doctype, target_name=doc.name, changed_fields=sorted(changes),
                  event_id=key, native_state={field: pair[1] for field, pair in changes.items()})


def related_field_key(source):
    return source["doctype"] + "." + source["field"]


def related_document_key(doc):
    return hashlib.sha256(json.dumps([doc.doctype, doc.name, str(doc.creation)]).encode()).hexdigest()


def mapped_observation_hash(doc, field):
    """Fingerprint the native value that the active assignment can actually send."""
    from hero_integration.services.binding_registry import native_fingerprint
    value = doc.get(field)
    fieldtype = doc.meta.get_field(field).fieldtype
    if (doc.doctype, field, fieldtype) == ("Item", "barcodes", "Table"):
        value = [row.barcode for row in value or () if row.barcode]
    return native_fingerprint(value, fieldtype)


def _observe_related_changes(frappe, doc, previous):
    """Refer a saved related field to the existing mapped-update owner for exact readback."""
    from hero_integration.services.binding_registry import incarnation, native_fingerprint
    from hero_integration.services.integration_events import record_output

    if doc.doctype not in {"Address", "Contact", "ToDo", "Item Price", "User"} or managed_target_update_active(doc.doctype, doc.name):
        return
    if doc.doctype in {"Address", "Contact"}:
        primary = "customer_primary_address" if doc.doctype == "Address" else "customer_primary_contact"
        owner = "Customer"
        names = frappe.get_all(owner, filters={primary: doc.name}, pluck="name", limit_page_length=0)
    elif doc.doctype == "User":
        owner = "Employee"
        names = frappe.get_all(owner, filters={"user_id": doc.name}, pluck="name", limit_page_length=0)
    elif doc.doctype == "Item Price":
        owner, names = "Item", [doc.item_code]
    else:
        owner, names = "Task", [doc.reference_name] if doc.reference_type == "Task" else []
    if not names:
        return
    parents = frappe.get_all("HERO Object Binding", filters={"site_scope": frappe.local.site, "lifecycle": "VALID",
        "target_doctype": owner, "target_name": ["in", names]}, fields=["name", "target_name", "target_incarnation",
        "source_instance", "object_type", "external_object_id"], limit_page_length=0)
    for parent in parents:
        if managed_target_update_active(owner, parent.target_name):
            continue
        created = frappe.db.get_value(owner, parent.target_name, "creation")
        if not created or parent.target_incarnation != incarnation(created):
            continue
        mappings = frappe.get_all("HERO GraphQL Mapping", filters={"source_instance": parent.source_instance,
            "direction": "FRAPPE_TO_MUTATION", "status": "Active", "operation_kind": "MUTATION"}, pluck="mapping_json")
        changed = {}
        for raw in mappings:
            profile = json.loads(raw)
            if profile.get("usage") != "MAPPED_UPDATE_V2" or profile.get("source", {}).get("doctype") != owner:
                continue
            for item in profile["assignments"]:
                source = item["source"]
                field = source["field"]
                if source["doctype"] != doc.doctype or not (meta := doc.meta.get_field(field)):
                    continue
                current = native_fingerprint(doc.get(field), meta.fieldtype)
                if previous is None or current != native_fingerprint(previous.get(field), meta.fieldtype):
                    changed[related_field_key(source)] = current
        if not changed:
            continue
        observed = {**changed, "_related_document": related_document_key(doc)}
        event_id = hashlib.sha256(json.dumps([parent.name, str(doc.modified), observed], sort_keys=True).encode()).hexdigest()
        record_output(source_type=parent.object_type, source_id=parent.external_object_id, source_instance=parent.source_instance,
            result="OBSERVED", action="frappe_related_change_observed", origin="frappe", correlation_id=parent.name,
            target_doctype=owner, target_name=parent.target_name, changed_fields=sorted(changed),
            event_id=event_id, native_state=observed)


def recover_related_changes(ctx, prepared, run_id):
    """Observe eligible domain-mapped differences from the approved inventory snapshot.

    The sequence belongs only to detection. It never acknowledges a field, sends
    a mutation, or copies an address, price or assignment value into the ledger.
    """
    import frappe
    from hero_integration.services.integration_events import record_output
    from hero_integration.services.offset_pagination import fingerprint
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    parent = ctx.parent
    if not parent:
        return
    changes = {}
    for entry in prepared.changes:
        doc = ctx.source_documents[entry["id"]]
        field = entry["source_field"]
        if doc.doctype == ctx.document.doctype and field in ctx.fields:
            continue  # The scalar processor already owns this observation.
        identity = related_document_key(doc)
        name = doc.doctype + "." + field
        changes.setdefault(identity, {})[name] = mapped_observation_hash(doc, field)
    ledger = FrappeBoundProcessor.EVENT
    key = fingerprint(["outbound-related-scan", parent.site_scope, parent.identity_key, ctx.mapping.name,
                       ctx.mapping.mapping_revision])
    previous = frappe.db.get_value(ledger, key, ["source_version", "field_changes"], as_dict=True, for_update=True)
    stamp = fingerprint(changes)
    if previous and previous.source_version == stamp or not previous and not changes:
        return
    sequence = json.loads(previous.field_changes)["sequence"] + 1 if previous else 1
    if changes:
        from frappe.query_builder.functions import Coalesce
        event = frappe.qb.DocType("HERO Integration Record")
        pending = (frappe.qb.from_(event).select(event.native_state_json)
            .where((event.correlation_id == parent.identity_key) & (event.technical_action == "frappe_related_change_observed")
                   & Coalesce(event.outbound_status, "").isin(("", "Deferred"))).run(pluck=True))
        pending = [json.loads(raw or "{}") for raw in pending]
        for identity, fields in changes.items():
            missing = {field: value for field, value in fields.items() if not any(
                item.get("_related_document") == identity and item.get(field) == value for item in pending)}
            if missing:
                record_output(source_type=parent.object_type, source_id=parent.external_object_id,
                    source_instance=parent.source_instance, result="OBSERVED", action="frappe_related_change_observed",
                    origin="frappe", correlation_id=parent.identity_key, target_doctype=parent.target_doctype,
                    target_name=parent.target_name, changed_fields=sorted(missing),
                    event_id=fingerprint([key,sequence,identity,missing]), native_state={**missing,"_related_document":identity})
    values = {"run_id":run_id, "source_version":stamp, "field_changes":json.dumps({"sequence":sequence})}
    if previous:
        frappe.db.set_value(ledger, key, values)
    else:
        frappe.get_doc({"doctype":ledger,"event_id":key,"site_scope":parent.site_scope,"source_instance":parent.source_instance,
            "object_type":parent.object_type,"external_id":parent.external_object_id,"object_binding":parent.identity_key,
            "mapping_name":ctx.mapping.name,"mapping_revision":ctx.mapping.mapping_revision,
            "outcome":"NATIVE_OBSERVATION",**values}).insert(ignore_permissions=True)


def native_create_event_id(site, instance, doc, mapping):
    from hero_integration.services.frappe_writeback import object_write_scope
    scope = object_write_scope(site, instance, doc.doctype, doc.name)
    data = ["frappe-create/v1", scope, str(doc.creation), mapping.name, int(mapping.mapping_revision)]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest(), scope


def _observe_unbound_creation(frappe, doc):
    from hero_integration.services.integration_events import record_output
    from hero_integration.services.personal_mapping_content import unique_json_object

    operation = {"Task": "update_task", "Customer": "create_contact", "Project": "create_project_match"}.get(doc.doctype)
    if doc.doctype == "Task" and doc.get("parent_task"):
        return
    if not operation or managed_target_update_active(doc.doctype, doc.name):
        return
    instance = frappe.db.get_single_value("HERO Integration Settings", "hero_source_instance")
    if not instance:
        return
    rows = frappe.get_all("HERO GraphQL Mapping", filters={"source_instance": instance, "operation_kind": "MUTATION",
        "operation_name": operation, "direction": "FRAPPE_TO_MUTATION", "status": "Active"},
        fields=["name", "mapping_revision", "mapping_json"])
    for row in rows:
        try:
            content = json.loads(row.mapping_json, object_pairs_hook=unique_json_object)
        except (TypeError, ValueError):
            continue
        if (not isinstance(content, dict) or content.get("usage") != "SEND_TO_HERO"
                or not isinstance(content.get("send_options"), dict) or content["send_options"].get("mode") != "CREATE"
                or not isinstance(content.get("source"), dict) or content["source"].get("doctype") != doc.doctype):
            continue
        event_id, scope = native_create_event_id(frappe.local.site, instance, doc, row)
        record_output(source_type="Frappe." + doc.doctype, source_id=doc.name, source_instance=instance, result="OBSERVED",
                      action="frappe_create_observed", origin="frappe", correlation_id=scope,
                      target_doctype=doc.doctype, target_name=doc.name, event_id=event_id)


def observe_frappe_target_change(doc, method=None) -> None:
    """Record a mapped local edit without scheduling outbound work.

    Object and field bindings scope the native transition. Older integration
    links continue to observe their own mapped fields.
    """
    del method
    import frappe
    from frappe.utils import now_datetime

    # Metadata writes can precede columns during migration or follow table
    # removal during uninstall. Skip these hooks before querying Link fields.
    if frappe.flags.in_migrate or frappe.flags.in_uninstall:
        return
    from hero_integration.services.frappe_mapping_draft_storage import personal_publication_active
    if personal_publication_active():
        return

    if doc.doctype in {"HERO Integration Record", "HERO Integration Link", "HERO Object Binding",
                       "HERO Field Binding", "HERO Sync Change Event"}:
        return
    before_save = getattr(doc, "get_doc_before_save", None)
    previous = before_save() if before_save else None
    if previous or getattr(getattr(doc, "flags", None), "in_insert", False):
        _observe_related_changes(frappe, doc, previous)
    covered = _observe_bound_changes(frappe, doc, previous) if previous else {}
    links = frappe.get_all(
        "HERO Integration Link",
        filters={"target_doctype": doc.doctype, "target_name": doc.name, "sync_status": ["!=", "Source Deleted"]},
        fields=["name", "source_instance", "source_type", "source_id", "target_field_hashes", "observed_target_hash"],
    )
    if not previous and not links and getattr(getattr(doc, "flags", None), "in_insert", False):
        _observe_unbound_creation(frappe, doc)
    for row in links:
        # A specific owner suppresses only its link. A relation writer using
        # None owns the target save itself, before a relation link is present.
        if _managed_owner(doc, row.name):
            continue
        baseline = decode(row.get("target_field_hashes"))
        if not baseline:
            continue
        employee_baseline = ((doc.doctype == "User" and row.source_type == "User") or
                             (doc.doctype == "Employee" and row.source_type == "partner"))
        if employee_baseline:
            # Employee links predate the hashed baselines and store normalized
            # source values; the employee importer still needs those values.
            baseline = field_hashes(baseline)
        # ``hero_phone_home`` is a composite relation snapshot, not a field on
        # the target document.  Its lifecycle remains with the dedicated
        # relation resolver and must not produce a false Frappe field change.
        baseline = {field: value for field, value in baseline.items() if not field.startswith("hero_")}
        if not baseline:
            continue
        from hero_integration.services.employee_migration import native_value
        current_values = {field: native_value(doc.get(field)) if employee_baseline else doc.get(field) for field in baseline}
        current_hashes = field_hashes(current_values)
        fields = changed_fields(baseline, current_hashes)
        if previous:
            fields = {field for field in baseline if not _same_mapped_value(previous.get(field), doc.get(field))}
        fields -= covered.get((row.get("source_instance"), row.source_id), set())
        if not fields:
            continue
        observed_hash = hashlib.sha256(
            json.dumps(current_values, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        if not previous and observed_hash == (row.get("observed_target_hash") or ""):
            continue
        link = frappe.get_doc("HERO Integration Link", row.name)
        link.observed_target_hash = observed_hash
        link.last_observed_on = now_datetime()
        link.save(ignore_permissions=True)
        from hero_integration.services.integration_events import record_output

        record_output(
            source_type=row.source_type,
            source_id=row.source_id,
            result="OBSERVED",
            action="frappe_change_observed",
            detail="Mapped Frappe fields changed locally; no outbound HERO action was prepared.",
            target_doctype=doc.doctype,
            target_name=doc.name,
            changed_fields=sorted(fields),
            **({"event_id": hashlib.sha256(json.dumps([row.name, str(doc.modified), observed_hash,
                                                        sorted(fields)]).encode()).hexdigest(), "origin": "frappe",
                "correlation_id": row.name} if previous else {}),
        )
