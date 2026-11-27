"""Concurrent, transactionally admitted areas of a full migration."""

from time import sleep
from uuid import uuid4

import frappe

from hero_integration.services import file_migration as fm
from hero_integration.services.run_reservation import admit_wave


# Each group has at most one native lease and one file transfer. Services can
# consume price/material bindings created by Product in the preceding group.
WAVES = {
    3: ('Schedule Migration', 'Document Migration', 'Product Migration'),
    6: ('Projection Migration', 'Picture Migration', 'Service Migration'),
}


PROJECT_PRODUCT_WAVES = {
    2: ('Project Migration', 'Product Migration'),
    4: ('Schedule Migration', 'Document Migration'),
    6: WAVES[6],
}


def wave_plan(state):
    waves = PROJECT_PRODUCT_WAVES if state.get('project_product_wave') else WAVES
    if state.get('steps') == list(fm.FULL_STEPS_WITHOUT_PICTURES):
        return {index: tuple(action for action in actions if action != 'Picture Migration')
                for index, actions in waves.items()}
    return waves


def _states(children):
    return {action: frappe.db.get_value('HERO Integration Run', child, 'state')
            for action, child in children.items()}


def _watch(parent, token, tick):
    frappe.enqueue('hero_integration.services.migration_waves.check_publication', queue='long',
                   parent=parent, token=token, tick=tick, enqueue_after_commit=True,
                   job_id=f'hero-wave-watch-{parent}-{token}-{tick % 2}', deduplicate=True)


def recover_watches():
    """Revive a missing observer even when all post-commit callbacks were lost."""
    from frappe.utils.background_jobs import get_job_status
    from hero_integration.services.run_reservation import parallel_migrations
    if not parallel_migrations(frappe):
        return
    roots = frappe.get_all('HERO Integration Run', filters={'action': fm.FULL_ACTION,
        'state': ['in', list(fm.ACTIVE)]}, pluck='name', limit_page_length=0)
    for root in roots:
        state = fm._full(root)
        token = state.get('wave_watch_token')
        if token and state.get('wave_children') and all(get_job_status(
                f'hero-wave-watch-{root}-{token}-{index}') in (None, 'finished', 'failed', 'stopped', 'canceled')
                for index in (0, 1)):
            _watch(root, token, 0)


def _repair_running_slice(child, action, receipt):
    from frappe.utils.file_lock import LockTimeoutError
    from frappe.utils.synchronization import filelock

    # A finished intake handle alone says nothing about a subsequent slice.
    # Recheck the precise current delivery and the same lock as the writer.
    try:
        with filelock(fm._area_lock(action), timeout=0):
            current = fm._stored_default(fm.WAVE_DELIVERY_KEY.format(child), lock=True)
            if current == receipt and frappe.db.get_value(
                    'HERO Integration Run', child, 'state', for_update=True) == 'Running':
                fm._enqueue(child, receipt=receipt)
    except LockTimeoutError:
        return


def check_publication(parent, token, tick=0):
    """Reoffer a lost confirmed delivery and close missing terminal handoffs."""
    from frappe.utils.background_jobs import get_job_status
    from hero_integration.api import _run_job_timeout
    from hero_integration.services.migration import run_lease_alive

    sleep(4)
    frappe.db.rollback()
    state = fm._full(parent)
    children = state.get('wave_children') or {}
    if (not children or token != state.get('wave_watch_token') or
            frappe.db.get_value('HERO Integration Run', parent, 'state') not in fm.ACTIVE):
        return
    states = _states(children)
    if not any(status in fm.ACTIVE for status in states.values()):
        fm.execute(parent)
        return
    for action, child in children.items():
        status = states[action]
        if status not in fm.ACTIVE:
            continue
        native = action not in fm.SLICE_ACTIONS
        if native and status == 'Running' and run_lease_alive(child):
            continue
        receipt = (f'hero-integration:{frappe.local.site}:{child}' if native
                   else fm._stored_default(fm.WAVE_DELIVERY_KEY.format(child)))
        if not receipt or get_job_status(receipt) not in (None, 'failed', 'stopped', 'canceled', 'finished'):
            continue
        if native:
            # _claim_run locks and rechecks both the slot and the exact lease.
            frappe.enqueue('hero_integration.services.migration.execute_run', queue='long', run_name=child,
                           timeout=_run_job_timeout(), enqueue_after_commit=True, job_id=receipt, deduplicate=True)
        elif status == 'Running':
            _repair_running_slice(child, action, receipt)
        else:
            fm._enqueue(child, receipt=receipt)
    _watch(parent, token, tick + 1)


def advance_wave(parent, state):
    """Return True only after all members have completed and may release successors."""
    actions = wave_plan(state)[state['step']]
    children = state.get('wave_children')
    if not children:
        requests = [(action, '') for action in actions]
        with admit_wave(frappe, parent, requests):
            children = {}
            for action in actions:
                child = fm._start_step(action, parent)
                if not child:
                    # The outer execution boundary rolls back every sibling,
                    # parent checkpoint and pending after-commit publication.
                    raise fm.SourceBindingError(f'{action}: no approved read service in this batch.')
                children[action] = child
            state['wave_children'] = children
            state['wave_watch_token'] = uuid4().hex
            fm._save_full(parent, state)
            frappe.db.set_value('HERO Integration Run', parent, {
                'resume_phase': 'Parallel migration areas', 'error_message': ' / '.join(actions)})
            _watch(parent, state['wave_watch_token'], 0)
        frappe.db.commit()
        return False
    if set(children) != set(actions) or len(set(children.values())) != len(actions):
        raise fm.SourceBindingError('Invalid child set at the full migration checkpoint.')
    states = _states(children)
    if any(status in fm.ACTIVE for status in states.values()):
        return False
    for action in actions:
        status = states[action]
        if status not in fm.SUCCESS:
            fm._finish(parent, 'Budget Reached' if status == 'Budget Reached' else 'Blocked',
                       f'{action} ({children[action]}) ended as {status}. Review or resume this batch.')
            return False
    for action in actions:
        if states[action] == 'Completed With Conflicts':
            state['notes'].append(f'{action}: conflicts in {children[action]}')
    state.setdefault('completed_children', []).extend(children[action] for action in actions)
    del state['wave_children']
    state.pop('wave_watch_token', None)
    state['step'] += len(actions)
    # Persist before the next admission reads the locked chain state.
    fm._save_full(parent, state)
    return True


def resume_wave(run):
    """Resume original failed children under the same root and frozen contract."""
    from hero_integration.api import resume_run

    state = fm._full(run.name)
    children = state['wave_children']
    if 'Picture Migration' in children and not fm.pictures_enabled():
        raise fm.SourceBindingError('Picture migration is disabled for this site.')
    if set(children) != set(wave_plan(state).get(state['step'], ())):
        raise fm.SourceBindingError('Invalid children in the full migration resume checkpoint.')
    with admit_wave(frappe, run.name, list(children.items()), resuming=True) as restarts:
        run.state, run.started_on, run.error_message, run.finished_on = 'Queued', None, '', None
        run.save(ignore_permissions=True)
        for action, child in restarts:
            resume_run(child)
    # At least one member may have finished before the previous parent was
    # stopped. Recheck every original ID, including a lost queued delivery.
    state['wave_watch_token'] = uuid4().hex
    fm._save_full(run.name, state)
    _watch(run.name, state['wave_watch_token'], 0)
    fm._enqueue(run.name)
