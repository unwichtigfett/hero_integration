"""Retry only previously observed records when their source-bound identities arrive.

This queue never scans a HERO collection or expands a test run. Source observations
are private, exclude transport credentials, and are removed after reconciliation.
Every replay uses the original actor, frozen mapping and ordinary conflict guards.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from hashlib import sha256
import json
import time
import traceback
from pathlib import Path

import frappe
from hero_integration.services import migration_archive
from hero_integration.services import concurrency
from hero_integration.services import execution_budget
from hero_integration.services.performance import measured, profiled

MAIN = 'HERO Pending Reference'
CHILD = 'HERO Pending Dependency'
BATCH = 50
BINDING_QUEUE = 'hero_binding'
LEGACY_BINDING_WORKERS = 3
MAX_SHARED_WORKERS = 10
BINDING_CHUNK_SIZE = 8
DEFERRED_CREATE = '__hero_deferred_create'
ADDRESS_REVIEW = '__hero_reviewed_primary_address'
NATIVE_TASK_DENIAL = 'Native Task write denied; retained for the next inbound comparison.'
_replaying = ContextVar('hero_reference_replay', default=False)
_project_history = ContextVar('hero_reference_project_history', default=None)
_writing = ContextVar('hero_pending_reference_write', default=False)


class PendingObservationError(ValueError):
    """The frozen source record cannot authorize a relationship write."""


class ReferenceRetryExhausted(RuntimeError):
    """A relationship page has a Waiting observation it could not process."""


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
    return _replaying.get() is True


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
        fields=['name', 'origin_run', 'detail'], limit_page_length=0)


def reference_kind(rule):
    kind = {'HERO Customer Link': 'customer', 'HERO Project Link': 'project',
            'HERO User Link': 'User', 'User By Partner ID': 'partner'}.get(rule.transformation)
    return {'Customer Relation': 'customer', 'Project Relation': 'project'}.get(rule.purpose, kind)


def references(plan, record):
    from hero_integration.services.direct_mapping import lookup
    result = set()
    for rule in plan.definitions:
        kind = reference_kind(rule)
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
            if type(identity) is int and identity > 0 and not migration_archive.primary_address_excluded(record, identity):
                result.add(('customer_address', str(identity)))
    for relation in (getattr(plan, 'adapter_options', None) or {}).get('native_relations', []):
        if relation.get('kind') != 'customer_addresses':
            continue
        rows = lookup(record, relation.get('collection_path', ''))
        if not isinstance(rows, list):
            continue
        primary = lookup(record, relation.get('primary_identity_path', ''))
        for row in rows:
            if not isinstance(row, dict):
                continue
            deleted = lookup(row, relation.get('deleted_path', ''))
            if type(deleted) is not bool or deleted and not migration_archive.includes_archived():
                continue
            value = lookup(row, relation.get('identity_path', ''))
            if (type(value) in (int, str) and str(value).strip()
                    and (type(value) is not int or value > 0) and str(value) != str(primary)):
                result.add(('customer_address', str(value).strip()))
    return sorted(_active_dependencies(record.get('_hero_source_instance') or 'default', result))


def _active_dependencies(source, dependencies):
    from hero_integration.services.employee_migration import historical_users, historical_partners
    excluded = {'User': historical_users(source), 'partner': historical_partners(source)}
    return [(kind, identity) for kind, identity in dependencies if identity not in excluded.get(kind, ())]


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
    from hero_integration.services.migration import _digest

    current = _origin_binding(pending.source_instance, pending.operation, pending.source_id)
    if isinstance(proof, dict) and proof and current:
        if proof == current:
            return
        # The first subsequent worker can record its observation of the same
        # payload. Prove the exact frozen source before accepting that addition.
        if not proof.get('observed_source_hash') and current == {
                **proof, 'observed_source_hash': proof.get('source_payload_hash')}:
            record = _source_observation(pending)
            for field in ('__hero_origin_binding', DEFERRED_CREATE, ADDRESS_REVIEW, '__hero_historical_users', '__hero_historical_partners'):
                record.pop(field, None)
            if _digest(record) == current['observed_source_hash']:
                return
    raise ValueError('The imported source observation or binding changed; refresh the source record before retrying.')



def remember(run, source_type, record, result):
    if (_replaying.get() or run.mode == 'Preview' or source_type not in OPERATIONS or not installed()):
        return
    from hero_integration.services.migration import _mapping_plan_for_run, _source_instance
    operation = OPERATIONS[source_type]
    if migration_archive.excluded(operation, record):
        return
    plan = _mapping_plan_for_run(run, operation)
    try:
        identity = plan.source_identity(record)
    except ValueError:
        return  # Invalid upstream identities need review; there is no safe retry key.
    if result.code in {'SKIPPED_SOURCE_DELETED', 'SKIPPED_REJECTED', 'SKIPPED_CONTACT_PERSON'}:
        invalidate(_source_instance(record), operation, identity)
        return
    source = _source_instance(record)
    from hero_integration.services.employee_migration import historical_users
    historical = historical_users(source)
    observation = {**record, '__hero_origin_binding': _origin_binding(source, operation, identity)}
    # This service owns the admission evidence even if a reader returns the same JSON alias.
    observation.pop(DEFERRED_CREATE, None)
    dependencies = references(plan, record)
    if result.code == 'DEFERRED':
        if observation['__hero_origin_binding'] is not None:
            raise PendingObservationError('Deferred creation already has a target.')
        if not result.required_references or not set(result.required_references).issubset(dependencies):
            raise PendingObservationError('Mandatory reference has no verifiable source identity.')
        observation[DEFERRED_CREATE] = result.required_references
    observation.pop(ADDRESS_REVIEW, None)
    verified_object = getattr(result, "verified_object", "")
    address = record.get("address")
    if (operation == "HeroCustomers" and verified_object and not result.pending_relation
            and plan.binding_contract and any(rule.target_doctype == "Address" for rule in plan.definitions)
            and isinstance(address, dict) and type(address.get("id")) is int and address["id"] > 0
            and not migration_archive.primary_address_excluded(record, address["id"])):
        observation[ADDRESS_REVIEW] = {"object": verified_object, "address_id": str(address["id"]),
                                       "parent_field_conflict": result.code == "CONFLICT"}
    from hero_integration.services.employee_migration import historical_partners
    observation.pop('__hero_historical_partners', None)
    if historical_partners(source):
        observation['__hero_historical_partners'] = sorted(historical_partners(source))
    observation.pop('__hero_historical_users', None)
    if historical:
        observation['__hero_historical_users'] = sorted(historical)
    save(run.name, source, operation, identity, plan.mapping_version,
         observation, dependencies,
         defer_empty=bool((getattr(plan, 'adapter_options', None) or {}).get('native_relations')) or (
             operation == 'HeroProjectMatches' and isinstance(record.get('project_match_assignments'), list)) or (
             operation == 'HeroCustomers' and any(rule.purpose in {'Customer Primary Contact', 'Customer Home Phone'}
                 or rule.target_doctype in {'Contact', 'Address'} or (rule.target_doctype == 'Customer'
                     and rule.target_field in {'first_name', 'last_name', 'email_id', 'mobile_no'})
                 for rule in plan.definitions)),
         result_code='PENDING_REVIEW' if result.code != 'CONFLICT' and getattr(result, 'pending_relation', False) else result.code)


def file_dependencies(detail, cohort=None):
    from hero_integration.services.file_transfer import file_references
    projects, customers, _ = file_references(detail)
    deps = [('project', value) for value in projects] + [('customer', value) for value in customers]
    if cohort is not None:
        project_ids, customer_ids = set(cohort['project_ids']), set(cohort['customer_ids'])
        deps = [(kind, identity) for kind, identity in deps if identity in (project_ids if kind == 'project' else customer_ids)]
    return sorted(set(deps))


def remember_file(run_name, source, operation, read, record, detail, *, cohort=None):
    if not run_name or _replaying.get() or not installed():
        return
    observation = {'row': {k: v for k, v in record.items() if k != '_hero_file_detail'}, 'detail': detail}
    if cohort is not None:
        observation['scope'] = {key: sorted(set(cohort[key])) for key in ('project_ids', 'customer_ids')}
    save(run_name, source, operation, read.plan.source_identity(record), read.mapping_version,
         observation, file_dependencies(detail, cohort))


def invalidate(source, operation, identity):
    """A newer deletion/rejection cancels the old observation atomically."""
    for observed in _observations(source, operation, identity):
        frappe.db.set_value(MAIN, observed.name, {'status': 'Resolved', 'record_json': '',
            'detail': 'Source record is no longer eligible; deferred writes cancelled.'})


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


def save(run_name, source, operation, identity, version, record, dependencies, *, result_code=None, defer_empty=False, native_task_denial=False):
    if budget := execution_budget.current_budget():
        budget.reserve_many(dependencies, relation='observation')
    name = observation_key(source, operation, identity, run_name)
    for observed in _observations(source, operation, identity):
        if native_task_denial and observed.detail != NATIVE_TASK_DENIAL:
            # A scalar write refusal is not a new decision about earlier relationships.
            if observed.name == name:
                raise PendingObservationError('Another observation owns this Task review.')
            continue
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
    review = (ready or result_code != 'PENDING_REVIEW') and result_code != 'DEFERRED' and not successful and not (operation == 'HeroCustomers' and record.get(ADDRESS_REVIEW))
    doc = frappe.get_doc(MAIN, name, for_update=True) if exists else frappe.get_doc({'doctype': MAIN, 'identity_key': name})
    doc.update({'source_instance': source, 'operation': operation, 'source_id': str(identity),
        'origin_run': run_name, 'mapping_version': version, 'status': 'Conflict' if review else 'Waiting',
        'realization_started': 0,
        'detail': NATIVE_TASK_DENIAL if native_task_denial else 'Relationship requires review.' if review else '',
        'record_json': json.dumps(_private_observation(record), ensure_ascii=False),
        'dependencies': [{'dependency_key': key(source, kind, value), 'source_type': kind, 'source_id': value}
                         for kind, value in dependencies]})
    with _write_scope():
        doc.save(ignore_permissions=True) if exists else doc.insert(ignore_permissions=True)
    if not execution_budget.current_budget():
        frappe.db.after_commit.add(lambda: wake_reference(name))
    # Bounded children notify reconcile_completed_run with their exact origin.



def _dedicated_workers():
    conf = getattr(frappe, 'conf', None)
    return bool(conf and BINDING_QUEUE in (conf.get('workers') or {}))


def _shared_workers():
    return concurrency.shared_width(frappe)


def _queue():
    return BINDING_QUEUE if _dedicated_workers() else 'default'


def _enqueue_pending(method, **kwargs):
    """Pending references are durable when queue capacity temporarily runs out."""
    try:
        frappe.enqueue(method, **kwargs)
    except getattr(frappe, 'QueueOverloaded', ()):
        return False
    return True


def _enqueue_dependency(source, kind, identity, *, after='', enqueue_after_commit=False, test_revision=None, through=None):
    # For test notifications a later provider commit may complete a User's
    # registry binding while an earlier notification is still running. Do not
    # coalesce those edges. The observation row lock makes redelivery safe.
    cursor = [kind, str(identity), after]
    if through is not None:
        cursor.append(through)
    job = key(source, 'deferred-page', cursor)
    _enqueue_pending('hero_integration.services.deferred_references.resolve_dependency', queue=_queue(),
        source=source, kind=kind, identity=identity, after=after, test_revision=test_revision,
        enqueue_after_commit=enqueue_after_commit, **({'through': through} if through is not None else {}),
        **({'job_id': 'hero-reference-' + job, 'deduplicate': True} if test_revision is None else {}))


def binding_updated(doc, method=None):
    budget = execution_budget.current_budget()
    if budget and (budget.scope != 'migration' or budget.version != 3 or budget.source_instance != doc.source_instance):
        return
    if doc.source_type not in TARGETS or doc.sync_status != 'Synchronized':
        return
    if method == 'on_update' and doc.source_type == 'customer_address':
        previous = doc.get_doc_before_save()
        # Replaying a Customer can save its synchronized Address link again.
        # Other providers (e.g. Employee/User) also depend on external fields.
        identity_fields = ('source_system', 'source_instance', 'source_type', 'source_id',
                           'target_doctype', 'target_name', 'target_incarnation', 'sync_status')
        if previous is not None and all(previous.get(field) == doc.get(field) for field in identity_fields):
            return
    if not installed():
        return
    if not frappe.db.sql('''SELECT d.name FROM `tabHERO Pending Dependency` d
        JOIN `tabHERO Pending Reference` p ON p.name=d.parent AND d.parenttype=%s
        WHERE d.dependency_key=%s AND p.status='Waiting' LIMIT 1''',
            (MAIN, key(doc.source_instance, doc.source_type, doc.source_id))):
        return
    revision = budget.source_revision if budget else None
    if doc.source_type == 'customer_address':
        # Frappe keeps after_commit callbacks across a partial savepoint rollback.
        # The address writer can roll several sibling inserts back as a unit.
        frappe.db.after_commit.add(partial(_wake_committed_address, doc.source_instance, doc.source_id, revision))
    else:
        _enqueue_dependency(doc.source_instance, doc.source_type, doc.source_id, enqueue_after_commit=True,
                            test_revision=revision)


def _wake_committed_address(source, identity, test_revision):
    if available(source, 'customer_address', identity):
        _enqueue_dependency(source, 'customer_address', identity, test_revision=test_revision)


def _mixed_run_rows(*, origin=None, names=()):
    """Admit individual waiting observations of mixed-outcome runs."""
    if not names and not origin:
        return set()
    condition, value = ("p.origin_run=%s", origin) if origin else ("p.name IN %s", tuple(names))
    # Pending rows are service-owned. Direct writers recheck their frozen
    # reader and object proof. Native writers also need an accepted receipt
    # for this exact source and target, or their original deferred-create proof.
    receipt_type = """CASE p.operation WHEN 'HeroTasks' THEN 'Task' WHEN 'HeroCalendarEvents' THEN 'CalendarEvent'
                           WHEN 'HeroCustomers' THEN 'customer' WHEN 'HeroProjectMatches' THEN 'project' END"""
    rows = frappe.db.sql(f"""SELECT p.name FROM `tabHERO Pending Reference` p
        JOIN `tabHERO Integration Run` r ON r.name=p.origin_run
        WHERE {condition} AND p.status='Waiting' AND p.source_instance=r.source_instance
        AND r.state='Completed With Conflicts' AND r.mode='Execute'
        AND ((p.operation='HeroCustomers' AND JSON_VALUE(p.record_json, '$.{ADDRESS_REVIEW}.object') IS NOT NULL)
          OR (r.testing_enabled=0 AND (
              (p.operation,r.action) IN (('HeroDocuments','Document Migration'),('HeroPictures','Picture Migration'),
                    ('HeroEmployees','Employee Migration'),('HeroServices','Service Migration'))
              OR ((p.operation,r.action) IN (('HeroTasks','Projection Migration'),('HeroCalendarEvents','Schedule Migration'),
                    ('HeroProjectMatches','Project Migration'),('HeroCustomers','Customer Migration'),
                    ('HeroCustomers','Project Migration'))
                  AND EXISTS (SELECT 1 FROM `tabHERO Integration Record` h
                    WHERE h.run=p.origin_run AND h.source_id=p.source_id AND h.technical_action='hero_to_frappe'
                    AND h.source_type={receipt_type}
                    AND ((h.result IN ('CREATED','UPDATED','SKIPPED','CONVERGED','OBSERVED','UNCHANGED')
                          AND h.target_doctype=JSON_VALUE(p.record_json,'$.__hero_origin_binding.target_doctype')
                          AND h.target_name=JSON_VALUE(p.record_json,'$.__hero_origin_binding.target_name'))
                        OR (h.result='DEFERRED' AND JSON_LENGTH(JSON_EXTRACT(p.record_json,'$.{DEFERRED_CREATE}'))>0)))
                  AND NOT EXISTS (SELECT 1 FROM `tabHERO Integration Record` h
                    WHERE h.run=p.origin_run AND h.source_id=p.source_id AND h.technical_action='hero_to_frappe'
                    AND h.source_type={receipt_type}
                    AND h.result NOT IN ('CREATED','UPDATED','SKIPPED','CONVERGED','OBSERVED','UNCHANGED','DEFERRED')
                    AND NOT (h.result='BLOCKED_RECORD' AND p.detail=CONCAT('Technical retry: ',h.name))
                    AND NOT (h.result='BLOCKED_RECORD' AND h.detail='Deferred relationship reconciliation: ERPNext rejected the record (QueueOverloaded); see Error Log.')
                    AND NOT (h.result='PENDING_REVIEW' AND COALESCE(h.detail,'') LIKE
                        'Deferred relationship reconciliation: %%'))))))
        {"LIMIT 1" if origin else ""}""", (value,))
    return {row[0] for row in rows}


def wake_reference(name, *, scheduled=None):
    """Post-commit race closure: enqueue only if every missing identity arrived."""
    try:
        pending = frappe.db.get_value(MAIN, name, ['source_instance', 'status', 'operation', 'origin_run', 'realization_started'], as_dict=True)
        if not pending or pending.status != 'Waiting':
            return
        state = frappe.db.get_value('HERO Integration Run', pending.origin_run, 'state')
        if state != 'Completed' and not (state == 'Completed With Conflicts' and _mixed_run_rows(names=[name])):
            return
        dependencies = (frappe.get_all(CHILD, filters={'parent': name, 'parenttype': MAIN},
            fields=['source_type', 'source_id'], limit_page_length=0)
            if getattr(pending, 'realization_started', 1) else [])
        _wake_observed(name, pending, dependencies, scheduled=scheduled)
    except Exception:
        frappe.log_error(title='Pending HERO relationship wakeup failed', message='Reference ' + name)


def _wake_observed(name, pending, dependencies, *, scheduled=None, availability=None):
    if not getattr(pending, 'realization_started', 1) or not dependencies:
        _enqueue_pending('hero_integration.services.deferred_references.resolve_reference', queue=_queue(), name=name,
            job_id='hero-reference-' + name, deduplicate=True)
    else:
        pairs = [(row.source_type, row.source_id) for row in dependencies]
        if dependencies_ready(pending.source_instance, pending.operation, pairs, availability=availability):
            kind, source_id = pairs[0]
        elif pending.operation in ('HeroTasks', 'HeroCalendarEvents', 'HeroDocuments', 'HeroPictures'):
            # A deleted Project or a lead will never emit a native binding notification.
            project = next((pair for pair in pairs if pair[0] == 'project' and
                not (availability.get(pair, False) if availability is not None else available(pending.source_instance, *pair))), None)
            if project is None:
                return
            kind, source_id = project
        else:
            return
        identity = (pending.source_instance, kind, source_id)
        if scheduled is not None:
            if identity in scheduled:
                return
            scheduled.add(identity)
        _enqueue_dependency(*identity)


@profiled('binding.recovery')
def recover_pending():
    """Bounded restart recovery for lost post-commit notifications, never a reimport."""
    from hero_integration.services.migration_waves import recover_watches
    recover_watches()
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
    partial = _mixed_run_rows(names=[row.name for row in rows if row.origin_run not in completed])
    eligible = [row for row in rows if row.origin_run in completed or row.name in partial]
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
        if row.origin_run in completed or row.name in partial:
            try:
                _wake_observed(row.name, row, dependencies[row.name], scheduled=scheduled,
                    availability=readiness.get(row.source_instance, {}))
            except Exception:
                frappe.log_error(title='Pending HERO relationship wakeup failed', message='Reference ' + row.name)
        frappe.db.set_value(MAIN, row.name, 'last_checked', frappe.utils.now_datetime(), update_modified=False)


def _enqueue_completed_run(run_name, after='', *, shard=None, shard_count=None):
    _enqueue_pending('hero_integration.services.deferred_references.reconcile_completed_run',
        run_name=run_name, after=after, enqueue_after_commit=True, queue=_queue(),
        **({'shard': shard} if shard is not None else {}),
        **({'shard_count': shard_count} if shard_count is not None else {}),
        job_id='hero-reference-run-' + key(run_name, 'page', [shard, after] if shard_count is None
                                           else [shard_count, shard, after]), deduplicate=True)


def _completed_for_replay(run):
    return run.state == 'Completed' or (run.state == 'Completed With Conflicts'
        and getattr(run, 'budget_contract_version', 0) == 3 and getattr(run, 'testing_enabled', 0))


def notify_completed_run(run_name):
    """Wake a successful area's observations; recovery covers a lost notification."""
    try:
        if not installed():
            return
        run = frappe.db.get_value('HERO Integration Run', run_name, ['state', 'mode', 'budget_contract_version', 'testing_enabled'], as_dict=True)
        if (not run or run.mode == 'Preview' or
                not (_completed_for_replay(run) or run.state == 'Completed With Conflicts' and _mixed_run_rows(origin=run_name))
                or not frappe.db.exists(MAIN, {'origin_run':run_name, 'status':'Waiting'})):
            return
        if _dedicated_workers():
            width = _shared_workers()
            for shard in range(width or LEGACY_BINDING_WORKERS):
                _enqueue_completed_run(run_name, shard=shard, shard_count=width or None)
        else:
            _enqueue_completed_run(run_name)
    except Exception:
        frappe.log_error(title='Pending HERO relationship completion wakeup failed', message='Run ' + run_name)


