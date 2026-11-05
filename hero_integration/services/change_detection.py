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


def observe_frappe_target_change(doc, method=None) -> None:
    """Record a mapped local edit without scheduling outbound work.

    The wildcard hook considers only targets of app-owned Links. It preserves
    baselines so later HERO changes to the same field remain conflicts.
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

    links = frappe.get_all(
        "HERO Integration Link",
        filters={"target_doctype": doc.doctype, "target_name": doc.name},
        fields=["name", "source_type", "source_id", "target_field_hashes", "observed_target_hash"],
    )
    for row in links:
        # The inbound writer owns this link's baseline in the same transaction.
        # Other links on the document still observe the change normally.
        if (doc.doctype, doc.name, row.name) in _managed_target.get():
            continue
        baseline = decode(row.get("target_field_hashes"))
        if not baseline:
            continue
        # ``hero_phone_home`` is a composite relation snapshot, not a field on
        # the target document.  Its lifecycle remains with the dedicated
        # relation resolver and must not produce a false Frappe field change.
        baseline = {field: value for field, value in baseline.items() if not field.startswith("hero_")}
        if not baseline:
            continue
        current_values = {field: doc.get(field) for field in baseline}
        current_hashes = field_hashes(current_values)
        fields = changed_fields(baseline, current_hashes)
        if not fields:
            continue
        observed_hash = hashlib.sha256(
            json.dumps(current_values, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        if observed_hash == (row.get("observed_target_hash") or ""):
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
        )
