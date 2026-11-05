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

import frappe

MAIN = 'HERO Pending Reference'
CHILD = 'HERO Pending Dependency'
BATCH = 50
_replaying = ContextVar('hero_reference_replay', default=False)
_writing = ContextVar('hero_pending_reference_write', default=False)


def assert_owned_write():
    if not _writing.get():
        raise PermissionError('Pending reference observations are service-owned.')


@contextmanager
def _write_scope():
    token = _writing.set(True)
    try:
        yield
    finally:
        _writing.reset(token)
OPERATIONS = {'customer': 'HeroCustomers', 'project': 'HeroProjectMatches', 'Task': 'HeroTasks', 'CalendarEvent': 'HeroCalendarEvents'}
TARGETS = {'customer': 'Customer', 'project': 'Project', 'User': 'User', 'partner': 'Employee'}


def installed():
    db = getattr(frappe, 'db', None)
    return bool(db and getattr(db, 'table_exists', lambda _: False)(MAIN))


def key(source, kind, identity):
    return sha256(json.dumps([source, kind, str(identity)], separators=(',', ':')).encode()).hexdigest()


def references(plan, record):
    from hero_integration.services.direct_mapping import lookup
    result = set()
    for rule in plan.definitions:
        kind = {'HERO Customer Link': 'customer', 'HERO Project Link': 'project',
                'HERO User Link': 'User', 'User By Partner ID': 'partner'}.get(rule.transformation)
        kind = {'Customer Relation': 'customer', 'Project Relation': 'project'}.get(rule.purpose, kind)
        value = lookup(record, rule.source_path) if kind else None
        if type(value) is int and value > 0:
            result.add((kind, str(value)))
    for row in record.get('project_match_assignments') or []:
        value = row.get('user_id') if isinstance(row, dict) else None
        if type(value) is int and value > 0:
            result.add(('User', str(value)))
    return sorted(result)


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


def _private_observation(value):
    if isinstance(value, dict):
        return {k: _private_observation(v) for k, v in value.items()
                if k.casefold().replace('_', '') not in
                {'downloadurl', 'signedurl', 'token', 'apitoken', 'apikey', 'accesstoken',
                 'refreshtoken', 'clientsecret', 'secretkey', 'password', 'secret', 'authorization'}}
    if isinstance(value, list):
        return [_private_observation(v) for v in value]
    return value


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
    save(run.name, _source_instance(record), operation, identity, plan.mapping_version,
         record, references(plan, record))


def remember_file(run_name, source, operation, read, record, detail):
    if not run_name or _replaying.get() or not installed():
        return
    document = (detail or {}).get('customer_document') or {}
    links = (detail or {}).get('file_upload_links') or []
    projects = [row.get('project_match_id') for row in (detail or {}).get('file_upload_matches') or []]
    projects += [document.get('project_match_id')] + [row.get('project_match_id') for row in links]
    customers = [document.get('contact_id')] + [row.get('customer_id') for row in links]
    deps = [('project', str(v)) for v in projects if type(v) is int and v > 0]
    deps += [('customer', str(v)) for v in customers if type(v) is int and v > 0]
    save(run_name, source, operation, read.plan.source_identity(record), read.mapping_version,
         {'row': record, 'detail': detail}, sorted(set(deps)))


def invalidate(source, operation, identity):
    """A newer deletion/rejection cancels the old observation atomically."""
    name = key(source, operation, identity)
    if frappe.db.exists(MAIN, name):
        frappe.db.set_value(MAIN, name, {'status': 'Resolved', 'record_json': '',
            'detail': 'Source deletion or rejection observed; deferred writes cancelled.'})


def dependencies_ready(source, operation, dependencies):
    candidates = [available(source, kind, identity) for kind, identity in dependencies]
    if operation in {'HeroDocuments', 'HeroPictures'}:
        return not candidates or any(candidates)
    return all(candidates)


