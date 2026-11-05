"""Synchronization page: areas, mapping scope, sync switches and the one-button start."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.tests._frappe_test_import import load_api


api = load_api()
from hero_integration.services import sync_page
from hero_integration.services.polling import runtime_plan, scheduler_due


def _settings(**values):
    return SimpleNamespace(**{"polling_mode": "MONITOR", "scheduled_sync_enabled": 0, "polling_interval_minutes": 60, **values})


class TestAreas(unittest.TestCase):
    def test_manual_prerequisites_are_source_scoped_and_require_terminal_success(self):
        runs = {}
        def get_all(doctype, *, filters, **kwargs):
            self.assertEqual(filters['source_instance'], 'source-a')
            state = runs.get(filters['action'])
            return [{'state': state}] if state else []
        owner = SimpleNamespace(get_all=get_all)
        self.assertEqual(sync_page.manual_prerequisite('source-a', owner), 'employees')
        for state in ('Queued', 'Running', 'Failed', 'Cancelled', 'Blocked'):
            runs['Employee Migration'] = state
            self.assertEqual(sync_page.manual_prerequisite('source-a', owner), 'employees')
        runs['Employee Migration'] = 'Completed With Conflicts'
        self.assertEqual(sync_page.manual_prerequisite('source-a', owner), 'contacts')
        runs['Customer Migration'] = 'Completed'
        self.assertEqual(sync_page.manual_prerequisite('source-a', owner), '')
        self.assertTrue(sync_page.manual_allowed('employees', 'employees'))
        self.assertFalse(sync_page.manual_allowed('contacts', 'employees'))
        self.assertFalse(sync_page.manual_allowed('tasks', 'contacts'))
        self.assertTrue(sync_page.manual_allowed('tasks', ''))

    def test_offline_import_restores_frappe_and_api_module_cache(self):
        script = """\
import sys
sys.path.insert(0, sys.argv[1])
import hero_integration
assert not hasattr(hero_integration, 'api')
import hero_integration.tests.test_sync_page
assert 'frappe' not in sys.modules
assert 'hero_integration.api' not in sys.modules
assert not hasattr(hero_integration, 'api')
import hero_integration.tests.test_workspace_snapshot_binding
assert 'frappe' not in sys.modules
assert 'hero_integration.api' not in sys.modules
assert not hasattr(hero_integration, 'api')
for name in ('test_workspace_snapshot_selection', 'test_workspace_overview_contract', 'test_node_bookmark',
             'test_workspace_mutation_draft_contract', 'test_workspace_service_execution'):
    __import__('hero_integration.tests.' + name)
    assert 'frappe' not in sys.modules
    assert 'hero_integration.api' not in sys.modules
    assert not hasattr(hero_integration, 'api')
"""
        subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(Path(__file__).resolve().parents[2])],
            check=True,
        )

    def test_failed_offline_api_import_restores_preexisting_package_alias(self):
        script = """\
import importlib
import sys
sys.path.insert(0, sys.argv[1])
import hero_integration
sentinel = object()
hero_integration.api = sentinel
original_import = importlib.import_module
def failing_import(name, *args, **kwargs):
    if name == 'hero_integration.api':
        raise RuntimeError('synthetic import failure')
    return original_import(name, *args, **kwargs)
importlib.import_module = failing_import
try:
    import hero_integration.tests.test_sync_page
except RuntimeError as error:
    assert str(error) == 'synthetic import failure'
else:
    raise AssertionError('synthetic import did not fail')
