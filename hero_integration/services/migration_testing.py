"""Frozen per-area record limits for preview and execute migration runs."""
import json

import frappe


from hero_integration.services import migration_archive
def configuration(parent=None, *, frappe_module=None):
    owner = frappe_module if frappe_module is not None else frappe
    doc = parent if parent is not None else owner.get_single('HERO Integration Settings')
    enabled = bool(int(getattr(doc, 'testing_enabled', 0) or 0))
    maximum = getattr(doc, 'testing_max_actions', None)
    if enabled and (isinstance(maximum, bool) or not str(maximum).isdigit() or not 1 <= int(maximum) <= 100):
        raise ValueError('Testing record limit must be between 1 and 100.')
    return {'testing_enabled': int(enabled), 'testing_max_actions': int(maximum) if enabled else 0}


def record_limit(run):
    if not bool(int(getattr(run, 'testing_enabled', 0) or 0)): return None
    value = getattr(run, 'testing_max_actions', 0)
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= 100:
        raise ValueError('Testing record limit must be between 1 and 100.')
    return int(value)


def validate_limit(maximum):
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= 100):
        raise ValueError('Testing record limit must be between 1 and 100.')


def validate_checkpoint(state, maximum, **context):
    validate_limit(maximum)
    if not state or maximum is None:
        return
    if state.get('testing_record_limit') not in (None, maximum) or any(
            state.get(key) != value for key, value in context.items()):
        raise ValueError('The frozen testing context changed; start a new run.')


def run_metadata(testing):
    if not testing['testing_enabled']:
        return {}
    return {'testing_results_json': json.dumps({'record_limit': testing['testing_max_actions'], 'areas': {}})}


def processed(counts):
    return sum(int(value or 0) for key, value in counts.items()
               if key in {'added', 'updated', 'unchanged', 'conflict', 'failed', 'would_add', 'would_update'}
               or (key.startswith('skipped_') and key != 'skipped_archived'))


def bounded_rows(state, rows, maximum):
    validate_limit(maximum)
    if maximum is None: return rows
    prior = state.get('testing_record_limit')
    if prior is not None and prior != maximum:
        raise ValueError('The frozen testing record limit changed; start a new run.')
    state['testing_record_limit'] = maximum
    state['testing_records_received'] = int(state.get('testing_records_received', 0)) + len(rows)
    remaining = max(0, maximum - processed(state.get('counts', {})))
    selected = rows[:remaining]
    if len(rows) >= remaining:
        state['testing_limit_reached'] = True
    return selected


def persist(run_name, operation, state):
    if state.get('testing_record_limit') is None: return
    counts = state.get('counts', {})
    completed = bool(state.get('done'))
    missing = any(state.get(key) for key in ('testing_missing_user_ids', 'testing_ambiguous_user_ids', 'testing_missing_partner_ids'))
    status = ('BLOCKED' if missing else 'LIMIT_REACHED' if state.get('testing_limit_reached') else 'COMPLETED') if completed else ''
    result = {'record_limit': state['testing_record_limit'], 'areas': {operation: {
        'processed': processed(counts), 'received': state.get('testing_records_received', 0), 'status': status,
        'missing_related_users': len(state.get('testing_missing_user_ids', [])),
        'ambiguous_related_users': len(state.get('testing_ambiguous_user_ids', [])),
        'missing_related_partners': len(state.get('testing_missing_partner_ids', [])),
        'historical_related_users': len(state.get('testing_historical_user_ids', [])),
        'historical_related_partners': len(state.get('testing_historical_partner_ids', []))}}}
    if state.get('testing_cohort_fingerprint') or 'testing_cohort_user_ids' in state:
        result['selection'] = 'related_projects'
    frappe.db.set_value('HERO Integration Run', run_name, {
        'testing_status': status, 'testing_records_received': state.get('testing_records_received', 0),
        'testing_results_json': json.dumps(result, sort_keys=True)})


def test_state(run_name, operation):
    if not run_name: raise ValueError('Testing migration requires a run-bound checkpoint.')
    raw = frappe.db.get_value('DefaultValue', {'parent': '__default',
        'defkey': 'hero_testing_migration:' + run_name + ':' + operation}, 'defvalue')
    return json.loads(raw) if raw else {}


