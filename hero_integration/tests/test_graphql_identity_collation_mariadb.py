"""Opt-in disposable MariaDB collation regression; no mapping activation."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOCKET = os.environ.get("HERO_IDENTITY_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires disposable MariaDB socket")
class TestGraphQLIdentityCollationMariaDB(unittest.TestCase):
    def setUp(self):
        import pymysql
        from hero_integration.patches.v0_1 import normalize_empty_graphql_definition_ids as pre
        self.mysql, self.pre = pymysql, pre
        self.admin = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with self.admin.cursor() as cur: cur.execute("CREATE DATABASE hero_identity_collation_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        self.conn = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_identity_collation_test", autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("CREATE TABLE `tabHERO GraphQL Mapping` (name VARCHAR(30) PRIMARY KEY, definition_id VARCHAR(140), status VARCHAR(20), modified VARCHAR(30), mapping_json TEXT) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
            for name, value in (("empty-a", ""), ("empty-b", ""), ("null", None), ("one-space", " "), ("two-spaces", "  "), ("nonempty", "persisted")):
                cur.execute("INSERT INTO `tabHERO GraphQL Mapping` VALUES (%s,%s,%s,%s,%s)", (name, value, "Draft", "unchanged", "{}"))
        self.conn.commit()
        outer = self
        class DB:
            def table_exists(self, name):
                with outer.conn.cursor() as c:
                    c.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", ("tab" + name,))
                    return bool(c.fetchone()[0])
            def has_column(self, name, column):
                with outer.conn.cursor() as c:
                    c.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s", ("tab" + name, column))
                    return bool(c.fetchone()[0])
            def sql(self, query):
                with outer.conn.cursor() as c:
                    c.execute(query)
                    return c.fetchall()
        self.p = patch.object(pre, "frappe", SimpleNamespace(db=DB()))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.conn.rollback()
        self.conn.close()
        with self.admin.cursor() as cur: cur.execute("DROP DATABASE hero_identity_collation_test")
        self.admin.close()

    def test_only_true_empty_bytes_are_normalized(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT name FROM `tabHERO GraphQL Mapping` WHERE definition_id = %s ORDER BY name", ("",))
            self.assertEqual({row[0] for row in cur.fetchall()}, {"empty-a", "empty-b", "one-space", "two-spaces"})
        self.pre.execute()
        with self.conn.cursor() as cur:
            cur.execute("SELECT name, definition_id, status, modified, mapping_json FROM `tabHERO GraphQL Mapping` ORDER BY name")
            rows = {name: (identity, status, modified, value) for name, identity, status, modified, value in cur.fetchall()}
        self.assertEqual(rows, {"empty-a": (None, "Draft", "unchanged", "{}"), "empty-b": (None, "Draft", "unchanged", "{}"),
                                "null": (None, "Draft", "unchanged", "{}"), "one-space": (" ", "Draft", "unchanged", "{}"),
                                "two-spaces": ("  ", "Draft", "unchanged", "{}"), "nonempty": ("persisted", "Draft", "unchanged", "{}")})
        with self.conn.cursor() as cur:
            with self.assertRaises(self.mysql.err.IntegrityError):
                cur.execute("ALTER TABLE `tabHERO GraphQL Mapping` ADD UNIQUE INDEX `definition_id` (`definition_id`)")

    def test_empty_old_registry_table_does_not_block(self):
        with self.conn.cursor() as cur:
            cur.execute("CREATE TABLE `tabHERO Mapping Registry Version` (name VARCHAR(140) PRIMARY KEY) ENGINE=InnoDB")
        self.pre.execute()
        with self.conn.cursor() as cur:
            cur.execute("SELECT definition_id FROM `tabHERO GraphQL Mapping` WHERE name=%s", ("empty-a",))
            self.assertIsNone(cur.fetchone()[0])

    def test_populated_unreadable_old_registry_blocks_before_normalization(self):
        with self.conn.cursor() as cur:
            cur.execute("CREATE TABLE `tabHERO Mapping Registry Version` (name VARCHAR(140) PRIMARY KEY) ENGINE=InnoDB")
            cur.execute("INSERT INTO `tabHERO Mapping Registry Version` VALUES (%s)", ("legacy",))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "reconciliation is required"):
            self.pre.execute()
        with self.conn.cursor() as cur:
            cur.execute("SELECT definition_id FROM `tabHERO GraphQL Mapping` WHERE name=%s", ("empty-a",))
            self.assertEqual(cur.fetchone()[0], "")
