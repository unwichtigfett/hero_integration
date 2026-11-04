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
    status = ('LIMIT_REACHED' if state.get('testing_limit_reached') else 'COMPLETED') if completed else ''
    result = {'record_limit': state['testing_record_limit'], 'areas': {operation: {
        'processed': processed(counts), 'received': state.get('testing_records_received', 0), 'status': status}}}
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