def save_test_state(run_name, operation, state):
    frappe.db.set_default('hero_testing_migration:' + run_name + ':' + operation, json.dumps(state))
    frappe.db.commit()


class RecordLimitedSource:
    """Cap each logical source area, even when one page contains more records."""
    def __init__(self, source, maximum):
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError('Testing record limit must be between 1 and 100.')
        self.source, self.maximum = source, maximum
        self.results = {}
        self.executed_actions = 0
        self.records_received = 0
        self.status = 'COMPLETED'

    def __getattr__(self, name): return getattr(self.source, name)

    def _page(self, operation, reader, offset):
        from hero_integration.sources.hero import SourcePage
        prior = self.results.setdefault(operation, {'processed': 0, 'received': 0, 'status': ''})
        remaining = self.maximum - prior['processed']
        if remaining <= 0: return SourcePage((), None)
        page = reader(offset)
        self.executed_actions += 1
        received = (getattr(page, "offset_checkpoint", None) or {}).get("count", len(page.records))
        self.records_received += received
        prior['received'] += received
        eligible = [row for row in page.records if not migration_archive.excluded(operation, row)]
        prior['skipped_archived'] = prior.get('skipped_archived', 0) + len(page.records) - len(eligible)
        rows = eligible[:remaining]
        prior['processed'] += len(rows)
        reached = prior['processed'] >= self.maximum
        prior['status'] = 'LIMIT_REACHED' if reached else 'COMPLETED'
        if reached: self.status = 'LIMIT_REACHED'
        return SourcePage(tuple(rows), None if reached else page.next_offset,
                          getattr(page, "offset_checkpoint", None) or ({"count": received} if not eligible and received else None), getattr(page, "changed_overlap_ids", frozenset()))

    def customers(self, offset=0): return self._page('HeroCustomers', self.source.customers, offset)
    def projects(self, offset=0): return self._page('HeroProjectMatches', self.source.projects, offset)
    def projection(self, offset=0):
        operation = self.source.contract.target_operation
        return self._page(operation, self.source.projection, offset)


def project_cohort(records, plan, maximum, *, version=1):
    """Freeze only identities needed for a related full-migration test sample."""
    from hero_integration.services.direct_mapping import MISSING, lookup
    validate_limit(maximum)
    if maximum is None:
        raise ValueError('A related test sample requires an explicit project limit.')
    selected = list(records)[:maximum]
    projects, customers, users, hashes = set(), set(), set(), {}
    customer_rules = [rule for rule in plan.definitions
                      if rule.transformation == 'HERO Customer Link' or rule.purpose == 'Customer Relation']
    manager_paths = [rule.source_path for rule in plan.definitions if rule.transformation == 'User By Partner ID'] if version == 2 else []
    partners = set()
    provenance = {}
    missing = []
    for record in selected:
        identity = str(plan.source_identity(record))
        if not identity or identity in projects:
            raise ValueError('The project sample has a missing or duplicate source identity.')
        projects.add(identity)
        related = set()
        if version == 2 and not customer_rules:
            raise ValueError('The approved project reader proves no customer relationship.')
        for rule in customer_rules:
            value = lookup(record, rule.source_path)
            if value is MISSING:
                raise ValueError('The project sample omits the mapped customer identity.')
            if type(value) is int and value > 0:
                customers.add(str(value))
                related.add(('customer', str(value)))
            elif version == 2:
                missing.append(['project', identity, 'customer', rule.source_path])
        for path in manager_paths:
            manager = lookup(record, path)
            if type(manager) is int and manager > 0:
                partners.add(str(manager))
                related.add(('partner', str(manager)))
            elif manager is not None:
                raise ValueError(f'Project {identity} has no verifiable manager identity at {path}.')
        if 'project_match_assignments' not in record:
            raise ValueError('The project sample omits its assignee selection.')
        for row in record.get('project_match_assignments') or []:
            value = row.get('user_id') if isinstance(row, dict) else None
            if type(value) is int and value > 0:
                users.add(str(value))
                related.add(('User', str(value)))
            elif version == 2:
                missing.append(['project', identity, 'User', 'project_match_assignments'])
        provenance[identity] = sorted(related)
        hashes[identity] = relationship_fingerprint('HeroProjectMatches', record, [rule.source_path for rule in customer_rules],
                                                    manager_paths=manager_paths)
    contract = {'graph': {'project': provenance}, 'unresolved': missing, 'files': {},
                'employees': {}, 'manager_paths': manager_paths} if version == 2 else {}
    return {'version': version, **contract, 'project_ids': sorted(projects), 'customer_ids': sorted(customers),
            'user_ids': sorted(users), 'partner_ids': sorted(partners), 'task_ids': [], 'event_ids': [],
            'customer_paths': [rule.source_path for rule in customer_rules], 'project_relation_hashes': hashes,
            'task_relation_hashes': {}, 'event_relation_hashes': {}}


