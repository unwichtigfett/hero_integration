"""Coordinate bounded binding jobs from a full migration's durable checkpoint."""

from time import sleep
from uuid import uuid4

import frappe
from hero_integration.services import concurrency

from hero_integration.services import deferred_references as refs


def _lease(parent):
    state, started = frappe.db.get_value('HERO Integration Run', parent, ['state', 'started_on'])
    if state not in ('Running', 'Queued'):
        raise ValueError('The full migration no longer owns the binding pass.')
    return str(started)


def _owns_record(parent, expected):
    # A consistent read can still see the generation from before cancel/resume.
    # A shared current read fences the subsequent write until its commit and
    # lets all other lanes hold the same parent lock concurrently.
    rows = frappe.db.sql(
        'SELECT state, started_on FROM `tabHERO Integration Run` WHERE name=%s LOCK IN SHARE MODE',
        (parent,),
    )
    return bool(rows and rows[0][0] == 'Running' and str(rows[0][1]) == expected)


def _enqueue(parent, batch, shard, lane):
    lane['attempts'] += 1
    lane['job'] = f"hero-binding-{batch['token']}-{shard}-{lane['attempts']}"
    frappe.enqueue(
        'hero_integration.services.binding_dispatch.realize_page', queue=refs.BINDING_QUEUE,
        parent=parent, token=batch['token'], shard=shard, job_id=lane['job'],
        enqueue_after_commit=True, deduplicate=True,
    )


@concurrency.limited_worker
def realize_page(parent, token, shard):
    """Read the exact assigned observation IDs from the committed parent on the worker."""
    from hero_integration.services.file_migration import _full

    state = _full(parent)
    batch = state.get('binding_batch')
    if not batch or batch['token'] != token:
        return  # A controller restart or explicit resume can supersede a delivery.
    if type(shard) is not int or not 0 <= shard < len(batch['lanes']):
        raise ValueError('Invalid binding job lane.')
    stride = batch.get('stride', refs.LEGACY_BINDING_WORKERS)
    names = batch['names'][shard::stride]
    refs._realize_names(names, check_owner=lambda: _owns_record(parent, batch['lease']), require_complete=True)


def _finish_page(state, names, more):
    state.pop('binding_batch', None)
    state['reconciliation_after'] = names[-1] if more else ''
    if not more:
        state['reconciliation_stage'] += 1


def advance_page(source, origins, state, operation):
    """Advance only after every lane committed its attempts, including blocked bindings.

    The coordinator runs on the long queue. A vanished RQ result can redeliver
    the same original rows. Their normal row locks and statuses govern replay.
    """
    from frappe.utils.background_jobs import get_job_status
    from hero_integration.services.file_migration import _parent_run

    parent = _parent_run(origins[0])
    if not parent:
        raise ValueError('The binding pass has no full migration parent.')
    lease = _lease(parent)
    batch = state.get('binding_batch')
    if batch and batch['operation'] != operation:
        raise ValueError('Binding checkpoint belongs to another relationship stage.')
    if not batch or batch['lease'] != lease:
        # On explicit resume the new owner can dispatch the page again. The
        # cursor still points before it; every previously committed row is safe.
        shared = refs._shared_workers()
        limit = (4 * shared * refs.BINDING_CHUNK_SIZE if shared else refs.BATCH * refs.LEGACY_BINDING_WORKERS)
        rows = refs._waiting_page(source, state.get('reconciliation_after', ''), limit, origins, [operation])
        names = [row.name for row in rows]
        if not names:
            _finish_page(state, [], False)
            return
        if shared:
            # Offer several short jobs per consumer so faster workers can take
            # the next chunk while slower observations are still running.
            chunks = (len(names) + refs.BINDING_CHUNK_SIZE - 1) // refs.BINDING_CHUNK_SIZE
            lanes = min(len(names), max(shared, chunks))
        else:
            lanes = min(len(names), refs.LEGACY_BINDING_WORKERS)
        batch = state['binding_batch'] = {
            'token': uuid4().hex, 'lease': lease, 'operation': operation, 'names': names,
            'more': len(names) == limit,
            'stride': lanes, 'lanes': [{'attempts': 0} for _ in range(lanes)],
        }
        for shard, lane in enumerate(batch['lanes']):
            _enqueue(parent, batch, shard, lane)
        return

    waiting = False
    for shard, lane in enumerate(batch['lanes']):
        if lane.get('done'):
            continue
        status = get_job_status(lane['job'])
        if status == 'finished':
            lane['done'] = True
        elif status in ('queued', 'started', 'deferred', 'scheduled'):
            waiting = True
        elif lane['attempts'] < 3:
            _enqueue(parent, batch, shard, lane)
            waiting = True
        else:
            raise RuntimeError(f"Binding job {lane['job']} has no successful completion after three deliveries.")
    if waiting:
        # Bound polling while the parent owns the chain lock. The long worker
        # releases the lock/returns to its queue after this short invocation.
        sleep(1)
        return
    _finish_page(state, batch['names'], batch['more'])
