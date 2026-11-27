"""Controlled transactions exercising the same planner and writer used by Desk."""
from collections import defaultdict
from datetime import datetime
from hashlib import md5
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from frappe import _dict
from hero_integration.services import selective_test_reset as reset


class Native(_dict):
    meta = SimpleNamespace(get_table_fields=lambda: [])

    def as_dict(self):
        return dict(self)

    def validate_protected_file(self):
        return None

    def has_permission(self, permission):
        return permission == "delete"


class ResetFixture:
    def __init__(self):
        self.local = SimpleNamespace(site="test.local")
        self.rows = defaultdict(dict)
        self.db = self
        self.after_commit = SimpleNamespace(add=Mock())
        self.edges = defaultdict(set)
        self.removed = []
        self.time = datetime(2026, 1, 1)
        self.rows["Custom Field"]["existing"] = _dict(name="existing", dt="Task", fieldname="independent")
        self.rows["HERO GraphQL Mapping"]["production"] = _dict(name="production", mapping_revision=8)
        self.rows["HERO Integration Settings"]["site"] = _dict(name="site", polling_mode="OFF")

    def get_all(self, doctype, *, fields, filters, **kwargs):
        answer = []
        for doc in self.rows[doctype].values():
            def matches(field, predicate):
                if isinstance(predicate, list):
                    operator, value = predicate
                    if operator == "in":
                        return doc.get(field) in value
                    if operator == "is" and value == "set":
                        return bool(doc.get(field))
                    raise AssertionError(predicate)
                return doc.get(field) == predicate
            if all(matches(key, value) for key, value in filters.items()):
                answer.append(_dict({key: doc.get(key) for key in fields}))
        return answer

    def exists(self, doctype, key):
        return key in self.rows[doctype]

    def get_doc(self, doctype, name, **kwargs):
        return self.rows[doctype][name]

    def get_value(self, doctype, name, field, **kwargs):
        return self.rows[doctype].get(name, {}).get(field)

    def get_table_columns(self, doctype):
        return next(iter(self.rows[doctype].values())).keys()

    def table_exists(self, doctype):
        return doctype in self.rows

    def delete(self, doctype, filters):
        for name in filters["name"][1]:
            self.delete_doc(doctype, name)

    def delete_doc(self, doctype, name, **kwargs):
        self.removed.append((doctype, name))
        del self.rows[doctype][name]
        for refs in self.edges.values():
            refs.discard((doctype, name))

    def set_value(self, doctype, name, values, **kwargs):
        self.rows[doctype][name].update(values)

    def run(self, name, testing=1):
        self.rows[reset.RUN][name] = _dict(name=name, testing_enabled=testing, source_instance="default",
                                         creation=self.time, mode="Execute")

    def document(self, doctype, name):
        doc = Native(doctype=doctype, name=name, creation=self.time, docstatus=0, subject="original")
        self.rows[doctype][name] = doc
        return doc

    def receipt(self, run, doc):
        key = f"{run}:{doc.doctype}:{doc.name}"
        self.rows[reset.RECORD][key] = _dict(name=key, run=run, target_doctype=doc.doctype,
            target_name=doc.name, source_type=doc.doctype, source_id=doc.name, result="CREATED",
            technical_action=reset.PROOF, native_state_json=json.dumps({
                "creation": str(doc.creation), "digest": reset._digest(reset._row_state(doc, self)),
            }), creation=self.time)

    def binding(self, run, target):
        obj = self.document(reset.OBJECT, "binding-" + target.name)
        obj.update(site_scope=self.local.site, source_instance="default", object_type=target.doctype,
                   external_object_id=target.name, target_name=target.name, target_doctype=target.doctype)
        self.receipt(run, obj)
        field = self.document(reset.FIELD, "field-" + target.name)
        field.update(object_binding=obj.name, target_doctype=target.doctype, target_name=target.name)
        return obj, field