def relationship_fingerprint(operation, record, customer_paths=(), *, manager_paths=()):
    """Detect relationship changes between selection and the real test import."""
    from hashlib import sha256
    from hero_integration.services.direct_mapping import MISSING, lookup
    if operation == 'HeroProjectMatches':
        if 'project_match_assignments' not in record or 'is_deleted' not in record:
            raise ValueError('Selected project relationship information is incomplete.')
        customers = [lookup(record, path) for path in customer_paths]
        if any(value is MISSING for value in customers):
            raise ValueError('Selected project customer identity is unavailable.')
        assignments = record['project_match_assignments']
        if not isinstance(assignments, list) or any(not isinstance(row, dict) or 'user_id' not in row for row in assignments):
            raise ValueError('Selected project assignee identities are incomplete.')
        value = [record.get('is_deleted'), customers, sorted(str(row['user_id']) for row in assignments)]
        if manager_paths:
            managers = [lookup(record, path) for path in manager_paths]
            if any(manager is MISSING for manager in managers):
                raise ValueError('Selected project manager identity is unavailable.')
            value.append(managers)
    elif operation == 'HeroTasks':
        keys = ('target_project_match_id', 'author_user_id', 'target_user_id')
        if any(key not in record for key in keys):
            raise ValueError('Selected Task relationship information is incomplete.')
        value = [record[key] for key in keys]
    elif operation == 'HeroCalendarEvents':
        if 'project_match_id' not in record or 'partners' not in record:
            raise ValueError('Selected Event relationship information is incomplete.')
        partners = record['partners'] or []
        if not isinstance(partners, list) or any(not isinstance(row, dict) or 'id' not in row for row in partners):
            raise ValueError('Selected Event partner identities are incomplete.')
        value = [record['project_match_id'], sorted(str(row['id']) for row in partners)]
    else:
        raise ValueError('Unsupported related test fingerprint operation.')
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def distinct_rows(rows, identity, fingerprints):
    """Suppress identical repetitions; contradictory rows cannot prove one source record."""
    from hero_integration.services.offset_pagination import fingerprint
    result = []
    for row in rows:
        key = identity(row)
        if type(key) not in (int, str) or not str(key).strip():
            raise ValueError('Missing identity during cohort selection.')
        key = str(key)
        stamp = fingerprint(row)
        if key in fingerprints:
            if fingerprints[key] != stamp:
                raise ValueError(f'Source identity {key} changed between repeated rows.')
        else:
            fingerprints[key] = stamp
            result.append(row)
    return result


