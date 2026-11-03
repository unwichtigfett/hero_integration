"""HERO file transfer: streaming download, URL guard, phase listing, targets."""

from __future__ import annotations

import copy
import io
import os
import shutil
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import file_transfer as ft


class _Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
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
        return all(row.get(key) == value for key, value in filters.items())

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
        source_type, doctype, instance = params
        return [(row["source_id"], row["target_name"]) for row in self.data["HERO Integration Link"].values()
                if row["source_type"] == source_type and row["target_doctype"] == doctype
                and row["source_instance"] == instance and row.get("target_name")]

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
    def insert(self, **kwargs):
        self.name = self._db.insert(self._values)
        return self


def _fake_frappe(db, root):
    return SimpleNamespace(
        db=db, flags=SimpleNamespace(), log_error=lambda **k: None,
        get_doc=lambda values: _Doc(_db=db, _values=values, flags=SimpleNamespace()),
        get_all=lambda doctype, filters, fields, limit_page_length=0: [
            SimpleNamespace(name=name, **{field: row.get(field) for field in fields if field != "name"})
            for name, row in db.data.get(doctype, {}).items() if db._match(row, filters)],
        get_site_path=lambda *parts: os.path.join(root, *parts[1:]),
        utils=SimpleNamespace(now_datetime=lambda: "now"))


class _FileFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        os.makedirs(os.path.join(self.root, "files"))
        with open(self.path("old.pdf"), "wb") as handle:
            handle.write(b"old")
        self.db = _TxDB({
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
        read_patch = patch.object(ft.direct_runtime, "resolve_read", side_effect=lambda op, source: ft.direct_runtime.builtin_read(op))
        read_patch.start()
        self.addCleanup(read_patch.stop)
        self.link = SimpleNamespace(name="L1", target_name="F1", source_payload_hash="old")

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
        return ft.commit_file(self.ROW, 3, "md5", self.download(), ("Project", "PROJECT-A"),
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
            return {"file_uploads": [self.detail_rows[uuid] for uuid in variables["uuids"]]}
        return {"FileUpload_FileUploads": _page(self.rows, "end", False)}


class _NoDownload:
    def fetch(self, *args):
        raise AssertionError("unchanged content must not be downloaded")


class TestSourceAndTargetBinding(_FileFixture):
    def run_transfer(self, rows, details, instance="source-a", downloader=None):
        with patch.object(ft, "_state", return_value={}), patch.object(ft, "_save_state"), \
                patch.object(ft, "allow_unlimited_attachments"), \
                patch.object(ft, "_folder", side_effect=lambda *parts: "Home/" + "/".join(parts)):
            return ft.transfer("documents", source_instance=instance, client=_ListClient(rows, details),
                               downloader=downloader or _NoDownload(), min_free=0)

    def test_project_ids_resolve_only_within_the_run_source(self):
        projects = ft._bindings("project", "Project", "source-a")
        self.assertEqual(projects, {"42": "PROJECT-A"})
        self.assertEqual(ft._bindings("project", "Project", "source-b"), {"42": "PROJECT-B"})
        self.assertEqual(ft.resolve_target({"file_upload_matches": [{"project_match_id": 42}]}, projects, {}),
                         ("Project", "PROJECT-A"))

    def test_new_file_link_records_the_run_source(self):
        row = {"id": 7, "uuid": "u7", "filename": "Rechnung.pdf", "modified": "1", "size": 3,
               "downloadUrl": URL, "category": "invoice"}

        class _Download:
            def fetch(inner, url, target, size):
                with open(target, "wb") as handle:
                    handle.write(b"pdf")
                return 3, "md5"

        state = self.run_transfer([row], {"u7": {"uuid": "u7", "file_upload_matches": [{"project_match_id": 42}]}},
                                  instance="source-b", downloader=_Download())
        self.assertEqual(state["counts"]["added"], 1)
        link = next(row for row in self.db.committed["HERO Integration Link"].values() if row["source_id"] == "7")
        self.assertEqual(link["source_instance"], "source-b")
        file = next(row for name, row in self.db.committed["File"].items() if name != "F1")
        self.assertEqual(file["attached_to_name"], "PROJECT-B")

    def test_known_files_of_another_source_are_not_reused(self):
        self.assertEqual(list(ft._known_files("source-a")), ["5"])
        self.assertEqual(ft._known_files("source-b"), {})

    def test_unchanged_unassigned_file_is_attached_once_its_project_exists(self):
        row = {"id": 5, "uuid": "u5", "filename": "Angebot.pdf", "modified": "1", "size": 3,
               "downloadUrl": URL, "category": "offer"}
        self.db.data["HERO Integration Link"]["L1"]["source_payload_hash"] = ft.source_hash(row)
        self.db.commit()
        with patch.object(ft, "_known_files", return_value={
                "5": SimpleNamespace(name="L1", target_name="F1", source_payload_hash=ft.source_hash(row),
                                     target_field_hashes=ft.field_hashes(ft.mapped_values(ft.direct_runtime.builtin_read("HeroDocuments"), row)))}):
            state = self.run_transfer([row], {"u5": {"uuid": "u5", "file_upload_matches": [{"project_match_id": 42}]}})
        self.assertEqual((state["counts"].get("relinked"), state["counts"].get("unchanged")), (1, None))
        self.assertEqual((self.current_file()["attached_to_doctype"], self.current_file()["attached_to_name"],
                          self.current_file()["folder"]), ("Project", "PROJECT-A", "Home/HERO/Dokumente"))
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
            state = ft.transfer("documents", source_instance="source-a", client=client, downloader=_NoDownload())
        self.assertNotIn("after", calls[0])
        self.assertEqual((state["source_instance"], state["counts"].get("added")), ("source-a", None))


if __name__ == "__main__":
    unittest.main()