class TestSelectiveReset(unittest.TestCase):
    def setUp(self):
        self.site = ResetFixture()
        self.enterContext(patch.object(reset, "_references", side_effect=lambda f, d: f.edges[d.doctype, d.name]))
        self.enterContext(patch.object(reset, "_native_blockers", return_value=()))
        self.enterContext(patch("hero_integration.services.guards.assert_deletion_allowed"))

    def plan(self):
        return reset.plan(self.site)

    def test_two_runs_cleanup_then_idempotent_new_run(self):
        for number in range(2):
            run = f"test-{number}"
            self.site.run(run)
            task = self.site.document("Task", f"Task-{number}")
            self.site.receipt(run, task)
            self.site.binding(run, task)
            name = f"budget-{number}"
            self.site.rows["HERO Execution Budget"][name] = _dict(name=name, run_id=run, scope="migration")
            self.site.rows["HERO Budget Outcome"][name] = _dict(name=name, budget=name)
        self.site.edges["Task", "Task-0"].add(("Task", "Task-1"))
        before, counts = self.plan()
        self.assertEqual(counts["deletable_records"], {"Task": 2})
        self.assertEqual(counts["removable_artifacts"][reset.FIELD], 2)
        self.assertEqual(counts["removable_artifacts"]["HERO Execution Budget"], 2)
        reset.apply(self.site, before)
        self.assertLess(self.site.removed.index(("Task", "Task-1")), self.site.removed.index(("Task", "Task-0")))
        empty, _ = self.plan()
        self.assertFalse(empty["artifacts"])
        self.assertFalse(empty["native"])
        self.site.run("next")
        new = self.site.document("Task", "Task-0")
        self.site.receipt("next", new)
        self.assertEqual(self.plan()[1]["deletable_records"], {"Task": 1})

    def test_regular_data_config_schema_and_new_cockpit_child_are_preserved(self):
        self.site.run("regular", testing=0)
        self.site.run("test")
        regular = self.site.document("Task", "regular")
        self.site.receipt("regular", regular)
        parent = self.site.document("Task", "parent")
        self.site.receipt("test", parent)
        child = self.site.document("Task", "later-cockpit-child")
        self.site.edges[parent.doctype, parent.name].add((child.doctype, child.name))
        manifest, report = self.plan()
        self.assertEqual(report["unresolved_dependencies"], 1)
        reset.apply(self.site, manifest)
        self.assertEqual(set(self.site.rows["Task"]), {"parent", "later-cockpit-child", "regular"})
        self.assertIn("production", self.site.rows["HERO GraphQL Mapping"])
        self.assertIn("site", self.site.rows["HERO Integration Settings"])
        self.assertIn("existing", self.site.rows["Custom Field"])

    def test_existing_native_target_loses_only_its_test_binding(self):
        self.site.run("test")
        target = self.site.document("Task", "existing")
        obj, field = self.site.binding("test", target)
        manifest, _ = self.plan()
        reset.apply(self.site, manifest)
        self.assertIn(target.name, self.site.rows["Task"])
        self.assertNotIn(obj.name, self.site.rows[reset.OBJECT])
        self.assertNotIn(field.name, self.site.rows[reset.FIELD])

    def test_independent_update_and_unverifiable_historical_origin_are_protected(self):
        self.site.run("test")
        changed = self.site.document("Task", "changed")
        self.site.receipt("test", changed)
        changed.subject = "independent alteration"
        historical = self.site.document("Task", "historical")
        self.site.receipt("test", historical)
        r = self.site.rows[reset.RECORD]["test:Task:historical"]
        r.technical_action, r.native_state_json = "hero_to_frappe", None
        manifest, report = self.plan()
        self.assertFalse(manifest["native"])
        self.assertIn("kein revisionssicherer nativer Erstellungsbeleg", report["protected"]["Task"])

    def test_retained_legacy_link_keeps_its_identity_checkpoint_and_origin_run(self):
        self.site.run("test")
        target = self.site.document("File", "legacy")
        link = self.site.document(reset.LINK, "historical-binding")
        link.update(source_system="HERO", source_instance="default", source_type="File",
                    source_id="41", target_doctype=target.doctype, target_name=target.name)
        for source_id in ("41", "42"):
            self.site.rows[reset.RECORD][source_id] = _dict(name=source_id, run="test",
                technical_action="identity_checkpoint", source_type="File", source_id=source_id)
        for _ in range(2):
            manifest, report = self.plan()
            self.assertIn("Testidentität belegt, native Herkunft offen", report["protected"]["File"])
            reset.apply(self.site, manifest)
            self.assertEqual(set(self.site.rows[reset.RECORD]), {"41"})
            self.assertIn("test", self.site.rows[reset.RUN])
            self.assertIn(link.name, self.site.rows[reset.LINK])
            self.assertIn(target.name, self.site.rows["File"])

    def test_file_with_shared_content_is_preserved(self):
        self.site.run("test")
        f = self.site.document("File", "shared")
        self.site.receipt("test", f)
        with patch.object(reset, "_attachment", return_value=None):
            manifest, report = self.plan()
        self.assertFalse(manifest["native"])
        self.assertEqual((report["file_count"], report["file_bytes"]), (0, 0))
        self.assertIn("shared", self.site.rows["File"])

    def test_exclusive_file_cleanup_is_deferred_until_commit(self):
        self.site.run("test")
        f = self.site.document("File", "exclusive")
        self.site.receipt("test", f)
        with patch.object(reset, "_attachment", return_value=("/private/files/owned", 128, "md5")), \
                patch("hero_integration.services.integration_events.record_output") as receipt:
            manifest, report = self.plan()
            self.assertEqual((report["file_count"], report["file_bytes"]), (1, 128))
            reset.apply(self.site, manifest)
        receipt.assert_called_once()
        self.site.after_commit.add.assert_called_once()
        self.assertEqual(self.site.after_commit.add.call_args.args[0].args[1:], ("/private/files/owned", 128, "md5"))

    def test_retry_rechecks_file_references_and_closes_only_confirmed_cleanup_receipts(self):
        url = "/private/files/hero-1-0123456789abcdef0123456789abcdef-report.pdf"
        content = b"fixture"
        with TemporaryDirectory() as directory:
            blob = Path(directory) / url.rsplit("/", 1)[-1]
            blob.write_bytes(content)
            self.site.get_site_path = lambda *parts: str(blob)
            for name in ("cleanup-1", "cleanup-2"):
                self.site.rows[reset.RECORD][name] = _dict(name=name, technical_action=reset.BLOB_CLEANUP,
                    native_state_json=json.dumps({"url": url, "size": len(content),
                                                  "content_hash": md5(content).hexdigest()}))
            with patch.object(reset, "_blob_unshared", return_value=True) as unshared, \
                    patch("hero_integration.services.file_transfer._discard_unreferenced",
                          side_effect=lambda _: blob.unlink()) as discard, \
                    patch("hero_integration.client.settings._configuration_connection") as connection:
                manifest, report = self.plan()
                self.assertEqual((report["file_count"], report["file_bytes"]), (1, len(content)))
                reset.apply(self.site, manifest)
                self.site.after_commit.add.assert_called_once()
                callback = self.site.after_commit.add.call_args.args[0]
                unshared.return_value = False  # A new reference appeared before the commit callback.
                callback()
                self.assertEqual(blob.read_bytes(), content)
                discard.assert_not_called()
                connection.assert_not_called()
                unshared.return_value = True
                callback()
                self.assertFalse(blob.exists())
                db = connection.return_value
                self.assertEqual(db.sql.call_args.args[1], (("cleanup-1", "cleanup-2"), reset.BLOB_CLEANUP))
                db.commit.assert_called_once()
                db.close.assert_called_once()

    def test_mutation_idempotency_evidence_and_unmarked_manual_sync_are_retained(self):
        self.site.run("test")
        target = self.site.document("Task", "native")
        self.site.receipt("test", target)
        obj, field = self.site.binding("test", target)
        event = "test:Task:native"
        self.site.rows[reset.AUDIT]["sent"] = _dict(name="sent", origin_event=event, bound_object_key=obj.name)
        sync = "HERO Sync Run Ledger"
        self.site.rows[sync]["manual"] = _dict(name="manual", sync_run_id="manual", site_scope=self.site.local.site,
                                               automatic=0, request_fingerprint="fingerprint")
        manifest, _ = self.plan()
        reset.apply(self.site, manifest)
        self.assertIn(event, self.site.rows[reset.RECORD])
        self.assertIn("sent", self.site.rows[reset.AUDIT])
        self.assertIn(obj.name, self.site.rows[reset.OBJECT])
        self.assertIn(field.name, self.site.rows[reset.FIELD])
        self.assertIn("manual", self.site.rows[sync])
        self.assertIn("native", self.site.rows["Task"])

    def test_test_checkpoints_clear_without_touching_regular_default_values(self):
        self.site.run("root")
        self.site.run("regular", testing=0)
        defaults = self.site.rows["DefaultValue"]
        for n, key in (("full", "hero_full_migration:root"),
                       ("test-cursor", "hero_testing_migration:root:HeroDocuments"),
                       ("regular", "hero_testing_migration:regular:HeroDocuments"),
                       ("global", "hero_file_transfer:documents")):
            defaults[n] = _dict(name=n, parent="__default", defkey=key)
        manifest, counts = self.plan()
        self.assertEqual(counts["removable_artifacts"]["DefaultValue"], 2)
        reset.apply(self.site, manifest)
        self.assertEqual(set(defaults), {"global", "regular"})

    def test_test_sync_marker_removes_only_its_own_budget_and_run(self):
        sync = "HERO Sync Run Ledger"
        for name in ("test", "ordinary"):
            self.site.rows[sync][name] = _dict(name=name, sync_run_id=name, site_scope=self.site.local.site,
                                              automatic=0, request_fingerprint="receipt")
            self.site.rows["HERO Execution Budget"][name] = _dict(name=name, scope="sync", run_id=name)
        self.site.rows[reset.RECORD]["proof"] = _dict(name="proof", technical_action=reset.SYNC_PROOF, source_id="test")
        manifest, _ = self.plan()
        reset.apply(self.site, manifest)
        self.assertEqual(set(self.site.rows[sync]), {"ordinary"})
        self.assertEqual(set(self.site.rows["HERO Execution Budget"]), {"ordinary"})


