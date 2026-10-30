"""Field-level, redacted comparison state for a linked HERO target."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Mapping

_managed_target = ContextVar("hero_integration_managed_target", default=None)


@contextmanager
def managed_target_update(doctype: str, name: str, link_name: str):
    """Assign one inbound writer ownership of one link for one target save."""
    token = _managed_target.set((doctype, name, link_name))
    try:
        yield
    finally:
        _managed_target.reset(token)


def managed_target_update_active(doctype: str, name: str | None) -> bool:
    current = _managed_target.get()
    return bool(current and current[0] == doctype and current[1] in (None, name))


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
        frappe.throw('The external HERO deadline is managed by HERO Integration.', frappe.PermissionError)


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
    """Classify each tracked field; never compare a field HERO did not return.

    ``missing`` fields were absent from the GraphQL response.  They are neither
    a change nor a deletion, so their baselines are carried forward unchanged.
    A field without a baseline (for example a newly selected mapping) counts as
    changed in HERO only when HERO has a value, and as changed locally only
    when Frappe holds a non-empty value.  A HERO change of an ``unwritable``
    field (an empty value under the no-clear policy) is acknowledged only.
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
    """Record a mapped Frappe-side field change without scheduling an outbound action.

    The wildcard Frappe hook reaches every DocType, therefore the first query
    is the scope boundary: only targets already named by an app-owned Link are
    considered.  The synchronized baseline remains untouched so that a later
    HERO-side change to the same field still becomes a conflict rather than an
    automatic overwrite.
    """
    del method
    import frappe
    from frappe.utils import now_datetime

    # During install/migrate, DocType metadata can be written before the
    # corresponding database columns exist.  This global on_update hook runs
    # for those technical writes as well, so it must return before querying
    # the Link fields introduced by the same synchronization.  During uninstall
    # the app tables are already dropped while this hook is still registered.
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
        if _managed_target.get() == (doc.doctype, doc.name, row.name):
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
