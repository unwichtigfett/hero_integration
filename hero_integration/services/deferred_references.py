"""Retry only previously observed records when their source-bound identities arrive.

This queue never scans a HERO collection or expands a test run. Source observations
are private, exclude transport credentials, and are removed after reconciliation.
Every replay uses the original actor, frozen mapping and ordinary conflict guards.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import time
import traceback
from pathlib import Path

import frappe
from hero_integration.services.performance import measured, profiled

MAIN = 'HERO Pending Reference'
CHILD = 'HERO Pending Dependency'
BATCH = 50
_replaying = ContextVar('hero_reference_replay', default=False)
_writing = ContextVar('hero_pending_reference_write', default=False)


class PendingObservationError(ValueError):
    """The frozen source record cannot authorize a relationship write."""


def _source_observation(pending):
    try:
        record = json.loads(pending.record_json)
    except (TypeError, ValueError):
        raise PendingObservationError('Saved source observation is missing or invalid; import the affected source record again.') from None
    if not isinstance(record, dict) or not record:
        raise PendingObservationError('Saved source observation is missing or invalid; import the affected source record again.')
    return record


def assert_owned_write():
    if not _writing.get():
        raise PermissionError('Pending reference observations are service-owned.')


def realizing():
    """Whether the post-success reference service owns the current write."""
    return _replaying.get()


@contextmanager
def _write_scope():
    token = _writing.set(True)
    try:
        yield
    finally:
        _writing.reset(token)
OPERATIONS = {'customer': 'HeroCustomers', 'project': 'HeroProjectMatches', 'Task': 'HeroTasks', 'CalendarEvent': 'HeroCalendarEvents'}
TARGETS = {'customer': 'Customer', 'project': 'Project', 'User': 'User', 'partner': 'Employee',
           'customer_address': 'Address', 'supply_product':'Item', 'WageGroup':'Activity Type', 'service_file':'File'}


def installed(frappe_module=None):
    db = getattr(frappe_module if frappe_module is not None else frappe, 'db', None)
    exists = getattr(db, 'table_exists', lambda _: False)
    return bool(db and exists(MAIN) and exists(CHILD))


def require_storage(frappe_module=None):
    """Never accept an import that could silently lose deferred references."""
    owner = frappe_module if frappe_module is not None else frappe
    if not installed(owner):
        owner.throw(owner._("Pending relationship storage is not installed."))


def key(source, kind, identity):
    return sha256(json.dumps([source, kind, str(identity)], separators=(',', ':')).encode()).hexdigest()


def observation_key(source, operation, identity, run_name):
    return key(source, operation, ['run', run_name, str(identity)])


def _observations(source, operation, identity):
    return frappe.get_all(MAIN, filters={'source_instance': source, 'operation': operation,
        'source_id': str(identity), 'status': ['in', ['Waiting', 'Conflict']]},
        fields=['name', 'origin_run'], limit_page_length=0)


def references(plan, record):
    from hero_integration.services.direct_mapping import lookup
    result = set()
    for rule in plan.definitions:
        kind = {'HERO Customer Link': 'customer', 'HERO Project Link': 'project',
                'HERO User Link': 'User', 'User By Partner ID': 'partner'}.get(rule.transformation)
        kind = {'Customer Relation': 'customer', 'Project Relation': 'project'}.get(rule.purpose, kind)
        if ((getattr(plan, 'target_operation', '') or getattr(plan, 'operation', '')) == 'HeroTasks'
                and rule.source_path in {'target_user_id', 'author_user_id'}):
            kind = 'User'
        value = lookup(record, rule.source_path) if kind else None
        if type(value) is int and value > 0:
            result.add((kind, str(value)))
    operation = getattr(plan, 'target_operation', '') or getattr(plan, 'operation', '')
    if operation == 'HeroTasks' and not getattr(plan, 'adapter_options', None):
        # The legacy native lifecycle consumes these returned fields directly,
        # including the author even when it has no individual field rule.
        for path in ('target_user_id', 'author_user_id'):
            value = record.get(path)
            if type(value) is int and value > 0:
                result.add(('User', str(value)))
    for row in record.get('project_match_assignments') or []:
        value = row.get('user_id') if isinstance(row, dict) else None
        if type(value) is int and value > 0:
            result.add(('User', str(value)))
    if operation == 'HeroCustomers':
        address = record.get('address')
        selected = any(rule.target_doctype == 'Address' for rule in plan.definitions)
        if (selected or not getattr(plan, 'adapter_options', None)) and isinstance(address, dict):
            identity = address.get('id')
            if type(identity) is int and identity > 0:
                result.add(('customer_address', str(identity)))
    for relation in (getattr(plan, 'adapter_options', None) or {}).get('native_relations', []):
        if relation.get('kind') != 'customer_addresses':
            continue
        rows = lookup(record, relation.get('collection_path', ''))
        if not isinstance(rows, list):
            continue
        primary = lookup(record, relation.get('primary_identity_path', ''))
        for row in rows:
            if not isinstance(row, dict) or lookup(row, relation.get('deleted_path', '')) is not False:
                continue
            value = lookup(row, relation.get('identity_path', ''))
            if (type(value) in (int, str) and str(value).strip()
                    and (type(value) is not int or value > 0) and str(value) != str(primary)):
                result.add(('customer_address', str(value).strip()))
    from hero_integration.services.employee_migration import historical_users
    historical = historical_users(record.get('_hero_source_instance') or 'default')
    return sorted((kind, identity) for kind, identity in result
                  if kind != 'User' or identity not in historical)


def available(source, kind, identity):
    rows = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
        'source_instance': source, 'source_type': kind, 'source_id': identity},
        fields=['target_doctype', 'target_name', 'sync_status'], limit_page_length=2)
    if len(rows) != 1:
        return False
    link = rows[0]
    statuses = {'Synchronized', 'Conflict'} if kind in {'customer', 'project'} else {'Synchronized'}
    if (link.target_doctype != TARGETS[kind] or link.sync_status not in statuses
            or not frappe.db.exists(link.target_doctype, link.target_name)):
        return False
    if kind == 'User':
        if not frappe.db.get_value('User', link.target_name, 'enabled'):
            return False
        bindings = frappe.get_all('HERO Object Binding', filters={'site_scope': frappe.local.site,
            'source_instance': source, 'object_type': 'User', 'external_object_id': identity,
            'target_doctype': 'User', 'lifecycle': 'VALID'}, fields=['target_name'], limit_page_length=2)
        return len(bindings) == 1 and bindings[0].target_name == link.target_name
    if kind == 'partner':
        from hero_integration.integrations.hero_people import user_by_partner
        return bool(user_by_partner(int(identity), source_instance=source))
    return True


@measured('binding.availability_bulk')
def available_many(source, dependencies):
    """Read a deduplicated scheduling snapshot, never a write authorization."""
    from collections import defaultdict
    requested = {(kind, str(identity)) for kind, identity in dependencies}
    ready = dict.fromkeys(requested, False)
    grouped = defaultdict(list)
    for kind, identity in requested:
        if kind in TARGETS:
            grouped[kind].append(identity)

    def chunks(values):
        values = list(values)
        for start in range(0, len(values), 500):
            yield values[start:start + 500]

    for kind, identities in grouped.items():
        # Partner resolution has additional Employee/User ownership semantics.
        if kind == 'partner':
            for identity in identities:
                ready[kind, identity] = available(source, kind, identity)
            continue
        links = defaultdict(list)
        for batch in chunks(identities):
            for row in frappe.get_all('HERO Integration Link', filters={
                    'source_system': 'HERO', 'source_instance': source,
                    'source_type': kind, 'source_id': ['in', batch]},
                    fields=['source_id', 'target_doctype', 'target_name', 'sync_status'], limit_page_length=0):
                links[str(row.source_id)].append(row)
        statuses = {'Synchronized', 'Conflict'} if kind in {'customer', 'project'} else {'Synchronized'}
        candidates = {identity: rows[0].target_name for identity, rows in links.items()
            if len(rows) == 1 and rows[0].target_doctype == TARGETS[kind] and rows[0].sync_status in statuses}
        existing = set()
        for batch in chunks(set(candidates.values())):
            filters = {'name': ['in', batch]}
            if kind == 'User':
                filters['enabled'] = 1
            existing.update(frappe.get_all(TARGETS[kind], filters=filters, pluck='name', limit_page_length=0))
        bindings = defaultdict(list)
        if kind == 'User':
            for batch in chunks(identities):
                for row in frappe.get_all('HERO Object Binding', filters={
                        'site_scope': frappe.local.site, 'source_instance': source, 'object_type': 'User',
                        'external_object_id': ['in', batch], 'target_doctype': 'User', 'lifecycle': 'VALID'},
                        fields=['external_object_id', 'target_name'], limit_page_length=0):
                    bindings[str(row.external_object_id)].append(row.target_name)
        for identity, target in candidates.items():
            ready[kind, identity] = target in existing and (kind != 'User' or bindings[identity] == [target])
    return ready


def _private_observation(value):
    if isinstance(value, dict):
        return {k: _private_observation(v) for k, v in value.items()
                if k.casefold().replace('_', '') not in
                {'downloadurl', 'urldownload', 'signedurl', 'token', 'apitoken', 'apikey', 'accesstoken',
                 'refreshtoken', 'clientsecret', 'secretkey', 'password', 'secret', 'authorization'}}
    if isinstance(value, list):
        return [_private_observation(v) for v in value]
    return value


def _origin_binding(source, operation, identity):
    """Lock the exact imported identity before acknowledging or replaying it."""
    from hero_integration.services import migration as m
    kind = next(kind for kind, name in OPERATIONS.items() if name == operation)
    link = m._link(kind, identity, source)
    if not link:
        return None
    link = frappe.get_doc('HERO Integration Link', link.name, for_update=True)
    return {field: getattr(link, field, '') or '' for field in (
        'name', 'target_doctype', 'target_name', 'source_payload_hash',
        'observed_source_hash', 'mapping_version')}


def _verify_origin_binding(pending, proof):
    if not isinstance(proof, dict) or not proof or _origin_binding(
            pending.source_instance, pending.operation, pending.source_id) != proof:
        raise ValueError('The imported source observation or binding changed; refresh the source record before retrying.')


def remember(run, source_type, record, result):
    if (_replaying.get() or run.mode == 'Preview' or source_type not in OPERATIONS or not installed()):
        return
    from hero_integration.services.migration import _mapping_plan_for_run, _source_instance
    operation = OPERATIONS[source_type]
    plan = _mapping_plan_for_run(run, operation)
    try:
        identity = plan.source_identity(record)
    except ValueError:
        return  # Invalid upstream identities need review; there is no safe retry key.
    if result.code in {'SKIPPED_SOURCE_DELETED', 'SKIPPED_REJECTED'}:
        invalidate(_source_instance(record), operation, identity)
        return
    source = _source_instance(record)
    from hero_integration.services.employee_migration import historical_users
    historical = historical_users(source)
    observation = {**record, '__hero_origin_binding': _origin_binding(source, operation, identity)}
    if historical:
        observation['__hero_historical_users'] = sorted(historical)
    save(run.name, source, operation, identity, plan.mapping_version,
         observation, references(plan, record),
         defer_empty=bool((getattr(plan, 'adapter_options', None) or {}).get('native_relations')) or (
             operation == 'HeroProjectMatches' and isinstance(record.get('project_match_assignments'), list)) or (
             operation == 'HeroCustomers' and any(rule.purpose in {'Customer Primary Contact', 'Customer Home Phone'}
                 or rule.target_doctype in {'Contact', 'Address'} or (rule.target_doctype == 'Customer'
                     and rule.target_field in {'first_name', 'last_name', 'email_id', 'mobile_no'})
                 for rule in plan.definitions)),
         result_code='PENDING_REVIEW' if getattr(result, 'pending_relation', False) else result.code)


def remember_file(run_name, source, operation, read, record, detail, *, cohort=None):
    if not run_name or _replaying.get() or not installed():
        return
    document = (detail or {}).get('customer_document') or {}
    links = (detail or {}).get('file_upload_links') or []
    projects = [row.get('project_match_id') for row in (detail or {}).get('file_upload_matches') or []]
    projects += [document.get('project_match_id')] + [row.get('project_match_id') for row in links]
    customers = [document.get('contact_id')] + [row.get('customer_id') for row in links]
    deps = [('project', str(v)) for v in projects if type(v) is int and v > 0]
    deps += [('customer', str(v)) for v in customers if type(v) is int and v > 0]
    observation = {'row': record, 'detail': detail}
    if cohort is not None:
        project_ids, customer_ids = set(cohort['project_ids']), set(cohort['customer_ids'])
        deps = [(kind, identity) for kind, identity in deps if identity in (project_ids if kind == 'project' else customer_ids)]
        observation['scope'] = {'project_ids': sorted(project_ids), 'customer_ids': sorted(customer_ids)}
    save(run_name, source, operation, read.plan.source_identity(record), read.mapping_version,
         observation, sorted(set(deps)))


def invalidate(source, operation, identity):
    """A newer deletion/rejection cancels the old observation atomically."""
    for observed in _observations(source, operation, identity):
        frappe.db.set_value(MAIN, observed.name, {'status': 'Resolved', 'record_json': '',
            'detail': 'Source deletion or rejection observed; deferred writes cancelled.'})


def dependencies_ready(source, operation, dependencies, *, availability=None):
    """Stop as soon as the required all/any condition is decided.

    A project may have hundreds of assignees. Missing one is enough to
    postpone its complete replay, while a file can attach to any one of its
    eligible targets. Avoid querying every remaining binding in either case.
    """
    if operation == 'HeroServices':
        dependencies = [(kind, identity) for kind, identity in dependencies if kind == 'supply_product']
    if not dependencies:
        return True
    def check(kind, identity):
        return (available(source, kind, identity) if availability is None else
                availability.get((kind, str(identity)), False))
    if operation in {'HeroDocuments', 'HeroPictures'}:
        # A Customer can serve as a temporary attachment, but must not erase
        # a pending Project reference that should take precedence later.
        projects = [(kind, identity) for kind, identity in dependencies if kind == 'project']
        candidates = projects or dependencies
        return any(check(kind, identity) for kind, identity in candidates)
    return all(check(kind, identity) for kind, identity in dependencies)


def save(run_name, source, operation, identity, version, record, dependencies, *, result_code=None, defer_empty=False):
    name = observation_key(source, operation, identity, run_name)
    for observed in _observations(source, operation, identity):
        if observed.name != name:
            frappe.db.set_value(MAIN, observed.name, {'status': 'Resolved', 'record_json': '',
                'detail': 'Superseded by a newer source observation; saved dependency identities retained.'})
    ready = dependencies_ready(source, operation, dependencies)
    exists = frappe.db.exists(MAIN, name)
    if not dependencies and not defer_empty and ready and result_code != 'PENDING_REVIEW':
        if exists:
            successful = result_code is None or result_code in {
                'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'
            }
            if successful:
                frappe.db.set_value(MAIN, name, {'status': 'Resolved', 'record_json': '', 'detail': ''})
            else:
                frappe.db.set_value(MAIN, name, {'status': 'Conflict',
                    'detail': 'Native reconciliation requires review: ' + result_code})
        return
    # Persist every observed relationship, including already available targets.
    # Only the post-success worker may consume this observation.
    successful = result_code is None or result_code in {
        'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'
    }
    review = ready and not successful
    doc = frappe.get_doc(MAIN, name, for_update=True) if exists else frappe.get_doc({'doctype': MAIN, 'identity_key': name})
    doc.update({'source_instance': source, 'operation': operation, 'source_id': str(identity),
        'origin_run': run_name, 'mapping_version': version, 'status': 'Conflict' if review else 'Waiting',
        'realization_started': 0,
        'detail': 'Relationship requires review.' if review else '',
        'record_json': json.dumps(_private_observation(record), ensure_ascii=False),
        'dependencies': [{'dependency_key': key(source, kind, value), 'source_type': kind, 'source_id': value}
                         for kind, value in dependencies]})
    with _write_scope():
        doc.save(ignore_permissions=True) if exists else doc.insert(ignore_permissions=True)
    frappe.db.after_commit.add(lambda: wake_reference(name))


def _enqueue_dependency(source, kind, identity, *, after='', enqueue_after_commit=False):
    job = key(source, 'deferred-page', [kind, str(identity), after])
    frappe.enqueue('hero_integration.services.deferred_references.resolve_dependency',
        source=source, kind=kind, identity=identity, after=after,
        enqueue_after_commit=enqueue_after_commit, job_id='hero-reference-' + job, deduplicate=True)


def binding_updated(doc, method=None):
    if (doc.source_type not in TARGETS or doc.sync_status != 'Synchronized' or not installed()):
        return
    if not frappe.db.sql('''SELECT d.name FROM `tabHERO Pending Dependency` d
        JOIN `tabHERO Pending Reference` p ON p.name=d.parent AND d.parenttype=%s
        WHERE d.dependency_key=%s AND p.status='Waiting' LIMIT 1''',
            (MAIN, key(doc.source_instance, doc.source_type, doc.source_id))):
        return
    _enqueue_dependency(doc.source_instance, doc.source_type, doc.source_id, enqueue_after_commit=True)


def wake_reference(name, *, scheduled=None):
    """Post-commit race closure: enqueue only if every missing identity arrived."""
    try:
        pending = frappe.db.get_value(MAIN, name, ['source_instance', 'status', 'operation', 'origin_run', 'realization_started'], as_dict=True)
        if not pending or pending.status != 'Waiting':
            return
        if frappe.db.get_value('HERO Integration Run', pending.origin_run, 'state') != 'Completed':
            return
        dependencies = (frappe.get_all(CHILD, filters={'parent': name, 'parenttype': MAIN},
            fields=['source_type', 'source_id'], limit_page_length=0)
            if getattr(pending, 'realization_started', 1) else [])
        _wake_observed(name, pending, dependencies, scheduled=scheduled)
    except Exception:
        frappe.log_error(title='Pending HERO relationship wakeup failed', message='Reference ' + name)


def _wake_observed(name, pending, dependencies, *, scheduled=None, availability=None):
    if not getattr(pending, 'realization_started', 1) or not dependencies:
        frappe.enqueue('hero_integration.services.deferred_references.resolve_one', name=name,
            job_id='hero-reference-' + name, deduplicate=True)
    elif dependencies_ready(pending.source_instance, pending.operation,
            [(row.source_type, row.source_id) for row in dependencies], availability=availability):
        identity = (pending.source_instance, dependencies[0].source_type, dependencies[0].source_id)
        if scheduled is not None:
            if identity in scheduled:
                return
            scheduled.add(identity)
        _enqueue_dependency(*identity)


@profiled('binding.recovery')
def recover_pending():
    """Bounded restart recovery for lost post-commit notifications, never a reimport."""
    if not installed():
        return
    from collections import defaultdict
    rows = frappe.get_all(MAIN, filters={'status': 'Waiting'},
        fields=['name', 'source_instance', 'operation', 'origin_run', 'realization_started'],
        order_by='last_checked asc, creation asc', limit_page_length=BATCH)
    if not rows:
        return
    completed = set(frappe.get_all('HERO Integration Run', filters={
        'name': ['in', list({row.origin_run for row in rows})], 'state': 'Completed'},
        pluck='name', limit_page_length=0))
    eligible = [row for row in rows if row.origin_run in completed]
    dependencies = defaultdict(list)
    if eligible:
        for item in frappe.get_all(CHILD, filters={
                'parent': ['in', [row.name for row in eligible]], 'parenttype': MAIN},
                fields=['parent', 'source_type', 'source_id'], order_by='parent, idx', limit_page_length=0):
            dependencies[item.parent].append(item)
    by_source = defaultdict(set)
    for row in eligible:
        if row.realization_started:
            by_source[row.source_instance].update((item.source_type, item.source_id) for item in dependencies[row.name])
    readiness = {source: available_many(source, deps) for source, deps in by_source.items()}
    scheduled = set()
    for row in rows:
        if row.origin_run in completed:
            try:
                _wake_observed(row.name, row, dependencies[row.name], scheduled=scheduled,
                    availability=readiness.get(row.source_instance, {}))
            except Exception:
                frappe.log_error(title='Pending HERO relationship wakeup failed', message='Reference ' + row.name)
        frappe.db.set_value(MAIN, row.name, 'last_checked', frappe.utils.now_datetime(), update_modified=False)


def _enqueue_completed_run(run_name, after=''):
    frappe.enqueue('hero_integration.services.deferred_references.reconcile_completed_run',
        run_name=run_name, after=after, enqueue_after_commit=True,
        job_id='hero-reference-run-' + key(run_name, 'page', after), deduplicate=True)


def notify_completed_run(run_name):
    """Wake a successful area's observations; recovery covers a lost notification."""
    try:
        if not installed():
            return
        run = frappe.db.get_value('HERO Integration Run', run_name, ['state', 'mode'], as_dict=True)
        if (not run or run.state != 'Completed' or run.mode == 'Preview' or
                not frappe.db.exists(MAIN, {'origin_run':run_name, 'status':'Waiting'})):
            return
        _enqueue_completed_run(run_name)
    except Exception:
        frappe.log_error(title='Pending HERO relationship completion wakeup failed', message='Run ' + run_name)


