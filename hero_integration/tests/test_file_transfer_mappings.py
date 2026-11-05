"""Configurable file intake, metadata-only updates and offset checkpoints."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import direct_runtime as rt
from hero_integration.services import file_transfer as ft
from hero_integration.services.mapping_runtime import plan_from_definitions
from hero_integration.tests.test_file_transfer import URL, _FileFixture, _NoDownload


class _Client:
    def __init__(self, rows):
        self.rows, self.calls, self.details = rows, [], []

    def query(self, document, variables):
        if "uuids" in variables:
            self.details.extend(variables["uuids"])
            return {"file_uploads": [{"uuid": uuid, "file_upload_matches": [{"project_match_id": 42}]}
                                     for uuid in variables["uuids"]]}
        self.calls.append((document, dict(variables)))
        offset = variables.get("offset", variables.get("start"))
        size = variables.get("first", variables.get("limit"))
        return {"FileUpload_FileUploads": {"edges": [{"node": row} for row in self.rows[offset:offset + size]]}}


class _Download:
    def __init__(self):
        self.calls = 0

    def fetch(self, url, target, size):
        self.calls += 1
        with open(target, "wb") as stream:
            stream.write(b"pdf")
        return 3, "md5"


def _row(identity=7, **values):
    return {"id": identity, "uuid": f"u{identity}", "filename": "Angebot.pdf", "category": "offer",
            "downloadUrl": URL, "modified": "1", "created": "2026-01-01T12:00:00", "size": 3, **values}


def _own_read():
    builtin = rt.builtin_read("HeroDocuments")
    extra = replace(builtin.plan.definitions[0], definition_id="section", source_path="section",
                    source_type="string|null", target_doctype="File", target_field="custom_hero_section",
                    purpose="Field", transformation="Trim")
    title = replace(builtin.plan.definitions[1], source_path="title")
    plan = plan_from_definitions("HeroDocuments", [builtin.plan.definitions[0], title, extra],
                                 target_operation="HeroDocuments", adapter_options={"version": 2})
    return replace(builtin, plan=plan, origin="approved-service", mapping="own-mapping",
                   document="query OwnFiles { selected fields }", variables={"limit": "$page_size", "start": "$offset"})


class TestFileMappingImport(_FileFixture):
    def run_slice(self, rows, *, read=None, saved=None, phase="documents", downloader=None, **kwargs):
        self.client = _Client(rows)
        read = read or rt.builtin_read(ft.OPERATION[phase])
        self.saved_states = []
        with patch.object(ft.direct_runtime, "resolve_read", return_value=read) as resolve, \
                patch.object(ft, "_state", return_value=copy.deepcopy(saved or {})), \
                patch.object(ft, "_save_state", side_effect=lambda phase, state: self.saved_states.append(copy.deepcopy(state))), \
                patch.object(ft, "allow_unlimited_attachments"), \
                patch.object(ft, "_folder", side_effect=lambda *parts: "Home/" + "/".join(parts)):
            state = ft.transfer(phase, source_instance="source-a", client=self.client,
                                downloader=downloader or _NoDownload(), min_free=0, **kwargs)
        resolve.assert_called_once_with(ft.OPERATION[phase], "source-a")
        return state

    def imported(self):
        link = next(row for row in self.db.committed["HERO Integration Link"].values() if row["source_id"] == "7")
        return self.db.committed["File"][link["target_name"]], link

    def test_builtin_filename_and_category_split_in_both_phases(self):
        rows = [_row(), _row(8, filename="Bild.jpg", category="images")]
        for phase, uuid, filename in (("documents", "u7", "Angebot.pdf"), ("images", "u8", "Bild.jpg")):
            with self.subTest(phase=phase):
                state = self.run_slice(rows, phase=phase, downloader=_Download())
                self.assertEqual(state["counts"]["added"], 1)
                self.assertEqual(self.client.details, [uuid])
                files = list(self.db.committed["File"].values())
                self.assertIn(filename, [file.get("file_name") for file in files])
                self.assertEqual(state["offset"], 2)
                self.assertTrue(state["done"])

    def test_own_fields_on_insert_values_only_change_and_unchanged_rerun(self):
        read, downloader = _own_read(), _Download()
        row = _row(title="Mapped.pdf", section=" Roof ")
        self.run_slice([row], read=read, downloader=downloader)
        file, link = self.imported()
        self.assertEqual((file["file_name"], file["custom_hero_section"]), ("Mapped.pdf", "Roof"))
        self.assertEqual((link["mapping_version"], link["source_id"]), (read.mapping_version, "7"))
        self.assertEqual(len(json.loads(link["target_field_hashes"])["fields"]), 64)
        self.assertEqual(self.client.calls[0], (read.document, {"limit": read.page_size, "start": 0}))
        original_url, original_content_hash = file["file_url"], link["source_payload_hash"]
        row.update(title="Renamed.pdf", section="Wall")
        state = self.run_slice([row], read=read)
        file, link = self.imported()
        self.assertEqual(state["counts"]["updated"], 1)
        self.assertEqual((file["file_name"], file["custom_hero_section"]), ("Renamed.pdf", "Wall"))
        self.assertEqual((file["file_url"], link["source_payload_hash"]), (original_url, original_content_hash))
        state = self.run_slice([row], read=read)
        self.assertEqual(state["counts"]["unchanged"], 1)
        self.assertEqual(downloader.calls, 1)

    def test_content_update_writes_mapped_values_and_keeps_attachment(self):
        read, downloader = _own_read(), _Download()
        row = _row(title="First.pdf", section="Roof")
        self.run_slice([row], read=read, downloader=downloader)
        row.update(modified="2", title="Second.pdf", section="Wall")
        state = self.run_slice([row], read=read, downloader=downloader)
        file, _ = self.imported()
        self.assertEqual((file["file_name"], file["custom_hero_section"], file["attached_to_name"]),
                         ("Second.pdf", "Wall", "PROJECT-A"))
        self.assertEqual((state["counts"]["updated"], downloader.calls), (1, 2))

    def test_mapping_change_applies_to_existing_content_without_download(self):
        row = _row(title="Mapped.pdf", section="Roof")
        self.run_slice([row], downloader=_Download())
        state = self.run_slice([row], read=_own_read())
        file, link = self.imported()
        self.assertEqual(state["counts"]["updated"], 1)
        self.assertEqual(file["custom_hero_section"], "Roof")
        self.assertEqual(link["mapping_version"], _own_read().mapping_version)

    def test_empty_name_falls_back_and_null_custom_value_clears_field(self):
        row = _row(title="", section="Roof")
        self.run_slice([row], read=_own_read(), downloader=_Download())
        row["section"] = None
        self.run_slice([row], read=_own_read())
        file, _ = self.imported()
        self.assertEqual(file["file_name"], ft.safe_name(""))
        self.assertIsNone(file["custom_hero_section"])

    def test_metadata_failure_rolls_back_both_file_and_digest(self):
        row, read = _row(title="First.pdf", section="Roof"), _own_read()
        self.run_slice([row], read=read, downloader=_Download())
        original = copy.deepcopy(self.db.committed)
        self.db.fail_on = "HERO Integration Link"
        row.update(title="Second.pdf", section="Wall")
        state = self.run_slice([row], read=read)
        self.assertEqual(state["counts"]["failed"], 1)
        self.assertEqual(self.db.data, original)
        self.assertEqual(self.db.committed, original)

    def test_preview_reports_metadata_update_without_writes_or_download(self):
        row, read = _row(title="First.pdf", section="Roof"), _own_read()
        self.run_slice([row], read=read, downloader=_Download())
        original = copy.deepcopy(self.db.committed)
        row["section"] = "Wall"
        with patch.object(ft.os, "makedirs") as mkdir, patch.object(ft.frappe.db, "set_value") as write:
            state = self.run_slice([row], read=read, dry_run=True)
        self.assertEqual(state["counts"]["would_update"], 1)
        write.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(self.saved_states, [])
        self.assertEqual(self.db.committed, original)

    def test_preview_new_file_and_stop_do_not_write(self):
        with patch.object(ft.os, "makedirs") as mkdir:
            state = self.run_slice([_row()], dry_run=True)
            self.assertEqual(state["counts"]["would_add"], 1)
            state = self.run_slice([_row()], dry_run=True, should_stop=lambda: True)
        self.assertTrue(state["stopped"])
        self.assertEqual(self.saved_states, [])
        mkdir.assert_not_called()

    def test_missing_digest_is_upgraded_without_downloading(self):
        row = _row()
        self.run_slice([row], downloader=_Download())
        link_name = next(name for name, link in self.db.data["HERO Integration Link"].items() if link["source_id"] == "7")
        self.db.data["HERO Integration Link"][link_name].pop("target_field_hashes")
        self.db.commit()
        state = self.run_slice([row])
        self.assertEqual(state["counts"]["updated"], 1)
        self.assertIn("target_field_hashes", self.imported()[1])


class TestFileOffsetPaging(_FileFixture):
    run_slice = TestFileMappingImport.run_slice

    # Keep the paging fixtures independent of category filtering: offset counts every fetched row.
    def test_saved_offset_resumes_and_filtered_pages_advance(self):
        read = replace(rt.builtin_read("HeroDocuments"), page_size=2)
        rows = [_row(i, category="images") for i in range(6)] + [_row()]
        state = self.run_slice(rows, read=read, saved={"source_instance": "source-a", "offset": 2,
                                                     "mapping": read.mapping_version}, dry_run=False,
                               downloader=_Download())
        self.assertEqual([variables["offset"] for _, variables in self.client.calls], [2, 4, 6])
        self.assertEqual((state["offset"], state["counts"]["added"]), (7, 1))
        self.assertEqual(self.client.details, ["u7"])

    def test_foreign_finished_and_legacy_states_restart_at_zero(self):
        for saved in ({"source_instance": "source-b", "offset": 900},
                      {"source_instance": "source-a", "offset": 900, "done": True},
                      {"source_instance": "source-a", "offset": 900},
                      {"source_instance": "source-a", "cursor": "legacy"}):
            with self.subTest(saved=saved):
                state = self.run_slice([], saved={**saved, "counts": {"added": 10}})
                self.assertEqual(self.client.calls[0][1]["offset"], 0)
                self.assertNotIn("added", state["counts"])
                self.assertNotIn("cursor", state)

    def test_mapping_change_restarts_before_old_offset_and_remaps_without_download(self):
        old_read, new_read = rt.builtin_read('HeroDocuments'), _own_read()
        row = _row(title='Mapped.pdf', section='Roof')
        self.run_slice([row], read=old_read, downloader=_Download())
        checkpoint = {'source_instance': 'source-a', 'offset': 200, 'mapping': old_read.mapping_version,
                      'counts': {'added': 200}, 'done': False}
        state = self.run_slice([row], read=new_read, saved=checkpoint)
        self.assertEqual(self.client.calls[0][1]['start'], 0)
        self.assertEqual(state['mapping'], new_read.mapping_version)
        self.assertEqual(state['counts']['updated'], 1)
        self.assertNotIn('added', state['counts'])
        self.assertTrue(state['done'])
        link = next(row for row in self.db.committed['HERO Integration Link'].values() if row['source_id'] == '7')
        self.assertEqual(self.db.committed['File'][link['target_name']]['custom_hero_section'], 'Roof')

    def test_frozen_test_checkpoint_mapping_change_still_blocks_before_fetch(self):
        old_read, new_read = rt.builtin_read('HeroDocuments'), _own_read()
        checkpoint = {'source_instance': 'source-a', 'offset': 2, 'mapping': old_read.mapping_version,
                      'testing_record_limit': 5, 'counts': {'added': 2}}
        with patch('hero_integration.services.migration_testing.test_state', return_value=checkpoint), \
             patch('hero_integration.services.migration_testing.save_test_state') as save, \
             self.assertRaisesRegex(ValueError, 'frozen testing context changed'):
            self.run_slice([], read=new_read, record_limit=5, run_name='test-run')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(checkpoint['offset'], 2)
        self.assertEqual(checkpoint['mapping'], old_read.mapping_version)
        save.assert_not_called()

    def test_stop_keeps_current_offset_and_time_budget_saves_next_page(self):
        read = replace(rt.builtin_read("HeroDocuments"), page_size=1)
        rows = [_row(7), _row(8)]
        stopped = self.run_slice(rows, read=read, should_stop=lambda: True)
        self.assertEqual((stopped["offset"], stopped["stopped"]), (0, True))
        paused = self.run_slice(rows, read=read, time_budget=-1, downloader=_Download())
        self.assertEqual((paused["offset"], paused["done"]), (1, False))
        resumed = self.run_slice(rows, read=read, saved=paused, downloader=_Download())
        self.assertEqual([variables["offset"] for _, variables in self.client.calls], [1, 2])
        self.assertEqual((resumed["offset"], resumed["done"]), (2, True))

    def test_disk_guard_keeps_offset_before_download(self):
        with patch.object(ft.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            state = self.run_slice([_row()])
        self.assertEqual(state["offset"], 0)
        self.assertIn("disk space", state["last_error"])