@concurrency.limited_worker
def reconcile_completed_run(run_name, after='', shard=None, shard_count=None):
    """Realize one bounded page belonging only to this successful origin run."""
    run = frappe.get_doc('HERO Integration Run', run_name)
    if (run.mode == 'Preview' or not (_completed_for_replay(run)
            or run.state == 'Completed With Conflicts' and _mixed_run_rows(origin=run_name))):
        return
    # Previous queue entries without a shard retain their original cursor.
    options = {'shard': shard} if shard is not None else {}
    if shard_count is not None:
        options['shard_count'] = shard_count
    page_size = {'limit': BINDING_CHUNK_SIZE} if shard_count is not None else {}
    cursor, more = reconcile_source_page(run.source_instance, after, origin_runs=[run_name], **page_size, **options)
    if more and cursor != after:
        _enqueue_completed_run(run_name, cursor, **options)


def _retryable_reference_error(error):
    types = tuple(kind for name in ('QueryDeadlockError', 'QueryTimeoutError', 'TimestampMismatchError',
                                    'RetryBackgroundJobError')
                  if isinstance(kind := getattr(frappe, name, None), type))
    internal_error = getattr(frappe.db, 'InternalError', ())
    return (isinstance(error, types)
            or (isinstance(internal_error, type) and isinstance(error, internal_error)
                and (frappe.db.is_deadlocked(error) or frappe.db.is_timedout(error))))


