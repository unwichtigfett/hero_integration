"""Permission-checked PDF page images, independent of browser PDF plug-ins."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import frappe
from redis.exceptions import LockError

from hero_integration.services.project_pdf_renderer import MAX_INPUT_BYTES, MAX_PAGES, MAX_PNG_BYTES
from hero_integration.services.project_workspace import _local_file_url, _read, _text


@frappe.whitelist(methods=["GET"])
def get_page(project: str, file: str, expected_modified: str, page: int | str = 1):
    """Read one authorized local PDF page; persist no previews or attachments."""
    if not getattr(frappe.session, "user", None) or frappe.session.user == "Guest":
        frappe.throw(frappe._("Log in to view project information."), frappe.PermissionError)
    headers = getattr(frappe.local, "response_headers", None)
    if headers is not None:
        headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    project, file = _text(project, 140), _text(file, 140)
    expected_modified = _text(expected_modified, 40)
    if (isinstance(page, bool) or len(str(page)) > 3 or not str(page).isascii()
            or not str(page).isdigit() or not 1 <= int(page) <= MAX_PAGES):
        frappe.throw(frappe._("Invalid PDF preview page."))
    _read("Project", project)
    document = _read("File", file)
    if document.get("attached_to_doctype") != "Project" or document.get("attached_to_name") != project:
        frappe.throw(frappe._("This document does not belong to the selected project."), frappe.PermissionError)
    if str(document.get("modified")) != expected_modified:
        frappe.throw(frappe._("The document changed. Refresh the document list before previewing it."),
            frappe.TimestampMismatchError)
    if str(document.get("file_type") or "").upper() != "PDF" or not _local_file_url(document.get("file_url")):
        frappe.throw(frappe._("Only local PDF attachments can be previewed."))
    if int(document.get("file_size") or 0) > MAX_INPUT_BYTES:
        frappe.throw(frappe._("This PDF is too large for the preview. Open the original file."))
    try:
        # One parser per site, no waiting queue. Authorization is checked before
        # acquiring the lock and before reading any file content.
        with frappe.cache.lock("project-pdf-preview:" + frappe.local.site, timeout=12, blocking=False):
            data = _read_bytes(document)
            return _render(data, int(page))
    except LockError:
        frappe.throw(frappe._("Another PDF preview is loading. Try again shortly."))
    except (OSError, ValueError, subprocess.SubprocessError):
        frappe.throw(frappe._("PDF preview unavailable. Open the original file."))


def _read_bytes(document):
    url = document.get("file_url")
    relative = ("private", "files") if url.startswith("/private/files/") else ("public", "files")
    base = Path(frappe.get_site_path(*relative)).resolve()
    path = Path(document.get_full_path()).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("outside_site_files")
    with path.open("rb") as source:
        data = source.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input_limit")
    return data


def _render(data, page):
    worker = Path(__file__).with_name("project_pdf_renderer.py")
    result = subprocess.run([sys.executable, "-I", "-B", str(worker), str(page)],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=True,
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    if len(result.stdout) > 6 * 1024 * 1024:
        raise ValueError("output_limit")
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or not isinstance(value.get("png"), str)
            or value.get("page") != page or type(value.get("page_count")) is not int
            or not page <= value["page_count"] <= MAX_PAGES):
        raise ValueError("invalid_page")
    png = base64.b64decode(value["png"], validate=True)
    if len(png) > MAX_PNG_BYTES or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid_image")
    return {"page": page, "page_count": value["page_count"],
        "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}
