"""PDF authorization/protocol tests and optional real bounded-renderer checks."""
from __future__ import annotations

import base64
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


APP = Path(os.environ.get("PROJECT_WORKSPACE_APP", Path(__file__).parents[1]))
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Denied(Exception):
    pass


class Stale(Exception):
    pass


class Busy(Exception):
    pass


class Doc:
    def __init__(self, name, **fields):
        self.name, self.fields = name, fields
        self.allowed, self.protected = True, set()

    def get(self, name):
        return self.fields.get(name)

    def check_permission(self, permission):
        assert permission == "read"
        if not self.allowed:
            raise Denied()

    def apply_fieldlevel_read_permissions(self):
        for key in self.protected:
            self.fields.pop(key, None)


class PDFBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.project = Doc("P1")
        self.file = Doc("F1", attached_to_doctype="Project", attached_to_name="P1", file_type="PDF",
            file_url="/private/files/synthetic.pdf", modified="v1", file_size=100)
        self.docs = {("Project", "P1"): self.project, ("File", "F1"): self.file}
        self.exposed = []
        self.fake = NS(session=NS(user="reader@example.invalid"),
            local=NS(site="dev.example.invalid", response_headers={}),
            PermissionError=Denied, TimestampMismatchError=Stale,
            _=lambda text: text, get_doc=Mock(side_effect=lambda doctype, name: self.docs[doctype, name]),
            throw=lambda message, exception=ValueError: (_ for _ in ()).throw(exception(message)),
            whitelist=lambda **options: self.exposed.append(options) or (lambda fn: fn),
            cache=NS(lock=Mock(return_value=nullcontext())))
        with patch.dict(sys.modules, {"frappe": self.fake}):
            workspace = load("pdf_test_workspace", APP / "services/project_workspace.py")
        with patch.dict(sys.modules, {"frappe": self.fake,
                "hero_integration.services.project_workspace": workspace,
                "redis.exceptions": NS(LockError=Busy)}):
            self.api = load("pdf_api_test", APP / "services/project_pdf_preview.py")
        self.reader_patch = patch.object(self.api, "_read_bytes", return_value=b"%PDF-synthetic")
        self.renderer_patch = patch.object(self.api, "_render", return_value={"page": 1, "page_count": 2,
            "data_url": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        self.reader, self.renderer = self.reader_patch.start(), self.renderer_patch.start()
        self.addCleanup(self.reader_patch.stop)
        self.addCleanup(self.renderer_patch.stop)

    def page(self, **kwargs):
        return self.api.get_page("P1", "F1", "v1", **kwargs)

    def test_authenticated_get_only_and_private_response(self):
        self.assertEqual(self.exposed[-1], {"methods": ["GET"]})
        self.assertEqual(self.page()["page"], 1)
        self.assertEqual(self.fake.local.response_headers["Cache-Control"], "private, no-store")
        self.fake.cache.lock.assert_called_once_with(
            "project-pdf-preview:dev.example.invalid", timeout=12, blocking=False)

    def test_guest_and_document_denials_precede_file_io_and_rendering(self):
        for denied in ("guest", "project", "file"):
            self.fake.session.user = "Guest" if denied == "guest" else "reader"
            self.project.allowed = denied != "project"
            self.file.allowed = denied != "file"
            with self.subTest(denied=denied), self.assertRaises(Denied):
                self.page()
        self.reader.assert_not_called()
        self.renderer.assert_not_called()

    def test_other_parent_or_masked_attachment_link_is_denied(self):
        for key, value in [("attached_to_name", "OTHER"), ("attached_to_doctype", "Customer")]:
            original = self.file.fields[key]
            self.file.fields[key] = value
            with self.assertRaises(Denied):
                self.page()
            self.file.fields[key] = original
        self.file.protected.add("attached_to_name")
        with self.assertRaises(Denied):
            self.page()
        self.reader.assert_not_called()

    def test_stale_selection_is_refused_before_loading_bytes(self):
        self.file.fields["modified"] = "v2"
        with self.assertRaises(Stale):
            self.page()
        self.reader.assert_not_called()

    def test_page_inputs_are_bounded_before_source_access(self):
        for value in (0, -1, 201, True, "1.5", "９", "1234", ""):
            with self.subTest(page=value), self.assertRaises(ValueError):
                self.page(page=value)
        self.fake.get_doc.assert_not_called()

    def test_active_formats_remote_urls_and_traversal_are_not_loaded(self):
        for field, value in [("file_type", "HTML"), ("file_url", "https://external.invalid/a.pdf"),
                ("file_url", "/files/%2e%2e/secret"), ("file_url", "javascript:alert(1)")]:
            original = self.file.fields[field]
            self.file.fields[field] = value
            with self.assertRaises(ValueError):
                self.page()
            self.file.fields[field] = original
        self.reader.assert_not_called()

    def test_metadata_size_limit_precedes_file_io(self):
        self.file.fields["file_size"] = self.api.MAX_INPUT_BYTES + 1
        with self.assertRaisesRegex(ValueError, "too large"):
            self.page()
        self.reader.assert_not_called()

    def test_busy_and_parser_failures_are_controlled_without_raw_errors(self):
        self.fake.cache.lock.side_effect = Busy()
        with self.assertRaisesRegex(ValueError, "Another PDF"):
            self.page()
        self.fake.cache.lock.side_effect = None
        self.renderer.side_effect = subprocess.TimeoutExpired("secret parser command", 8)
        with self.assertRaisesRegex(ValueError, "^PDF preview unavailable"):
            self.page()

    def test_disk_reader_checks_resolved_site_containment_and_actual_size(self):
        self.reader_patch.stop()
        directory = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="pdf-boundary-", dir=directory) as temporary:
            root = Path(temporary)
            base = root / "private/files"
            base.mkdir(parents=True)
            path = base / "synthetic.pdf"
            path.write_bytes(b"%PDF-synthetic")
            self.fake.get_site_path = lambda *args: str(root.joinpath(*args))
            self.file.get_full_path = lambda: str(path)
            self.assertEqual(self.api._read_bytes(self.file), b"%PDF-synthetic")
            outside = root / "outside.pdf"
            outside.write_bytes(b"private unrelated bytes")
            self.file.get_full_path = lambda: str(outside)
            with self.assertRaises(ValueError):
                self.api._read_bytes(self.file)
            self.file.get_full_path = lambda: str(path)
            with path.open("wb") as stream:
                stream.truncate(self.api.MAX_INPUT_BYTES + 1)
            with self.assertRaises(ValueError):
                self.api._read_bytes(self.file)

    def test_worker_protocol_accepts_only_bounded_png_and_expected_page(self):
        self.renderer_patch.stop()
        value = {"page": 1, "page_count": 2, "png": base64.b64encode(PNG).decode()}
        with patch.object(self.api.subprocess, "run", return_value=NS(stdout=json.dumps(value).encode())) as call:
            result = self.api._render(b"%PDF", 1)
            self.assertTrue(result["data_url"].startswith("data:image/png;base64,"))
            self.assertEqual(call.call_args.kwargs["timeout"], 8)
            self.assertEqual(call.call_args.args[0][1:3], ["-I", "-B"])
            self.assertEqual(call.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(set(call.call_args.kwargs["env"]), {"PATH", "LANG", "LC_ALL"})
            for invalid in [[], {**value, "page": 2}, {**value, "page_count": 201},
                    {**value, "png": base64.b64encode(b"<svg onload=bad()>").decode()}]:
                call.return_value = NS(stdout=json.dumps(invalid).encode())
                with self.assertRaises(ValueError):
                    self.api._render(b"%PDF", 1)


def synthetic_pdf():
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for word in ("ONE", "TWO"):
        content = ("BT /F1 24 Tf 40 300 Td (SYNTHETIC PAGE " + word + ") Tj ET").encode()
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(b"xref\n0 8\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(b"trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(data)


@unittest.skipUnless(importlib.util.find_spec("pypdfium2"), "PDFium is verified separately in the candidate runtime")
class NativeRendererTests(unittest.TestCase):
    def run_worker(self, data, page):
        return subprocess.run([sys.executable, "-I", "-B", str(APP / "services/project_pdf_renderer.py"), str(page)],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})

    def test_real_two_page_pdf_renders_distinct_valid_pngs(self):
        from io import BytesIO
        from PIL import Image

        pages = []
        for page in (1, 2):
            result = self.run_worker(synthetic_pdf(), page)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            value = json.loads(result.stdout)
            self.assertEqual((value["page"], value["page_count"]), (page, 2))
            png = base64.b64decode(value["png"])
            with Image.open(BytesIO(png)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertLessEqual(image.width * image.height, 2_000_000)
                self.assertLess(min(image.convert("L").getextrema()), 250)
            pages.append(png)
        self.assertNotEqual(pages[0], pages[1])

    def test_invalid_pdf_and_out_of_range_page_return_no_document_or_diagnostics(self):
        for data, page in [(b"<html>not a PDF</html>", 1), (synthetic_pdf(), 3)]:
            result = self.run_worker(data, page)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
