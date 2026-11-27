"""Existing test environment admission and selective reset confirmation."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


class TestResetBoundary(unittest.TestCase):
    def setUp(self):
        from hero_integration.services import test_environment as env
        self.env = env
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.state = folder / 'sandbox.json'
        self.inbox = folder / 'inbox'
        self.inbox.mkdir()
        self.ident = uuid4().hex
        self.site = f'hero-test-{self.ident}.localhost'
        self.context = SimpleNamespace(site=self.site)
        self.frappe = SimpleNamespace(local=self.context, conf=SimpleNamespace(db_name='ht_' + self.ident[:20], db_host='db'),
                                      session=SimpleNamespace(user='Administrator'))
        for name, value in [('STATE', self.state), ('INBOX', self.inbox)]:
            p = patch.object(env, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.doc = dict(environment=self.ident, site=self.site, database=self.frappe.conf.db_name, phase='ready',
                        generation=uuid4().hex, baseline='a'*64)
        self.state.write_text(json.dumps(self.doc))

    def test_no_marker_foreign_site_and_pending_reset_deny_admission(self):
        self.assertEqual(self.env.require_environment(self.frappe), self.ident)
        self.context.site = 'dev.example.invalid'
        with self.assertRaises(ValueError):
            self.env.require_environment(self.frappe)
        self.context.site = self.site
        (self.inbox / 'pending.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.env.require_environment(self.frappe)
        self.state.unlink()
        with self.assertRaises(ValueError):
            self.env.require_environment(self.frappe)

    def test_only_administrator_with_current_preview_can_submit(self):
        self.frappe.session.user = 'Guest'
        with self.assertRaises(PermissionError):
            self.env.submit(self.frappe, 'bad', True)
        self.frappe.session.user = 'Administrator'
        from hero_integration.services import run_reservation
        for name, value in [('acquire',True), ('release',None), ('locked_legacy_slot',''),
                            ('locked_pending_migrations',[]), ('locked_b8_runs',[])]:
            self.enterContext(patch.object(run_reservation, name, return_value=value))
        from hero_integration.services import selective_test_reset as reset
        activity = dict.fromkeys(("active_or_queued", "waiting_jobs", "running_jobs",
                                  "synchronization_enabled", "external_send_enabled"), 0)
        manifest = {"artifacts": {}, "native": [], "files": [], "retained_runs": []}
        with patch.object(self.env, "_reset_activity", return_value=activity), \
                patch.object(self.env, "_key", return_value="synthetic-site-key"), \
                patch.object(reset, "plan", return_value=(manifest, {"active_mutations": False})), \
                patch.object(reset, "apply") as apply:
            preview = self.env.preview(self.frappe)
            for token, confirm in [(preview["token"], False), ("bad", True)]:
                with self.assertRaises(ValueError):
                    self.env.submit(self.frappe, token, confirm)
            with patch.object(self.env, "_reset_activity", return_value={**activity, "running_jobs": 1}):
                with self.assertRaisesRegex(ValueError, "finish"):
                    self.env.submit(self.frappe, preview["token"], True)
            with patch.object(reset, "plan", return_value=({**manifest, "native": [("Task", "unreviewed")]}, {})):
                with self.assertRaisesRegex(ValueError, "selection changed"):
                    self.env.submit(self.frappe, preview["token"], True)
            apply.assert_not_called()
            self.assertEqual(self.env.submit(self.frappe, preview["token"], True)["status"], "completed")
            apply.assert_called_once_with(self.frappe, manifest)
            self.assertFalse((self.inbox / "pending.json").exists())
            self.doc["generation"] = uuid4().hex
            self.state.write_text(json.dumps(self.doc))
            with self.assertRaises(ValueError):
                self.env.submit(self.frappe, preview["token"], True)

    def test_exclusive_dev_uses_operator_bound_site_database_and_environment(self):
        self.context.site = self.doc['site'] = 'development.invalid'
        self.frappe.conf.db_name = self.doc['database'] = 'dev_database'
        self.state.write_text(json.dumps(self.doc))
        with self.assertRaises(ValueError):
            self.env.require_environment(self.frappe)
        self.frappe.conf.hero_exclusive_test_environment = self.ident
        self.assertEqual(self.env.require_environment(self.frappe), self.ident)
        self.frappe.conf.db_name = 'different_db'
        with self.assertRaises(ValueError):
            self.env.require_environment(self.frappe)
