"""Opt-in isolated MariaDB RR/DDL regression; no Frappe site or mapping writes."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOCKET = os.environ.get("HERO_SCHEMA_CURRENT_TEST_SOCKET")


@unittest.skipUnless(SOCKET, "requires explicitly disposable MariaDB socket")
class TestAdoptionSchemaCurrentMariaDB(unittest.TestCase):
    def setUp(self):
        import pymysql
        from hero_integration.services import schema_provisioning
        self.mysql, self.sp = pymysql, schema_provisioning
        self.admin = pymysql.connect(unix_socket=SOCKET, user="root", autocommit=True)
        with self.admin.cursor() as c: c.execute("CREATE DATABASE hero_schema_current_test")
        self.first = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_schema_current_test", autocommit=False)
        self.second = pymysql.connect(unix_socket=SOCKET, user="root", database="hero_schema_current_test", autocommit=False)
        with self.first.cursor() as c:
            c.execute("CREATE TABLE `tabProperty Setter` (doc_type VARCHAR(100), doctype_or_field VARCHAR(30), field_name VARCHAR(100), property VARCHAR(100)) ENGINE=InnoDB")
            c.execute("CREATE TABLE `tabDocField` (parent VARCHAR(100), parenttype VARCHAR(100), fieldname VARCHAR(100), fieldtype VARCHAR(30), options TEXT, label TEXT, reqd INT, `unique` INT, read_only INT) ENGINE=InnoDB")
            c.execute("CREATE TABLE `tabCustom Field` (dt VARCHAR(100), fieldname VARCHAR(100), fieldtype VARCHAR(30), options TEXT, label TEXT, reqd INT, `unique` INT, read_only INT, UNIQUE KEY(dt,fieldname)) ENGINE=InnoDB")
            c.execute("INSERT INTO `tabCustom Field` VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", ("Customer", "custom_hero_x", "Data", "", "Original", 0, 0, 0))
        self.first.commit()
        self.field = {"id": "field:x", "doctype": "Customer", "fieldname": "custom_hero_x", "fieldtype": "Data", "options": "", "label": "Original", "insert_after": "customer_name"}
        outer = self
        class Row(dict):
            __getattr__ = dict.__getitem__
        class DB:
            def sql(self, query, args, as_dict=False):
                assert as_dict and "FOR UPDATE" in query
                with outer.first.cursor(pymysql.cursors.DictCursor) as c:
                    c.execute(query, args)
                    return [Row(row) for row in c.fetchall()]
        self.p = patch.object(schema_provisioning, "frappe", SimpleNamespace(db=DB()))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.first.rollback()
        self.second.rollback()
        self.first.close()
        self.second.close()
        with self.admin.cursor() as c: c.execute("DROP DATABASE hero_schema_current_test")
        self.admin.close()

    def test_concurrent_change_after_ddl_is_seen_despite_old_snapshot(self):
        with self.first.cursor() as c:
            c.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            c.execute("CREATE TABLE synthetic_post_ddl (value INT)")
            c.execute("SELECT label FROM `tabCustom Field` WHERE dt=%s AND fieldname=%s", ("Customer", "custom_hero_x"))
            self.assertEqual(c.fetchone()[0], "Original")
        with self.second.cursor() as c:
            c.execute("UPDATE `tabCustom Field` SET label=%s WHERE dt=%s AND fieldname=%s", ("Changed", "Customer", "custom_hero_x"))
        self.second.commit()
        with self.first.cursor() as c:
            c.execute("SELECT label FROM `tabCustom Field` WHERE dt=%s AND fieldname=%s", ("Customer", "custom_hero_x"))
            self.assertEqual(c.fetchone()[0], "Original")
        definitions = {"custom_doctypes": [], "custom_fields": [self.field], "relations": []}
        with self.assertRaisesRegex(self.sp.SchemaProvisioningError, "reconciliation"):
            self.sp._recheck_required_schema_current(definitions, {"field:x"})

    def test_unchanged_schema_passes_current_read(self):
        definitions = {"custom_doctypes": [], "custom_fields": [self.field], "relations": []}
        self.sp._recheck_required_schema_current(definitions, {"field:x"})

    def test_current_relation_read_detects_changed_target_after_snapshot(self):
        with self.first.cursor() as c:
            c.execute("INSERT INTO `tabDocField` VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      ("HERO Detail", "DocType", "custom_hero_link", "Link", "Customer", "Link", 0, 0, 0))
        self.first.commit()
        with self.first.cursor() as c:
            c.execute("SELECT options FROM `tabDocField` WHERE parent=%s", ("HERO Detail",))
            self.assertEqual(c.fetchone()[0], "Customer")
        with self.second.cursor() as c:
            c.execute("UPDATE `tabDocField` SET options=%s WHERE parent=%s", ("Other", "HERO Detail"))
        self.second.commit()
        with self.first.cursor() as c:
            c.execute("SELECT options FROM `tabDocField` WHERE parent=%s", ("HERO Detail",))
            self.assertEqual(c.fetchone()[0], "Customer")
        relation = {"id": "relation:x", "doctype": "HERO Detail", "fieldname": "custom_hero_link",
                    "field_id": "field:link", "relation_type": "Link", "target_doctype": "Customer"}
        definitions = {"custom_doctypes": [], "custom_fields": [], "relations": [relation]}
        with self.assertRaisesRegex(self.sp.SchemaProvisioningError, "reconciliation"):
            self.sp._recheck_required_schema_current(definitions, {"relation:x"})

    def test_only_relevant_field_setter_is_rejected(self):
        definitions = {"custom_doctypes": [], "custom_fields": [self.field], "relations": []}
        with self.first.cursor() as c:
            c.execute("INSERT INTO `tabProperty Setter` VALUES (%s,%s,%s,%s)", ("Customer", "DocField", "custom_hero_x", "description"))
        self.first.commit()
        self.sp._recheck_required_schema_current(definitions, {"field:x"})
        self.first.commit()
        with self.first.cursor() as c:
            c.execute("INSERT INTO `tabProperty Setter` VALUES (%s,%s,%s,%s)", ("Customer", "DocField", "custom_hero_x", "label"))
        self.first.commit()
        with self.assertRaisesRegex(self.sp.SchemaProvisioningError, "reconciliation"):
            self.sp._recheck_required_schema_current(definitions, {"field:x"})
