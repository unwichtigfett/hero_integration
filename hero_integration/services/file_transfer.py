"""Transfer HERO files into private Frappe Files: documents first, then images.

Read-only towards HERO. Each file is streamed to disk in 1 MiB chunks and
never held in memory, then attached to its bound Project (or Customer).
One HERO Integration Link per HERO file makes reruns skip unchanged files
and replace regenerated ones. A run stops on its time budget and continues
from the saved cursor in a new background job.

Usage (bench execute):
    hero_integration.services.file_transfer.preview --kwargs "{'phase': 'documents'}"
    hero_integration.services.file_transfer.start --kwargs "{'phase': 'documents'}"
    hero_integration.services.file_transfer.status
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import frappe

PHASES = ("documents", "images")
SOURCE_TYPE = "file_upload"
MAPPING_VERSION = "file-transfer-v1"
LIST_PAGE = 1000  # HERO answers 1000 small file rows in well under a second
DETAIL_BATCH = 50  # HERO rejects larger uuid lists ("Too many UUIDs given")
CHUNK = 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024
TIME_BUDGET_SECONDS = 50 * 60
STATE_KEY = "hero_file_transfer:{}"
LOCK_NAME = "hero_file_transfer"
ROOT_FOLDER = "HERO"
PHASE_FOLDER = {"documents": "Dokumente", "images": "Bilder"}
UNASSIGNED_FOLDER = "Ohne Zuordnung"
DELETED_DOCUMENT_STATUS = 1000

LIST_QUERY = (
    "query HeroFileList($first: Int!, $after: String, $filters: FileUpload_FileUploadFiltersInput) { "
    "FileUpload_FileUploads(first: $first, after: $after, filters: $filters, sortings: [ID_ASC]) { "
    "edges { node { id uuid filename category imageCategory section type size downloadUrl created modified } } "
    "pageInfo { endCursor hasNextPage } } }"
)
DETAIL_QUERY = (
    "query HeroFileDetails($uuids: [String!], $first: Int) { file_uploads(uuids: $uuids, first: $first) { "
    "id uuid is_deleted file_upload_matches { project_match_id } "
    "file_upload_links { target target_id customer_id project_match_id } "
    "customer_document { id nr status_code project_match_id contact_id localized_type document_type { name } } } }"
)


class TransferError(RuntimeError):
    pass


# ---------------------------------------------------------------- download

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Downloader:
    """Stream one HERO file to disk; the API token only goes to the API host."""

    host: str
    token_provider: object
    timeout: int = 60
    max_bytes: int = MAX_FILE_BYTES
    retries: int = 2
    opener: object = field(default=None)

    def __post_init__(self):
        if self.opener is None:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                _NoRedirect(),
            )

    def check_url(self, url: str) -> str:
        parts = urlsplit(url or "")
        if parts.scheme != "https" or (parts.hostname or "").lower() != self.host or not parts.path.startswith("/files/"):
            raise TransferError("HERO download URL is not on the HERO file path.")
        if parts.username or parts.password or parts.port not in (None, 443):
            raise TransferError("HERO download URL is not on the HERO file path.")
        return url

    def fetch(self, url: str, target: str, expected_size: int | None = None) -> tuple[int, str]:
        """Write ``url`` to ``target``; returns (bytes, md5). Retries 429/5xx/transport."""
        request = urllib.request.Request(self.check_url(url), headers={"Authorization": f"Bearer {self.token_provider()}"})
        if expected_size and expected_size > self.max_bytes:
            raise TransferError(f"File is larger than {self.max_bytes // 2**20} MB.")
        for attempt in range(self.retries + 1):
            try:
                return self._stream(request, target)
            except urllib.error.HTTPError as error:
                retry = error.code == 429 or error.code >= 500
                reason = f"HTTP {error.code}"
            except (urllib.error.URLError, OSError) as error:
                retry, reason = True, type(error).__name__
            _remove(target)
            if not retry or attempt == self.retries:
                raise TransferError(f"HERO download failed ({reason}).")
            time.sleep(2 ** (attempt + 1))
        raise TransferError("HERO download failed.")

    def _stream(self, request, target: str) -> tuple[int, str]:
        digest, size = hashlib.md5(usedforsecurity=False), 0
        with self.opener.open(request, timeout=self.timeout) as response, open(target, "wb") as out:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                raise TransferError(f"File is larger than {self.max_bytes // 2**20} MB.")
            while chunk := response.read(CHUNK):
                size += len(chunk)
                if size > self.max_bytes:
                    raise TransferError(f"File is larger than {self.max_bytes // 2**20} MB.")
                digest.update(chunk)
                out.write(chunk)
        if declared and declared.isdigit() and int(declared) != size:
            _remove(target)
            raise OSError("Download ended before Content-Length.")
        return size, digest.hexdigest()


# ---------------------------------------------------------------- listing

def list_pages(client, phase: str, after: str | None = None):
    """Yield (rows, end_cursor) per HERO page; documents = every category except images."""
    if phase not in PHASES:
        raise TransferError(f"Unknown phase {phase!r}; use one of {PHASES}.")
    filters = {"category": {"equals": "images"}} if phase == "images" else None
    while True:
        variables = {"first": LIST_PAGE, "filters": filters}
        if after:
            variables["after"] = after  # HERO answers an explicit null cursor with an internal error
        data = client.query(LIST_QUERY, variables)
        page = (data or {}).get("FileUpload_FileUploads") or {}
        rows = [edge["node"] for edge in page.get("edges") or []]
        if phase == "documents":
            rows = [row for row in rows if row.get("category") != "images"]
        info = page.get("pageInfo") or {}
        after = info.get("endCursor") or after
        yield rows, after
        if not info.get("hasNextPage"):
            return


def details(client, rows: list[dict]) -> dict[str, dict]:
    """Project/customer/document data per uuid, fetched in batches."""
    found = {}
    for start in range(0, len(rows), DETAIL_BATCH):
        uuids = [row["uuid"] for row in rows[start:start + DETAIL_BATCH]]
        data = client.query(DETAIL_QUERY, {"uuids": uuids, "first": len(uuids)})
        for item in (data or {}).get("file_uploads") or []:
            found[item.get("uuid")] = item
    return found


def resolve_target(detail: dict, projects: dict, customers: dict) -> tuple[str | None, str | None]:
    document = (detail or {}).get("customer_document") or {}
    links = (detail or {}).get("file_upload_links") or []
    project_ids = [m.get("project_match_id") for m in (detail or {}).get("file_upload_matches") or []]
    project_ids += [document.get("project_match_id")] + [link.get("project_match_id") for link in links]
    for source_id in project_ids:
        if source_id and str(source_id) in projects:
            return "Project", projects[str(source_id)]
    for source_id in [document.get("contact_id")] + [link.get("customer_id") for link in links]:
        if source_id and str(source_id) in customers:
            return "Customer", customers[str(source_id)]
    return None, None


def skip_reason(row: dict, detail: dict | None) -> str | None:
    if detail is None:
        return "no_detail"
    if detail.get("is_deleted"):
        return "deleted"
    document = detail.get("customer_document") or {}
    if document.get("status_code") == DELETED_DOCUMENT_STATUS:
        return "deleted"
    if not row.get("downloadUrl"):
        return "no_url"
    return None


def source_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps([row.get("uuid"), row.get("modified"), row.get("size")]).encode()).hexdigest()


# ---------------------------------------------------------------- Frappe side

def _bindings(source_type: str, doctype: str) -> dict[str, str]:
    return dict(frappe.db.sql("""SELECT source_id, target_name FROM `tabHERO Integration Link`
        WHERE source_type=%s AND target_doctype=%s AND IFNULL(target_name, '') != ''""", (source_type, doctype)))


def _known_files() -> dict[str, dict]:
    rows = frappe.get_all("HERO Integration Link", filters={"source_type": SOURCE_TYPE},
                          fields=["name", "source_id", "target_name", "source_payload_hash"], limit_page_length=0)
    return {row.source_id: row for row in rows}


def _folder(*parts: str) -> str:
    parent = "Home"
    for part in parts:
        name = f"{parent}/{part}"
        if not frappe.db.exists("File", name):
            from frappe.core.api.file import create_new_folder

            create_new_folder(part, parent)
        parent = name
    return parent


def safe_name(filename: str) -> str:
    base = re.sub(r"[^\w.\-]+", "_", os.path.basename(filename or "datei"), flags=re.UNICODE).strip("._") or "datei"
    stem, ext = os.path.splitext(base)
    return stem[:120] + ext[:12]


def _disk_path(file_url: str) -> str:
    return frappe.get_site_path("private", "files", file_url.rsplit("/", 1)[-1])


def store(row: dict, size: int, md5: str, temp: str, target: tuple, folder: str, existing: dict | None) -> str:
    """Move the downloaded blob into place and create or update the File and its link."""
    file_url = f"/private/files/hero-{row['id']}-{int(time.time())}-{safe_name(row.get('filename'))}"
    os.replace(temp, _disk_path(file_url))
    try:
        return _record(row, size, md5, file_url, target, folder, existing)
    except Exception:
        _remove(_disk_path(file_url))  # no File points at the new blob
        raise


def _record(row, size, md5, file_url, target, folder, existing) -> str:
    doctype, name = target
    if existing and frappe.db.exists("File", existing.target_name):
        old_url = frappe.db.get_value("File", existing.target_name, "file_url")
        frappe.db.set_value("File", existing.target_name, {"file_url": file_url, "file_size": size, "content_hash": md5,
                                                           "file_name": row.get("filename")})
        if old_url and old_url != file_url and not frappe.db.exists("File", {"file_url": old_url}):
            _remove(_disk_path(old_url))
        file_name = existing.target_name
    else:
        doc = frappe.get_doc({"doctype": "File", "file_name": row.get("filename") or safe_name(""), "file_url": file_url,
                              "is_private": 1, "folder": folder, "file_size": size, "content_hash": md5,
                              "attached_to_doctype": doctype, "attached_to_name": name})
        doc.flags.copy_from_existing_file = True  # blob is already on disk; skip reading it into memory
        doc.flags.ignore_duplicate_entry_error = True
        doc.insert(ignore_permissions=True)
        file_name = doc.name
    created = (row.get("created") or "").replace("T", " ")[:19]
    if created:
        frappe.db.set_value("File", file_name, {"creation": created}, update_modified=False)
    values = {"sync_status": "Synchronized", "target_name": file_name, "source_payload_hash": source_hash(row),
              "target_payload_hash": md5, "last_synced_on": frappe.utils.now_datetime()}
    if existing:
        frappe.db.set_value("HERO Integration Link", existing.name, values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": str(row["id"]), "source_instance": "default", "target_doctype": "File",
                        "mapping_version": MAPPING_VERSION, **values}).insert(ignore_permissions=True)
    return file_name


def allow_unlimited_attachments(doctypes=("Project", "Customer")) -> None:
    """HERO projects carry hundreds of files; ERPNext limits Project to 4 attachments."""
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    for doctype in doctypes:
        if frappe.get_meta(doctype).max_attachments:
            make_property_setter(doctype, None, "max_attachments", 0, "Int", for_doctype=True,
                                 validate_fields_for_doctype=False)
    frappe.db.commit()


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- run

def _state(phase: str) -> dict:
    raw = frappe.db.get_default(STATE_KEY.format(phase))
    return json.loads(raw) if raw else {}


def _save_state(phase: str, state: dict) -> None:
    state["updated"] = str(frappe.utils.now_datetime())
    frappe.db.set_default(STATE_KEY.format(phase), json.dumps(state))
    frappe.db.commit()


def _client_and_downloader():
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    client = HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)
    return client, Downloader(config.endpoint.host, lambda: config.api_token)


def reset_phase(phase: str) -> None:
    """Forget cursor and counts so the next transfer starts a fresh pass."""
    frappe.db.set_default(STATE_KEY.format(phase), "")
    frappe.db.commit()


def transfer(phase: str, *, client=None, downloader=None, dry_run: bool = False,
             time_budget: int = TIME_BUDGET_SECONDS, min_free: int = MIN_FREE_BYTES, should_stop=None) -> dict:
    """Process one phase from its saved cursor until done or the time budget ends."""
    if client is None or downloader is None:
        client, downloader = _client_and_downloader()
    state = {} if dry_run else _state(phase)
    if state.get("done"):
        state = {}  # a finished phase starts over; unchanged files are skipped by their link
    counts = state.setdefault("counts", {})
    projects, customers = _bindings("project", "Project"), _bindings("customer", "Customer")
    known = _known_files()
    if not dry_run:
        allow_unlimited_attachments()
    folders = {} if dry_run else {
        True: _folder(ROOT_FOLDER, PHASE_FOLDER[phase]), False: _folder(ROOT_FOLDER, PHASE_FOLDER[phase], UNASSIGNED_FOLDER)}
    partial_dir = frappe.get_site_path("private", "files", ".hero-partial")
    os.makedirs(partial_dir, exist_ok=True)
    deadline = time.monotonic() + time_budget
    state.update(phase=phase, done=False, last_error=None, stopped=False)

    def bump(key, amount=1):
        counts[key] = counts.get(key, 0) + amount

    for rows, cursor in list_pages(client, phase, state.get("cursor")):
        bump("requests")
        todo = []
        for row in rows:
            link = known.get(str(row["id"]))
            if link and link.source_payload_hash == source_hash(row):
                bump("unchanged")
            else:
                todo.append((row, link))
        found = details(client, [row for row, _ in todo]) if todo else {}
        bump("requests", -(-len(todo) // DETAIL_BATCH))
        for index, (row, link) in enumerate(todo):
            if should_stop and index % 25 == 0 and should_stop():
                state["stopped"] = True
                _save_state(phase, state)  # cursor still points at this page; its finished files are skipped later
                return state
            detail = found.get(row["uuid"])
            reason = skip_reason(row, detail)
            if reason:
                bump(f"skipped_{reason}")
                continue
            target = resolve_target(detail, projects, customers)
            bump("bytes_planned", int(row.get("size") or 0))
            if dry_run:
                bump("would_update" if link else "would_add")
                bump(f"target_{(target[0] or 'none').lower()}")
                continue
            if shutil.disk_usage(partial_dir).free - int(row.get("size") or 0) < min_free:
                state["last_error"] = "Not enough free disk space; transfer paused."
                _save_state(phase, state)
                return state
            temp = os.path.join(partial_dir, f"{row['id']}.part")
            try:
                size, md5 = downloader.fetch(row["downloadUrl"], temp, row.get("size"))
                store(row, size, md5, temp, target, folders[bool(target[0])], link)
                frappe.db.commit()
                bump("updated" if link else "added")
                bump("bytes", size)
            except Exception as error:
                frappe.db.rollback()
                _remove(temp)
                bump("failed")
                frappe.log_error(title="HERO file transfer failed",
                                 message=f"HERO file {row.get('id')} ({row.get('filename')}): {error}")
        state["cursor"] = cursor
        if not dry_run:
            _save_state(phase, state)
        if time.monotonic() > deadline:
            return state
    state.update(done=True, cursor=None)
    if not dry_run:
        _save_state(phase, state)
    return state


@frappe.whitelist()
def preview(phase: str = "documents") -> dict:
    """Count what a transfer would do; reads HERO, writes nothing."""
    frappe.only_for("System Manager")
    return transfer(phase, dry_run=True, time_budget=24 * 3600)


@frappe.whitelist()
def start(phase: str = "documents") -> str:
    frappe.only_for("System Manager")
    if phase not in PHASES:
        frappe.throw(f"Unknown phase {phase!r}.")
    return _enqueue(phase)


def _enqueue(phase: str) -> str:
    job_id = f"hero_file_transfer_{phase}_{int(time.time())}"
    frappe.enqueue("hero_integration.services.file_transfer.run_job", queue="long",
                   timeout=TIME_BUDGET_SECONDS + 600, phase=phase, job_id=job_id, enqueue_after_commit=True)
    return job_id


@frappe.whitelist()
def status() -> dict:
    frappe.only_for("System Manager")
    return {phase: _state(phase) for phase in PHASES}


def run_job(phase: str) -> None:
    """Background job: one time-boxed slice, then queue the next slice."""
    from frappe.utils.synchronization import filelock

    with filelock(LOCK_NAME, timeout=5):
        state = transfer(phase)
    if not state.get("done") and not state.get("last_error"):
        _enqueue(phase)
