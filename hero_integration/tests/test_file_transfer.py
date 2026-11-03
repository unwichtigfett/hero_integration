"""HERO file transfer: streaming download, URL guard, phase listing, targets."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import urllib.error
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


class _Client:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def query(self, document, variables):
        self.calls.append(variables)
        return {"FileUpload_FileUploads": self.pages[variables.get("after")]}


def _page(nodes, end, more):
    return {"edges": [{"node": n} for n in nodes], "pageInfo": {"endCursor": end, "hasNextPage": more}}


class TestListingAndTargets(unittest.TestCase):
    def test_documents_phase_drops_images_and_follows_cursor(self):
        client = _Client({None: _page([{"id": 1, "category": "invoice"}, {"id": 2, "category": "images"}], "c1", True),
                          "c1": _page([{"id": 3, "category": None}], "c2", False)})
        pages = list(ft.list_pages(client, "documents"))
        self.assertEqual([[r["id"] for r in rows] for rows, _ in pages], [[1], [3]])
        self.assertEqual(pages[-1][1], "c2")
        self.assertIsNone(client.calls[0]["filters"])
        self.assertNotIn("after", client.calls[0])

    def test_images_phase_filters_on_hero_side_and_resumes(self):
        client = _Client({"c5": _page([{"id": 9, "category": "images"}], "c6", False)})
        list(ft.list_pages(client, "images", "c5"))
        self.assertEqual(client.calls[0]["filters"], {"category": {"equals": "images"}})

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


if __name__ == "__main__":
    unittest.main()
