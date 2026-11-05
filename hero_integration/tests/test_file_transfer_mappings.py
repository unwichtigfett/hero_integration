"""Configurable file intake, metadata-only updates and offset checkpoints."""
from __future__ import annotations

import copy
import hashlib
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
        return 3, hashlib.md5(b'pdf').hexdigest()


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
    def legacy_file(self, *, native_type="PDF", phase="documents"):
        read = rt.builtin_read(ft.OPERATION[phase])
        type_rule = replace(read.plan.definitions[1], definition_id="type", source_path="type",
                            target_field="file_type", purpose="Field")
        read = replace(read, plan=plan_from_definitions(ft.OPERATION[phase],
            [*read.plan.definitions, type_rule], target_operation=ft.OPERATION[phase],
            adapter_options={"version": 2}))
        row = _row(type="application/pdf")
        if phase == "images":
            row.update(filename="Bild.jpg", category="images", type="image/jpeg")
        self.run_slice([row], read=read, phase=phase, downloader=_Download())
        name, link = next((name, link) for name, link in self.db.data["HERO Integration Link"].items()
                          if link["source_id"] == "7")
        file = self.db.data["File"][link["target_name"]]
        file["file_type"] = native_type
        link.update(target_field_hashes=ft.field_hashes(ft._mapped_source_values(read, row)),
            sync_status="Conflict", conflict_detail="Recheck requested: The imported file has no verified target baseline and requires review.")
        self.db.commit()
        return read, row, name, link, file

    def test_legacy_documents_and_images_reconcile_without_download(self):
        for phase, native in (("documents", "PDF"), ("images", "JPG")):
            with self.subTest(phase=phase):
                read, row, name, link, file = self.legacy_file(native_type=native, phase=phase)
                before = copy.deepcopy(file)
                state = self.run_slice([row], read=read, phase=phase)
                self.assertEqual(state["counts"]["baselines_verified"], 1)
                current = self.db.committed["HERO Integration Link"][name]
                self.assertEqual(current["sync_status"], "Synchronized")
                self.assertEqual(json.loads(current["target_field_hashes"])["version"], 2)
                self.assertEqual(self.db.committed["File"][current["target_name"]], before)
                self.assertEqual(self.run_slice([row], read=read, phase=phase)["counts"]["unchanged"], 1)

    def test_legacy_mime_repair_preview_and_atomic_commit(self):
        read, row, name, link, file = self.legacy_file(native_type="application/pdf")
        before = copy.deepcopy(self.db.committed)
        state = self.run_slice([row], read=read, dry_run=True)
        self.assertEqual(state["counts"]["would_update"], 1)
        self.assertEqual(self.db.data, before)
        self.db.fail_on = "HERO Integration Link"
        self.assertEqual(self.run_slice([row], read=read)["counts"]["failed"], 1)
        self.assertEqual(self.db.data, before)
        self.db.fail_on = None
        self.assertEqual(self.run_slice([row], read=read)["counts"]["updated"], 1)
        self.assertEqual(self.imported()[0]["file_type"], "PDF")

    def test_legacy_local_changes_and_ambiguous_bindings_stay_conflicts(self):
        read, row, name, link, file = self.legacy_file()
        baseline = copy.deepcopy(self.db.committed)
        for change in ("file_name", "folder", "attached_to_name", "creation", "blob", "source", "mapping", "binding", "pending"):
            with self.subTest(change=change):
                self.db.data = copy.deepcopy(baseline)
                link = self.db.data["HERO Integration Link"][name]
                file = self.db.data["File"][link["target_name"]]
                incoming = dict(row)
                with open(ft._disk_path(file["file_url"]), "wb") as stream:
                    stream.write(b"pdf")
                if change in {"file_name", "folder", "attached_to_name", "creation"}:
                    file[change] = "LOCAL"
                elif change == "blob":
                    with open(ft._disk_path(file["file_url"]), "wb") as stream:
                        stream.write(b"local")
                elif change == "source":
                    incoming["modified"] = "2"
                elif change == "mapping":
                    incoming["filename"] = "Other.pdf"
                elif change == "binding":
                    self.db.data["HERO Integration Link"]["foreign"] = dict(link, source_instance="source-b")
                else:
                    link["sync_status"] = "Pending Review"
                self.db.commit()
                original_file, original_hash = copy.deepcopy(file), link["target_field_hashes"]
                state = self.run_slice([incoming], read=read)
                self.assertEqual(state["counts"]["conflict"], 1)
                self.assertEqual(self.db.committed["File"][link["target_name"]], original_file)
                self.assertEqual(self.db.committed["HERO Integration Link"][name]["target_field_hashes"], original_hash)
                if change == "pending":
                    self.assertEqual(self.db.committed["HERO Integration Link"][name]["sync_status"], "Pending Review")

    def test_first_file_conflict_preserves_new_folders_for_following_file(self):
        self.db.data['HERO Integration Link']['L1'].pop('target_field_hashes', None)
        self.db.commit()
        folders = []
        def folder(*parts):
            name = 'Home/' + '/'.join(parts)
            self.db.data['File'][name] = {'is_folder': 1, 'file_name': parts[-1]}
            folders.append(name)
            return name
        insert = self.db.insert
        def checked_insert(values):
            if values['doctype'] == 'File' and values.get('file_url'):
                self.assertIn(values['folder'], self.db.data['File'], 'Folder was rolled back by the previous conflict')
            return insert(values)
        with patch.object(ft, '_state', return_value={}), patch.object(ft, '_save_state'), \
             patch.object(ft, 'allow_unlimited_attachments'), patch.object(ft, '_folder', side_effect=folder), \
             patch.object(self.db, 'insert', side_effect=checked_insert):
            state = ft.transfer('documents', source_instance='source-a', client=_Client([_row(5), _row(7)]),
                                downloader=_Download(), min_free=0)
        self.assertEqual(state['counts']['conflict'], 1)
        self.assertEqual(state['counts']['added'], 1)
        self.assertEqual(state['counts'].get('failed', 0), 0)
        self.assertTrue(all(name in self.db.committed['File'] for name in folders))
        self.assertEqual(self.db.committed['HERO Integration Link']['L1']['sync_status'], 'Conflict')
        self.assertEqual(self.imported()[0]['folder'], folders[0])

    def test_boolean_file_values_match_native_check_integer_baselines(self):
        from hero_integration.services import mapped_update_v2 as update
        item = {'id': 'check', 'source': {'doctype': 'File', 'field': 'custom_checked'}, 'resolver': 'FIELD'}
        for source, native in ((True, 1), (False, 0)):
            with self.subTest(source=source):
                self.assertEqual(ft.value_hash(source), ft.value_hash(native))
                state = {'version': 2, 'mapped_fields': ['custom_checked'],
                         'target': {'custom_checked': ft.value_hash(native)}}
                ctx = SimpleNamespace(content={'area': 'HeroDocuments'}, query_plan=None,
                    source_links={'check': SimpleNamespace(target_field_hashes=json.dumps(state))})
                with patch.object(ft, 'mapped_values', return_value={'custom_checked': source}):
                    self.assertTrue(update._baseline_matches(ctx, item, {}, source))
        self.assertNotEqual(ft.value_hash(False), ft.value_hash(None))
        self.assertNotEqual(ft.value_hash(True), ft.value_hash('1'))

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

    def test_missing_digest_requires_review_without_downloading_or_rebaselining(self):
        row = _row()
        self.run_slice([row], downloader=_Download())
        link_name = next(name for name, link in self.db.data["HERO Integration Link"].items() if link["source_id"] == "7")
        self.db.data["HERO Integration Link"][link_name].pop("target_field_hashes")
        self.db.commit()
        state = self.run_slice([row])
        self.assertEqual(state['counts']['conflict'], 1)
        self.assertNotIn('target_field_hashes', self.imported()[1])
        self.assertEqual(self.imported()[1]['sync_status'], 'Conflict')

    def test_local_file_changes_block_metadata_relink_and_content_replacement(self):
        for change in ('filename', 'attachment', 'folder', 'blob', 'new_mapping_field'):
            for source_changed in (False, True):
                with self.subTest(change=change, source_changed=source_changed):
                    row = _row(title='Mapped.pdf', section='Roof')
                    self.run_slice([row], downloader=_Download())
                    file, link = self.imported()
                    target = self.db.data['File'][link['target_name']]
                    if change == 'filename': target['file_name'] = 'My local name.pdf'
                    elif change == 'attachment': target['attached_to_name'] = 'LOCAL-PROJECT'
                    elif change == 'folder': target['folder'] = 'Home/Local'
                    elif change == 'new_mapping_field': target['custom_hero_section'] = 'Local section'
                    else:
                        with open(ft._disk_path(target['file_url']), 'wb') as stream: stream.write(b'local content')
                    self.db.commit()
                    before = copy.deepcopy(target)
                    baseline = link['target_field_hashes']
                    if source_changed: row['modified'] = '2'
                    state = self.run_slice([row], read=_own_read())  # forbids any download
                    self.assertEqual(state['counts']['conflict'], 1)
                    current, current_link = self.imported()
                    self.assertEqual(current, before)
                    self.assertEqual(current_link['target_field_hashes'], baseline)
                    self.assertEqual(current_link['sync_status'], 'Conflict')
                    if change == 'blob':
                        self.assertEqual(open(ft._disk_path(current['file_url']), 'rb').read(), b'local content')
                    # Isolate each case while retaining the fixture's unrelated source bindings.
                    self.db.data['File'].pop(link['target_name'])
                    self.db.data['HERO Integration Link'].pop(next(name for name, value in self.db.data['HERO Integration Link'].items()
                        if value.get('source_id') == '7'))
                    self.db.commit()

    def test_legacy_locally_renamed_file_is_not_adopted_as_baseline(self):
        row = _row()
        self.run_slice([row], downloader=_Download())
        file, link = self.imported()
        self.db.data['File'][link['target_name']]['file_name'] = 'Locally renamed.pdf'
        link_name = next(name for name, value in self.db.data['HERO Integration Link'].items() if value.get('source_id') == '7')
        self.db.data['HERO Integration Link'][link_name]['target_field_hashes'] = ft.field_hashes({'file_name': row['filename']})
        self.db.commit()
        before = copy.deepcopy(self.db.committed['File'])
        state = self.run_slice([row])
        self.assertEqual(state['counts']['conflict'], 1)
        self.assertEqual(self.db.committed['File'], before)

    def test_preview_reports_local_file_conflict_without_database_writes(self):
        row = _row()
        self.run_slice([row], downloader=_Download())
        _, link = self.imported()
        self.db.data['File'][link['target_name']]['file_name'] = 'Local.pdf'
        self.db.commit()
        before = copy.deepcopy(self.db.committed)
        state = self.run_slice([row], dry_run=True)
        self.assertEqual(state['counts']['conflict'], 1)
        self.assertEqual(self.db.committed, before)

    def test_partial_outgoing_ack_preserves_unsent_metadata_and_attachment_baselines(self):
        from hero_integration.services import mapped_update_v2 as update
        from unittest.mock import patch
        read = _own_read()
        row = _row(title='Imported.pdf', section='Roof')
        self.run_slice([row], read=read, downloader=_Download())
        file, link = self.imported()
        link_name = next(name for name, value in self.db.data['HERO Integration Link'].items() if value.get('source_id') == '7')
        original = json.loads(link['target_field_hashes'])
        target = self.db.data['File'][link['target_name']]
        target.update(file_name='Sent.pdf', custom_hero_section='Unsent local edit', attached_to_name='LOCAL')
        self.db.commit()
        assignment = {'id': 'name', 'source': {'doctype': 'File', 'field': 'file_name'}, 'resolver': 'FIELD'}
        native_link = SimpleNamespace(name=link_name, source_field_hashes='', **link)
        ctx = SimpleNamespace(content={'area': 'HeroDocuments', 'assignments': [assignment]},
                              source_links={'name': native_link}, query_plan=read.plan)
        self.assertTrue(update._baseline_matches(ctx, assignment, row, 'Imported.pdf'))
        self.assertFalse(update._baseline_matches(ctx, assignment, {**row, 'title': 'Remote edit.pdf'}, 'Remote edit.pdf'))
        with patch.object(update, 'frappe', ft.frappe), patch.object(update, '_canonical', return_value='Sent.pdf'):
            update._acknowledge(ctx, SimpleNamespace(changes=[{'id': 'name', 'local': 'Sent.pdf'}]), {**row, 'title': 'Sent.pdf'})
        self.db.commit()
        acknowledged = json.loads(self.db.committed['HERO Integration Link'][link_name]['target_field_hashes'])
        self.assertEqual(acknowledged['target']['file_name'], ft.value_hash('Sent.pdf'))
        for field in set(original['target']) - {'file_name'}:
            self.assertEqual(acknowledged['target'][field], original['target'][field])
        state = self.run_slice([{**row, 'title': 'Sent.pdf'}], read=read)
        self.assertEqual(state['counts']['conflict'], 1)
        self.assertEqual(self.imported()[0]['custom_hero_section'], 'Unsent local edit')

    def test_change_during_download_is_rechecked_before_blob_replacement(self):
        row = _row()
        self.run_slice([row], downloader=_Download())
        file, link = self.imported()
        old_url = file['file_url']
        outer = self
        class ChangedDuringDownload(_Download):
            def fetch(self, url, target, size):
                result = super().fetch(url, target, size)
                outer.db.data['File'][link['target_name']]['file_name'] = 'Concurrent local rename.pdf'
                outer.db.commit()
                return result
        state = self.run_slice([{**row, 'modified': '2'}], downloader=ChangedDuringDownload())
        self.assertEqual(state['counts']['conflict'], 1)
        file, _ = self.imported()
        self.assertEqual(file['file_name'], 'Concurrent local rename.pdf')
        self.assertEqual(file['file_url'], old_url)
        self.assertTrue(ft.os.path.exists(ft._disk_path(old_url)))

    def test_held_and_missing_native_targets_never_get_overwritten_or_recreated(self):
        row = _row()
        self.run_slice([row], downloader=_Download())
        file, link = self.imported()
        link_name = next(name for name, value in self.db.data['HERO Integration Link'].items() if value.get('source_id') == '7')
        for status in ('Rejected', 'Pending Review', 'Source Deleted'):
            self.db.data['HERO Integration Link'][link_name]['sync_status'] = status
            self.db.commit()
            state = self.run_slice([{**row, 'modified': '2'}])
            self.assertEqual(state['counts']['conflict'], 1)
            self.assertEqual(self.imported()[1]['sync_status'], status)
        self.db.data['HERO Integration Link'][link_name]['sync_status'] = 'Synchronized'
        self.db.data['File'].pop(link['target_name'])
        self.db.commit()
        state = self.run_slice([row])
        self.assertEqual(state['counts']['conflict'], 1)
        self.assertNotIn(link['target_name'], self.db.committed['File'])


    def test_deferred_attachment_after_project_arrives_preserves_blob_and_local_edits(self):
        from hero_integration.services import deferred_references as deferred
        project_link = self.db.data['HERO Integration Link'].pop('PA')
        self.db.commit()
        row, downloader = _row(), _Download()
        self.run_slice([row], downloader=downloader)
        file, link = self.imported()
        self.assertFalse(file.get('attached_to_name'))
        original_url = file['file_url']
        with open(ft._disk_path(original_url), 'rb') as stream:
            original_bytes = stream.read()
        self.db.data['HERO Integration Link']['PA'] = project_link
        self.db.commit()
        read = rt.builtin_read('HeroDocuments')
        pending = SimpleNamespace(operation='HeroDocuments', source_instance='source-a', source_id='7',
            mapping_version=read.mapping_version, record_json=json.dumps({'row': row,
                'detail': {'file_upload_matches': [{'project_match_id': 42}]}}))
        with patch.object(deferred, 'frappe', ft.frappe), \
             patch.object(rt, 'resolve_read', return_value=read), \
             patch.object(ft, '_folder', return_value='Home/HERO/Dokumente'), \
             patch.object(ft, 'commit_file', side_effect=AssertionError('must not replace the blob')):
            self.assertEqual(deferred._resolve_file(pending), 'UPDATED')
            self.assertEqual(self.db.data['File'][link['target_name']]['attached_to_name'], 'PROJECT-A')
            self.assertEqual(self.db.data['File'][link['target_name']]['file_url'], original_url)
            with open(ft._disk_path(original_url), 'rb') as stream:
                self.assertEqual(stream.read(), original_bytes)
            self.db.commit()
            self.db.data['File'][link['target_name']]['folder'] = 'LOCAL'
            self.db.commit()
            with self.assertRaises(ft.FileConflict): deferred._resolve_file(pending)
        self.assertEqual(downloader.calls, 1)


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
