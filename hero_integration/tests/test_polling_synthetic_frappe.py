"""Synthetic scheduler-to-MariaDB polling; never connects to HERO."""
from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from hero_integration import api, tasks


class TestSyntheticPollingMariaDB(TestCase):
    def setUp(self):
        site = os.environ.get('HERO_CI_TEST_SITE') or os.environ.get('HERO_TEST_PRIVATE_SITE')
        if not site or os.environ.get('HERO_SYNTHETIC_POLLING_TEST') != '1':
            self.skipTest('Requires explicitly selected synthetic Frappe site')
        self.assertEqual(site, frappe.local.site)
        self.assertTrue(frappe.conf.allow_tests)
        frappe.set_user('Administrator')
        self.source = 'synthetic-poll-' + uuid4().hex[:12]
        self.settings_fields = ('polling_mode', 'sync_service_paused', 'polling_interval_minutes',
            'polling_timezone', 'polling_execution_window', 'hero_source_instance',
            'sync_area_contacts', 'sync_area_projects', 'sync_area_events', 'sync_area_tasks', 'sync_status', 'sync_error')
        self.last_start = frappe.db.get_single_value("HERO Integration Sync State", "last_poll_started_on")
        self.original = {name: frappe.db.get_single_value('HERO Integration Settings', name) for name in self.settings_fields}
        values = {'polling_mode': 'AUTO_SYNC', 'sync_service_paused': 0, 'polling_interval_minutes': 1,
            'polling_timezone': 'UTC', 'polling_execution_window': '', 'hero_source_instance': self.source,
            'sync_area_contacts': 1, 'sync_area_projects': 0, 'sync_area_events': 0, 'sync_area_tasks': 0}
        frappe.db.set_single_value('HERO Integration Settings', values)
        frappe.db.commit()
        frappe.clear_cache(doctype='HERO Integration Settings')

    def tearDown(self):
        frappe.db.delete("HERO Polling Event", {"source_instance": self.source})
        frappe.db.delete("HERO Integration Run", {"source_instance": self.source})
        frappe.db.set_single_value("HERO Integration Sync State", "last_poll_started_on", self.last_start)
        frappe.db.set_single_value('HERO Integration Settings', self.original)
        frappe.db.commit()
        frappe.clear_cache(doctype='HERO Integration Settings')

    def test_scheduler_with_no_approved_plan_records_skip_without_importing(self):
        from hero_integration.client.graphql import HeroGraphQLClient

        frappe.db.set_single_value("HERO Integration Sync State", "last_poll_started_on", None)
        frappe.db.commit()
        with patch("frappe.utils.scheduler.is_scheduler_inactive", return_value=False), \
             patch.object(HeroGraphQLClient, "query") as transport, patch.object(api, "_start_migration") as start:
            tasks.run_scheduled_sync()
            tasks.run_scheduled_sync()
            start.assert_not_called()
            transport.assert_not_called()
        self.assertEqual(frappe.db.count("HERO Integration Run", {"source_instance": self.source}), 0)
        self.assertEqual(frappe.db.count("HERO Polling Event", {"source_instance": self.source, "status": "Skipped"}), 1)

    def test_manual_employee_import_with_scheduler_and_polling_off(self):
        from types import SimpleNamespace
        from frappe.utils.background_jobs import get_queue
        from hero_integration.integrations.hero_people import sync_hero_people
        from hero_integration.services import employee_migration, file_migration, file_transfer

        frappe.db.set_single_value("HERO Integration Settings", "polling_mode", "OFF")
        frappe.db.commit()
        config = SimpleNamespace(source_instance=self.source, source_binding_revision=3, source_binding_confirmed=True)
        read = SimpleNamespace(mapping_version="test-read")
        queue = get_queue("long")
        with patch.object(file_migration, "_configuration", return_value=config), \
             patch.object(file_migration.direct_runtime, "resolve_read", return_value=read), \
             patch.object(employee_migration, "preflight", return_value=("HERO CI Company","fixture","synthetic","partners")), \
             patch.object(file_transfer, "_client_and_downloader", return_value=(None, None, self.source)), \
             patch.object(employee_migration.runtime, "fetch_page", return_value=[]) as fetch, \
             patch("frappe.utils.scheduler.is_scheduler_inactive", side_effect=AssertionError("manual import consulted scheduler")):
            accepted = sync_hero_people()
            run = accepted["run"]
            matches = lambda: [job for job in queue.jobs if job.kwargs.get("kwargs", {}).get("run_name") == run]
            self.assertEqual(matches(), [])
            frappe.db.commit()
            jobs = matches()
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.addCleanup(job.delete)
            self.assertEqual(job.kwargs["site"], frappe.local.site)
            self.assertEqual(frappe.db.get_value("HERO Integration Run",run,"state"), "Queued")
            frappe.get_attr(job.kwargs["method"])(**job.kwargs["kwargs"])
            fetch.assert_called_once()
            self.assertEqual(frappe.db.get_value("HERO Integration Run",run,"state"), "Completed")
            self.assertEqual(frappe.db.get_single_value("HERO Integration Settings","polling_mode"), "OFF")
