"""Opt-in two-connection lock/read checks; never insert or update mappings."""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOCKET = os.environ.get("HERO_ACTIVE_GUARD_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires disposable MariaDB socket")
class TestActiveGuardMariaDB(unittest.TestCase):
    def setUp(self):
        import pymysql
        from frappe.database.database import CallbackManager
        from hero_integration.hero_integration.doctype.hero_graphql_mapping import hero_graphql_mapping as controller
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as locks
        self.mysql, self.controller, self.locks = pymysql, controller, locks
        admin = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with admin.cursor() as c: c.execute("CREATE DATABASE hero_guard_read_test")
        admin.close()
        self.connections = []
        self.thread = threading.local()
        outer = self
        class DB:
            def __init__(self):
                self.conn = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_guard_read_test", autocommit=False)
                self.after_commit, self.after_rollback = CallbackManager(), CallbackManager()
                outer.connections.append(self)
            def sql(self, query, args=None, as_dict=False):
                with self.conn.cursor(pymysql.cursors.DictCursor if as_dict else pymysql.cursors.Cursor) as c:
                    c.execute(query, args)
                    rows = c.fetchall()
                    return [SimpleNamespace(**row) for row in rows] if as_dict else rows
            def commit(self):
                self.conn.commit()
                self.after_rollback.reset()
                self.after_commit.run()
            def rollback(self):
                self.conn.rollback()
                self.after_commit.reset()
                self.after_rollback.run()
        class Facade:
            local = SimpleNamespace(site="disposable.localhost")
            @property
            def db(self): return outer.thread.db
            def throw(self, text): raise ValueError(text)
        self.first, self.second = DB(), DB()
        self.first.sql("CREATE TABLE `tabHERO GraphQL Mapping` (name VARCHAR(100), direction VARCHAR(50), status VARCHAR(30), schema_snapshot VARCHAR(100), mapping_json TEXT) ENGINE=InnoDB")
        self.first.commit()
        self.patches = [patch.object(controller, "frappe", Facade()), patch.object(locks, "frappe", Facade())]
        for p in self.patches: p.start()

    def tearDown(self):
        for db in self.connections:
            db.rollback()
            db.conn.close()
        for p in reversed(self.patches): p.stop()
        admin = self.mysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with admin.cursor() as c: c.execute("DROP DATABASE hero_guard_read_test")
        admin.close()

    def test_controller_guard_shares_api_lock_until_commit(self):
        self.thread.db = self.first
        self.locks._lock_active_target("writeback:S", "Customer", "FRAPPE_TO_MUTATION:customer_name")
        entered, done, results = threading.Event(), threading.Event(), []
        def competing():
            self.thread.db = self.second
            entered.set()
            try:
                self.controller._check_active_writeback_conflicts("S", {"doctype": "Customer", "field": "customer_name"}, "D2")
                results.append("clear")
            except Exception as error:
                results.append(type(error).__name__ + ": " + str(error))
            finally:
                self.second.rollback()
                done.set()
        thread = threading.Thread(target=competing)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(done.wait(0.2))
        self.first.commit()
        self.assertTrue(done.wait(8))
        thread.join()
        self.assertEqual(results, ["clear"])
        self.assertEqual(self.first.sql("SELECT COUNT(*) FROM `tabHERO GraphQL Mapping`")[0][0], 0)
