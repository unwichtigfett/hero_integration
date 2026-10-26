"""Opt-in Frappe database-hook test against an explicitly disposable MariaDB."""
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOCKET = os.environ.get("HERO_LOCK_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires explicitly supplied disposable MariaDB socket")
class TestActualFrappeTransactionHooks(unittest.TestCase):
    def setUp(self):
        import frappe
        import pymysql
        from frappe.database.mariadb.database import MariaDBDatabase
        self.frappe, self.pymysql = frappe, pymysql
        self.admin = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with self.admin.cursor() as cursor:
            cursor.execute("CREATE DATABASE hero_frappe_hook_test")
        self.temp = tempfile.TemporaryDirectory(prefix="hero-frappe-hook-")
        site = Path(self.temp.name) / "disposable.localhost"
        site.mkdir()
        (site / "site_config.json").write_text(json.dumps({"db_type": "mariadb"}))
        with patch.object(frappe, "setup_module_map"), patch.object(frappe, "setup_redis_cache_connection"):
            frappe.init("disposable.localhost", sites_path=self.temp.name, force=True)
        with patch.object(frappe, "logger", return_value=logging.getLogger("hero-hook-test")):
            frappe.local.db = MariaDBDatabase(socket=SOCKET, user="root", cur_db_name="hero_frappe_hook_test")
        frappe.local.db.connect()
        self.other = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_frappe_hook_test", autocommit=True)

    def tearDown(self):
        self.frappe.db.close()
        self.frappe.destroy()
        self.other.close()
        with self.admin.cursor() as cursor:
            cursor.execute("DROP DATABASE hero_frappe_hook_test")
        self.admin.close()
        self.temp.cleanup()

    def try_lock(self, name):
        with self.other.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0)", (name,))
            return cursor.fetchone()[0]

    def release(self, name):
        with self.other.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_actual_commit_and_rollback_callbacks_and_savepoint(self):
        from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
        db = self.frappe.db
        _lock_active_target("HeroCustomers", "Customer", "custom_hero_x")
        name = next(iter(db._hero_mapping_target_locks))
        db.savepoint("probe")
        db.rollback(save_point="probe")
        self.assertEqual(self.try_lock(name), 0)
        db.commit()
        self.assertEqual(self.try_lock(name), 1)
        self.release(name)
        _lock_active_target("HeroCustomers", "Customer", "custom_hero_x")
        self.assertEqual(self.try_lock(name), 0)
        db.rollback()
        self.assertEqual(self.try_lock(name), 1)
        self.release(name)