def reconcile_completed_run(run_name, after=''):
    """Realize one bounded page belonging only to this successful origin run."""
    run = frappe.get_doc('HERO Integration Run', run_name)
    if run.state != 'Completed' or run.mode == 'Preview':
        return
    cursor, more = reconcile_source_page(run.source_instance, after, origin_runs=[run_name])
    if more and cursor != after:
        _enqueue_completed_run(run_name, cursor)


def _record_failure(name, error):
    """Re-read committed state before classifying a failed write or callback."""
    diagnostic = {'reference': name, 'error_type': type(error).__name__,
        'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)[-12:]]}
    frappe.db.rollback()
    # COMMIT can succeed before an after_commit callback raises. Another worker
    # may also have superseded this observation after our rollback released locks.
    status = frappe.db.get_value(MAIN, name, 'status', for_update=True)
    diagnostic['committed_status'] = status
    if status == 'Waiting':
        detail = ('Saved source observation is missing or invalid; import the affected source record again.'
            if isinstance(error, PendingObservationError) else
            'Deferred relationship validation failed; see the reference error diagnostic.')
        frappe.db.set_value(MAIN, name, {'status': 'Conflict', 'detail': detail})
        frappe.db.commit()
    else:
        # Resolved, superseded, deleted and independently reviewed observations
        # must retain their committed state and cleared/private source evidence.
        frappe.db.rollback()
    frappe.log_error(title='Pending HERO relationship failed', message=json.dumps(diagnostic, sort_keys=True))


@profiled('binding.reconcile_page')
def reconcile_source_page(source, after='', limit=BATCH, *, origin_runs=None, operations=None):
    """Recheck one stable page at the end of a full migration.

    Still-missing dependencies remain Waiting for the normal binding wakeup.
    Each completed reconciliation is committed independently, as in the worker.
    """
    if not installed():
        raise RuntimeError('Pending relationship storage is not installed.')
    if origin_runs is not None and not origin_runs:
        return after, False
    origin_clause = ' AND origin_run IN %s' if origin_runs is not None else ''
    operation_clause = ' AND operation IN %s' if operations is not None else ''
    if operations is not None and not operations:
        return after, False
    params = (source,after)
    if origin_runs is not None: params += (tuple(origin_runs),)
    if operations is not None: params += (tuple(operations),)
    params += (limit,)
    rows = frappe.db.sql('''SELECT name FROM `tabHERO Pending Reference`
        WHERE source_instance=%s AND status='Waiting' AND name>%s''' + origin_clause + operation_clause +
        ' ORDER BY name LIMIT %s', params, as_dict=True)
    actor = frappe.session.user
    for row in rows:
        try:
            resolve_one(row.name)
            frappe.db.commit()
        except Exception as error:
            _record_failure(row.name, error)
        finally:
            frappe.set_user(actor)
    return (rows[-1].name if rows else after), len(rows) == limit


FINAL_RELATION_ORDER = ('HeroEmployees','HeroCustomers','HeroProjectMatches','HeroCalendarEvents',
    'HeroTasks','HeroServices','HeroDocuments','HeroPictures')


def reconcile_full_page(source, origin_runs, state):
    """Advance one bounded dependency stage only after all scalar areas finished."""
    if not origin_runs:
        raise ValueError('Full relationship reconciliation requires its recorded child runs.')
    if 'reconciliation_stage' not in state:
        state['reconciliation_stage']=0
        state['reconciliation_after']=''  # Older checkpoints used one global cursor.
    stage = state['reconciliation_stage']
    if type(stage) is not int or not 0<=stage<=len(FINAL_RELATION_ORDER):
        raise ValueError('The full relationship reconciliation checkpoint is invalid.')
    if stage<len(FINAL_RELATION_ORDER):
        cursor,more=reconcile_source_page(source,state.get('reconciliation_after',''),
            origin_runs=origin_runs,operations=(FINAL_RELATION_ORDER[stage],))
        state['reconciliation_after']=cursor if more else ''
        if not more: state['reconciliation_stage']=stage+1
        return False
    # Only the completed full import may classify genuinely unresolved bindings.
    # Keep their frozen records for review/new observations; never invent a target.
    after=state.get('reconciliation_final_after','')
    rows=frappe.db.sql("""SELECT name FROM `tabHERO Pending Reference`
        WHERE source_instance=%s AND origin_run IN %s AND status='Waiting' AND name>%s
        ORDER BY name LIMIT %s""",(source,tuple(origin_runs),after,BATCH),as_dict=True)
    for row in rows:
        pending=frappe.get_doc(MAIN,row.name,for_update=True)
        if pending.status=='Waiting' and pending.source_instance==source and pending.origin_run in origin_runs:
            frappe.db.set_value(MAIN,row.name,{'status':'Conflict',
                'detail':'Full import finished; required relationship bindings are missing or require review.'})
        frappe.db.commit()
    state['reconciliation_final_after']=rows[-1].name if rows else after
    if len(rows)==BATCH:
        return False
    state['reconciliation_conflicts']=frappe.db.count(MAIN,{'source_instance':source,
        'origin_run':['in',list(origin_runs)],'status':'Conflict'})
    state['reconciliation_done']=True
    return True


@profiled('binding.dependency_page')
def resolve_dependency(source, kind, identity, after=''):
    """One bounded keyset page per job; no retry loop or upstream collection read."""
    if not installed() or kind not in TARGETS:
        return
    rows = frappe.db.sql('''SELECT DISTINCT p.name FROM `tabHERO Pending Reference` p
        JOIN `tabHERO Pending Dependency` d ON d.parent=p.name AND d.parenttype=%s
        WHERE p.source_instance=%s AND p.status='Waiting' AND d.dependency_key=%s AND p.name>%s
        ORDER BY p.name LIMIT %s''', (MAIN, source, key(source, kind, identity), after, BATCH), as_dict=True)
    actor = frappe.session.user
    last = after
    deadline = time.monotonic() + 120
    for row in rows:
        if time.monotonic() >= deadline:
            break
        try:
            resolve_one(row.name, dependency=(kind, str(identity)))
            frappe.db.commit()
        except Exception as error:
            _record_failure(row.name, error)
        finally:
            frappe.set_user(actor)
        last = row.name
    if last != after and (len(rows) == BATCH or rows and last != rows[-1].name):
        _enqueue_dependency(source, kind, identity, after=last, enqueue_after_commit=True)


@profiled('binding.resolve')
def resolve_one(name, *, dependency=None):
    pending = frappe.get_doc(MAIN, name, for_update=True)
    if pending.status != 'Waiting':
        return
    run = frappe.get_doc('HERO Integration Run', pending.origin_run)
    if run.mode == 'Preview' or run.state in {'Cancelled', 'Failed'}:
        raise ValueError('The original migration does not permit deferred writes.')
    # A dependency can arrive while its originating area is still running.
    # Keep its observation intact until that exact run completed successfully.
    if run.state != 'Completed':
        return
    record = _source_observation(pending) if pending.operation in OPERATIONS.values() else None
    first_attempt = not getattr(pending, 'realization_started', 1)
    if first_attempt:
        frappe.db.set_value(MAIN, name, 'realization_started', 1)
    dependencies = [(row.source_type, row.source_id) for row in pending.dependencies]
    # Secondary Address identities belong to the relationship writer itself.
    # Its first attempt may create them; an existing unbound match waits for review.
    required = [(kind, identity) for kind, identity in dependencies if not first_attempt or kind != 'customer_address']
    if pending.operation == 'HeroEmployees':
        required = []  # The employee service realizes both of these owned identities itself.
    if not dependencies_ready(pending.source_instance, pending.operation, required):
        if pending.operation == 'HeroProjectMatches' and dependency and dependency[0] == 'User':
            _resolve_available_project_assignee(pending, dependency[1])
        return
    from hero_integration.services import migration as m
    from hero_integration.client.settings import get_configuration
    if run.owner != 'Administrator' and not frappe.db.get_value('User', run.owner, 'enabled'):
        raise PermissionError('The original migration actor is inactive.')
    frappe.set_user(run.owner)
    frappe.only_for('System Manager')
    token = _replaying.set(True)
    try:
        config = get_configuration(fresh=True)
        if config.source_instance != pending.source_instance:
            raise ValueError('The configured source changed.')
        if pending.operation == 'HeroServices':
            from hero_integration.services.service_migration import realize_relationships
            from hero_integration.services.file_migration import bound_configuration
            bound_configuration(run, configuration=config)
            code = realize_relationships(pending, config)
            bound_configuration(run, configuration=get_configuration(fresh=True))
        elif pending.operation == 'HeroEmployees':
            from hero_integration.services.employee_migration import realize_relationships
            from hero_integration.services.file_migration import bound_configuration
            bound_configuration(run, configuration=config)
            code = realize_relationships(pending)
            bound_configuration(run, configuration=get_configuration(fresh=True))
        elif pending.operation in {'HeroDocuments', 'HeroPictures'}:
            from hero_integration.services.file_transfer import FileConflict
            from hero_integration.services.file_migration import bound_configuration
            if bound_configuration(run, configuration=config).source_instance != pending.source_instance:
                raise ValueError('The deferred file source changed.')
            try:
                code = _resolve_file(pending)
            except FileConflict as error:
                links = frappe.get_all('HERO Integration Link', filters={'source_instance': pending.source_instance,
                    'source_type': 'file_upload', 'source_id': pending.source_id},
                    fields=['name', 'sync_status'], limit_page_length=2)
                if len(links) == 1:
                    changes = {'conflict_detail': str(error)}
                    if links[0].sync_status not in {'Pending Review', 'Rejected', 'Source Deleted'}:
                        changes['sync_status'] = 'Conflict'
                    frappe.db.set_value('HERO Integration Link', links[0].name, changes)
                code = 'CONFLICT'
            # A fresh connection avoids an older request transaction's read snapshot.
            bound_configuration(run, configuration=get_configuration(fresh=True))
        else:
            m.validate_run_mappings(run)
            source = m._source_for_run(run, config)  # Source/endpoint proof; no external request.
            plan = m._mapping_plan_for_run(run, pending.operation)
            if plan.mapping_version != pending.mapping_version:
                raise ValueError('The approved mapping changed; refresh the source record before retrying.')
            proof = record.pop('__hero_origin_binding', None)
            historical = record.pop('__hero_historical_users', [])
            if not isinstance(historical, list) or any(not isinstance(value, str) or not value.isdigit() or int(value) <= 0 for value in historical):
                raise ValueError('The deferred historical identity proof is invalid.')
            if str(plan.source_identity(record)) != pending.source_id or m._source_instance(record) != pending.source_instance:
                raise ValueError('The deferred source identity changed.')
            _verify_origin_binding(pending, proof)
            from hero_integration.services.employee_migration import historical_user_scope
            with historical_user_scope(pending.source_instance, historical):
                if (pending.operation in {'HeroTasks','HeroProjectMatches','HeroCalendarEvents'} and
                        (getattr(plan,'adapter_options',None) or {}).get('version')==2):
                    if pending.operation=='HeroTasks':
                        m._load_task_user_directory(None,pending.operation)
                    result = m._migrate_isolated(lambda:m.realize_mapped_bindings(record,run,plan))
                elif pending.operation in {'HeroCustomers', 'HeroProjectMatches'}:
                    action = m._migrate_customer if pending.operation == 'HeroCustomers' else m._migrate_project
                    result = m._migrate_isolated(lambda: action(record, run))
                else:
                    # A long-lived worker must not reuse another run's legacy User directory.
                    m._load_task_user_directory(None, pending.operation)
                    mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
                    scope = m._projection_task_scope(run, plan, mapping, source.source_instance, source.source_instance)
                    result = m._migrate_projection_record(record, run, plan, scope)
            code = result.code
            pending_detail = ''
            if getattr(result, 'pending_relation', False) and code in {
                    'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}:
                code = 'PENDING_REVIEW'
                pending_detail = 'A related assignment or address still requires review.'
                # This worker may have advanced its own native baseline before
                # discovering an owned relation that still needs review.
                saved = json.loads(pending.record_json)
                saved['__hero_origin_binding'] = _origin_binding(
                    pending.source_instance, pending.operation, pending.source_id)
                pending.record_json = json.dumps(saved, ensure_ascii=False)
            if (pending.operation == 'HeroTasks' and result.target_name
                    and code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}):
                from hero_integration.services.direct_mapping import MISSING, lookup
                assignment = next((rule for rule in getattr(plan, 'definitions', ())
                                   if getattr(rule, 'target_doctype', None) == 'ToDo'
                                   and getattr(rule, 'target_field', None) == 'allocated_to'), None)
                raw = lookup(record, assignment.source_path) if assignment else MISSING
                if raw is not MISSING and raw is not None:
                    decision = m._plan_task_assignment(run, {**record, 'target_user_id': raw},
                                                       result.target_name, pending.source_instance)
                    if decision.action in {'BLOCKED', 'PENDING_IDENTITY', 'CONFLICT'}:
                        code = 'CONFLICT' if decision.action == 'CONFLICT' else 'PENDING_REVIEW'
                        pending_detail = decision.detail
            from hero_integration.services.integration_events import record_output
            record_output(run=run.name, source_type={'HeroCustomers': 'customer', 'HeroProjectMatches': 'project', 'HeroTasks': 'Task',
                'HeroCalendarEvents': 'CalendarEvent'}[pending.operation], source_id=pending.source_id,
                result=code, action='hero_to_frappe', detail='Deferred relationship reconciliation: ' + (pending_detail or result.detail or ''),
                target_doctype=result.target_doctype, target_name=result.target_name)
        success = code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}
        needs_review = code == 'PENDING_REVIEW'
        # Dependencies are already present here. Do not repeatedly replay an
        # authority/invalid-identity hold on every scheduler recovery cycle.
        waiting_identity = needs_review and not dependencies_ready(pending.source_instance, pending.operation, dependencies)
        frappe.db.set_value(MAIN, name, {'status': 'Resolved' if success else 'Waiting' if waiting_identity else 'Conflict',
            'record_json': '' if success else pending.record_json,
            'detail': '' if success else 'Assignment remains pending; review its source identity and authority.' if needs_review
                      else 'Native reconciliation requires review: ' + code})
    finally:
        _replaying.reset(token)


