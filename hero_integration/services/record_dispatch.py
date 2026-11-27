"""Distribute one validated native reader page over the Dev site's shared RQ pool."""

import json
from datetime import timedelta
from time import sleep
from uuid import uuid4

import frappe
from frappe.utils import get_datetime, now_datetime

from hero_integration.services import migration_archive
from hero_integration.services import concurrency, migration as m


def _readers(run):
    from hero_integration.services.read_contract import from_documents

    readers = json.loads(run.checkpoint_contract_json)['readers']
    definitions = set()
    for reader in readers:
        service = frappe.get_doc('HERO GraphQL Service', reader['service'])
        mapping = frappe.get_doc('HERO GraphQL Mapping', reader['mapping'])
        snapshot = frappe.get_doc('HERO GraphQL Schema Snapshot', reader['schema'])
        definitions.update(from_documents(service, mapping, snapshot).definition_ids)
    return sorted(definitions)


def _pin_contract(run, batch):
    # Acquire shared current reads before opening a repeatable-read snapshot.
    # Competing publishers need to write these exact settings, reader documents
    # or selected definitions. Other record workers can hold the same locks.
    frappe.db.sql("SELECT field FROM tabSingles WHERE doctype='HERO Integration Settings' LOCK IN SHARE MODE")
    for reader in json.loads(run.checkpoint_contract_json)['readers']:
        for table, key in (('HERO GraphQL Mapping', 'mapping'), ('HERO GraphQL Service', 'service'),
                           ('HERO GraphQL Schema Snapshot', 'schema')):
            frappe.db.sql(f'SELECT name FROM `tab{table}` WHERE name=%s LOCK IN SHARE MODE', reader[key])
    frappe.db.sql('SELECT definition_id FROM `tabHERO Frappe Mapping` WHERE definition_id IN %s '
                  'ORDER BY definition_id LOCK IN SHARE MODE', (tuple(batch['definitions']),))


def _page(run):
    return json.loads(run.page_checkpoint_json or '{}')


def _owns(run, token, started):
    return (run.state == 'Running' and str(run.started_on) == started
            and run.lease_expires_on and get_datetime(run.lease_expires_on) > now_datetime()
            and _page(run).get('record_batch', {}).get('token') == token)


