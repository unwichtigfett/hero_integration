"""Synthetic scheduler-to-MariaDB polling; never connects to HERO."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration import api, tasks
from hero_integration.services import migration
from hero_integration.sources.hero import SourcePage


class TestSyntheticPollingMariaDB(TestCase):
    def setUp(self):
        if (not os.environ.get('HERO_TEST_PRIVATE_SITE') or os.environ.get('HERO_TEST_PRIVATE_SITE') != frappe.local.site or
                os.environ.get('HERO_SYNTHETIC_POLLING_TEST') != '1'):
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        frappe.set_user('Administrator')
        self.source = 'synthetic-poll-' + uuid4().hex[:12]
        self.record = {'id': 11001, 'name': 'Synthetic Poll ' + uuid4().hex[:10]}
        self.settings_fields = ('polling_mode', 'sync_service_paused', 'polling_interval_minutes',
            'polling_timezone', 'polling_execution_window', 'hero_source_instance',
            'sync_area_contacts', 'sync_area_projects', 'sync_area_events', 'sync_area_tasks', 'sync_status', 'sync_error')
        self.original = {name: frappe.db.get_single_value('HERO Integration Settings', name) for name in self.settings_fields}
        values = {'polling_mode': 'AUTO_SYNC', 'sync_service_paused': 0, 'polling_interval_minutes': 1,
            'polling_timezone': 'UTC', 'polling_execution_window': '', 'hero_source_instance': self.source,
            'sync_area_contacts': 1, 'sync_area_projects': 0, 'sync_area_events': 0, 'sync_area_tasks': 0}
        frappe.db.set_single_value('HERO Integration Settings', values)
        frappe.db.commit()
        frappe.clear_cache(doctype='HERO Integration Settings')

    def tearDown(self):
        runs = frappe.get_all('HERO Integration Run', filters={'source_instance': self.source}, pluck='name')
        links = frappe.get_all('HERO Integration Link', filters={'source_instance': self.source},
                               fields=['name', 'target_doctype', 'target_name'])
        if frappe.db.get_single_value('HERO Integration Sync State', 'active_run') in runs:
            frappe.db.set_single_value('HERO Integration Sync State', 'active_run', '')
        if runs:
            frappe.db.delete('HERO Integration Record', {'run': ['in', runs]})
        frappe.db.delete('HERO Polling Event', {'source_instance': self.source})
        frappe.db.delete('HERO Integration Link', {'source_instance': self.source})
        frappe.db.delete('HERO Integration Run', {'source_instance': self.source})
        for link in links:
            if link.target_doctype == 'Customer' and link.target_name:
                frappe.db.delete('Customer', link.target_name)
        frappe.db.set_single_value('HERO Integration Settings', self.original)
        frappe.db.commit()
        frappe.clear_cache(doctype='HERO Integration Settings')

    def test_scheduler_and_real_mariadb_handle_remote_and_local_changes(self):
        source = self.source
        row = self.record
        contract = {'version': 1, 'fixture': source}
        started = []
        plan = SimpleNamespace(definitions=[], source_identity=lambda record: str(record['id']), mapping_version='synthetic-v1')

        class SyntheticSource:
            source_instance = source
            def customers(self, offset=0):
                return SourcePage(({**row, '_hero_source_instance': source},), 1) if offset == 0 else SourcePage((), None)

        def start(**kwargs):
            self.assertTrue(kwargs['scheduled'])
            run = frappe.get_doc({'doctype': 'HERO Integration Run', 'action': kwargs['action'], 'mode': 'Execute',
                'state': 'Queued', 'source_instance': source, 'checkpoint_contract_json': json.dumps(contract, sort_keys=True)}).insert()
            started.append(run.name)
            return {'status': 'QUEUED', 'run': run.name}

        def migrate(record, run):
            return migration._upsert('customer', str(record['id']), record,
                {'customer_name': record['name'], 'customer_type': 'Individual'}, 'Customer', 'customer_name',
                'synthetic-v1', run, source_instance=source)

        clock = [frappe.utils.now_datetime()]
        with patch.object(api, '_sync_area_read_services', return_value={'HeroCustomers': [{'service': 'synthetic'}]}), \
             patch.object(api, '_start_migration', side_effect=start), \
             patch.object(tasks, '_system_timezone', return_value='UTC'), \
             patch.object(frappe.utils, 'now_datetime', side_effect=lambda: clock[0]), \
             patch.object(migration, '_run_checkpoint_contract', return_value=contract), \
             patch.object(migration, 'validate_run_mappings'), \
             patch.object(migration, 'get_configuration', return_value=SimpleNamespace(source_instance=source)), \
             patch.object(migration, '_source_for_run', return_value=SyntheticSource()), \
             patch.object(migration, '_mapping_plan_for_run', return_value=plan), \
             patch.object(migration, '_migrate_customer', side_effect=migrate):
            tasks.run_scheduled_sync()
            self.assertEqual(len(started), 1)
            tasks.run_scheduled_sync()  # queued run prevents overlap
            self.assertEqual(len(started), 1)
            migration.execute_run(started[-1]); frappe.db.commit()
            link = frappe.get_doc('HERO Integration Link', {'source_type': 'customer', 'source_instance': source, 'source_id': '11001'})
            customer = frappe.get_doc('Customer', link.target_name)
            self.assertEqual(customer.customer_name, row['name'])
            self.assertEqual(frappe.db.get_value('HERO Integration Run', started[-1], 'state'), 'Completed')

            clock[0] += timedelta(seconds=2)  # document creation uses the real test-site clock
            tasks.run_scheduled_sync()  # same interval is not due
            self.assertEqual(len(started), 1)
            clock[0] += timedelta(minutes=2)
            tasks.run_scheduled_sync(); self.assertEqual(len(started), 2)
            migration.execute_run(started[-1]); frappe.db.commit()
            self.assertEqual(frappe.db.count('HERO Integration Link',
                {'source_instance': source, 'source_type': 'customer', 'source_id': '11001'}), 1)
            self.assertEqual(frappe.db.get_value('HERO Integration Run', started[-1], 'created_records'), 0)
            row['name'] += ' Remote update'
            clock[0] += timedelta(minutes=2)
            tasks.run_scheduled_sync(); self.assertEqual(len(started), 3)
            migration.execute_run(started[-1]); frappe.db.commit()
            customer.reload(); self.assertEqual(customer.customer_name, row['name'])

            customer.customer_name += ' Local edit'; customer.save(); frappe.db.commit()
            local_name = customer.customer_name
            row['name'] += ' conflicting remote update'
            clock[0] += timedelta(minutes=2)
            tasks.run_scheduled_sync(); self.assertEqual(len(started), 4)
            migration.execute_run(started[-1]); frappe.db.commit()
            customer.reload(); link.reload()
            self.assertEqual(customer.customer_name, local_name)
            self.assertEqual(link.sync_status, 'Conflict')
            self.assertEqual(frappe.db.get_value('HERO Integration Run', started[-1], 'state'), 'Completed With Conflicts')
            self.assertEqual(frappe.db.count('HERO Polling Event', {'source_instance': source, 'status': 'Started'}), 4)
            from hero_integration.services.polling import execution_summary
            counts = execution_summary(frappe, source, '2000-01-01 00:00:00')
            self.assertEqual((counts['runs'], counts['pass'], counts['conflicts'], counts['running']), (4, 3, 1, 0))
