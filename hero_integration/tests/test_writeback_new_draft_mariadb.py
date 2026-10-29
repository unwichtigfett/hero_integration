"""Opt-in two-connection API draft creation race, without activation."""
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOCKET = os.environ.get("HERO_CAS_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires disposable MariaDB socket")
class TestWritebackNewDraftMariaDB(unittest.TestCase):
    def setUp(self):
        import pymysql
        from frappe.database.database import CallbackManager
        from hero_integration import api
        from hero_integration.services import frappe_writeback
        from hero_integration.hero_integration.doctype.hero_frappe_mapping import hero_frappe_mapping as locks
        self.mysql, self.api = pymysql, api
        admin = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with admin.cursor() as c: c.execute("CREATE DATABASE hero_new_draft_test")
        admin.close()
        self.connections = []
        self.thread = threading.local()
        self.local = SimpleNamespace(site="disposable.localhost")
        outer = self

        class DB:
            def __init__(self):
                self.conn = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_new_draft_test", autocommit=False)
                self.after_commit, self.after_rollback = CallbackManager(), CallbackManager()
                outer.connections.append(self)
            def sql(self, query, args=None, as_dict=False):
                cursor_type = pymysql.cursors.DictCursor if as_dict else pymysql.cursors.Cursor
                with self.conn.cursor(cursor_type) as c:
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
            PermissionError = PermissionError
            local = outer.local
            session = SimpleNamespace(user="alice")
            @property
            def db(self): return outer.thread.db
            def only_for(self, _role): pass
            def throw(self, text, exception=ValueError): raise exception(text)
            def get_doc(self, doctype, name, **kwargs):
                assert kwargs == {"for_update": True}
                rows = self.db.sql("SELECT * FROM `tabHERO GraphQL Mapping` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
                return rows[0]
            def new_doc(self, doctype):
                class Doc:
                    name = "D1"
                    schema_fingerprint = "fp"
                    mapping_revision = 1
                    def save(doc):
                        assert doc.status == "Draft"
                        outer.thread.db.sql("INSERT INTO `tabHERO GraphQL Mapping` (name, schema_snapshot, direction, mapping_json, status, modified_by, mapping_revision, schema_fingerprint, operation_kind, operation_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (doc.name, doc.schema_snapshot, doc.direction, doc.mapping_json, doc.status, "alice", 1, "fp", doc.operation_kind, doc.operation_name))
                return Doc()

        self.facade, self.DB = Facade(), DB
        self.patches = [patch.object(api, "frappe", self.facade), patch.object(locks, "frappe", self.facade),
                        patch.object(api, "_validated_snapshot", return_value=(SimpleNamespace(source_instance='source', schema_fingerprint='fp'), {})),
                        patch.object(frappe_writeback, "normalise_mapping", side_effect=lambda value, schema: value)]
        for p in self.patches: p.start()
        self.first, self.second = DB(), DB()
        self.first.sql("CREATE TABLE `tabHERO GraphQL Mapping` (name VARCHAR(40) PRIMARY KEY, schema_snapshot VARCHAR(40), direction VARCHAR(40), mapping_json TEXT, status VARCHAR(20), modified_by VARCHAR(100), mapping_revision INT, schema_fingerprint VARCHAR(80), operation_kind VARCHAR(40), operation_name VARCHAR(40)) ENGINE=InnoDB")
        self.first.commit()
        self.mapping = json.dumps({"source": {"doctype": "Customer", "field": "customer_name"}, "target": {"operation": "update"}})

    def tearDown(self):
        for db in self.connections:
            db.rollback()
            db.conn.close()
        for p in reversed(self.patches): p.stop()
        admin = self.mysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with admin.cursor() as c: c.execute("DROP DATABASE hero_new_draft_test")
        admin.close()

    def test_same_source_concurrent_new_api_drafts_are_serialized(self):
        self.thread.db = self.first
        result = self.api.graphql_mapping_save("S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping)
        self.assertEqual(result["status"], "Draft")
        entered, done, results = threading.Event(), threading.Event(), []
        def competing():
            self.thread.db = self.second
            entered.set()
            try:
                self.api.graphql_mapping_save("S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping)
                results.append("accepted")
            except Exception as error:
                results.append(str(error))
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
        self.assertEqual(results, ["GraphQL mapping revision is invalid."])
        self.assertEqual(self.first.sql("SELECT COUNT(*) FROM `tabHERO GraphQL Mapping` WHERE status=%s", ("Draft",))[0][0], 1)

    def test_existing_duplicate_drafts_fail_closed_without_mutation(self):
        self.thread.db = self.first
        for name in ("D1", "D2"):
            self.first.sql("INSERT INTO `tabHERO GraphQL Mapping` (name, schema_snapshot, direction, mapping_json, status, modified_by, mapping_revision, schema_fingerprint, operation_kind, operation_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (name, "S", "FRAPPE_TO_MUTATION", self.mapping, "Draft", "alice", 1, "fp", "MUTATION", "update"))
        self.first.commit()
        with self.assertRaisesRegex(ValueError, "More than one stored writeback mapping"):
            self.api.graphql_mapping_save("S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping)
        self.assertEqual(self.first.sql("SELECT name, status FROM `tabHERO GraphQL Mapping` ORDER BY name"), (("D1", "Draft"), ("D2", "Draft")))

    def test_existing_source_metadata_prevents_duplicate_without_mutation(self):
        self.thread.db = self.first
        stored = json.loads(self.mapping)
        stored["source"]["legacy_metadata"] = {"value": False}
        original = json.dumps(stored)
        self.first.sql("INSERT INTO `tabHERO GraphQL Mapping` (name, schema_snapshot, direction, mapping_json, status, modified_by, mapping_revision, schema_fingerprint, operation_kind, operation_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       ("D1", "S", "FRAPPE_TO_MUTATION", original, "Draft", "alice", 1, "fp", "MUTATION", "update"))
        self.first.commit()
        with self.assertRaisesRegex(ValueError, "source contains parameters"):
            self.api.graphql_mapping_save("S", "MUTATION", "update", "FRAPPE_TO_MUTATION", self.mapping)
        self.assertEqual(self.first.sql("SELECT name, mapping_json, status FROM `tabHERO GraphQL Mapping`"), (("D1", original, "Draft"),))