def _resolve_available_project_assignee(pending, user_id):
    """Add one newly bound assignee while keeping other relationships pending.

    Never remove assignments in this partial pass. Reuse only the exact last
    source observation acknowledged by the existing Project link.
    """
    if not available(pending.source_instance, 'User', user_id):
        return
    from hero_integration.services import migration as m
    from hero_integration.client.settings import get_configuration
    from hero_integration.integrations.project_assignments import sync
    run = frappe.get_doc('HERO Integration Run', pending.origin_run)
    if run.owner != 'Administrator' and not frappe.db.get_value('User', run.owner, 'enabled'):
        raise PermissionError('The original migration actor is inactive.')
    frappe.set_user(run.owner)
    frappe.only_for('System Manager')
    if run.mode == 'Preview' or run.state in {'Cancelled', 'Failed'}:
        raise ValueError('The original migration does not permit deferred writes.')
    config = get_configuration(fresh=True)
    if config.source_instance != pending.source_instance:
        raise ValueError('The configured source changed.')
    m.validate_run_mappings(run)
    m._source_for_run(run, config)
    plan = m._mapping_plan_for_run(run, pending.operation)
    record = _source_observation(pending)
    proof = record.pop('__hero_origin_binding', None)
    record.pop('__hero_historical_users', None)
    if (plan.mapping_version != pending.mapping_version or str(plan.source_identity(record)) != pending.source_id
            or m._source_instance(record) != pending.source_instance):
        raise ValueError('The deferred Project mapping or source identity changed.')
    _verify_origin_binding(pending, proof)
    link = m._link('project', pending.source_id, pending.source_instance)
    if (not link or link.target_doctype != 'Project' or link.sync_status != 'Synchronized'
            or link.mapping_version != pending.mapping_version or link.source_payload_hash != m._digest(record)
            or not frappe.db.exists('Project', link.target_name)):
        return
    project = frappe.get_doc('Project', link.target_name)
    frappe.has_permission('Project', 'write', doc=project, throw=True)
    assignments = [row for row in record.get('project_match_assignments') or []
                   if isinstance(row, dict) and type(row.get('user_id')) is int and str(row['user_id']) == user_id]
    if assignments:
        sync(link.target_name, {**record, 'project_match_assignments': assignments}, allow_removals=False)


