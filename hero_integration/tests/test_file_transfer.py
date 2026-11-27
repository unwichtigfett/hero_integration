"""HERO file transfer: streaming download, URL guard, phase listing, targets."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hero_integration.services import file_transfer as ft
from hero_integration.tests.saved_reader_fixture import isolate_direct_materialization


class _Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _jpeg():
    from PIL import Image

    with io.BytesIO() as body:
        Image.new("RGB", (8, 8), "red").save(body, "JPEG")
        return body.getvalue()


class _Opener:
    def __init__(self, *results):
        self.results, self.requests = list(results), []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


URL = "https://login.hero-software.de/files/document/offer/abc.pdf/Angebot.pdf?cb=1&download=1"


def test_file_details_materialize_against_approved_snapshot():
    from hero_integration.services.graphql_graph import analyze_snapshot

    scalar = lambda name: {'kind': 'SCALAR', 'name': name}
    obj = lambda name: {'kind': 'OBJECT', 'name': name}
    field = lambda name, type_: {'name': name, 'args': [], 'type': type_}
    raw = {'__schema': {'queryType': {'name': 'Query'}, 'types': [
        {'kind': 'OBJECT', 'name': 'Query', 'fields': [{'name': 'file_uploads',
            'args': [{'name': 'uuids', 'type': {'kind': 'LIST', 'ofType': scalar('String')}},
                     {'name': 'first', 'type': scalar('Int')}],
            'type': {'kind': 'LIST', 'ofType': obj('File')}}]},
        {'kind': 'OBJECT', 'name': 'File', 'fields': [field('id', scalar('Int')), field('uuid', scalar('String')),
            field('is_deleted', scalar('Boolean')), field('file_upload_matches', obj('Match')),
            field('file_upload_links', obj('Link')), field('customer_document', obj('Document'))]},
        {'kind': 'OBJECT', 'name': 'Match', 'fields': [field('project_match_id', scalar('Int'))]},
        {'kind': 'OBJECT', 'name': 'Link', 'fields': [field(name, scalar('String')) for name in
            ('target', 'target_id', 'customer_id', 'project_match_id')]},
        {'kind': 'OBJECT', 'name': 'Document', 'fields': [*[field(name, scalar('String')) for name in
            ('id', 'nr', 'status_code', 'project_match_id', 'contact_id', 'localized_type')],
            field('document_type', obj('DocumentType'))]},
        {'kind': 'OBJECT', 'name': 'DocumentType', 'fields': [field('name', scalar('String'))]},
        *({'kind': 'SCALAR', 'name': name} for name in ('String', 'Int', 'Boolean')),
    ]}}
    fingerprint = analyze_snapshot(raw).fingerprint
    mapping = SimpleNamespace(status='Active', source_instance='source-a', schema_snapshot='snap', schema_fingerprint=fingerprint)
    snapshot = SimpleNamespace(name='snap', snapshot_json=raw, schema_fingerprint=fingerprint,
                               source_instance='source-a', analysis_status='Validated', execution_contract_status='Compatible')
    fake = SimpleNamespace(get_doc=lambda doctype, name: mapping if doctype == 'HERO GraphQL Mapping' else snapshot)
    with patch.object(ft, 'frappe', fake):
        request = ft._detail_request(SimpleNamespace(mapping='mapped'), 'source-a', ['u1'])
        assert 'u1' not in request.document
        assert ['u1'] in request.variables.values()
        assert ('customer_document', 'document_type', 'name') in {item.selection_path for item in request.result_bindings}
        mapping.source_instance = 'other'
        with unittest.TestCase().assertRaises(ft.TransferError):
            ft._detail_request(SimpleNamespace(mapping='mapped'), 'source-a', ['u1'])


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.target = os.path.join(self.dir, "x.part")
        sleeper = patch.object(ft.time, "sleep", lambda s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def make(self, *results, max_bytes=ft.MAX_FILE_BYTES):
        opener = _Opener(*results)
        return ft.Downloader("login.hero-software.de", lambda: "tok", max_bytes=max_bytes, opener=opener), opener

    def test_streams_body_to_disk_with_token_and_md5(self):
        body = b"%PDF" + b"x" * (3 * ft.CHUNK)
        downloader, opener = self.make(_Response(body))
        size, md5 = downloader.fetch(URL, self.target)
        self.assertEqual(size, len(body))
        self.assertEqual(open(self.target, "rb").read(), body)
        self.assertEqual(md5, __import__("hashlib").md5(body).hexdigest())
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer tok")

    def test_foreign_hosts_and_paths_never_get_the_token(self):
        downloader, opener = self.make()
        for url in ("https://evil.example/files/a.pdf", "http://login.hero-software.de/files/a.pdf",
                    "https://login.hero-software.de/api/a.pdf", "https://login.hero-software.de:8443/files/a.pdf"):
            with self.assertRaises(ft.TransferError):
                downloader.fetch(url, self.target)
        self.assertEqual(opener.requests, [])

    def test_oversized_file_is_rejected_and_removed(self):
        downloader, _ = self.make(_Response(b"x" * 2048, length=0), max_bytes=1024)
        with self.assertRaises(ft.TransferError):
            downloader.fetch(URL, self.target)
        self.assertFalse(os.path.exists(self.target))
        downloader, _ = self.make(max_bytes=1024)
        with self.assertRaises(ft.TransferError):
            downloader.fetch(URL, self.target, expected_size=4096)

    def test_server_errors_are_retried_but_403_is_not(self):
        error = lambda code: urllib.error.HTTPError(URL, code, "x", {}, None)
        downloader, opener = self.make(error(503), _Response(b"ok"))
        self.assertEqual(downloader.fetch(URL, self.target)[0], 2)
        downloader, opener = self.make(error(403), _Response(b"ok"))
        with self.assertRaises(ft.TransferError):
            downloader.fetch(URL, self.target)
        self.assertEqual(len(opener.requests), 1)

    def test_truncated_body_is_retried(self):
        downloader, opener = self.make(_Response(b"abc", length=10), _Response(b"abcdefghij"))
        self.assertEqual(downloader.fetch(URL, self.target)[0], 10)
        self.assertEqual(len(opener.requests), 2)

    def test_hero_size_mismatch_retries_and_removes_partial_file(self):
        downloader, opener = self.make(*[_Response(b"abc") for _ in range(3)])
        with self.assertRaises(ft.TransferError):
            downloader.fetch(URL, self.target, expected_size=10)
        self.assertEqual(len(opener.requests), 3)
        self.assertFalse(os.path.exists(self.target))

    def test_image_checks_content_even_when_metadata_size_matches(self):
        for body in (b"not an image", _jpeg()[:-12]):
            with self.subTest(length=len(body)):
                downloader, _ = self.make(_Response(body))
                with self.assertRaisesRegex(ft.TransferError, "invalid or incomplete"):
                    downloader.fetch(URL, self.target, len(body), image_id="7")
                self.assertFalse(os.path.exists(self.target))

    def test_image_rejects_incomplete_http_even_with_decodable_body(self):
        body = _jpeg()
        responses = [_Response(body), _Response(body), _Response(body, length=len(body) + 10)]
        responses[0].status = 206
        responses[1].headers["Content-Range"] = f"bytes 0-{len(body)-1}/{len(body)+10}"
        for response in responses:
            with self.subTest(headers=response.headers):
                downloader, _ = self.make(response)
                downloader.retries = 0
                with self.assertRaises(ft.TransferError):
                    downloader.fetch(URL, self.target, len(body), image_id="7")
                self.assertFalse(os.path.exists(self.target))

    def test_hero_size_retry_success_and_empty_files(self):
        downloader, _ = self.make(_Response(b"abc"), _Response(b"abcdefghij"))
        self.assertEqual(downloader.fetch(URL, self.target, expected_size=10)[0], 10)
        downloader, _ = self.make(_Response(b""))
        self.assertEqual(downloader.fetch(URL, self.target, expected_size=0)[0], 0)

    def test_unknown_size_and_nonempty_body_with_zero_metadata(self):
        response = _Response(b"abc")
        response.headers.clear()
        downloader, _ = self.make(response)
        self.assertEqual(downloader.fetch(URL, self.target, expected_size=None)[0], 3)
        downloader, _ = self.make(*[_Response(b"abc") for _ in range(3)])
        with self.assertRaises(ft.TransferError):
            downloader.fetch(URL, self.target, expected_size=0)
        self.assertFalse(os.path.exists(self.target))


def _page(nodes, end, more):
    return {"edges": [{"node": n} for n in nodes], "pageInfo": {"endCursor": end, "hasNextPage": more}}


class TestListingAndTargets(unittest.TestCase):
    def test_target_prefers_project_then_customer(self):
        projects, customers = {"10": "PV-1-H"}, {"77": "KD-00001"}
        self.assertEqual(ft.resolve_target({"file_upload_matches": [{"project_match_id": 10}]}, projects, customers),
                         ("Project", "PV-1-H"))
        self.assertEqual(ft.resolve_target({"customer_document": {"project_match_id": 99, "contact_id": 77}},
                                           projects, customers), ("Customer", "KD-00001"))
        self.assertEqual(ft.resolve_target({}, projects, customers), (None, None))

    def test_deleted_files_and_documents_are_skipped(self):
        row = {"downloadUrl": URL}
        self.assertEqual(ft.skip_reason(row, {"is_deleted": True}), "deleted")
        self.assertEqual(ft.skip_reason(row, {"customer_document": {"status_code": 1000}}), "deleted")
        self.assertEqual(ft.skip_reason(row, None), "no_detail")
        self.assertIsNone(ft.skip_reason(row, {"customer_document": {"status_code": 100}}))

    def test_safe_name_and_change_hash(self):
        self.assertEqual(ft.safe_name("../Angebot ANG-1 Müller.pdf"), "Angebot_ANG-1_Müller.pdf")
        self.assertNotEqual(ft.source_hash({"uuid": "a", "modified": "1"}), ft.source_hash({"uuid": "a", "modified": "2"}))


class _TxDB:
    """In-memory tables with commit/rollback, enough for the File + link writes."""

    def __init__(self, tables):
        self.committed = copy.deepcopy(tables)
        self.data = copy.deepcopy(tables)
        self.fail_on = None
        self.fail_commit = False
        self.counter = 0

    def _match(self, row, filters):
        for key, value in filters.items():
            actual = row.get(key, "HERO" if key == "source_system" else None)
            if isinstance(value, list):
                operator, expected = value
                if operator == "in" and actual in expected or operator == "!=" and actual != expected:
                    continue
                return False
            if actual != value:
                return False
        return True

    def exists(self, doctype, name):
        rows = self.data.get(doctype, {})
        if isinstance(name, dict):
            return next((key for key, row in rows.items() if self._match(row, name)), None)
        return name if name in rows else None

    def get_value(self, doctype, name, fields, as_dict=False):
        row = self.data[doctype][name]
        if isinstance(fields, (list, tuple)):
            return {field: row.get(field) for field in fields}
        return row.get(fields)

    def set_value(self, doctype, name, values, update_modified=True):
        if self.fail_on == doctype:
            raise RuntimeError(f"{doctype} write failed")
        self.data[doctype][name].update(values)

    def sql(self, query, params):
        source_type, instance, *scope = params
        rows = [(row["source_id"], row["target_doctype"], row.get("target_name"), row.get("sync_status", "Synchronized"))
                for row in self.data["HERO Integration Link"].values()
                if row["source_type"] == source_type and row["source_instance"] == instance
                and row.get("source_system", "HERO") == "HERO"]

        if not scope:
            return rows
        identities, doctype = scope
        return [(identity, target) for identity, target_type, target, status in rows
                if identity in identities and target_type == doctype and target
                and status in ("Synchronized", "Conflict") and sum(row[0] == identity for row in rows) == 1]

    def insert(self, values):
        self.counter += 1
        name = f"{values['doctype'][:4].upper()}-NEW{self.counter}"
        self.data.setdefault(values["doctype"], {})[name] = {k: v for k, v in values.items() if k != "doctype"}
        return name

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("commit failed")
        self.committed = copy.deepcopy(self.data)

    def rollback(self):
        self.data = copy.deepcopy(self.committed)


class _Doc(SimpleNamespace):
    def get(self, key):
        return self._values.get(key)

    def set(self, key, value):
        self._values[key] = value
        setattr(self, key, value)

    def update(self, values):
        for key, value in values.items():
            self.set(key, value)

    def get_doc_before_save(self):
        return self._before

    def load_doc_before_save(self, **kwargs):
        self._before = copy.deepcopy(self._db.data[self._values["doctype"]][self.name])

    def set_file_type(self):
        self.set("file_type", ft._native_file_type(self.file_name))

    def insert(self, **kwargs):
        self.name = self._db.insert(self._values)
        return self

    def save(self, **kwargs):
        self._db.set_value(self._values["doctype"], self.name, self._values)
        return self


def _fake_frappe(db, root):
    def get_doc(values, name=None, **kwargs):
        if isinstance(values, dict):
            return _Doc(_db=db, _values=values, _before=None, flags=SimpleNamespace(), **values)
        if values != "File":
            return SimpleNamespace(**db.data[values][name])
        row = copy.deepcopy(db.data[values][name])
        return _Doc(_db=db, _values={"doctype": values, **row}, _before=row,
                    flags=SimpleNamespace(), name=name, **row)

    return SimpleNamespace(
        db=db, flags=SimpleNamespace(), log_error=lambda **k: None,
        get_doc=get_doc,
        get_all=lambda doctype, filters, fields, limit_page_length=0: [
            SimpleNamespace(name=name, **{field: row.get(field) for field in fields if field != "name"})
            for name, row in db.data.get(doctype, {}).items() if db._match(row, filters)],
        get_site_path=lambda *parts: os.path.join(root, *parts[1:]),
        utils=SimpleNamespace(now_datetime=lambda: "now"))


class _FileFixture(unittest.TestCase):
    def setUp(self):
        isolate_direct_materialization(self)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, "files"))
        with open(self.path("old.pdf"), "wb") as handle:
            handle.write(b"old")
        self.db = _TxDB({
            "Project Image Category": {"HERO-Import": {"name": "HERO-Import", "disabled": 0}},
            "File": {"F1": {"file_url": "/private/files/old.pdf", "attached_to_doctype": None,
                            "attached_to_name": None, "folder": "Home/HERO/Dokumente/Ohne Zuordnung"}},
            "HERO Integration Link": {
                "L1": {"source_type": ft.SOURCE_TYPE, "source_id": "5", "source_instance": "source-a",
                       "target_doctype": "File", "target_name": "F1", "source_payload_hash": "old"},
                "PA": {"source_type": "project", "source_id": "42", "source_instance": "source-a",
                       "target_doctype": "Project", "target_name": "PROJECT-A"},
                "PB": {"source_type": "project", "source_id": "42", "source_instance": "source-b",
                       "target_doctype": "Project", "target_name": "PROJECT-B"}}})
        frappe_patch = patch.object(ft, "frappe", _fake_frappe(self.db, self.root))
        frappe_patch.start()
        self.addCleanup(frappe_patch.stop)
        from hero_integration.services import project_image_categories
        category_patch = patch.object(project_image_categories, "frappe", ft.frappe)
        category_patch.start()
        self.addCleanup(category_patch.stop)
        read_patch = patch.object(ft.direct_runtime, "resolve_read", side_effect=lambda op, source: ft.direct_runtime.builtin_read(op))
        read_patch.start()
        self.addCleanup(read_patch.stop)
        detail_patch = patch.object(ft, '_detail_request', side_effect=lambda read, source, uuids, *, paths=ft.DETAIL_PATHS:
            SimpleNamespace(document='materialized file detail', variables={'uuids': uuids, 'first': len(uuids)}))
        detail_patch.start()
        self.addCleanup(detail_patch.stop)
        observation_patch = patch('hero_integration.services.deferred_references.remember_file')
        self.observations = observation_patch.start()
        self.addCleanup(observation_patch.stop)
        storage_patch = patch('hero_integration.services.deferred_references.require_storage')
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.link = SimpleNamespace(name="L1", target_name="F1", source_payload_hash="old")
        self.db.data['File']['F1']['file_name'] = 'Angebot.pdf'
        self.db.data['HERO Integration Link']['L1']['target_field_hashes'] = ft._target_baseline('F1', {'file_name': 'Angebot.pdf'})
        self.db.data['HERO Integration Link']['L1']['target_payload_hash'] = hashlib.md5(b'old').hexdigest()
        self.db.commit()

    def path(self, name):
        return os.path.join(self.root, "files", name)

    def download(self, body=b"new"):
        temp = self.path("5.part")
        with open(temp, "wb") as handle:
            handle.write(body)
        return temp

    def current_file(self):
        return self.db.committed["File"]["F1"]


class TestReplaceKeepsAValidFile(_FileFixture):
    ROW = {"id": 5, "uuid": "u5", "filename": "Angebot.pdf", "modified": "2", "size": 3}

    def replace(self):
        return ft.commit_file(self.ROW, 3, hashlib.md5(b'new').hexdigest(), self.download(), ("Project", "PROJECT-A"),
                              "Home/HERO/Dokumente", self.link, "source-a")

    def assert_points_at_existing_file(self):
        url = self.current_file()["file_url"]
        self.assertTrue(os.path.exists(self.path(url.rsplit("/", 1)[-1])), url)

    def test_failed_link_update_restores_the_old_file_and_keeps_its_blob(self):
        self.db.fail_on = "HERO Integration Link"
        with self.assertRaises(RuntimeError):
            self.replace()
        self.assertEqual(self.current_file()["file_url"], "/private/files/old.pdf")
        self.assert_points_at_existing_file()
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "files"))), ["old.pdf"])

    def test_failed_commit_removes_the_new_blob_and_keeps_the_old(self):
        self.db.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.replace()
        self.assertEqual(self.current_file()["file_url"], "/private/files/old.pdf")
        self.assert_points_at_existing_file()
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "files"))), ["old.pdf"])

    def test_successful_replace_removes_the_old_blob_after_commit(self):
        self.replace()
        self.assert_points_at_existing_file()
        self.assertFalse(os.path.exists(self.path("old.pdf")))
        self.assertEqual((self.current_file()["attached_to_doctype"], self.current_file()["attached_to_name"],
                          self.current_file()["folder"]), ("Project", "PROJECT-A", "Home/HERO/Dokumente"))


class _ListClient:
    def __init__(self, rows, details):
        self.rows, self.detail_rows = rows, details

    def query(self, document, variables):
        if "uuids" in variables:
            ids = {row["uuid"]: row["id"] for row in self.rows}
            return {"file_uploads": [{"id": ids[uuid], **self.detail_rows[uuid]} for uuid in variables["uuids"]]}
        offset = variables.get('offset', 0)
        first = variables.get('first', len(self.rows))
        return {"FileUpload_FileUploads": _page(self.rows[offset:offset + first], "end", False)}


class _NoDownload:
    def fetch(self, *args):
        raise AssertionError("unchanged content must not be downloaded")


class TestSourceAndTargetBinding(_FileFixture):
    def run_transfer(self, rows, details, instance="source-a", downloader=None):
        with patch.object(ft, "_state", return_value={}), patch.object(ft, "_save_state"), \
                patch.object(ft, "allow_unlimited_attachments"), \
                patch.object(ft, "_folder", side_effect=lambda *parts: "Home/" + "/".join(parts)):
            return ft.transfer("documents", source_instance=instance, client=_ListClient(rows, details),
                               downloader=downloader or _NoDownload(), min_free=0, run_name="synthetic-run")

    def test_deferred_image_relink_preserves_native_save_category_and_baseline(self):
        import json
        from hero_integration.services import deferred_references as deferred

        row = {"id": 5, "uuid": "u5", "filename": "image.png", "modified": "1", "size": 3,
               "downloadUrl": URL, "category": "images"}
        read = ft.direct_runtime.builtin_read("HeroPictures")
        self.db.data["File"]["F1"].update(file_name="image.png", file_type="PNG")
        self.db.data["HERO Integration Link"]["L1"].update(
            source_system="HERO", source_payload_hash=ft.source_hash(row),
            target_field_hashes=ft._target_baseline("F1", {"file_name": "image.png"}))
        self.db.commit()
        pending = SimpleNamespace(origin_run="synthetic-run",operation="HeroPictures", source_instance="source-a", source_id="5",
            mapping_version=read.mapping_version, record_json=json.dumps({"row": row,
                "detail": {"file_upload_matches": [{"project_match_id": 42}]}}))
        saved = []
        native_save = _Doc.save

        def save(document, **kwargs):
            # The category must already exist when native File validation starts.
            saved.append((document.get("attached_to_name"), document.get("custom_project_image_category")))
            return native_save(document, **kwargs)

        with patch.object(deferred, "frappe", ft.frappe), \
             patch.object(ft, "_folder", return_value="Home/HERO/Bilder"), \
             patch.object(ft, "mapped_values", return_value={"file_name": "image.png"}), \
             patch.object(_Doc, "save", save):
            self.assertEqual(deferred._resolve_file(pending), "UPDATED")
        self.assertEqual(saved, [("PROJECT-A", "HERO-Import")])
        baseline = json.loads(self.db.data["HERO Integration Link"]["L1"]["target_field_hashes"])
        self.assertEqual(baseline["target"]["attached_to_name"], ft.value_hash("PROJECT-A"))
        self.assertEqual(baseline["target"]["_blob_md5"], hashlib.md5(b"old").hexdigest())

    def test_execute_requires_an_originating_run_before_transfer(self):
        with self.assertRaisesRegex(ft.TransferError, 'originating integration run'):
            ft.transfer('documents', source_instance='source-a', client=object(), downloader=object())

    def test_project_ids_resolve_only_within_the_run_source(self):
        projects = ft._bindings("project", "Project", "source-a")
        self.assertEqual(projects, {"42": "PROJECT-A"})
        self.assertEqual(ft._bindings("project", "Project", "source-b"), {"42": "PROJECT-B"})
        self.assertEqual(ft.resolve_target({"file_upload_matches": [{"project_match_id": 42}]}, projects, {}),
                         ("Project", "PROJECT-A"))

    def test_related_test_selection_uses_project_and_customer_identities(self):
        cohort = {'project_ids': ['42'], 'customer_ids': ['5']}
        base = {'file_upload_matches': [], 'file_upload_links': [], 'customer_document': None}
        self.assertTrue(ft.belongs_to_test_cohort({**base, 'file_upload_matches': [{'project_match_id': 42}]}, cohort))
        self.assertTrue(ft.belongs_to_test_cohort({**base, 'customer_document': {'project_match_id': None, 'contact_id': 5}}, cohort))
        self.assertFalse(ft.belongs_to_test_cohort({**base, 'file_upload_matches': [{'project_match_id': 7}]}, cohort))
        with self.assertRaisesRegex(ft.TransferError, 'omits'):
            ft.belongs_to_test_cohort({'file_upload_matches': []}, cohort)

    def test_ambiguous_or_untrusted_target_is_not_adopted_for_an_attachment(self):
        self.db.data['HERO Integration Link']['DUP'] = {
            'source_type': 'project', 'source_id': '42', 'source_instance': 'source-a',
            'target_doctype': 'Project', 'target_name': 'OTHER', 'sync_status': 'Rejected'}
        self.db.data['HERO Integration Link']['FOREIGN'] = {
            'source_type': 'project', 'source_id': '88', 'source_instance': 'source-a', 'source_system': 'OTHER',
            'target_doctype': 'Project', 'target_name': 'FOREIGN', 'sync_status': 'Synchronized'}
        self.assertNotIn('42', ft._bindings('project', 'Project', 'source-a'))
        self.assertNotIn('88', ft._bindings('project', 'Project', 'source-a'))

    def test_truncated_transfer_preserves_blob_links_and_source_baselines(self):
        before = copy.deepcopy(self.db.committed)
        for identity in (5, 7):
            row = {"id": identity, "uuid": "sample", "filename": "Angebot.pdf", "modified": "2",
                   "size": 10, "downloadUrl": URL, "category": "offer"}
            downloader = ft.Downloader("login.hero-software.de", lambda: "fixture", retries=0,
                                       opener=_Opener(_Response(b"abc", length=3)))
            state = self.run_transfer([row], {"sample": {"uuid": "sample"}}, downloader=downloader)
            self.assertEqual(state["counts"].get("failed"), 1)
            self.assertFalse(state["counts"].get("added") or state["counts"].get("updated"))
            self.assertEqual(self.db.committed, before)
            with open(self.path("old.pdf"), "rb") as stream:
                self.assertEqual(stream.read(), b"old")
            self.assertFalse(any(name.endswith(".part") for _, _, files in os.walk(self.root) for name in files))

        complete = ft.Downloader("login.hero-software.de", lambda: "fixture", retries=0,
                                 opener=_Opener(_Response(b"0123456789")))
        state = self.run_transfer([row], {"sample": {"uuid": "sample"}}, downloader=complete)
        self.assertEqual(state["counts"].get("added"), 1)
        link = next(link for link in self.db.committed["HERO Integration Link"].values() if link["source_id"] == "7")
        self.assertEqual(link["source_payload_hash"], ft.source_hash(row))
        self.assertEqual(link["sync_status"], "Synchronized")

    def test_completed_image_retry_uses_failed_receipts_and_keeps_checkpoint(self):
        from dataclasses import replace
        from hero_integration.services import execution_budget, scoped_test_reads

        detail = {"uuid": "u8", "id": 8, "is_deleted": False, "file_upload_links": [],
                  "file_upload_matches": [{"project_match_id": 42}], "customer_document": None}
        cohort = {"version": 2, "scoped_project_reads": True, "project_ids": ["42"], "customer_ids": []}
        proof = ft.project_file_proof(detail, cohort)
        cohort["files"] = {"HeroPictures": {"7": {**proof, "uuid": "u7"}, "8": proof}}
        read = replace(ft.direct_runtime.builtin_read("HeroPictures"), validate_write=Mock())
        checkpoint = {"done": True, "offset": 2, "testing_record_limit": 1, "source_instance": "source-a",
                      "mapping": read.mapping_version, "testing_cohort_fingerprint":
                          hashlib.sha256(json.dumps(cohort, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        row = {"id": 8, "uuid": "u8", "filename": "img.jpg", "category": "images",
               "size": 12, "modified": "1", "downloadUrl": URL}
        opener = _Opener(_Response(_jpeg()))
        downloader = ft.Downloader("login.hero-software.de", lambda: "token", opener=opener)
        writer_outcomes = {"7": "processed", "8": "failed"}
        native_get_all = ft.frappe.get_all

        def get_all(dt, **kwargs):
            if dt == "HERO Budget Outcome":
                self.assertEqual(kwargs["filters"], {"budget": "B", "phase": "writer", "object_type": "file_upload",
                                                     "outcome": "failed", "external_id": ["in", ["7", "8"]]})
                return [i for i, result in writer_outcomes.items() if result == "failed"]
            return native_get_all(dt, **kwargs)

        budget = Mock(version=3, budget_id="B")
        budget.outcome.side_effect = lambda kind, identity, outcome: writer_outcomes.update({str(identity): outcome})
        save_checkpoint = Mock()
        def transfer(ids):
            return ft.transfer("images", client=_ListClient([row], {"u8": detail}), downloader=downloader, read=read,
                               source_instance="source-a", run_name="completed", cohort=cohort, record_limit=1,
                               min_free=0, retry_image_ids=ids)
        with patch.object(scoped_test_reads, "exact_rows", return_value=[row]) as metadata, \
                patch.object(execution_budget, "current_budget", return_value=budget), \
                patch.object(ft.frappe, "get_all", side_effect=get_all), \
                patch.object(ft, "scoped_file_target", return_value=("Project", "PROJECT-A")), \
                patch.object(ft, "_folder", side_effect=lambda *parts: "/".join(parts)), \
                patch.object(ft.direct_runtime, "run_progress", return_value=(checkpoint, save_checkpoint)):
            with self.assertRaisesRegex(ft.TransferError, "original image cohort"):
                transfer(["9"])
            self.assertEqual(transfer(["7", "8"])["counts"]["added"], 1)
            self.assertTrue(transfer(["7", "8"])["done"])
        self.assertEqual(writer_outcomes, {"7": "processed", "8": "processed"})
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(metadata.call_count, 1)
        self.assertEqual(metadata.call_args.args[2], ["8"])
        self.assertEqual(metadata.call_args.kwargs["uuids"], {"8": "u8"})
        self.assertEqual(self.observations.call_args.kwargs["cohort"], {"project_ids": ["42"], "customer_ids": []})
        save_checkpoint.assert_not_called()
        self.assertTrue(checkpoint["done"])
        self.assertEqual(checkpoint["offset"], 2)

    def test_scoped_missing_upload_keeps_offset_and_later_file_receipt(self):
        from hero_integration.services import execution_budget, scoped_test_reads
        detail = {'uuid':'u8', 'id':8, 'is_deleted':False, 'file_upload_links':[],
                  'file_upload_matches':[{'project_match_id':42}], 'customer_document':None}
        cohort = {'version':2, 'scoped_project_reads':True, 'project_ids':['42'], 'customer_ids':[]}
        proof = ft.project_file_proof(detail, cohort)
        cohort['files'] = {'HeroDocuments':{'7':{**proof,'uuid':'u7'}, '8':proof}}
        # A missing download URL records a separate receipt for the second, present file.
        row = {'id':8,'uuid':'u8','filename':'demo.pdf','category':'offer','size':3,'modified':'1'}
        budget = Mock(version=3)
        with patch.object(scoped_test_reads, 'exact_rows', return_value=[row]) as metadata, \
                patch.object(execution_budget, 'current_budget', return_value=budget), \
                patch.object(ft.direct_runtime, 'run_progress', return_value=({},Mock())):
            state = ft.transfer('documents', client=_ListClient([row],{'u8':detail}), downloader=_NoDownload(),
                                source_instance='source-a', run_name='synthetic-full', cohort=cohort, record_limit=1)
        self.assertTrue(state['done'])
        self.assertEqual(state['offset'],2)
        self.assertEqual(state['testing_missing_file_ids'],['7'])
        self.assertEqual(state['testing_file_ids_found'],['8'])
        self.assertEqual(state['counts']['rows_read'],1)
        self.assertEqual(state['testing_records_received'],1)
        self.assertEqual(metadata.call_args.args[2],['7','8'])
        self.assertEqual(metadata.call_args.kwargs['uuids'],{'7':'u7','8':'u8'})
        self.assertCountEqual([call.args for call in budget.outcome.call_args_list],
                              [('file_upload','7','missing'),('file_upload','8','missing')])

    def test_invalid_file_is_reported_and_next_customer_only_file_is_imported(self):
        rows = [{'id':i,'uuid':str(i),'filename':'synthetic.pdf','size':3,'modified':'1',
                 'downloadUrl':URL,'category':'invoice'} for i in (7,8)]
        details = {str(i):{'uuid':str(i),'customer_document':{'project_match_id':p,'contact_id':22}}
                   for i,p in [(7,False),(8,0)]}
        downloader = ft.Downloader("login.hero-software.de", lambda:"fixture", retries=0,
                                   opener=_Opener(_Response(b'pdf')))
        with patch('hero_integration.services.integration_events.record_output') as event:
            state = self.run_transfer(rows,details,downloader=downloader)
        self.assertEqual((state['counts']['failed'],state['counts']['added']),(1,1))
        self.assertEqual(event.call_args.kwargs['source_id'],'7')
        self.assertEqual(self.observations.call_args.args[4]['id'],8)
        self.assertTrue(state['done'])

    def test_new_file_link_records_the_run_source(self):
        row = {"id": 7, "uuid": "u7", "filename": "Rechnung.pdf", "modified": "1", "size": 3,
               "downloadUrl": URL, "category": "invoice"}

        class _Download:
            def fetch(inner, url, target, size):
                with open(target, "wb") as handle:
                    handle.write(b"pdf")
                return 3, hashlib.md5(b'pdf').hexdigest()

        state = self.run_transfer([row], {"u7": {"uuid": "u7", "file_upload_matches": [{"project_match_id": 42}]}},
                                  instance="source-b", downloader=_Download())
        self.assertEqual(state["counts"]["added"], 1)
        link = next(row for row in self.db.committed["HERO Integration Link"].values() if row["source_id"] == "7")
        self.assertEqual(link["source_instance"], "source-b")
        file = next(row for name, row in self.db.committed["File"].items() if name != "F1")
        self.assertIsNone(file["attached_to_name"])
        observed = self.observations.call_args
        self.assertEqual(observed.args[:3], ('synthetic-run', 'source-b', 'HeroDocuments'))
        self.assertEqual(observed.args[5]['file_upload_matches'], [{'project_match_id': 42}])

    def test_known_files_of_another_source_are_not_reused(self):
        self.assertEqual(list(ft._known_files("source-a", ["5"])), ["5"])
        self.assertEqual(ft._known_files("source-b", ["5"]), {})

    def test_unchanged_unassigned_file_waits_even_when_its_project_exists(self):
        row = {"id": 5, "uuid": "u5", "filename": "Angebot.pdf", "modified": "1", "size": 3,
               "downloadUrl": URL, "category": "offer"}
        self.db.data["HERO Integration Link"]["L1"]["source_payload_hash"] = ft.source_hash(row)
        self.db.commit()
        with patch.object(ft, "_known_files", return_value={
                "5": SimpleNamespace(name="L1", target_name="F1", source_payload_hash=ft.source_hash(row),
                                     target_field_hashes=ft.field_hashes(ft.mapped_values(ft.direct_runtime.builtin_read("HeroDocuments"), row)))}):
            state = self.run_transfer([row], {"u5": {"uuid": "u5", "file_upload_matches": [{"project_match_id": 42}]}})
        self.assertEqual((state["counts"].get("relinked"), state["counts"].get("unchanged")), (None, 1))
        self.assertEqual((self.current_file()["attached_to_doctype"], self.current_file()["attached_to_name"],
                          self.current_file()["folder"]), (None, None, "Home/HERO/Dokumente/Ohne Zuordnung"))
        self.assertEqual(self.current_file()["file_url"], "/private/files/old.pdf")

    def test_unchanged_file_without_resolvable_target_stays_untouched(self):
        row = {"id": 5, "uuid": "u5", "filename": "Angebot.pdf", "modified": "1", "size": 3,
               "downloadUrl": URL, "category": "offer"}
        with patch.object(ft, "_known_files", return_value={
                "5": SimpleNamespace(name="L1", target_name="F1", source_payload_hash=ft.source_hash(row),
                                     target_field_hashes=ft.field_hashes(ft.mapped_values(ft.direct_runtime.builtin_read("HeroDocuments"), row)))}):
            state = self.run_transfer([row], {"u5": {"uuid": "u5", "file_upload_matches": [{"project_match_id": 99}]}})
        self.assertEqual(state["counts"].get("unchanged"), 1)
        self.assertIsNone(self.current_file()["attached_to_name"])

    def test_cursor_of_another_source_is_not_resumed(self):
        saved = {"source_instance": "source-b", "cursor": "c9", "counts": {"added": 4}}
        client = _ListClient([], {})
        calls = []
        client.query = lambda document, variables: calls.append(variables) or {
            "FileUpload_FileUploads": _page([], "end", False)}
        with patch.object(ft, "_state", return_value=saved), patch.object(ft, "_save_state"), \
                patch.object(ft, "allow_unlimited_attachments"), patch.object(ft, "_folder", return_value="Home/HERO"):
            state = ft.transfer("documents", source_instance="source-a", client=client, downloader=_NoDownload(), run_name="synthetic-run")
        self.assertNotIn("after", calls[0])
        self.assertEqual((state["source_instance"], state["counts"].get("added")), ("source-a", None))


if __name__ == "__main__":
    unittest.main()
