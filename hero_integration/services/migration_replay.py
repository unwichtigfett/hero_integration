"""Rebuild native migration counts from committed receipts when a mutable reader is replayed."""
import json

import frappe


def enabled(run):
    return bool(json.loads(getattr(run, 'progress_counts_json', None) or '{}').get('native_replay'))


def _unbounded_native(run):
    return (getattr(run, 'action', '') in {'Customer Migration', 'Project Migration'}
            and getattr(run, 'mode', '') == 'Execute' and not getattr(run, 'testing_enabled', False)
            and not getattr(run, 'execution_budget_run', None))


def receipt(code, target, prior=None):
    """An unchanged target created earlier in this run still counts as one creation."""
    created = prior[1] if prior else None
    if code == 'CREATED':
        created = target
    if created and target == created and code in {'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED'}:
        code = 'CREATED'
    return code, created


def prior_receipts(run, phase, identities=None, *, changed_overlap=False):
    if not enabled(run) and not (changed_overlap and _unbounded_native(run)):
        return {}
    params = {'run': run.name, 'phase': phase, 'action': 'hero_to_frappe'}
    selection = ''
    if identities is not None:
        if not identities:
            return {}
        selection = 'AND source_id IN %(ids)s'
        params['ids'] = tuple(map(str, identities))
    rows = frappe.db.sql(f"""
        SELECT source_id, result, target_name, created_target FROM (
            SELECT source_id, result, target_name,
                FIRST_VALUE(CASE WHEN result='CREATED' THEN target_name END) OVER (
                    PARTITION BY source_id ORDER BY (result='CREATED') DESC, creation DESC, name DESC
                ) AS created_target,
                ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY creation DESC, name DESC) AS position_in_history
            FROM `tabHERO Integration Record`
            WHERE run=%(run)s AND source_type=%(phase)s AND technical_action=%(action)s {selection}
        ) history WHERE position_in_history=1""", params)
    return {str(identity): receipt(code, target, (None, created)) for identity, code, target, created in rows}


def count(run, code, delta, phase=None):
    from hero_integration.services.migration import PHASE_BLOCKING_RESULTS
    blocked = code in PHASE_BLOCKING_RESULTS
    if phase:
        group = 'blocked' if blocked else 'skipped'
        if code == 'CREATED':
            group = 'created'
        fields = (f'{phase}_phase_processed', f'{phase}_phase_{group}')
    else:
        if code == 'DEFERRED':
            return
        group = 'conflict_records' if blocked or code == 'PENDING_REVIEW' else 'skipped_records'
        if code == 'CREATED':
            group = 'created_records'
        fields = ('processed', group)
    for field in fields:
        setattr(run, field, int(getattr(run, field, 0) or 0) + delta)


def count_result(run, result, prior=None, phase=None):
    code = receipt(result.code, getattr(result, 'target_name', None), prior)[0]
    if prior and prior[0] == code:
        return
    if prior:
        count(run, prior[0], -1, phase)
    count(run, code, 1, phase)


def begin(run):
    """Keep finished prerequisites and re-enumerate the unfinished mutable phase from zero."""
    native = _unbounded_native(run)
    completed_customer = (native and run.action == 'Project Migration'
                          and getattr(run, 'customer_phase_state', '') == 'Completed')
    run.resume_phase, run.resume_offset, run.resume_record_index = '', 0, 0
    run.customer_phase_state = 'Completed' if completed_customer else 'Queued'
    run.project_phase_state = 'Queued'
    if not native:
        return
    prior_customer_count = int(getattr(run, 'customer_phase_processed', 0) or 0)
    counts = json.loads(getattr(run, 'progress_counts_json', None) or '{}')
    counts['native_replay'] = True
    run.progress_counts_json = json.dumps(counts)
    for field in ('processed', 'created_records', 'conflict_records', 'skipped_records'):
        setattr(run, field, 0)
    for phase in ('customer', 'project'):
        for field in ('processed', 'created', 'blocked', 'skipped'):
            setattr(run, f'{phase}_phase_{field}', 0)
        receipts = prior_receipts(run, phase)
        if completed_customer and phase == 'customer' and len(receipts) < prior_customer_count:
            run.customer_phase_state = 'Queued'
        for code, _ in receipts.values():
            count(run, code, 1)
            count(run, code, 1, phase)
