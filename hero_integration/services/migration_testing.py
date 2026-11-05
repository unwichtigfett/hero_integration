"""Frozen per-area record limits for preview and execute migration runs."""
import json

import frappe


def configuration(parent=None, *, frappe_module=None):
    owner = frappe_module if frappe_module is not None else frappe
    doc = parent if parent is not None else owner.get_single('HERO Integration Settings')
    enabled = bool(int(getattr(doc, 'testing_enabled', 0) or 0))
    maximum = getattr(doc, 'testing_max_actions', 5) or 5
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
               or key.startswith('skipped_'))


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
        'missing_related_partners': len(state.get('testing_missing_partner_ids', []))}}}
    if state.get('testing_cohort_fingerprint') or 'testing_cohort_user_ids' in state:
        result['selection'] = 'related_projects'
    frappe.db.set_value('HERO Integration Run', run_name, {
        'testing_status': status, 'testing_records_received': state.get('testing_records_received', 0),
        'testing_results_json': json.dumps(result, sort_keys=True)})


def test_state(run_name, operation):
    if not run_name: raise ValueError('Testing migration requires a run-bound checkpoint.')
    raw = frappe.db.get_default('hero_testing_migration:' + run_name + ':' + operation)
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
        self.records_received += len(page.records)
        prior['received'] += len(page.records)
        rows = page.records[:remaining]
        prior['processed'] += len(rows)
        reached = prior['processed'] >= self.maximum
        prior['status'] = 'LIMIT_REACHED' if reached else 'COMPLETED'
        if reached: self.status = 'LIMIT_REACHED'
        return SourcePage(tuple(rows), None if reached else page.next_offset)

    def customers(self, offset=0): return self._page('HeroCustomers', self.source.customers, offset)
    def projects(self, offset=0): return self._page('HeroProjectMatches', self.source.projects, offset)
    def projection(self, offset=0):
        operation = self.source.contract.target_operation
        return self._page(operation, self.source.projection, offset)


def project_cohort(records, plan, maximum):
    """Freeze only identities needed for a related full-migration test sample."""
    from hero_integration.services.direct_mapping import MISSING, lookup
    validate_limit(maximum)
    if maximum is None:
        raise ValueError('A related test sample requires an explicit project limit.')
    selected = list(records)[:maximum]
    projects, customers, users, hashes = set(), set(), set(), {}
    customer_rules = [rule for rule in plan.definitions
                      if rule.transformation == 'HERO Customer Link' or rule.purpose == 'Customer Relation']
    for record in selected:
        identity = str(plan.source_identity(record))
        if not identity or identity in projects:
            raise ValueError('The project sample has a missing or duplicate source identity.')
        projects.add(identity)
        for rule in customer_rules:
            value = lookup(record, rule.source_path)
            if value is MISSING:
                raise ValueError('The project sample omits the mapped customer identity.')
            if type(value) is int and value > 0:
                customers.add(str(value))
        if 'project_match_assignments' not in record:
            raise ValueError('The project sample omits its assignee selection.')
        for row in record.get('project_match_assignments') or []:
            value = row.get('user_id') if isinstance(row, dict) else None
            if type(value) is int and value > 0:
                users.add(str(value))
        hashes[identity] = relationship_fingerprint('HeroProjectMatches', record, [rule.source_path for rule in customer_rules])
    return {'version': 1, 'project_ids': sorted(projects), 'customer_ids': sorted(customers),
            'user_ids': sorted(users), 'partner_ids': [], 'task_ids': [], 'event_ids': [],
            'customer_paths': [rule.source_path for rule in customer_rules], 'project_relation_hashes': hashes,
            'task_relation_hashes': {}, 'event_relation_hashes': {}}


def relationship_fingerprint(operation, record, customer_paths=()):
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


def prepare_project_cohort(client, read, maximum):
    """Read a bounded anchor sample using the selected approved project reader."""
    from hero_integration.services.direct_runtime import fetch_page
    validate_limit(maximum)
    if maximum is None:
        raise ValueError('A related test sample requires an explicit project limit.')
    rows, offset, seen, pages = [], 0, set(), 0
    while len(rows) < maximum:
        if pages >= 1000:
            raise ValueError('Project test selection reached its page budget before enough active projects were found.')
        page = fetch_page(client, read, offset)
        pages += 1
        if not page:
            break
        identities = [str(read.plan.source_identity(row)) for row in page]
        if any(not identity for identity in identities) or len(set(identities)) != len(identities) or seen.intersection(identities):
            raise ValueError('The project sample reader repeated or omitted an identity.')
        if any(str(row.get('id', '')) != identity for row, identity in zip(page, identities)):
            raise ValueError('The project sample requires the selected project id to match its identity.')
        seen.update(identities)
        if any(type(row.get('is_deleted')) is not bool for row in page):
            raise ValueError('The project sample requires an explicit project deletion state.')
        rows.extend([row for row in page if not row['is_deleted']][:maximum - len(rows)])
        offset += len(page)
    return project_cohort(rows, read.plan, maximum)


