"""One transaction reservation boundary for legacy migrations and B8 runs.

The named lock serializes first-use row creation. The Singles row lock remains
held until the caller commits/rolls back; releasing the named lock is not a
transaction commit. All run checks below are current reads, not snapshot reads.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

NATIVE_ACTIONS = {
    "Customer Migration",
    "Project Migration",
    "Cheap Data Migration",
    "Full Data Migration",
    "Sync",
    "Projection Migration",
    "Schedule Migration",
}

LOCK_NAME = "hero_integration:active_run"
SLOT_DOCTYPE = "HERO Integration Sync State"
SLOT_FIELD = "active_run"
LEGACY_TERMINAL_STATES = frozenset({"Completed", "Completed With Conflicts", "Failed", "Cancelled", "Blocked", "Budget Reached"})


def acquire(frappe) -> bool:
    rows = frappe.db.sql("SELECT GET_LOCK(%s, %s) AS acquired", (LOCK_NAME, 10), as_dict=True)
    return bool(rows and int(rows[0].acquired or 0) == 1)


def release(frappe) -> None:
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))


def locked_legacy_slot(frappe) -> str:
    rows = frappe.db.sql(
        "SELECT `value` FROM `tabSingles` WHERE `doctype` = %s AND `field` = %s FOR UPDATE",
        (SLOT_DOCTYPE, SLOT_FIELD), as_dict=True,
    )
    if not rows:
        frappe.db.sql(
            "INSERT INTO `tabSingles` (`doctype`, `field`, `value`) VALUES (%s, %s, '')",
            (SLOT_DOCTYPE, SLOT_FIELD),
        )
        return ""
    return str(rows[0].value or "")


def legacy_slot_is_active(frappe, run_name: str) -> bool:
    if not run_name:
        return False
    rows = frappe.db.sql("SELECT state FROM `tabHERO Integration Run` WHERE name=%s FOR UPDATE", (run_name,), as_dict=True)
    # An orphan slot is uncertain: only the existing migration recovery path
    # may reclaim it; B8 must not infer that a missing row authorizes writing.
    return not rows or rows[0].state not in LEGACY_TERMINAL_STATES


def locked_b8_runs(frappe, site: str):
    if not frappe.db.table_exists("HERO Sync Run Ledger"):
        return []
    return frappe.db.sql(
        "SELECT name, sync_run_id FROM `tabHERO Sync Run Ledger` "
        "WHERE site_scope=%s AND state='RUNNING' FOR UPDATE", (site,), as_dict=True,
    )


def locked_pending_migrations(frappe):
    """Include committed intake and sliced file/full runs, which may have no lease."""
    return frappe.db.sql(
        "SELECT name FROM `tabHERO Integration Run` WHERE state IN ('Queued','Running') "
        "ORDER BY name FOR UPDATE", as_dict=True,
    )


def recover_stopped_owner(frappe, slot):
    """Called under the Singles and run row locks, in the admission transaction."""
    from frappe.utils.background_jobs import get_job_status
    from hero_integration.services import migration

    row = frappe.db.get_value("HERO Integration Run", slot,
                              ["state", "action", "started_on", "lease_expires_on"], as_dict=True, for_update=True)
    if not row or row.state != "Running" or row.action not in NATIVE_ACTIONS:
        return False
    if not (row.started_on or row.lease_expires_on):
        return False
    now = migration.now_datetime()
    if migration.run_lease_alive(slot, now, lock=True):
        return False
    job = get_job_status(f"hero-integration:{frappe.local.site}:{slot}")
    if job not in {None, "failed", "finished", "stopped", "canceled"}:
        return False
    migration._interrupt_run(slot, now)
    migration._release_run(slot)
    return True


_wave = ContextVar('hero_migration_wave_intake', default=None)
STANDALONE_PAIRS = (
    frozenset(('Product Migration', 'Customer Migration')),
    frozenset(('Product Migration', 'Project Migration')),
)


def parallel_migrations(frappe):
    conf = getattr(frappe, 'conf', None)
    return bool(conf and int(conf.get('hero_migration_workers') or 0) >= 2)


def _compatible_standalone(frappe, action, pending):
    # The native writer owns the only legacy slot. Product writes Item and
    # prices; it uses its own area lock and separate budget/selection series.
    if not parallel_migrations(frappe) or len(pending) != 1:
        return False
    peer, = pending
    peer_action = frappe.db.get_value('HERO Integration Run', peer, 'action', for_update=True)
    if frozenset((action, peer_action)) not in STANDALONE_PAIRS:
        return False
    from hero_integration.services.file_migration import _parent_run
    return not _parent_run(peer)


def in_wave_intake():
    return _wave.get() is not None


@contextmanager
def admit_wave(frappe, parent, requests, *, resuming=False):
    """Reserve once and grant one admission for each exact (action, child) in this transaction."""
    from hero_integration.services.file_migration import _parent_run
    admit_migration(frappe, parent='' if resuming else parent, resume=parent if resuming else '',
                    _resume_wave=resuming)
    if frappe.db.get_value('HERO Integration Run', parent, 'action', for_update=True) != 'Full Migration':
        raise frappe.ValidationError('Invalid parent for concurrent migration areas.')
    for action, child in requests:
        if child and (_parent_run(child) != parent or
                frappe.db.get_value('HERO Integration Run', child, 'action', for_update=True) != action):
            raise frappe.ValidationError('Concurrent migration child belongs to another parent or action.')
    if resuming:
        requests = [(action, child) for action, child in requests if frappe.db.get_value(
            'HERO Integration Run', child, 'state', for_update=True) in ('Failed', 'Cancelled', 'Blocked')]
    allowed = set(requests)
    token = _wave.set((frappe, parent, allowed))
    try:
        yield requests
        if allowed:
            raise frappe.ValidationError('Concurrent migration admission was not fully consumed.')
    finally:
        _wave.reset(token)


def admit_migration(frappe, *, parent="", resume="", recover_running=False, action="", _resume_wave=False):
    """Serialize acceptance with the sync ledger through the caller's transaction.

    A full run may submit its own next child. A resume is permitted to recover
    its own lease. Other queued work retains its place.
    """
    grant = _wave.get()
    if grant is not None:
        owner, expected_parent, allowed = grant
        if owner is not frappe or parent != expected_parent or recover_running or (action, resume) not in allowed:
            raise frappe.ValidationError('Unexpected child in concurrent migration admission.')
        allowed.remove((action, resume))
        return
    if not acquire(frappe):
        raise frappe.ValidationError("The HERO execution slot could not be reserved.")
    try:
        slot = locked_legacy_slot(frappe)
        from hero_integration.services.test_environment import INBOX
        if (INBOX / 'pending.json').exists():
            raise frappe.ValidationError('A HERO test reset is pending.')
        allowed = {value for value in (parent, resume) if value}
        if locked_b8_runs(frappe, frappe.local.site):
            raise frappe.ValidationError("Another HERO sync run owns the execution slot.")
        pending = {row.name for row in locked_pending_migrations(frappe)}
        if slot and slot not in allowed and slot not in pending and legacy_slot_is_active(frappe, slot):
            raise frappe.ValidationError("Another HERO migration owns the execution slot.")
        if _resume_wave:
            from hero_integration.services.file_migration import _full, _parent_run
            if parent or not resume or frappe.db.get_value('HERO Integration Run', resume, 'action',
                    for_update=True) != 'Full Migration':
                raise frappe.ValidationError('Only the owning full migration can resume this batch.')
            wave = _full(resume, lock=True).get('wave_children') or {}
            for member_action, child in wave.items():
                if _parent_run(child) != resume or frappe.db.get_value('HERO Integration Run', child,
                        'action', for_update=True) != member_action:
                    raise frappe.ValidationError('Invalid full migration wave ownership.')
                allowed.add(child)
        others = pending - allowed
        if others and not parent and action and _compatible_standalone(frappe, action, others):
            others = set()
        if others:
            # A full parent or queued sibling keeps its place. A single native
            # claim can also survive loss of its Singles pointer.
            if len(others) != 1 or not recover_stopped_owner(frappe, next(iter(others))):
                raise frappe.ValidationError("Another HERO migration is queued or running.")
        if recover_running and (not resume or slot not in ("", resume) or not recover_stopped_owner(frappe, resume)):
            raise frappe.ValidationError("Only a stopped worker with an expired reservation can resume.")
        if parent and (parent not in pending or
                       frappe.db.get_value("HERO Integration Run", parent, "action") != "Full Migration"):
            raise frappe.ValidationError("The owning full migration is no longer active.")
    finally:
        release(frappe)
