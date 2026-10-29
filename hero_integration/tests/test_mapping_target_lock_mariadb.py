"""Opt-in isolated MariaDB test; the supplied socket must be disposable."""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOCKET = os.environ.get("HERO_LOCK_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires explicitly supplied disposable MariaDB socket")
class TestMappingTargetLockMariaDB(unittest.TestCase):
    def setUp(self):
        import pymysql
        from frappe.database.database import CallbackManager
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as module

        self.pymysql, self.CallbackManager, self.module = pymysql, CallbackManager, module
        self.conn = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute("CREATE DATABASE hero_lock_test")
        self.conn.close()
        self.connections = []
        self.local = threading.local()

        class Facade:
            local = SimpleNamespace(site="disposable.localhost")
            @property
            def db(_self):
                return self.local.db
            def only_for(_self, role):
                pass
            def get_doc(_self, *_args, **_kwargs):
                return SimpleNamespace(analysis_status="Validated", snapshot_json="{}", schema_fingerprint="fp")
            def throw(_self, text):
                raise ValueError(text)

        self.patches = [patch.object(module, "frappe", Facade()),
                        patch.object(module, "analyze_snapshot", return_value=SimpleNamespace(fingerprint="fp")),
                        patch.object(module, "validate_spec")]
        for item in self.patches:
            item.start()
        self.admin = self.connect()
        self.admin.sql("CREATE TABLE `tabHERO Frappe Mapping` (name VARCHAR(100) PRIMARY KEY, operation VARCHAR(100), target_doctype VARCHAR(100), target_field VARCHAR(100), status VARCHAR(30), personal_graphql_mapping VARCHAR(140)) ENGINE=InnoDB")
        self.admin.commit()

    def tearDown(self):
        for connection in self.connections:
            connection.conn.close()
        for item in reversed(self.patches):
            item.stop()
        conn = self.pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE hero_lock_test")
        conn.close()

    def connect(self):
        conn = self.pymysql.connect(unix_socket=SOCKET, user="root", database="hero_lock_test", autocommit=False)
        callback = self.CallbackManager
        class DB:
            def __init__(_self):
                _self.conn = conn
                _self.after_commit, _self.after_rollback = callback(), callback()
            def sql(_self, sql, args=None):
                with conn.cursor() as cur:
                    cur.execute(sql, args)
                    return cur.fetchall()
            def commit(_self):
                conn.commit()
                _self.after_rollback.reset()
                _self.after_commit.run()
            def rollback(_self):
                conn.rollback()
                _self.after_commit.reset()
                _self.after_rollback.run()
        db = DB()
        self.connections.append(db)
        return db

    def doc(self, name):
        return SimpleNamespace(name=name, schema_snapshot="snapshot", status="Active", purpose="Field",
            operation="HeroCustomers", target_doctype="Customer", target_field="custom_hero_x",
            source_path="x", source_type="string", transformation="Identity", relation="", priority=5,
            definition_id=name, is_new=lambda: True)

    def test_controller_serializes_target_and_reads_current_state(self):
        self._serializes_target(False)

    def test_stale_repeatable_read_snapshot_fails_closed(self):
        self._serializes_target(True)

    def _serializes_target(self, stale_snapshot):
        first, second = self.connect(), self.connect()
        self.local.db = first
        first.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        second.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if stale_snapshot:
            self.assertEqual(second.sql("SELECT COUNT(*) FROM `tabHERO Frappe Mapping`")[0][0], 0)
        self.module.HEROFrappeMapping.validate(self.doc("first"))
        first.sql("INSERT INTO `tabHERO Frappe Mapping` (name,operation,target_doctype,target_field,status) VALUES (%s,%s,%s,%s,%s)",
                  ("first", "HeroCustomers", "Customer", "custom_hero_x", "Active"))
        entered, done, results = threading.Event(), threading.Event(), []
        def competing():
            self.local.db = second
            entered.set()
            try:
                self.module.HEROFrappeMapping.validate(self.doc("second"))
                results.append("accepted")
            except Exception as error:
                results.append((type(error).__name__, str(error)))
            finally:
                second.rollback()
                done.set()
        thread = threading.Thread(target=competing)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(done.wait(0.2))
        first.commit()
        self.assertTrue(done.wait(8))
        thread.join()
        self.assertEqual(len(results), 1)
        if stale_snapshot:
            self.assertTrue(results[0] == ("ValueError", "An active mapping already owns this target field.")
                            or (results[0][0] == "OperationalError" and "1020" in results[0][1]), results)
        else:
            self.assertEqual(results[0], ("ValueError", "An active mapping already owns this target field."))
        self.assertEqual(first.sql("SELECT COUNT(*) FROM `tabHERO Frappe Mapping` WHERE status = %s", ("Active",))[0][0], 1)

    def test_reentrancy_savepoint_and_rollback_release(self):
        first, second = self.connect(), self.connect()
        self.local.db = first
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_x")
        name = next(iter(first._hero_mapping_target_locks))
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_x")
        first.sql("CREATE TABLE implicit_commit_probe (value INT)")
        self.assertEqual(second.sql("SELECT GET_LOCK(%s, 0)", (name,))[0][0], 0)
        first.sql("SAVEPOINT test_point")
        first.sql("ROLLBACK TO SAVEPOINT test_point")
        self.assertEqual(second.sql("SELECT GET_LOCK(%s, 0)", (name,))[0][0], 0)
        first.rollback()
        self.assertEqual(second.sql("SELECT GET_LOCK(%s, 0)", (name,))[0][0], 1)
        self.assertEqual(second.sql("SELECT RELEASE_LOCK(%s)", (name,))[0][0], 1)

    def test_timeout_fails_closed_and_different_targets_are_independent(self):
        first, second = self.connect(), self.connect()
        self.local.db = first
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_x")
        self.local.db = second
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_other")
        with self.assertRaisesRegex(ValueError, "busy"):
            self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_x")
        second.rollback()
        self.local.db = first
        first.rollback()

    def test_opposite_lock_order_fails_bounded_without_leaking_locks(self):
        first, second = self.connect(), self.connect()
        self.local.db = first
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_a")
        self.local.db = second
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_b")
        start, done, results = threading.Barrier(2), [], []
        def worker(db, target):
            self.local.db = db
            try:
                start.wait(timeout=2)
                self.module._lock_active_target("HeroCustomers", "Customer", target)
                results.append("acquired")
            except ValueError:
                results.append("timeout")
            except self.pymysql.err.OperationalError as error:
                results.append("deadlock" if error.args[0] == 1213 else f"unexpected:{error}")
            finally:
                db.rollback()
                done.append(True)
        threads = [threading.Thread(target=worker, args=(first, "custom_hero_b")),
                   threading.Thread(target=worker, args=(second, "custom_hero_a"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(9)
        self.assertEqual(len(done), 2)
        self.assertTrue(any(result in {"timeout", "deadlock"} for result in results), results)
        self.assertFalse(any(result.startswith("unexpected:") for result in results), results)
        self.local.db = self.admin
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_a")
        self.module._lock_active_target("HeroCustomers", "Customer", "custom_hero_b")
        self.admin.rollback()