class TestResetBlobReferences(unittest.TestCase):
    token = "0123456789abcdef0123456789abcdef"
    url = f"/private/files/hero-1-{token}-report.pdf"

    def site(self, doctype, fields, stored):
        db = Mock()
        db.exists.return_value = False
        db.sql.return_value = []
        db.table_exists.return_value = True
        db.get_table_columns.return_value = ["name", *fields]
        site = SimpleNamespace(db=db)
        site.get_meta = lambda dt: SimpleNamespace(fields=[
            SimpleNamespace(fieldname=field, fieldtype=fieldtype) for field, fieldtype in fields.items()
        ])

        def get_all(dt, *, filters, fields, **kwargs):
            if dt == "DocType":
                return [_dict(name=doctype, issingle=False, is_virtual=False)]
            self.assertEqual(dt, doctype)
            matches = []
            for row in stored:
                permitted = all(row.get(field) != value for field, (operator, value) in filters.items()
                                if operator == "!=")
                if permitted and any(value.strip("%").lower() in str(row.get(field, "")).lower()
                                     for field, operator, value in kwargs["or_filters"] if operator == "like"):
                    matches.append(_dict(name=row["name"]))
            return matches

        site.get_all = get_all
        return site

    def test_independent_task_description_protects_file(self):
        site = self.site("Task", {"description": "Text Editor"}, [
            {"name": "independent", "description": f'<a href="https://example.invalid{self.url}">Report</a>'},
        ])
        self.assertFalse(reset._blob_unshared(site, self.url))

    def test_private_file_and_cleanup_receipt_do_not_count_as_independent_references(self):
        own = self.site("File", {"file_url": "Data"}, [{"name": "own", "file_url": self.url}])
        self.assertTrue(reset._blob_unshared(own, self.url, file_name="own"))
        own_receipt = self.site(reset.RECORD, {"native_state_json": "Long Text"}, [
            {"name": "cleanup", "technical_action": reset.BLOB_CLEANUP, "native_state_json": self.url},
        ])
        self.assertTrue(reset._blob_unshared(own_receipt, self.url))
        only_reference = self.site(reset.RECORD, {"native_state_json": "Long Text"}, [
            {"name": "independent", "technical_action": None, "native_state_json": self.url},
        ])
        self.assertFalse(reset._blob_unshared(only_reference, self.url))
