"""Optional synthetic Chromium DOM/layout test; never opens an ERPNext site."""
from __future__ import annotations

import base64
from functools import partial
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlsplit


APP = Path(__file__).parents[1]
CHROME = os.environ.get("CHROMIUM_BINARY")
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)


class Handler(BaseHTTPRequestHandler):
    original_pdf_requests = 0

    def do_GET(self):
        path_only = urlsplit(self.path).path
        if path_only == "/private/files/synthetic.pdf":
            type(self).original_pdf_requests += 1
        sources = {
            "/": ("text/html", APP / "tests/fixtures/project_workspace_browser.html"),
            "/workspace.js": ("application/javascript", APP / "public/js/project_workspace.js"),
            "/assets/hero_integration/css/project_workspace.css": ("text/css", APP / "public/css/project_workspace.css"),
        }
        if path_only == "/private/files/pixel.png":
            kind, data = "image/png", PIXEL
        elif path_only in sources:
            kind, path = sources[path_only]
            data = path.read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipUnless(CHROME, "Set CHROMIUM_BINARY for the optional offline browser contract")
class BrowserContractTests(unittest.TestCase):
    def test_desktop_and_narrow_layout_without_site_access(self):
        Handler.original_pdf_requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=partial(server.serve_forever, poll_interval=0.05), daemon=True)
        thread.start()
        try:
            for width in (1280, 500):
                with self.subTest(width=width), tempfile.TemporaryDirectory(
                    prefix="project-workspace-browser-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
                ) as profile:
                    args = [CHROME, "--headless", "--disable-gpu", "--disable-background-networking",
                        "--disable-component-update", "--no-first-run", "--no-default-browser-check",
                        "--user-data-dir=" + profile, "--window-size=" + str(width) + ",960",
                        "--virtual-time-budget=5000", "--dump-dom",
                        "http://127.0.0.1:" + str(server.server_port) + "/"]
                    if os.geteuid() == 0:
                        # Root Chromium cannot use its sandbox. This throwaway
                        # process receives synthetic localhost content only.
                        args.insert(1, "--no-sandbox")
                    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    match = re.search(r'<output id="result"[^>]*>(.*?)</output>', result.stdout, re.S)
                    self.assertIsNotNone(match, "Browser did not emit the synthetic contract result")
                    data = json.loads(unescape(match.group(1)))
                    self.assertGreaterEqual(len(data["checks"]), 20)
                    self.assertEqual([check["name"] for check in data["checks"] if not check["pass"]], [])
            self.assertEqual(Handler.original_pdf_requests, 0, "The browser must never embed/fetch the original PDF")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