def _resolve_file(pending):
    from hero_integration.services import file_transfer as files, direct_runtime
    read = direct_runtime.resolve_read(pending.operation, pending.source_instance)
    if read.mapping_version != pending.mapping_version:
        raise ValueError('The approved file mapping changed.')
    observation = json.loads(pending.record_json)
    row, detail = observation['row'], observation['detail']
    if str(read.plan.source_identity(row)) != pending.source_id:
        raise ValueError('The deferred file identity changed.')
    links = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
        'source_instance': pending.source_instance, 'source_type': files.SOURCE_TYPE, 'source_id': pending.source_id},
        fields=['name', 'target_name', 'source_payload_hash', 'target_field_hashes', 'target_payload_hash', 'sync_status'],
        limit_page_length=2)
    link = links[0] if len(links) == 1 else None
    if not link or link.source_payload_hash != files.source_hash(row):
        raise ValueError('The imported file observation changed.')
    projects = files._bindings('project', 'Project', pending.source_instance)
    customers = files._bindings('customer', 'Customer', pending.source_instance)
    if 'scope' in observation:
        scope = observation['scope']
        if (not isinstance(scope, dict) or not isinstance(scope.get('project_ids'), list)
                or not isinstance(scope.get('customer_ids'), list)):
            raise ValueError('The deferred file test scope is invalid.')
        projects = {key: value for key, value in projects.items() if key in scope['project_ids']}
        customers = {key: value for key, value in customers.items() if key in scope['customer_ids']}
    target = files.resolve_target(detail, projects, customers)
    if not target[0]:
        raise ValueError('The file attachment is still unresolved.')
    phase = 'images' if pending.operation == 'HeroPictures' else 'documents'
    folder = files._folder(files.ROOT_FOLDER, files.PHASE_FOLDER[phase])
    files._assert_target_baseline(link, files.mapped_values(read, row), lock=True)
    files.relink(link, target, folder)
    fields = files.mapped_values(read, row)
    frappe.db.set_value('HERO Integration Link', link.name, {'target_field_hashes': files._target_baseline(link.target_name, fields),
        'last_synced_on': frappe.utils.now_datetime()})
    return 'UPDATED'