assert 'frappe' not in sys.modules
assert 'hero_integration.api' not in sys.modules
assert hero_integration.api is sentinel
"""
        subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(Path(__file__).resolve().parents[2])],
            check=True,
        )

    def test_four_areas_use_the_new_names_and_keep_the_stored_run_actions(self):
        self.assertEqual([item.action_label for item in sync_page.AREAS],
                         ["Contact Migration", "Project Migration", "Event Migration", "Task Migration"])
        self.assertEqual(sync_page.action_label("Customer Migration"), "Contacts")
        self.assertEqual(sync_page.action_label("Schedule Migration"), "Events")
        self.assertEqual(sync_page.action_label("Projection Migration"), "Tasks")
        self.assertEqual(sync_page.action_label("Full Data Migration"), "Full Data Migration")
        with self.assertRaises(ValueError):
            sync_page.area("calendar")

    def test_mapping_scope_counts_each_object_and_field_once(self):
        scope = sync_page.mapping_scope([
            {"target_doctype": "Customer", "target_field": "customer_name"},
            {"target_doctype": "Customer", "target_field": "customer_name"},
            {"target_doctype": "Customer", "target_field": "email_id"},
            {"target_doctype": "Address", "target_field": "city"},
            {"target_doctype": "", "target_field": "ignored"},
        ])
        self.assertEqual((scope["object_count"], scope["field_count"]), (2, 3))
        self.assertEqual(scope["objects"][0], {"doctype": "Address", "fields": ["city"]})

    def test_area_switches_default_to_on(self):
        item = sync_page.area("events")
        self.assertTrue(sync_page.area_enabled(SimpleNamespace(), item))
        self.assertFalse(sync_page.area_enabled(SimpleNamespace(sync_area_events=0), item))


class TestPollingSwitches(unittest.TestCase):
    now = datetime(2026, 11, 10, 12, 0)

    def test_paused_service_runs_nothing_and_keeps_its_settings(self):
        settings = _settings(sync_service_paused=1, polling_page_size=25)
        plan = runtime_plan(settings)
        self.assertTrue(plan["paused"])
        self.assertEqual(plan["page_size"], 25)
        self.assertFalse(scheduler_due(settings, self.now))
        self.assertTrue(scheduler_due(_settings(sync_service_paused=0), self.now))

    def test_area_switches_select_the_polled_sources(self):
        plan = runtime_plan(_settings(sync_area_contacts=0))
        self.assertEqual(plan["sources"], ["HeroProjectMatches"])
        self.assertFalse(plan["areas"]["contacts"])
        off = _settings(sync_area_contacts=0, sync_area_projects=0, sync_area_events=0, sync_area_tasks=0)
        self.assertFalse(scheduler_due(off, self.now))
        self.assertFalse(runtime_plan(_settings(polling_mode="AUTO_SYNC", sync_service_paused=1))["can_auto_sync"])


class _Frappe:
    _ = staticmethod(lambda message: message)

    def __init__(self, settings, existing_runs=False):
        self.settings, self.existing_runs, self.started = settings, existing_runs, []
        self.db = SimpleNamespace(exists=lambda doctype, filters: self.existing_runs)

    def only_for(self, role):
        assert role == "System Manager"

    def get_single(self, doctype):
        return self.settings

    def throw(self, message):
        raise ValueError(message)


class TestSyncSwitchApi(unittest.TestCase):
    def test_sync_cannot_be_switched_on_while_polling_is_off_in_settings(self):
        settings = _settings(polling_mode="OFF", sync_service_paused=1, save=lambda: None)
        with patch.object(api, "frappe", _Frappe(settings)), patch.object(api, "sync_page_state", return_value={}):
            with self.assertRaisesRegex(ValueError, "Polling is switched off in Settings"):
                api.sync_service_set(enabled=1)
            api.sync_service_set(enabled=0)
        self.assertEqual(settings.sync_service_paused, 1)

    def test_switches_change_only_pause_and_area_flags(self):
        saved = []
        settings = _settings(sync_service_paused=1, polling_interval_minutes=15, save=lambda: saved.append(True))
        with patch.object(api, "frappe", _Frappe(settings)), patch.object(api, "sync_page_state", return_value={}):
            api.sync_service_set(enabled="1")
            api.sync_service_set(area="tasks", area_enabled=0)
            with self.assertRaisesRegex(ValueError, "Unknown synchronization area"):
                api.sync_service_set(area="tickets", area_enabled=1)
        self.assertEqual((settings.sync_service_paused, settings.sync_area_tasks, settings.polling_interval_minutes), (0, 0, 15))
        self.assertEqual(len(saved), 2)


class TestAreaStart(unittest.TestCase):
    def start(self, services, existing_runs=False):
        fake = _Frappe(_settings(hero_source_instance="main"), existing_runs)
        with patch.object(api, "frappe", fake), patch.object(api, "_configured_source_instance", return_value="main"), \
                patch.object(sync_page, "manual_prerequisite", return_value=""), \
                patch.object(api, "_sync_area_read_services", return_value=services), \
                patch.object(api, "_start_migration", return_value={"status": "QUEUED", "run": "RUN-1"}) as start:
            return api.sync_area_start("events"), start

    def test_server_rejects_start_before_prerequisites(self):
        fake = _Frappe(_settings())
        with patch.object(api, 'frappe', fake), patch.object(api, '_configured_source_instance', return_value='main'), \
                patch.object(sync_page, 'manual_prerequisite', return_value='employees'):
            with self.assertRaisesRegex(ValueError, 'Migrate Employees first'):
                api.sync_area_start('contacts')
            with self.assertRaisesRegex(ValueError, 'Migrate Employees first'):
                api.start_migration('Project Migration', dry_run=0, graphql_service='SERVICE')

    def test_missing_service_explanation_uses_current_language_and_keeps_source(self):
        source = ('{0} needs an approved read service with an active mapping for {1} '
                  'on a snapshot of the HERO source instance "{2}". Approve one under API & Schema and Mapping.')
        translated = '{0} benötigt ein aktives Mapping für {1} in Quelle "{2}".'
        messages = {source: translated, 'Event Migration': 'Event-Migration', 'Events': 'Termine'}
        fake = _Frappe(_settings())
        fake._ = lambda value: messages.get(value, value)
        with patch.object(api, 'frappe', fake):
            result = api._sync_area_blocker(sync_page.area('events'), 'source-42', translate=True)
            stored = api._sync_area_blocker(sync_page.area('events'), 'source-42')
        self.assertEqual(result, 'Event-Migration benötigt ein aktives Mapping für Termine in Quelle "source-42".')
        self.assertEqual(stored, source.format('Event Migration', 'Events', 'source-42'))

    def test_one_click_uses_the_resolved_service_and_source_instance(self):
        result, start = self.start({"HeroCalendarEvents": [{"service": "SVC-NEW", "mapping": "MAP", "snapshot": "SNAP"}]})
        self.assertEqual(result, {"status": "QUEUED", "run": "RUN-1", "area": "events"})
        start.assert_called_once_with(action="Schedule Migration", dry_run=0, scheduled=False,
                                      graphql_service="SVC-NEW", source_instance="main")

    def test_missing_service_or_active_run_is_explained_and_nothing_starts(self):
        with self.assertRaisesRegex(ValueError, "Event Migration needs an approved read service"):
            self.start({})
        with self.assertRaisesRegex(ValueError, "Another HERO run is still queued or running"):
            self.start({"HeroCalendarEvents": [{"service": "SVC"}]}, existing_runs=True)


class TestPendingRelationshipsApi(unittest.TestCase):
    def test_only_unresolved_records_are_returned_with_source_scoped_target_status(self):
        rows = [dict(name='a', operation='HeroTasks', source_id='7', status='Waiting', detail=''),
                dict(name='b', operation='HeroCalendarEvents', source_id='8', status='Waiting', detail=''),
                dict(name='c', operation='HeroTasks', source_id='9', status='Conflict', detail='review')]
        calls = []
        def get_all(doctype, **kwargs):
            calls.append((doctype, kwargs))
            if doctype == 'HERO Pending Reference':
                return rows
            if doctype == 'HERO Integration Link':
                return [{'target_doctype': 'Task', 'target_name': 'TASK-7'}] if kwargs['filters']['source_id'] == '7' else []
            return [{'source_type': 'project', 'source_id': '5'}]
        fake = SimpleNamespace(only_for=lambda role: None, get_all=get_all,
            db=SimpleNamespace(exists=lambda doctype, name: name == 'TASK-7', count=lambda *args: 3))
        with patch.object(api, 'frappe', fake), patch.object(api, '_configured_source_instance', return_value='source-a'), \
                patch('hero_integration.services.deferred_references.installed', return_value=True):
            result = api.sync_page_pending_relationships()
        self.assertEqual([r['display_status'] for r in result['rows']],
            ['Imported; waiting for relationship', 'Waiting to import', 'Conflict requires review'])
        self.assertEqual(calls[0][1]['filters'], {'source_instance': 'source-a', 'status': ['in', ['Waiting', 'Conflict']]})
        self.assertTrue(all(c[1]['filters']['source_instance'] == 'source-a' for c in calls if c[0] == 'HERO Integration Link'))


class TestAutoSyncAreaGate(unittest.TestCase):
    def test_switched_off_area_blocks_its_automatic_runtime_plan(self):
        from hero_integration.services import sync_runtime_adapter as adapter

        fake = SimpleNamespace(db=SimpleNamespace(get_value=lambda *args: '{"target_operation": "HeroTasks"}'))
        selection = SimpleNamespace(graphql_mapping="MAP-TASKS")
        with self.assertRaisesRegex(adapter.RuntimeBridgeError, "Automatic sync for Tasks is switched off"):
            adapter._require_area_enabled(fake, _settings(sync_area_tasks=0), selection)
        adapter._require_area_enabled(fake, _settings(sync_area_events=0), selection)
        adapter._require_area_enabled(None, _settings(), SimpleNamespace())  # all on: no lookup at all


if __name__ == "__main__":
    unittest.main()