def prepare_project_cohort(client, read, maximum, *, page_overlap=0, version=1):
    """Read a bounded anchor sample using the selected approved project reader."""
    from hero_integration.services.direct_runtime import fetch_page
    validate_limit(maximum)
    if maximum is None:
        raise ValueError('A related test sample requires an explicit project limit.')
    from hero_integration.services.offset_pagination import advance, validate_overlap
    if page_overlap:
        validate_overlap(page_overlap, read.page_size, migration=True)
    window = None
    rows, offset, seen, pages = [], 0, set(), 0
    fingerprints = {}
    while len(rows) < maximum:
        if pages >= 1000:
            raise ValueError('Project test selection reached its page budget before enough active projects were found.')
        page = fetch_page(client, read, offset)
        pages += 1
        if not page:
            break
        if page_overlap:
            window = advance(page, offset=offset, size=read.page_size, overlap=page_overlap, migration=True,
                             previous=window.checkpoint if window else None, identity=read.plan.source_identity)
            if window.changed:
                raise ValueError("A project changed while selecting the related test cohort; restart selection.")
            page = window.rows
        next_offset = window.next_offset if window else offset + len(page)
        if version == 2:
            # Only active anchor rows need to occupy memory across the bounded scan.
            if any(type(row.get('is_deleted')) is not bool for row in page):
                raise ValueError('The project sample requires an explicit project deletion state.')
            page = distinct_rows([row for row in page if not row['is_deleted'] or migration_archive.includes_archived()], read.plan.source_identity, fingerprints)
        identities = [str(read.plan.source_identity(row)) for row in page]
        if any(not identity for identity in identities) or len(set(identities)) != len(identities) or seen.intersection(identities):
            raise ValueError('The project sample reader repeated or omitted an identity.')
        if any(str(row.get('id', '')) != identity for row, identity in zip(page, identities)):
            raise ValueError('The project sample requires the selected project id to match its identity.')
        seen.update(identities)
        if any(type(row.get('is_deleted')) is not bool for row in page):
            raise ValueError('The project sample requires an explicit project deletion state.')
        rows.extend([row for row in page if not row['is_deleted'] or migration_archive.includes_archived()][:maximum - len(rows)])
        offset = next_offset
    return project_cohort(rows, read.plan, maximum, version=version)


def project_reader_fingerprint(read, *, version=1):
    """Bind a sample to its approved query and effective mapping version."""
    from hashlib import sha256
    payload = [read.operation, read.origin, read.mapping, read.mapping_version,
               read.document, read.records_path, read.variables, read.page_size]
    if version == 2:
        snapshot = getattr(read, 'snapshot', None)
        payload.extend([getattr(read, 'service_revision', 0), getattr(snapshot, 'name', ''),
                        getattr(snapshot, 'fingerprint', '')])
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def collect_related_projection(cohort, operation, records):
    """Add related Task/Event and employee identities from one approved page."""
    if operation not in {'HeroTasks', 'HeroCalendarEvents'}:
        raise ValueError('Unsupported related test discovery operation.')
    projects = set(cohort['project_ids'])
    users, partners = set(cohort['user_ids']), set(cohort['partner_ids'])
    name, path = (('task_ids', 'target_project_match_id') if operation == 'HeroTasks'
                  else ('event_ids', 'project_match_id'))
    identities = set(cohort[name])
    for row in records:
        if migration_archive.excluded(operation, row):
            continue
        if 'id' not in row or path not in row:
            raise ValueError('Related test discovery omits an identity or project reference.')
        if str(row[path]) not in projects:
            continue
        identity = row['id']
        if type(identity) is not int or identity <= 0:
            raise ValueError('Related test discovery found an invalid record identity.')
        hash_key = 'task_relation_hashes' if operation == 'HeroTasks' else 'event_relation_hashes'
        stamp = relationship_fingerprint(operation, row)
        if str(identity) in identities:
            if cohort.get('version') == 2 and stamp == cohort[hash_key].get(str(identity)):
                continue
            raise ValueError('Related test discovery repeated a record identity.')
        identities.add(str(identity))
        kind = 'Task' if operation == 'HeroTasks' else 'CalendarEvent'
        related = set()
        if cohort.get('version') == 2:
            graph = cohort['graph']
            graph['project'][str(row[path])].append([kind, str(identity)])
        cohort.setdefault(hash_key, {})[str(identity)] = stamp
        if operation == 'HeroTasks':
            for field in ('author_user_id', 'target_user_id'):
                if field not in row:
                    raise ValueError('Related Task selection omits an assigned user identity.')
                value = row[field]
                if type(value) is int and value > 0:
                    users.add(str(value))
                    related.add(('User', str(value)))
        else:
            if 'partners' not in row or (row['partners'] is not None and not isinstance(row['partners'], list)):
                raise ValueError('Related Event selection omits partner identities.')
            for partner in row['partners'] or []:
                if not isinstance(partner, dict) or 'id' not in partner:
                    raise ValueError('Related Event partner identity is unavailable.')
                value = partner['id']
                if type(value) is int and value > 0:
                    partners.add(str(value))
                    related.add(('partner', str(value)))
                elif cohort.get('version') == 2:
                    cohort['unresolved'].append([kind, str(identity), 'partner', 'partners'])
        if cohort.get('version') == 2:
            graph.setdefault(kind, {})[str(identity)] = sorted(related)
    cohort[name], cohort['user_ids'], cohort['partner_ids'] = sorted(identities), sorted(users), sorted(partners)