@concurrency.limited_worker
def process_chunk(run_name, token, started, index, records):
    """Commit domain writes, progress and the chunk receipt in the same transaction."""
    from hero_integration.services.employee_migration import historical_user_scope
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target

    run = frappe.get_doc('HERO Integration Run', run_name)
    if not _owns(run, token, started):
        return
    batch = _page(run)['record_batch']
    if not 0 <= index < len(batch['digests']) or m._digest(records) != batch['digests'][index]:
        raise ValueError('Record chunk does not match the admitted page.')
    frappe.set_user(run.owner)
    frappe.db.rollback()
    # Frappe begins another transaction after rollback. Set isolation for
    # this chunk alone so naming series and progress merges see newer commits.
    frappe.db.sql('ROLLBACK')
    frappe.db.sql('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
    frappe.db.begin()
    try:
        # A lost RQ delivery and its replacement may briefly coexist.
        _lock_active_target('migration-chunk', run_name, f'{token}-{index}')
        slot = frappe.db.sql("SELECT value FROM tabSingles WHERE doctype='HERO Integration Sync State' "
                             "AND field='active_run' LOCK IN SHARE MODE")
        if not slot or slot[0][0] != run_name:
            frappe.db.rollback()
            return
        _pin_contract(run, batch)
        run.reload()
        if (not _owns(run, token, started) or run.owner != frappe.session.user
                or index in _page(run)['record_batch']['done']):
            frappe.db.rollback()
            return
        m._assert_checkpoint_contract(run)
        operation, phase = batch['operation'], batch['phase']
        plan = m._mapping_plan_for_run(run, operation)
        if (plan.adapter_options or {}).get('version') != 2 or run.testing_enabled or run.mode != 'Execute':
            raise ValueError('Run no longer supports shared record processing.')
        kind = phase if phase in m.PHASES else m.PROJECTIONS[operation]['source_type']
        fields = ['processed', 'created_records', 'conflict_records', 'skipped_records']
        if phase in m.PHASES:
            fields.extend(f'{phase}_phase_{suffix}' for suffix in ('processed', 'created', 'blocked', 'skipped'))
        before = {field: int(run.get(field) or 0) for field in fields}
        if phase in m.PHASES:
            migrate = m._migrate_customer if phase == 'customer' else m._migrate_project
            def write(row):
                return migrate(row, run, plan=plan)
        else:
            mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
            scope = m._projection_task_scope(run, plan, mapping, mapping.source_instance, run.source_instance)
            def write(row):
                return m._migrate_projection_record(row, run, plan, scope)
        from hero_integration.services import migration_replay
        previous = {}
        if phase in m.PHASES:
            identities = {str(row['id']) for row in records}
            if migration_replay.enabled(run):
                previous = migration_replay.prior_receipts(run, phase, identities)
            elif changed := identities.intersection(batch.get('changed_overlap', ())):
                previous = migration_replay.prior_receipts(run, phase, changed, changed_overlap=True)
        with migration_archive.run_selection(run), historical_user_scope(run.source_instance, batch['historical']):
            for row in records:
                result = m._migrate_with_binding(row, run, plan, kind, lambda: write(row))
                prior = previous.get(str(row['id']))
                options = {'prior': prior} if prior else {}
                m._record_result(run, kind, row, result, **options)
                if phase in m.PHASES:
                    m._record_phase_result(run, phase, result, **options)

        # Only the short progress merge needs an exclusive lock on the run.
        current = frappe.get_doc('HERO Integration Run', run_name, for_update=True)
        if (not _owns(current, token, started) or current.owner != run.owner
                or current.checkpoint_contract_json != run.checkpoint_contract_json):
            frappe.db.rollback()
            return
        page = _page(current)
        receipt = page['record_batch']
        for field in fields:
            current.set(field, int(current.get(field) or 0) + int(run.get(field) or 0) - before[field])
        receipt['done'].append(index)
        pending = set(range(len(receipt['digests']))) - set(receipt['done'])
        confirmed = min(pending) * receipt['size'] if pending else receipt['count']
        page['processed'] = current.resume_record_index = confirmed
        current.page_checkpoint_json = json.dumps(page)
        current.lease_expires_on = now_datetime() + timedelta(seconds=m.RUN_LEASE_SECONDS)
        current.save()
        frappe.db.commit()
    except BaseException:
        frappe.db.rollback()
        raise


def distribute(run, page, phase, offset, plan, checkpoint):
    workers = concurrency.shared_width()
    if (workers < 2 or len(page.records) < 2 or not plan or run.mode != 'Execute'
            or getattr(run, 'testing_enabled', False) or getattr(run, 'execution_budget_run', None)
            or (getattr(plan, 'adapter_options', None) or {}).get('version') != 2):
        return False
    from hero_integration.services.employee_migration import historical_users
    from frappe.utils.background_jobs import get_job_status

    operation = plan.target_operation
    size = min(8, max(1, (len(page.records) + workers * 2 - 1) // (workers * 2)))
    chunks = [list(page.records[i:i + size]) for i in range(0, len(page.records), size)]
    token, started = uuid4().hex, str(run.started_on)
    with checkpoint():
        m._record_offset_checkpoint(run, page, phase, offset, 1)
        evidence = {'phase': phase, 'offset': offset, 'processed': 0, 'page_complete': False}
        if getattr(page, 'offset_checkpoint', None) is not None:
            evidence['window'] = page.offset_checkpoint
        evidence['record_batch'] = {
            'token': token, 'operation': operation, 'phase': phase, 'size': size,
            'count': len(page.records), 'digests': [m._digest(rows) for rows in chunks],
            'definitions': _readers(run), 'historical': sorted(historical_users(run.source_instance)), 'done': [],
        }
        if changed := getattr(page, 'changed_overlap_ids', None):
            evidence['record_batch']['changed_overlap'] = sorted(changed)
        run.page_checkpoint_json = json.dumps(evidence)
        run.resume_phase, run.resume_offset, run.resume_record_index = phase, offset, 0
    jobs = {}
    attempts = [0] * len(chunks)
    # The coordinator already occupies one pool process. Keep a chunk for it
    # while the remaining consumers pick up chunks or binding work.
    local = len(chunks) - 1
    while True:
        frappe.db.rollback()
        run.reload()
        if run.state == 'Cancelled':
            raise m._Cancelled()
        if not _owns(run, token, started):
            raise m.MappingPlanError('Record page no longer owns the run.')
        pending = set(range(len(chunks))) - set(_page(run)['record_batch']['done'])
        if not pending:
            break
        for i in sorted(pending - {local}):
            if i in jobs and get_job_status(jobs[i]) in ('queued', 'started', 'deferred', 'scheduled'):
                continue
            # The status read can see a completion newer than our DB snapshot.
            if attempts[i] == 3:
                frappe.db.rollback()
                run.reload()
                if i in _page(run).get('record_batch', {}).get('done', []):
                    continue
                raise RuntimeError(f'Record chunk {i} has no committed receipt after three deliveries.')
            attempts[i] += 1
            jobs[i] = f'hero-record-{token}-{i}-{attempts[i]}'
            frappe.enqueue('hero_integration.services.record_dispatch.process_chunk', queue='long',
                           job_id=jobs[i], run_name=run.name, token=token, started=started, index=i, records=chunks[i])
        if local in pending:
            try:
                process_chunk(run.name, token, started, local, chunks[local])
            except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
                # Reoffer this transaction through RQ on the next pass.
                pass
            local = None
        else:
            sleep(0.5)
    with checkpoint():
        evidence = _page(run)
        del evidence['record_batch']
        run.page_checkpoint_json = json.dumps(evidence)
    return True