def _resolve_committed(name, *, check_owner=None, **kwargs):
    """Retry only the affected reference transaction with its original actor."""
    actor = frappe.session.user
    for attempt in range(3):
        try:
            if check_owner and not check_owner():
                frappe.db.rollback()
                return False
            resolve_one(name, **kwargs)
        except Exception as error:
            if not _retryable_reference_error(error):
                raise
            frappe.db.rollback()
            frappe.set_user(actor)
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
        else:
            # Commit callbacks may fail after persistence. The failure handler
            # checks the durable state before deciding what remains eligible.
            frappe.db.commit()
            return


def _record_failure(name, error, *, check_owner=None, require_complete=False):
    """Re-read committed state before classifying a failed write or callback."""
    from hero_integration.services.test_environment import EnvironmentUnavailable
    frappe.db.rollback()
    if check_owner and not check_owner():
        frappe.db.rollback()
        return False
    if isinstance(error, getattr(frappe, 'QueueOverloaded', ())):
        return  # Committed Waiting observation is picked up by bounded recovery.
    if isinstance(error, EnvironmentUnavailable):
        frappe.db.rollback()
        return
    diagnostic = {'reference': name, 'error_type': type(error).__name__,
        'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)[-12:]]}
    # COMMIT can succeed before an after_commit callback raises. Another worker
    # may also have superseded this observation after our rollback released locks.
    status = frappe.db.get_value(MAIN, name, 'status', for_update=True)
    diagnostic['committed_status'] = status
    if _retryable_reference_error(error):
        # The scheduler already rediscovers Waiting observations. Escalating
        # a single retry through Frappe re-enters the entire job and can lose
        # its outer finalizer after an inner attempt has committed successfully.
        frappe.db.rollback()
        if status == 'Waiting':
            frappe.log_error(title='Pending HERO relationship deferred after transient error',
                             message=json.dumps(diagnostic, sort_keys=True))
            if require_complete:
                raise ReferenceRetryExhausted(
                    'Relationship reconciliation could not process its current page. '
                    'Continue from the last confirmed checkpoint.'
                ) from error
        return

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
def reconcile_source_page(source, after='', limit=BATCH, *, origin_runs=None, operations=None, shard=None, shard_count=None,
                          require_complete=False):
    """Recheck one stable page at the end of a full migration.

    Still-missing dependencies remain Waiting for the normal binding wakeup.
    Each completed reconciliation is committed independently, as in the worker.
    """
    rows = _waiting_page(source, after, limit, origin_runs, operations, shard=shard, shard_count=shard_count)
    _realize_names([row.name for row in rows], require_complete=require_complete)
    return (rows[-1].name if rows else after), len(rows) == limit


def _waiting_page(source, after, limit, origin_runs, operations, *, shard=None, shard_count=None):
    if not installed():
        raise RuntimeError('Pending relationship storage is not installed.')
    if origin_runs is not None and not origin_runs:
        return []
    origin_clause = ' AND origin_run IN %s' if origin_runs is not None else ''
    operation_clause = ' AND operation IN %s' if operations is not None else ''
    if operations is not None and not operations:
        return []
    params = (source,after)
    if origin_runs is not None: params += (tuple(origin_runs),)
    if operations is not None: params += (tuple(operations),)
    # Observation names are SHA-256 digests. Contiguous PK ranges let every
    # lane paginate independently without a function or offset on the index.
    shard_clause = ''
    if shard is not None:
        width = LEGACY_BINDING_WORKERS if shard_count is None else shard_count
        if (type(width) is not int or not 1 <= width <= MAX_SHARED_WORKERS or
                type(shard) is not int or not 0 <= shard < width):
            raise ValueError('Invalid binding shard.')
        lower = format(16 * shard // width, 'x') if shard else ''
        upper = format(16 * (shard + 1) // width, 'x') if shard + 1 < width else ''
        if lower:
            shard_clause += ' AND name>=%s'
            params += (lower,)
        if upper:
            shard_clause += ' AND name<%s'
            params += (upper,)
    params += (limit,)
    return frappe.db.sql('''SELECT name FROM `tabHERO Pending Reference`
        WHERE source_instance=%s AND status='Waiting' AND name>%s''' + origin_clause + operation_clause + shard_clause +
        ' ORDER BY name LIMIT %s', params, as_dict=True)


def resolve_reference(name):
    """Apply the same committed retry/failure semantics to a single queued wakeup."""
    _realize_names([name])


def _realize_names(names, *, check_owner=None, require_complete=False):
    if not names:
        return
    with _page_project_history():
        actor = frappe.session.user
        ownership = {'check_owner': check_owner} if check_owner else {}
        for name in names:
            try:
                result = _resolve_committed(name, **ownership)
            except Exception as error:
                result = _record_failure(name, error, require_complete=require_complete, **ownership)
            finally:
                frappe.set_user(actor)
            if result is False:
                return  # The cancelled or superseded owner has no further work.


@contextmanager
def _page_project_history():
    token = _project_history.set({})
    try:
        yield
    finally:
        _project_history.reset(token)


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
    if stage < len(FINAL_RELATION_ORDER) and (_dedicated_workers() or state.get('binding_batch')):
        from hero_integration.services.binding_dispatch import advance_page
        advance_page(source, origin_runs, state, FINAL_RELATION_ORDER[stage])
        return False
    if stage<len(FINAL_RELATION_ORDER):
        cursor,more=reconcile_source_page(source,state.get('reconciliation_after',''),
            origin_runs=origin_runs,operations=(FINAL_RELATION_ORDER[stage],), require_complete=True)
        state['reconciliation_after']=cursor if more else ''
        if not more: state['reconciliation_stage']=stage+1
        return False
    # Missing targets stay eligible for their exact dependency notification.
    state['reconciliation_waiting']=frappe.db.count(MAIN,{'source_instance':source,
        'origin_run':['in',list(origin_runs)],'status':'Waiting'})
    state['reconciliation_conflicts']=frappe.db.count(MAIN,{'source_instance':source,
        'origin_run':['in',list(origin_runs)],'status':'Conflict'})
    state['reconciliation_done']=True
    return True


@profiled('binding.dependency_page')
@concurrency.limited_worker
def resolve_dependency(source, kind, identity, after='', test_revision=None, through=None):
    """One bounded keyset page per job; no retry loop or upstream collection read."""
    if not installed() or kind not in TARGETS:
        return
    if kind == 'customer_address' and not available(source, kind, identity):
        return  # A queued address notification can outlive its binding.
    # A test provider may wake only observations admitted by the same test
    # contract on this site. resolve_one rechecks the origin's own frozen budget.
    scope = (" AND r.testing_enabled=1 AND r.budget_contract_version=3 AND r.source_binding_revision=%s"
             " AND r.mode!='Preview' AND r.state IN ('Completed','Completed With Conflicts')"
             if test_revision is not None else "")
    params = (MAIN, source, key(source, kind, identity), after)
    if test_revision is not None:
        params += (test_revision,)
    if through is not None:
        scope += " AND p.name<=%s"
        params += (through,)
    rows = frappe.db.sql('''SELECT DISTINCT p.name FROM `tabHERO Pending Reference` p
        JOIN `tabHERO Pending Dependency` d ON d.parent=p.name AND d.parenttype=%s
        JOIN `tabHERO Integration Run` r ON r.name=p.origin_run
        WHERE p.source_instance=%s AND p.status='Waiting' AND r.state IN ('Completed','Completed With Conflicts')
        AND d.dependency_key=%s AND p.name>%s''' + scope +
        ' ORDER BY p.name LIMIT %s', (*params, BATCH), as_dict=True)
    width = _shared_workers() if through is None and len(rows) > 1 else 0
    if width > 1:
        # Freeze contiguous endpoints, not observations or authorization. Each
        # delivery re-reads its exact provider/revision scope before writing.
        count = min(width, len(rows))
        lower = after
        for index in range(1, count + 1):
            upper = rows[len(rows) * index // count - 1].name
            _enqueue_dependency(source, kind, identity, after=lower, through=upper,
                                enqueue_after_commit=True, test_revision=test_revision)
            lower = upper
        if len(rows) == BATCH:
            _enqueue_dependency(source, kind, identity, after=lower, enqueue_after_commit=True,
                                test_revision=test_revision)
        return
    actor = frappe.session.user
    last = after
    deadline = time.monotonic() + 120
    with _page_project_history():
        for row in rows:
            if time.monotonic() >= deadline:
                break
            try:
                _resolve_committed(row.name, dependency=(kind, str(identity)))
            except Exception as error:
                _record_failure(row.name, error)
            finally:
                frappe.set_user(actor)
            last = row.name
    if last != after and last != through and (len(rows) == BATCH or rows and last != rows[-1].name):
        _enqueue_dependency(source, kind, identity, after=last, enqueue_after_commit=True, test_revision=test_revision,
                            **({'through': through} if through is not None else {}))


def finish_standalone_cohort(run):
    """Settle only this root's observations before confirming the standalone run."""
    if (getattr(run, 'budget_contract_version', 0) not in (2, 3) or run.execution_budget_run != run.name
            or run.action == 'Full Migration'):
        return ''
    budget = execution_budget.for_run(run)
    selection = budget.selection()
    if not selection:
        return 'The run has no frozen cohort.'
    main_type = selection['main_type']
    writer_status = {(r.object_type, r.external_id): r.outcome for r in frappe.get_all(
        'HERO Budget Outcome', filters={'budget':budget.budget_id, 'phase':'writer'},
        fields=['object_type','external_id','outcome'], limit_page_length=0)}
    if budget.version == 3:
        # Writer receipts and their pending observations commit together. The
        # existing completion notification runs the relationship pass after
        # the worker has published the scalar result and released its lease.
        if any((main_type, identity) not in writer_status for identity in selection['ids']):
            return 'The frozen cohort contains a record without a writer receipt.'
        if any(outcome in ('missing', 'failed') for outcome in writer_status.values()):
            return 'The frozen cohort contains a missing or failed writer outcome.'
        return ''
    problems = {}
    if run.mode != 'Preview':
        # One pending observation per operation/identity/origin, including the
        # selected customers of a standalone Project import.
        maximum = len(selection['ids']) + sum(len(ids) for ids in selection['evidence'].get('members', {}).values())
        entries = frappe.get_all(MAIN, filters={'origin_run':run.name, 'source_instance':run.source_instance,
            'status':['in', ['Waiting','Conflict']]}, fields=['name','operation','source_id','status'],
            order_by='creation,name', limit_page_length=maximum + 1)
        if len(entries) > maximum:
            return 'The run has more pending relationship observations than frozen source identities.'
        for entry in entries:
            kind, identity = _pending_identity(entry)
            key = (kind, str(identity))
            if not budget.allows_record(*key):
                return 'A relationship observation has no frozen owner.'
            if entry.status == 'Waiting' and writer_status.get(key) == 'processed':
                try:
                    _resolve_committed(entry.name, finishing_run=run)
                except Exception as error:
                    _record_failure(entry.name, error)
            status = frappe.db.get_value(MAIN, entry.name, 'status', for_update=True)
            if status != 'Resolved':
                problems[key] = 'missing' if status == 'Waiting' else 'conflict'
    # Missing dependency results also invalidate their owning primary records.
    for kind, key, child_kind, child_id in selection['evidence'].get('edges', ()):
        child = (child_kind, child_id)
        if child in problems or writer_status.get(child) in ('failed','conflict','missing'):
            problems.setdefault((kind, key), 'missing')
    for identity in selection['ids']:
        key = (main_type, identity)
        if key not in writer_status:
            problems[key] = 'missing'
    for key, status in problems.items():
        budget.outcome(*key, status, phase='relationships')
    # A replay may have resolved an observation that was missing on the first pass.
    for row in frappe.get_all('HERO Budget Outcome', filters={'budget':budget.budget_id,
            'phase':'relationships','outcome':['!=','processed']}, fields=['object_type','external_id'],limit_page_length=0):
        key = (row.object_type,row.external_id)
        if key not in problems and writer_status.get(key) == 'processed':
            budget.outcome(*key, 'processed', phase='relationships')
    if problems or any(s != 'processed' for s in writer_status.values()):
        return 'The frozen cohort contains missing, failed or conflicting records or required relationships.'
    return ''


def _pending_identity(pending):
    kinds = {operation: kind for kind, operation in OPERATIONS.items()}
    kinds.update(HeroEmployees='partner', HeroServices='supply_service',
                 HeroDocuments='file_upload', HeroPictures='file_upload')
    identity = pending.source_id
    if pending.operation in ('HeroDocuments', 'HeroPictures'):
        doc = pending if getattr(pending, 'record_json', None) else frappe.get_doc(MAIN, pending.name)
        identity = _source_observation(doc)['row']['id']
    return kinds[pending.operation], identity


@profiled('binding.resolve')
def resolve_one(name, *, dependency=None, finishing_run=None):
    pending = frappe.get_doc(MAIN, name, for_update=True)
    if pending.status != 'Waiting':
        return
    run = (frappe.get_doc('HERO Integration Run', pending.origin_run, for_update=True) if finishing_run
           else frappe.get_doc('HERO Integration Run', pending.origin_run))
    from hero_integration.services.test_environment import guard_run
    guard_run(run)
    if finishing_run and (run.name != finishing_run.name or run.execution_budget_run != run.name
            or run.budget_contract_version not in (2, 3) or run.state != 'Running' or run.started_on != finishing_run.started_on
            or run.owner != frappe.session.user):
        raise ValueError('The original worker no longer owns this cohort.')
    if run.mode == 'Preview' or run.state in {'Cancelled', 'Failed'}:
        return  # A queued notification can outlive its origin. Preserve the observation.
    # A dependency can arrive while its originating area is still running.
    # Keep its observation intact until that exact run completed successfully.
    if not finishing_run and not _completed_for_replay(run) and not (run.state == 'Completed With Conflicts'
            and (pending.operation == 'HeroCustomers' and _source_observation(pending).get(ADDRESS_REVIEW)
                 or name in _mixed_run_rows(names=[name]))):
        return
    with migration_archive.run_selection(run), execution_budget.run_scope(run) as budget:
        if budget:
            if budget.version == 3:
                from hero_integration.services.test_environment import require_environment
                selection = budget.selection()
                if not selection or selection.get('environment') != require_environment():
                    raise ValueError('Deferred observation belongs to another test environment.')
            if run.state == 'Completed With Conflicts' and budget.version == 3:
                kind, identity = _pending_identity(pending)
                receipt = frappe.db.get_value('HERO Budget Outcome', {'budget': budget.budget_id,
                    'object_type': kind, 'external_id': str(identity), 'phase': 'writer'}, 'outcome')
                if receipt not in ('processed', 'pending') and not (
                        pending.operation == 'HeroCustomers' and _source_observation(pending).get(ADDRESS_REVIEW)):
                    return
            budget.reserve_record(*_pending_identity(pending),
                                  ((row.source_type, row.source_id) for row in pending.dependencies), relation='replay')
        if (budget and budget.version == 3 and getattr(run, 'execution_budget_run', None) not in (None, run.name) and
                pending.operation in ('HeroTasks', 'HeroProjectMatches', 'HeroCalendarEvents')):
            from hero_integration.services.file_migration import _full, historical_employee_ids
            from hero_integration.services.employee_migration import historical_user_scope
            users, partners = historical_employee_ids(_full(run.execution_budget_run), run.source_instance,
                                                     run.source_binding_revision)
            with historical_user_scope(run.source_instance, users, partners):
                return _resolve_admitted(pending, run, name, dependency)
        return _resolve_admitted(pending, run, name, dependency)


def _accepted_customer_receipt(pending, record):
    proof = record.get('__hero_origin_binding') or {}
    if proof.get('target_doctype') != 'Customer' or not proof.get('target_name'):
        return False
    return bool(frappe.db.exists('HERO Integration Record', {
        'run': pending.origin_run, 'source_type': 'customer', 'source_id': pending.source_id,
        'technical_action': 'hero_to_frappe', 'target_doctype': 'Customer', 'target_name': proof['target_name'],
        'result': ['in', ['CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED']],
    }))


def _historical_projects(pending, run, dependencies, record, config):
    """Positive source evidence for unbound references in regular, frozen imports only."""
    if (pending.operation not in ('HeroTasks', 'HeroCalendarEvents', 'HeroDocuments', 'HeroPictures')
            or run.mode != 'Execute' or getattr(run, 'testing_enabled', None) != 0
            or not getattr(run, 'graphql_service', None) or run.source_instance != pending.source_instance):
        return {}
    ids = {str(i) for kind, i in dependencies if kind == 'project' and str(i).isascii()
           and str(i).isdecimal() and str(int(i)) == str(i) and int(i) > 0}
    if not ids:
        return {}
    links = frappe.get_all('HERO Integration Link', filters={'source_system':'HERO',
        'source_instance':pending.source_instance, 'source_type':'project', 'source_id':['in', sorted(ids)]},
        fields=['source_id', 'sync_status'], limit_page_length=0)
    by_id = {}
    for link in links:
        by_id.setdefault(str(link.source_id), []).append(link.sync_status)
    found = {i: 'DELETED' for i, states in by_id.items() if states == ['Source Deleted']}
    missing = ids - by_id.keys()
    for field in ('target_project_match', 'project_match'):
        nested = (record or {}).get(field)
        if not isinstance(nested, dict) or type(nested.get('id')) != int or str(nested['id']) not in missing:
            continue
        ident = str(nested['id'])
        if nested.get('is_deleted') is True:
            found[ident] = 'DELETED'
        elif nested.get('is_deleted') is False and str(nested.get('project_nr') or '').upper().startswith('KONT-'):
            found[ident] = 'LEAD'
    missing -= found.keys()
    if not missing:
        return found
    from hero_integration.client.graphql import HeroGraphQLClient, HeroClientError
    from hero_integration.sources.hero import GraphQLServiceSource
    from hero_integration.services.read_contract import ReadContractError
    from hero_integration.services.migration import _request_rate
    cache = _project_history.get()
    if cache is None:
        cache = {}
    unseen = {int(i) for i in missing if (pending.source_instance, i) not in cache}
    if unseen:
        try:
            client = HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds,
                                       requests_per_minute=_request_rate())
            reader = GraphQLServiceSource.approved(client, 'HeroProjectMatches', pending.source_instance)
            observed = reader.project_reference_states(unseen)
        except (ReadContractError, HeroClientError, frappe.ValidationError):
            observed = {}
        for ident in unseen:
            cache[pending.source_instance, str(ident)] = observed.get(ident)
    for i in missing:
        state = cache.get((pending.source_instance, i))
        if state in ('DELETED', 'LEAD'):
            found[i] = state
    return found


def _resolve_admitted(pending, run, name, dependency):
    record = _source_observation(pending) if pending.operation in OPERATIONS.values() else None
    archived_candidate = record
    if archived_candidate is None and migration_archive.active() and pending.operation in {"HeroServices", "HeroDocuments", "HeroPictures"}:
        observed = _source_observation(pending)
        archived_candidate = observed.get("row" if pending.operation == "HeroServices" else "detail")
    if archived_candidate and migration_archive.excluded(pending.operation, archived_candidate):
        frappe.db.set_value(MAIN, name, {"status": "Resolved", "detail": "Archived source excluded by migration settings."})
        return
    address_review = record.pop(ADDRESS_REVIEW, None) if pending.operation == 'HeroCustomers' else None
    accepted_customer = False
    if address_review and address_review.pop("parent_field_conflict", True) is False:
        if run.state in ("Running", "Completed"):
            address_review = None
        elif run.state == "Completed With Conflicts":
            # Another customer's conflict does not invalidate this exact writer receipt.
            # Verify its saved object/address claim before opening the normal relationship pass.
            accepted_customer = _accepted_customer_receipt(pending, record)
    first_attempt = not getattr(pending, 'realization_started', 1)
    if first_attempt:
        frappe.db.set_value(MAIN, name, 'realization_started', 1)
    dependencies = _active_dependencies(pending.source_instance,
        [(row.source_type, row.source_id) for row in pending.dependencies])
    # Secondary Address identities belong to the relationship writer itself.
    # Its first attempt may create them; an existing unbound match waits for review.
    required = [(kind, identity) for kind, identity in dependencies if not first_attempt or kind != 'customer_address']
    from hero_integration.services.employee_migration import historical_users, historical_partners
    archived_users = historical_users(pending.source_instance)
    archived_partners = historical_partners(pending.source_instance)
    creating = record is not None and DEFERRED_CREATE in record
    if creating:
        required = [tuple(pair) for pair in record[DEFERRED_CREATE]]
        if not required or not set(required).issubset(dependencies):
            raise PendingObservationError('Mandatory creation evidence differs from the saved dependencies.')
    if pending.operation == 'HeroServices':
        from hero_integration.services.service_migration import active_service_dependencies
        required = active_service_dependencies(pending, required)
    if pending.operation == 'HeroEmployees':
        required = []  # The employee service realizes both of these owned identities itself.
    from hero_integration.services import migration as m
    from hero_integration.client.settings import get_configuration
    if run.owner != 'Administrator' and not frappe.db.get_value('User', run.owner, 'enabled'):
        raise PermissionError('The original migration actor is inactive.')
    frappe.set_user(run.owner)
    frappe.only_for('System Manager')
    token = _replaying.set('create' if creating else True)
    try:
        config = get_configuration(fresh=True)
        if config.source_instance != pending.source_instance:
            raise ValueError('The configured source changed.')
        direct_read = None
        if pending.operation in {"HeroEmployees", "HeroServices", "HeroDocuments", "HeroPictures"}:
            from hero_integration.services.file_migration import _direct_reader, bound_configuration
            bound_configuration(run, configuration=config)
            direct_read = _direct_reader(run, expected_state=run.state, operation=pending.operation)
        else:
            m.validate_run_mappings(run)
            source = m._source_for_run(run, config)  # Source/endpoint proof; no external request.
            plan = m._mapping_plan_for_run(run, pending.operation)
            if plan.mapping_version != pending.mapping_version:
                raise ValueError('The approved mapping changed; refresh the source record before retrying.')
            if str(plan.source_identity(record)) != pending.source_id or m._source_instance(record) != pending.source_instance:
                raise ValueError('The deferred source identity changed.')
        history = _historical_projects(pending, run, required, record, config)
        deleted_parent = pending.operation in ('HeroTasks', 'HeroCalendarEvents') and 'DELETED' in history.values()
        if history:
            if creating and not deleted_parent and any(k == 'project' and str(i) in history for k, i in required):
                return  # A mandatory native Project cannot be manufactured from a lead.
            required = [(k, i) for k, i in required if k != 'project' or str(i) not in history]
        partial = not deleted_parent and not address_review and not dependencies_ready(pending.source_instance, pending.operation, required)
        if partial:
            if creating or pending.operation not in {'HeroTasks','HeroProjectMatches','HeroCalendarEvents'}:
                return
            if pending.operation == 'HeroProjectMatches' and dependency and dependency[0] == 'User':
                _resolve_available_project_assignee(pending, dependency[1])
        if pending.operation == 'HeroServices':
            from hero_integration.services.service_migration import realize_relationships
            code = realize_relationships(pending, config, read=direct_read)
            bound_configuration(run, configuration=get_configuration(fresh=True))
        elif pending.operation == 'HeroEmployees':
            from hero_integration.services.employee_migration import realize_relationships
            code = realize_relationships(pending, read=direct_read)
            bound_configuration(run, configuration=get_configuration(fresh=True))
        elif pending.operation in {'HeroDocuments', 'HeroPictures'}:
            from hero_integration.services.file_transfer import FileConflict
            try:
                code = (_resolve_file(pending, read=direct_read, historical=history) if history
                        else _resolve_file(pending, read=direct_read))
            except FileConflict as error:
                links = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_instance': pending.source_instance,
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
            if (pending.operation == 'HeroTasks' and run.state == 'Completed With Conflicts'
                    and not getattr(run, 'testing_enabled', 0)
                    and (getattr(plan, 'adapter_options', None) or {}).get('version') != 2):
                return
            partial_rules = None
            if partial:
                if (getattr(plan, 'adapter_options', None) or {}).get('version') != 2:
                    return
                from hero_integration.services.direct_mapping import lookup
                partial_rules = []
                for rule in plan.definitions:
                    if rule.target_doctype != plan.target_doctype and not (
                            pending.operation == 'HeroTasks' and rule.target_doctype == 'ToDo'
                            and rule.target_field == 'allocated_to'):
                        continue
                    kind = reference_kind(rule)
                    raw = lookup(record, rule.source_path)
                    if kind and type(raw) is int and (kind, str(raw)) in dependencies and available(
                            pending.source_instance, kind, str(raw)):
                        partial_rules.append(rule)
                if not partial_rules:
                    return
            proof = record.pop('__hero_origin_binding', None)
            historical = record.pop('__hero_historical_users', [])
            partners = record.pop('__hero_historical_partners', [])
            if any(not isinstance(ids, list) or any(not isinstance(value, str) or not value.isdigit()
                       or int(value) <= 0 for value in ids) for ids in (historical, partners)):
                raise ValueError('The deferred historical identity proof is invalid.')
            record.pop(DEFERRED_CREATE, None)
            if creating:
                if proof is not None or _origin_binding(pending.source_instance, pending.operation, pending.source_id):
                    raise PendingObservationError('Deferred creation identity was acquired by another writer.')
            else:
                _verify_origin_binding(pending, proof)
                if not address_review and (deleted_parent or
                        run.state == 'Completed With Conflicts' and not getattr(run, 'testing_enabled', 0)):
                    status = frappe.db.get_value('HERO Integration Link', proof['name'], 'sync_status', for_update=True)
                    if status != 'Synchronized':
                        return  # An existing conflict or review retains its decision and source evidence.
            if deleted_parent:
                frappe.db.set_value(MAIN, name, {'status':'Resolved', 'record_json':'',
                    'detail':'Referenced HERO project is deleted; existing native records were preserved.'})
                return
            from hero_integration.services.employee_migration import historical_user_scope
            def realize_native():
                if creating and _origin_binding(pending.source_instance, pending.operation, pending.source_id):
                    raise PendingObservationError('Deferred creation identity was acquired by another writer.')
                if address_review:
                    from hero_integration.services.binding_registry import FrappeBindingStorage
                    from hero_integration.services.query_mapping import address_values
                    owner = FrappeBindingStorage().lock_source_object(frappe.local.site, pending.source_instance,
                        plan.binding_contract.object_type, "Customer", pending.source_id)
                    address = address_values(plan, record)
                    if (not owner or not address or address_review != {
                            "object": owner.identity_key, "address_id": str(address["id"])}):
                        raise ValueError("The reviewed address relationship has changed.")
                    if accepted_customer:
                        return m._migrate_customer(record, run, plan=plan)
                    customer = frappe.get_doc("Customer", owner.target_name, for_update=True)
                    status = m._realize_primary_address(customer, address, pending.source_instance,
                                                        plan.mapping_version, plan, record, run.name)
                    # Other relationship adapters require their own authorized replay.
                    remaining = bool(plan.customer_contact_fields() or plan.has_home_phone()
                        or plan.adapter_options.get("native_relations") or any(
                            rule.target_doctype == "Customer" and rule.target_field in
                            {"first_name", "last_name", "email_id", "mobile_no"} for rule in plan.definitions))
                    return m.Result("CONFLICT" if status == "conflict" else "UPDATED",
                                    "Primary address fields await review." if status == "conflict" else "",
                                    "Customer", owner.target_name, pending_relation=remaining)
                if (not creating and pending.operation in {'HeroTasks','HeroProjectMatches','HeroCalendarEvents'} and
                        (getattr(plan,'adapter_options',None) or {}).get('version')==2):
                    if pending.operation=='HeroTasks':
                        m._load_task_user_directory(None,pending.operation)
                    if history:
                        return m.realize_mapped_bindings(record, run, plan, partial_rules=partial_rules,
                                                         historical_projects=history)
                    if partial:
                        return m.realize_mapped_bindings(record, run, plan, partial_rules=partial_rules)
                    return m.realize_mapped_bindings(record, run, plan)
                elif pending.operation in {'HeroCustomers', 'HeroProjectMatches'}:
                    action = m._migrate_customer if pending.operation == 'HeroCustomers' else m._migrate_project
                    return action(record, run, plan=plan)
                else:
                    # A long-lived worker must not reuse another run's legacy User directory.
                    m._load_task_user_directory(None, pending.operation)
                    mapping = frappe.get_doc('HERO GraphQL Mapping', run.graphql_mapping)
                    scope = m._projection_task_scope(run, plan, mapping, source.source_instance, source.source_instance)
                    return m._migrate_projection_record(record, run, plan, scope)

            kind = next(kind for kind, operation in OPERATIONS.items() if operation == pending.operation)
            with historical_user_scope(pending.source_instance, set(historical) | archived_users, set(partners) | archived_partners):
                result = m._migrate_with_binding(record, run, plan, kind, realize_native, relationships_only=not creating)
            code = result.code
            if creating and code != 'CREATED':
                code = 'CONFLICT'
                if budget := execution_budget.current_budget():
                    budget.outcome(kind, pending.source_id, 'conflict')
            created = creating and code == 'CREATED'
            if created:
                if budget := execution_budget.current_budget():
                    budget.outcome(kind, pending.source_id, 'processed')
                frappe.db.sql("""UPDATE `tabHERO Integration Run` SET created_records=created_records+1,
                    processed=processed+1 WHERE name=%s""", (run.name,))
            pending_detail = ''
            if partial and code in {'CREATED','UPDATED','SKIPPED','CONVERGED','OBSERVED','UNCHANGED'}:
                saved = _source_observation(pending)
                saved['__hero_origin_binding'] = _origin_binding(pending.source_instance, pending.operation, pending.source_id)
                if saved != json.loads(pending.record_json):
                    frappe.db.set_value(MAIN, name, 'record_json', json.dumps(saved, ensure_ascii=False))
                return
            if not created and getattr(result, 'pending_relation', False) and code in {
                    'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}:
                code = 'PENDING_REVIEW'
                pending_detail = 'A related assignment or address still requires review.'
                # This worker may have advanced its own native baseline before
                # discovering an owned relation that still needs review.
                saved = json.loads(pending.record_json)
                saved['__hero_origin_binding'] = _origin_binding(
                    pending.source_instance, pending.operation, pending.source_id)
                pending.record_json = json.dumps(saved, ensure_ascii=False)
            if (not created and pending.operation == 'HeroTasks' and result.target_name
                    and code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}):
                from hero_integration.services.direct_mapping import MISSING, lookup
                assignment = next((rule for rule in getattr(plan, 'definitions', ())
                                   if getattr(rule, 'target_doctype', None) == 'ToDo'
                                   and getattr(rule, 'target_field', None) == 'allocated_to'), None)
                raw = lookup(record, assignment.source_path) if assignment else MISSING
                if raw is not MISSING and raw is not None and not (type(raw) is int and (raw == 0 or str(raw) in archived_users or str(raw) in historical)):
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
            if created:
                # The ordinary create pass publishes the object and its scalar
                # baselines. Leave optional assignments/relationships to the
                # same locked observation, even when they were already bound.
                saved = _source_observation(pending)
                del saved[DEFERRED_CREATE]
                saved['__hero_origin_binding'] = _origin_binding(pending.source_instance, pending.operation, pending.source_id)
                if not saved['__hero_origin_binding']:
                    raise PendingObservationError('Deferred creation produced no verifiable origin binding.')
                frappe.db.set_value(MAIN, name, {'status':'Waiting', 'record_json':json.dumps(saved),
                    'realization_started':0, 'detail':''})
                _enqueue_pending('hero_integration.services.deferred_references.resolve_reference', queue=_queue(), name=name,
                    enqueue_after_commit=True, job_id='hero-created-reference-' + name, deduplicate=True)
                return
        success = code in {'CREATED', 'UPDATED', 'SKIPPED', 'CONVERGED', 'OBSERVED', 'UNCHANGED'}
        needs_review = code == 'PENDING_REVIEW'
        # Dependencies are already present here. Do not repeatedly replay an
        # authority/invalid-identity hold on every scheduler recovery cycle.
        waiting_identity = needs_review and not dependencies_ready(pending.source_instance, pending.operation,
            [(k, i) for k, i in dependencies if k != 'project' or str(i) not in history])
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
    record.pop('__hero_historical_partners', None)
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
        from hero_integration.services.execution_runtime import process_migration_record
        process_migration_record(plan, record, pending.source_instance, "project",
            lambda: sync(link.target_name, {**record, 'project_match_assignments': assignments}, allow_removals=False),
            evidence=run.name, successful=lambda result: False, relationships_only=True)


def _resolve_file(pending, *, read=None, historical=None):
    from hero_integration.services import file_transfer as files, direct_runtime
    read = read or direct_runtime.resolve_read(pending.operation, pending.source_instance)
    if read.mapping_version != pending.mapping_version:
        raise ValueError('The approved file mapping changed.')
    observation = json.loads(pending.record_json)
    row, detail = observation['row'], observation['detail']
    from hero_integration.services.source_history import enabled
    if enabled(read, 'file_details'):
        row = {**row, '_hero_file_detail': detail}
    if str(read.plan.source_identity(row)) != pending.source_id:
        raise ValueError('The deferred file identity changed.')
    links = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO',
        'source_instance': pending.source_instance, 'source_type': files.SOURCE_TYPE, 'source_id': pending.source_id},
        fields=['name', 'target_name', 'source_payload_hash', 'target_field_hashes', 'target_payload_hash', 'sync_status'],
        limit_page_length=2)
    link = links[0] if len(links) == 1 else None
    if not link or link.source_payload_hash != files.source_hash(row):
        raise ValueError('The imported file observation changed.')
    if link.sync_status in {'Conflict', 'Pending Review', 'Rejected', 'Source Deleted'}:
        return 'CONFLICT'
    scope = observation.get('scope')
    if 'scope' in observation and (not isinstance(scope, dict) or not isinstance(scope.get('project_ids'), list)
            or not isinstance(scope.get('customer_ids'), list)):
        raise ValueError('The deferred file test scope is invalid.')
    target = files.scoped_file_target(detail, pending.source_instance, scope)
    if not target[0]:
        if historical and all(row.source_type == 'project' and str(row.source_id) in historical
                              for row in pending.dependencies):
            def preserve_file():
                files._assert_target_baseline(link, files.mapped_values(read, row), lock=True)
                return 'SKIPPED'
            return direct_runtime.realize_record(read, row, pending, files.SOURCE_TYPE, preserve_file)
        raise ValueError('The file attachment is still unresolved.')
    phase = 'images' if pending.operation == 'HeroPictures' else 'documents'
    folder = files._folder(files.ROOT_FOLDER, files.PHASE_FOLDER[phase])
    def attach():
        files._assert_target_baseline(link, files.mapped_values(read, row), lock=True)
        if not files.relink(link, target, folder):
            return 'SKIPPED'
        fields = files.mapped_values(read, row)
        frappe.db.set_value('HERO Integration Link', link.name, {'target_field_hashes': files._target_baseline(link.target_name, fields),
            'last_synced_on': frappe.utils.now_datetime()})
        return 'UPDATED'

    return direct_runtime.realize_record(read, row, pending, files.SOURCE_TYPE, attach)