def collect_project_employees(cohort, records):
    """Resolve only selected user/partner references against the approved directory."""
    from hero_integration.services.employee_migration import _records
    wanted_users, wanted_partners = set(cohort['user_ids']), set(cohort['partner_ids'])
    by_user, by_partner = {}, {}
    for row in _records({'rows': records}, 'rows'):
        partner = str(row['id'])
        nested = row.get('user') or {}
        users = {str(v) for v in (row.get('user_id'), nested.get('id'))
                 if type(v) is int and v > 0}
        by_partner[partner] = sorted(users)
        for user in users:
            by_user.setdefault(user, []).append(partner)
    selected = wanted_partners & by_partner.keys()
    for user in sorted(wanted_users):
        matches = by_user.get(user, ())
        if len(matches) == 1 and len(by_partner[matches[0]]) == 1:
            selected.add(matches[0])
            cohort['graph'].setdefault('User', {})[user] = [['partner', matches[0]]]
        else:
            cohort['unresolved'].append(['User', user, 'partner', 'missing or ambiguous employee'])
    for partner in sorted(wanted_partners - by_partner.keys()):
        cohort['unresolved'].append(['partner', partner, 'employee', 'missing employee'])
    for partner in sorted(selected):
        users = by_partner[partner]
        if len(users) > 1 or any(len(by_user[user]) > 1 for user in users):
            cohort['unresolved'].append(['partner', partner, 'User', 'ambiguous user'])
            continue
        cohort['employees'][partner] = users
        cohort['graph'].setdefault('partner', {})[partner] = [['User', user] for user in users]


def collect_project_files(cohort, operation, rows, details):
    from hero_integration.services.file_transfer import FileRelationshipError, project_file_proof
    files = cohort['files'].setdefault(operation, {})
    customers = set(cohort['customer_ids'])
    for row in rows:
        identity = str(row['id'])
        try:
            detail = details.get(row['uuid'])
            if detail is None:
                raise FileRelationshipError('Requested file detail is missing.')
            if migration_archive.excluded(operation, detail):
                continue
            proof = project_file_proof(detail, cohort)
        except FileRelationshipError as error:
            issue = ['file_upload', identity, 'project', str(error)]
            if issue not in cohort['unresolved']:
                cohort['unresolved'].append(issue)
            continue
        if proof['projects']:
            if identity in files:
                if files[identity] == proof:
                    continue
                raise ValueError('File discovery found contradictory relationships for a repeated identity.')
            files[identity] = proof
            customers.update(proof['customers'])
            for project in proof['projects']:
                cohort['graph']['project'][project].append(['file_upload', identity])
            cohort['graph'].setdefault('file_upload', {})[identity] = [
                ['customer', customer] for customer in proof['customers']]
        elif proof['ambiguous_customer']:
            cohort['unresolved'].append(['file_upload', identity, 'project', 'customer alone cannot identify project'])
    cohort['customer_ids'] = sorted(customers)


def project_budget_evidence(cohort, digest):
    edges = sorted({(kind, identity, target, other) for kind, rows in cohort['graph'].items()
                    for identity, targets in rows.items() for target, other in targets})
    users = sorted(set(cohort['user_ids']).union(*(cohort['employees'].values())))
    return {'cohort_digest': digest,
            'file_areas': {operation: len(records) for operation, records in sorted(cohort['files'].items())},
            'members': {'customer': cohort['customer_ids'], 'User': users, 'partner': sorted(cohort['employees']),
                        'Task': cohort['task_ids'], 'CalendarEvent': cohort['event_ids'],
                        'file_upload': sorted({key for files in cohort['files'].values() for key in files})},
            'edges': [list(edge) for edge in edges], 'missing_relationships': cohort['unresolved']}