def project_reader_fingerprint(read):
    """Bind a sample to its approved query and effective mapping version."""
    from hashlib import sha256
    payload = [read.operation, read.origin, read.mapping, read.mapping_version,
               read.document, read.records_path, read.variables, read.page_size]
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
        if 'id' not in row or path not in row:
            raise ValueError('Related test discovery omits an identity or project reference.')
        if str(row[path]) not in projects:
            continue
        identity = row['id']
        if type(identity) is not int or identity <= 0:
            raise ValueError('Related test discovery found an invalid record identity.')
        if str(identity) in identities:
            raise ValueError('Related test discovery repeated a record identity.')
        identities.add(str(identity))
        hash_key = 'task_relation_hashes' if operation == 'HeroTasks' else 'event_relation_hashes'
        cohort.setdefault(hash_key, {})[str(identity)] = relationship_fingerprint(operation, row)
        if operation == 'HeroTasks':
            for field in ('author_user_id', 'target_user_id'):
                if field not in row:
                    raise ValueError('Related Task selection omits an assigned user identity.')
                value = row[field]
                if type(value) is int and value > 0:
                    users.add(str(value))
        else:
            if 'partners' not in row or (row['partners'] is not None and not isinstance(row['partners'], list)):
                raise ValueError('Related Event selection omits partner identities.')
            for partner in row['partners'] or []:
                if not isinstance(partner, dict) or 'id' not in partner:
                    raise ValueError('Related Event partner identity is unavailable.')
                value = partner['id']
                if type(value) is int and value > 0:
                    partners.add(str(value))
    cohort[name], cohort['user_ids'], cohort['partner_ids'] = sorted(identities), sorted(users), sorted(partners)


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
        if not isinstance(cohort, dict) or cohort.get('version') != 1:
            raise ValueError('The related project sample is missing or incompatible.')
        self.cohort = cohort
        self.identities = {key: frozenset(cohort.get(key, [])) for key in
                           ('project_ids', 'customer_ids', 'task_ids', 'event_ids')}
        self.scan_pages = {}
        self.last_page = {}
        self.found = {}

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
            self.records_received += len(page.records)
            prior['received'] += len(page.records)
            if page.next_offset is not None and (type(page.next_offset) is not int or page.next_offset <= offset or not page.records):
                raise ValueError('Related test source pagination did not advance safely.')
            values = [row.get('id') if isinstance(row, dict) else None for row in page.records]
            if any(not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip() for value in values):
                raise ValueError('Related test source page has missing or invalid identities.')
            ids = [str(value) for value in values]
            if len(set(ids)) != len(ids):
                raise ValueError('Related test source page has missing or duplicate identities.')
            fingerprint = sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest() if ids else ''
            if fingerprint and self.last_page.get(operation) == fingerprint:
                raise ValueError('Related test source repeated a page.')
            self.last_page[operation] = fingerprint
            rows = tuple(row for row in page.records if str(row.get(field, '')) in allowed
                         and (selected is None or str(row.get('id')) in selected))
            hash_key = {'HeroProjectMatches': 'project_relation_hashes', 'HeroTasks': 'task_relation_hashes',
                        'HeroCalendarEvents': 'event_relation_hashes'}.get(operation)
            expected_hashes = self.cohort.get(hash_key, {}) if hash_key else {}
            for row in rows:
                expected_hash = expected_hashes.get(str(row.get('id')))
                if expected_hash and relationship_fingerprint(operation, row, self.cohort.get('customer_paths', ())) != expected_hash:
                    raise ValueError('A selected test record changed its relationships; start a new test run.')
            self.found.setdefault(operation, set()).update(str(row.get('id') if selected is not None else row.get(field)) for row in rows)
            prior['processed'] += len(rows)
            if page.next_offset is None:
                expected = selected if selected is not None else allowed if operation in {'HeroCustomers', 'HeroProjectMatches'} else set()
                missing = len(expected - self.found[operation])
                prior['missing_related'] = missing
                prior['status'] = 'BLOCKED' if missing else 'COMPLETED'
                if missing:
                    self.status = 'BLOCKED'
            else:
                prior['status'] = ''
            return SourcePage(rows, page.next_offset)

    def customers(self, offset=0):
        return self._related_page('HeroCustomers', self.source.customers, offset, field='id',
                                  identities=self.identities['customer_ids'])

    def projects(self, offset=0):
        return self._related_page('HeroProjectMatches', self.source.projects, offset, field='id',
                                  identities=self.identities['project_ids'])

    def projection(self, offset=0):
        operation = self.source.contract.target_operation
        if operation not in self.PROJECT_PATHS:
            return super().projection(offset)
        key = 'task_ids' if operation == 'HeroTasks' else 'event_ids'
        return self._related_page(operation, self.source.projection, offset,
                                  field=self.PROJECT_PATHS[operation], identities=self.identities['project_ids'],
                                  selected_ids=self.identities[key] if key in self.cohort else None)