def save(run_name, source, operation, identity, version, record, dependencies):
    name = key(source, operation, identity)
    ready = dependencies_ready(source, operation, dependencies)
    exists = frappe.db.exists(MAIN, name)
    if ready:
        if exists:
            frappe.db.set_value(MAIN, name, {'status': 'Resolved', 'record_json': '', 'detail': ''})
        return
    doc = frappe.get_doc(MAIN, name, for_update=True) if exists else frappe.get_doc({'doctype': MAIN, 'identity_key': name})
    doc.update({'source_instance': source, 'operation': operation, 'source_id': str(identity),
        'origin_run': run_name, 'mapping_version': version, 'status': 'Waiting', 'detail': '',
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
        pending = frappe.db.get_value(MAIN, name, ['source_instance', 'status', 'operation'], as_dict=True)
        if not pending or pending.status != 'Waiting':
            return
        dependencies = frappe.get_all(CHILD, filters={'parent': name, 'parenttype': MAIN},
            fields=['source_type', 'source_id'], limit_page_length=0)
        if dependencies and dependencies_ready(pending.source_instance, pending.operation,
                [(row.source_type, row.source_id) for row in dependencies]):
            identity = (pending.source_instance, dependencies[0].source_type, dependencies[0].source_id)
            if scheduled is not None:
                if identity in scheduled:
                    return
                scheduled.add(identity)
            _enqueue_dependency(pending.source_instance, dependencies[0].source_type, dependencies[0].source_id)
    except Exception:
        frappe.log_error(title='Pending HERO relationship wakeup failed', message='Reference ' + name)


def recover_pending():
    """Bounded restart recovery for lost post-commit notifications, never a reimport."""
    if not installed():
        return
    rows = frappe.get_all(MAIN, filters={'status': 'Waiting'}, fields=['name'],
        order_by='last_checked asc, creation asc', limit_page_length=BATCH)
    scheduled = set()
    for row in rows:
        wake_reference(row.name, scheduled=scheduled)
        frappe.db.set_value(MAIN, row.name, 'last_checked', frappe.utils.now_datetime(), update_modified=False)


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
            resolve_one(row.name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.db.set_value(MAIN, row.name, {'status': 'Conflict',
                'detail': 'Deferred relationship validation failed; refresh the original source record.'})
            frappe.db.commit()
            frappe.log_error(title='Pending HERO relationship failed', message='Reference ' + row.name)
        finally:
            frappe.set_user(actor)
        last = row.name
    if last != after and (len(rows) == BATCH or rows and last != rows[-1].name):
        _enqueue_dependency(source, kind, identity, after=last, enqueue_after_commit=True)


def resolve_one(name):
    pending = frappe.get_doc(MAIN, name, for_update=True)
    if pending.status != 'Waiting' or not dependencies_ready(pending.source_instance, pending.operation,
            [(row.source_type, row.source_id) for row in pending.dependencies]):
        return
    from hero_integration.services import migration as m
    from hero_integration.client.settings import get_configuration
    run = frappe.get_doc('HERO Integration Run', pending.origin_run)
    if run.owner != 'Administrator' and not frappe.db.get_value('User', run.owner, 'enabled'):
        raise PermissionError('The original migration actor is inactive.')
    frappe.set_user(run.owner)
    frappe.only_for('System Manager')
    if run.mode == 'Preview' or run.state in {'Cancelled', 'Failed'}:
        raise ValueError('The original migration does not permit deferred writes.')
    token = _replaying.set(True)
    try:
        config = get_configuration(fresh=True)
        if config.source_instance != pending.source_instance:
            raise ValueError('The configured source changed.')
        if pending.operation in {'HeroDocuments', 'HeroPictures'}:
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
            record = json.loads(pending.record_json)
            if str(plan.source_identity(record)) != pending.source_id or m._source_instance(record) != pending.source_instance:
                raise ValueError('The deferred source identity changed.')
            if pending.operation in {'HeroCustomers', 'HeroProjectMatches'}:
                action = m._migrate_customer if pending.operation == 'HeroCustomers' else m._migrate_project
                result = m._migrate_isolated(lambda: action(record, run))
            else:
                # A long-lived worker must not reuse another run's legacy User directory.
                m._load_task_user_directory(None, pending.operation)
                mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
                scope = m._projection_task_scope(run, plan, mapping, source.source_instance, source.source_instance)
                result = m._migrate_projection_record(record, run, plan, scope)
            code = result.code
            from hero_integration.services.integration_events import record_output
            record_output(run=run.name, source_type={'HeroCustomers': 'customer', 'HeroProjectMatches': 'project', 'HeroTasks': 'Task',
                'HeroCalendarEvents': 'CalendarEvent'}[pending.operation], source_id=pending.source_id,
                result=code, action='hero_to_frappe', detail='Deferred relationship reconciliation: ' + (result.detail or ''),
                target_doctype=result.target_doctype, target_name=result.target_name)
        success = code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}
        frappe.db.set_value(MAIN, name, {'status': 'Resolved' if success else 'Conflict',
            'record_json': '' if success else pending.record_json,
            'detail': '' if success else 'Native reconciliation requires review: ' + code})
    finally:
        _replaying.reset(token)


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