def missing_employee_refs(issues):
    """Only the directory's two specific absence findings admit historical evidence."""
    reasons = {('User', 'partner', 'missing or ambiguous employee'),
               ('partner', 'employee', 'missing employee')}
    return {(kind, identity) for kind, identity, target, reason in issues
            if (kind, target, reason) in reasons}


def project_completion_issues(selection, outcomes, unresolved_records, *, waiting=(), historical=()):
    """Propagate incomplete required descendants to every owning selected project."""
    from collections import defaultdict
    evidence = selection['evidence']
    from hero_integration.services.execution_budget import additional_primary_records
    primary = {('project', key) for key in selection['ids']} | additional_primary_records(evidence)
    members = {(kind, key) for kind, ids in evidence['members'].items() for key in ids}
    # The partner writer owns its embedded User and records its result under partner.
    writers = {key for key in members if key[0] != 'User'} | primary
    issues = {key: outcomes.get(key, 'missing') for key in writers if outcomes.get(key) != 'processed'
              and not (key in waiting and outcomes.get(key) == 'pending')}
    for entry in evidence.get('missing_relationships', ()):
        kind, identity, _, _ = entry
        if (kind, identity) not in historical or not missing_employee_refs([entry]):
            issues.setdefault((kind, identity), 'missing')
    for key in unresolved_records:
        issues.setdefault(key, 'conflict')
    parents = defaultdict(set)
    for kind, key, target, other in evidence['edges']:
        parents[(target, other)].add((kind, key))
    failed = set(issues)
    frontier = list(failed)
    while frontier:
        ancestors = parents[frontier.pop()] - failed
        failed.update(ancestors)
        frontier.extend(ancestors)
    for project in failed & primary:
        issues.setdefault(project, 'failed')
    return {key: status for key, status in issues.items() if key in primary or key in members}


