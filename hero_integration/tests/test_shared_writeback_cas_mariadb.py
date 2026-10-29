"""Opt-in disposable MariaDB race check; never activates a mapping."""
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOCKET = os.environ.get("HERO_CAS_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires disposable MariaDB socket")
class TestSharedWritebackMariaDB(unittest.TestCase):
    def setUp(self):
        import pymysql
        from hero_integration import api
        from hero_integration.services import frappe_writeback
        self.mysql, self.api, self.writeback = pymysql, api, frappe_writeback
        admin = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with admin.cursor() as cursor:
            cursor.execute("CREATE DATABASE hero_cas_draft_test")
        admin.close()
        self.first = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_cas_draft_test", autocommit=False)
        self.second = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_cas_draft_test", autocommit=False)
        self.mapping = json.dumps({"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}})
        with self.first.cursor() as c:
            c.execute("CREATE TABLE draft (name VARCHAR(40) PRIMARY KEY, schema_snapshot VARCHAR(40), operation_kind VARCHAR(40), operation_name VARCHAR(40), direction VARCHAR(40), mapping_json TEXT, status VARCHAR(20), modified_by VARCHAR(100), mapping_revision INT, schema_fingerprint VARCHAR(80)) ENGINE=InnoDB")
            c.execute("INSERT INTO draft VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", ("D1", "S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping, "Draft", "alice", 1, "fp"))
        self.first.commit()

        outer = self
        class Facade:
            PermissionError = PermissionError
            session = SimpleNamespace(user="alice")
            def only_for(self, _role): pass
            def throw(self, text, exception=ValueError): raise exception(text)
            def get_doc(self, doctype, name, **kwargs):
                assert doctype == "HERO GraphQL Mapping" and kwargs == {"for_update": True}
                with outer.second.cursor(outer.mysql.cursors.DictCursor) as cur:
                    cur.execute("SELECT * FROM draft WHERE name=%s FOR UPDATE", (name,))
                    return SimpleNamespace(**cur.fetchone())
        self.patches = [patch.object(api, "frappe", Facade()), patch.object(api, "_lock_mapping_save_context", return_value={}),
                        patch.object(frappe_writeback, "normalise_mapping", side_effect=lambda value, schema: value)]
        for p in self.patches: p.start()

    def tearDown(self):
        self.first.rollback()
        self.second.rollback()
        self.first.close()
        self.second.close()
        for p in reversed(self.patches): p.stop()
        admin = self.mysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with admin.cursor() as c: c.execute("DROP DATABASE hero_cas_draft_test")
        admin.close()

    def test_locked_read_rejects_newer_status_revision(self):
        self._stale_read("status", "Disabled")

    def test_locked_read_rejects_newer_editor_revision(self):
        self._stale_read("modified_by", "bob")

    def _stale_read(self, field, value):
        assert field in {"status", "modified_by"}
        with self.first.cursor() as c:
            c.execute(f"UPDATE draft SET {field}=%s, mapping_revision=%s WHERE name=%s", (value, 2, "D1"))
        entered, done, result = threading.Event(), threading.Event(), []
        def save():
            entered.set()
            try:
                self.api.graphql_mapping_save("S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping,
                                              mapping_name="D1", expected_revision=1)
                result.append("accepted")
            except Exception as error:
                result.append(str(error))
            finally:
                done.set()
        thread = threading.Thread(target=save)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(done.wait(0.2))
        self.first.commit()
        self.assertTrue(done.wait(5))
        thread.join()
        self.assertEqual(result, ["GraphQL mapping revision is stale; reload the current Draft."])
        with self.second.cursor() as c:
            c.execute(f"SELECT {field}, mapping_revision FROM draft WHERE name=%s", ("D1",))
            self.assertEqual(c.fetchone(), (value, 2))

    def test_foreign_draft_noop_does_not_save_or_change_owner(self):
        with self.first.cursor() as c:
            c.execute("UPDATE draft SET modified_by=%s WHERE name=%s", ("bob", "D1"))
        self.first.commit()
        with self.assertRaisesRegex(PermissionError, "Foreign legacy mapping drafts"):
            self.api.graphql_mapping_save("S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping,
                                          mapping_name="D1", expected_revision=1)
        with self.second.cursor() as c:
            c.execute("SELECT status, modified_by, mapping_revision FROM draft WHERE name=%s", ("D1",))
            self.assertEqual(c.fetchone(), ("Draft", "bob", 1))