class RelatedProjectSource(RecordLimitedSource):
    """Stream rows belonging to frozen project identities without widening writes.

    Readers may not support identity filters, so scan their approved pages.
    Empty filtered pages retain their underlying offsets so the caller can
    checkpoint and renew its lease. A bounded scan fails visibly rather than
    claiming that a truncated sample is complete.
    """
    MAX_SCAN_PAGES = 10000
    PROJECT_PATHS = {'HeroCalendarEvents': 'project_match_id', 'HeroTasks': 'target_project_match_id'}

    def __init__(self, source, maximum, cohort):
        super().__init__(source, maximum)
        if not isinstance(cohort, dict) or cohort.get('version') not in (1, 2):
            raise ValueError('The related project sample is missing or incompatible.')
        self.cohort = cohort
        self.identities = {key: frozenset(cohort.get(key, [])) for key in
                           ('project_ids', 'customer_ids', 'task_ids', 'event_ids')}
        if cohort.get('version') == 2:
            self.source.logbook_project_ids = self.identities['project_ids']
        if cohort.get('scoped_project_reads'):
            operation = source.contract.target_operation
            group = {'HeroCustomers':'customer_ids','HeroProjectMatches':'project_ids',
                     'HeroCalendarEvents':'event_ids','HeroTasks':'task_ids'}[operation]
            source.scoped_test_ids = tuple(sorted(self.identities[group],key=int))
            if getattr(source,'customer_source',None):
                source.customer_source.scoped_test_ids = tuple(sorted(self.identities['customer_ids'],key=int))
        self.scan_pages = {}
        self.last_page = {}
        self.found = {}
        self.fingerprints = {}

    def _related_page(self, operation, reader, offset, *, field, identities, selected_ids=None):
        from hashlib import sha256
        from hero_integration.sources.hero import SourcePage
        allowed = identities if isinstance(identities, (set, frozenset)) else set(identities)
        selected = selected_ids if isinstance(selected_ids, (set, frozenset)) else None if selected_ids is None else set(selected_ids)
        prior = self.results.setdefault(operation, {'processed': 0, 'received': 0, 'status': ''})
        if not allowed or selected == set():
            prior['status'] = 'COMPLETED'
            return SourcePage((), None)
        while True:
            if self.scan_pages.get(operation, 0) >= self.MAX_SCAN_PAGES:
                raise ValueError('Related test scan reached its page budget before the source ended.')
            page = reader(offset)
            self.scan_pages[operation] = self.scan_pages.get(operation, 0) + 1
            self.executed_actions += 1
            received = (getattr(page, "offset_checkpoint", None) or {}).get("count", len(page.records))
            self.records_received += received
            prior['received'] += received
            if page.next_offset is not None and (
                    type(page.next_offset) is not int or page.next_offset <= offset
                    or (not page.records and not getattr(page, "offset_checkpoint", None))):
                raise ValueError('Related test source pagination did not advance safely.')
            values = [row.get('id') if isinstance(row, dict) else None for row in page.records]
            if any(not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip() for value in values):
                raise ValueError('Related test source page has missing or invalid identities.')
            ids = [str(value) for value in values]
            if self.cohort.get('version') != 2 and len(set(ids)) != len(ids):
                raise ValueError('Related test source page has missing or duplicate identities.')
            fingerprint = sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest() if ids else ''
            if fingerprint and self.last_page.get(operation) == fingerprint:
                raise ValueError('Related test source repeated a page.')
            self.last_page[operation] = fingerprint
            rows = tuple(row for row in page.records if str(row.get(field, '')) in allowed
                         and (selected is None or str(row.get('id')) in selected))
            if self.cohort.get('version') == 2:
                rows = tuple(distinct_rows(rows, lambda row: row['id'], self.fingerprints.setdefault(operation, {})))
            hash_key = {'HeroProjectMatches': 'project_relation_hashes', 'HeroTasks': 'task_relation_hashes',
                        'HeroCalendarEvents': 'event_relation_hashes'}.get(operation)
            expected_hashes = self.cohort.get(hash_key, {}) if hash_key else {}
            for row in rows:
                expected_hash = expected_hashes.get(str(row.get('id')))
                if expected_hash and relationship_fingerprint(operation, row, self.cohort.get('customer_paths', ()),
                        manager_paths=self.cohort.get('manager_paths', ())) != expected_hash:
                    raise ValueError('A selected test record changed its relationships; start a new test run.')
            self.found.setdefault(operation, set()).update(str(row.get('id') if selected is not None else row.get(field)) for row in rows)
            prior['processed'] += len(rows)
            expected = selected if selected is not None else allowed if operation in {'HeroCustomers', 'HeroProjectMatches'} else set()
            all_found = bool(expected) and expected <= self.found[operation]
            if page.next_offset is None or all_found:
                missing = len(expected - self.found[operation])
                prior['missing_related'] = missing
                if missing and self.cohort.get('version') == 2:
                    prior['missing_identities'] = sorted(expected - self.found[operation])
                prior['status'] = 'BLOCKED' if missing else 'COMPLETED'
                if missing:
                    self.status = 'BLOCKED'
            else:
                prior['status'] = ''
            return SourcePage(rows, None if all_found else page.next_offset,
                              getattr(page, "offset_checkpoint", None), getattr(page, "changed_overlap_ids", frozenset()))

    def customers(self, offset=0):
        return self._related_page('HeroCustomers', self.source.customers, offset, field='id',
                                  identities=self.identities['customer_ids'])

    def projects(self, offset=0):
        return self._related_page('HeroProjectMatches', self.source.projects, offset, field='id',
                                  identities=self.identities['project_ids'])

    def projection(self, offset=0):
        operation = self.source.contract.target_operation
        if operation not in self.PROJECT_PATHS:
            if self.cohort.get('version') == 2:
                raise ValueError('Projection has no verified relationship to the selected projects.')
            return super().projection(offset)
        key = 'task_ids' if operation == 'HeroTasks' else 'event_ids'
        return self._related_page(operation, self.source.projection, offset,
                                  field=self.PROJECT_PATHS[operation], identities=self.identities['project_ids'],
                                  selected_ids=self.identities[key] if key in self.cohort else None)
