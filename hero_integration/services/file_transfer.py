"""Transfer HERO files into private Frappe Files: documents first, then images.

Read-only towards HERO. Each file is streamed to disk in 1 MiB chunks and
never held in memory, then attached to its bound Project (or Customer).
One HERO Integration Link per HERO file makes reruns skip unchanged files
and replace regenerated ones. A run stops on its time budget and continues
from the saved offset in a new background job.

Runs are started as Document or Picture Migration (services.file_migration),
which binds them to the confirmed HERO source. Usage (bench execute):
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

from hero_integration.services import direct_runtime

PHASES = ("documents", "images")
SOURCE_TYPE = "file_upload"
OPERATION = {"documents": "HeroDocuments", "images": "HeroPictures"}
DETAIL_BATCH = 50  # HERO rejects larger uuid lists ("Too many UUIDs given")
CHUNK = 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024
TIME_BUDGET_SECONDS = 50 * 60
STATE_KEY = "hero_file_transfer:{}"
ROOT_FOLDER = "HERO"
PHASE_FOLDER = {"documents": "Dokumente", "images": "Bilder"}
UNASSIGNED_FOLDER = "Ohne Zuordnung"
DELETED_DOCUMENT_STATUS = 1000

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

def _bindings(source_type: str, doctype: str, source_instance: str) -> dict[str, str]:
    """HERO id → Frappe name, only for links of this HERO source (ids repeat across sources)."""
    return dict(frappe.db.sql("""SELECT source_id, target_name FROM `tabHERO Integration Link`
        WHERE source_type=%s AND target_doctype=%s AND source_instance=%s AND IFNULL(target_name, '') != ''""",
                              (source_type, doctype, source_instance)))


def _known_files(source_instance: str) -> dict[str, dict]:
    rows = frappe.get_all("HERO Integration Link", filters={"source_type": SOURCE_TYPE, "source_instance": source_instance},
                          fields=["name", "source_id", "target_name", "source_payload_hash", "target_field_hashes"], limit_page_length=0)
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


@dataclass
class StoredFile:
    file_name: str
    file_url: str
    old_url: str | None = None


def commit_file(row: dict, size: int, md5: str, temp: str, target: tuple, folder: str, existing,
                source_instance: str, read=None) -> str:
    """Store and commit one file. Blobs are only removed once the database agrees:
    the replaced blob after a successful commit, the new blob after any failure."""
    stored = store(row, size, md5, temp, target, folder, existing, source_instance, read)
    try:
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        _discard_unreferenced(stored.file_url)
        raise
    if stored.old_url and stored.old_url != stored.file_url:
        _discard_unreferenced(stored.old_url)
    return stored.file_name


def store(row: dict, size: int, md5: str, temp: str, target: tuple, folder: str, existing, source_instance: str, read=None) -> StoredFile:
    """Move the downloaded blob into place and create or update the File and its link (not committed)."""
    operation = "HeroPictures" if row.get("category") == "images" else "HeroDocuments"
    read = read or direct_runtime.resolve_read(operation, source_instance)
    file_url = f"/private/files/hero-{row['id']}-{int(time.time())}-{safe_name(row.get('filename'))}"
    os.replace(temp, _disk_path(file_url))
    try:
        return _record(row, size, md5, file_url, target, folder, existing, source_instance, read)
    except Exception:
        frappe.db.rollback()  # the File row goes back to the previous blob, which is still on disk
        _remove(_disk_path(file_url))
        raise


def _discard_unreferenced(file_url: str) -> None:
    if file_url and not frappe.db.exists("File", {"file_url": file_url}):
        _remove(_disk_path(file_url))


def _attachment(target: tuple, folder: str) -> dict:
    doctype, name = target
    return {"attached_to_doctype": doctype, "attached_to_name": name, "folder": folder} if doctype else {}


def mapped_values(read, row: dict) -> dict:
    values = direct_runtime.record_values(read.plan, row, "File")
    values["file_name"] = values.get("file_name") or safe_name("")
    return values


def field_hashes(values: dict) -> str:
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return json.dumps({"fields": digest})


def _record(row, size, md5, file_url, target, folder, existing, source_instance, read=None) -> StoredFile:
    operation = "HeroPictures" if row.get("category") == "images" else "HeroDocuments"
    read = read or direct_runtime.resolve_read(operation, source_instance)
    fields = mapped_values(read, row)
    doctype, name = target
    old_url = None
    if existing and frappe.db.exists("File", existing.target_name):
        old_url = frappe.db.get_value("File", existing.target_name, "file_url")
        # The old blob stays until the commit succeeded (see commit_file).
        frappe.db.set_value("File", existing.target_name, {"file_url": file_url, "file_size": size, "content_hash": md5,
                                                           **fields, **_attachment(target, folder)})
        file_name = existing.target_name
    else:
        doc = frappe.get_doc({"doctype": "File", **fields, "file_url": file_url,
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
              "target_payload_hash": md5, "target_field_hashes": field_hashes(fields),
              "mapping_version": read.mapping_version, "last_synced_on": frappe.utils.now_datetime()}
    if existing:
        frappe.db.set_value("HERO Integration Link", existing.name, values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": direct_runtime.identity(read, row), "source_instance": source_instance, "target_doctype": "File",
                        **values}).insert(ignore_permissions=True)
    return StoredFile(file_name, file_url, old_url)


def relink(existing, target: tuple, folder: str) -> bool:
    """Attach an unchanged file to its now resolvable target; never detaches. Not committed."""
    wanted = _attachment(target, folder)
    if not wanted or not existing or not frappe.db.exists("File", existing.target_name):
        return False
    current = frappe.db.get_value("File", existing.target_name, list(wanted), as_dict=True) or {}
    if all((current.get(key) or "") == value for key, value in wanted.items()):
        return False
    frappe.db.set_value("File", existing.target_name, wanted)
    return True


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


def _client_and_downloader(config=None):
    """Client and downloader for ``config``; without one only for the confirmed source binding."""
    from hero_integration.client.graphql import HeroGraphQLClient

    if config is None:
        from hero_integration.services.file_migration import confirmed_configuration

        config = confirmed_configuration()
    client = HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)
    return client, Downloader(config.endpoint.host, lambda: config.api_token), config.source_instance


def reset_phase(phase: str) -> None:
    """Forget offset and counts so the next transfer starts a fresh pass."""
    frappe.db.set_default(STATE_KEY.format(phase), "")
    frappe.db.commit()


def transfer(phase: str, *, source_instance: str | None = None, client=None, downloader=None, dry_run: bool = False,
             time_budget: int = TIME_BUDGET_SECONDS, min_free: int = MIN_FREE_BYTES, should_stop=None,
             progress=None, record_limit=None, run_name=None) -> dict:
    """Process one phase from its saved offset until done or the time budget ends.

    ``source_instance`` is the HERO source the client reads from; every lookup,
    new link and the saved offset belong to it."""
    if client is None or downloader is None:
        client, downloader, configured = _client_and_downloader()
        if source_instance is not None and configured != source_instance:
            raise TransferError("The configured HERO source instance changed; queue a new run.")
        source_instance = configured
    if not isinstance(source_instance, str) or not source_instance.strip():
        raise TransferError("A HERO source instance is required.")
    if phase not in OPERATION:
        raise TransferError(f"Unknown phase {phase!r}; use one of {PHASES}.")
    operation = OPERATION[phase]
    read = direct_runtime.resolve_read(operation, source_instance)
    from hero_integration.services.migration_testing import bounded_rows, processed, test_state, save_test_state, validate_checkpoint
    state = {} if dry_run else test_state(run_name, operation) if record_limit is not None else _state(phase)
    save = (lambda value: save_test_state(run_name, operation, value)) if record_limit is not None else (lambda value: _save_state(phase, value))
    if record_limit is not None:
        validate_checkpoint(state, record_limit, source_instance=source_instance, mapping=read.mapping_version)
        if state.get('done'):
            return state
    if (state.get("done") or state.get("source_instance") != source_instance or "offset" not in state
            or state.get("mapping") != read.mapping_version):
        # An offset belongs to its approved mapping: changed filters or field
        # selections require every row to be reconsidered under the new version.
        state = {}  # unchanged content is skipped by its link, without another download
    state.pop("cursor", None)
    state.setdefault("offset", 0)
    state["source_instance"] = source_instance
    state["mapping"] = read.mapping_version
    counts = state.setdefault("counts", {})
    projects = _bindings("project", "Project", source_instance)
    customers = _bindings("customer", "Customer", source_instance)
    known = _known_files(source_instance)
    if not dry_run:
        allow_unlimited_attachments()
    folders = {} if dry_run else {
        True: _folder(ROOT_FOLDER, PHASE_FOLDER[phase]), False: _folder(ROOT_FOLDER, PHASE_FOLDER[phase], UNASSIGNED_FOLDER)}
    partial_dir = frappe.get_site_path("private", "files", ".hero-partial")
    if not dry_run:
        os.makedirs(partial_dir, exist_ok=True)
    deadline = time.monotonic() + time_budget
    state.update(phase=phase, done=False, last_error=None, stopped=False)

    def bump(key, amount=1):
        counts[key] = counts.get(key, 0) + amount

    while True:
        if record_limit is not None and processed(counts) >= record_limit:
            state.update(testing_limit_reached=True)
            break
        page = direct_runtime.fetch_page(client, read, state["offset"])
        bump("requests")
        if not page:
            break
        rows = bounded_rows(state, [row for row in page if direct_runtime.belongs(operation, row)], record_limit)
        # Unchanged content is not downloaded again, but its target may have
        # become resolvable since (e.g. projects imported after the documents).
        todo = [(row, known.get(direct_runtime.identity(read, row))) for row in rows]
        found = details(client, rows) if rows else {}
        bump("requests", -(-len(rows) // DETAIL_BATCH))
        for index, (row, link) in enumerate(todo):
            if should_stop and index % 25 == 0 and should_stop():
                state["stopped"] = True
                if not dry_run:
                    save(state)  # retry this page; committed files are skipped later
                return state
            detail = found.get(row["uuid"])
            unchanged = bool(link) and link.source_payload_hash == source_hash(row)
            unchanged = unchanged and bool(frappe.db.exists("File", link.target_name))
            reason = skip_reason(row, detail)
            if reason:
                bump("unchanged" if unchanged else f"skipped_{reason}")
                continue
            target = resolve_target(detail, projects, customers)
            if unchanged:
                try:
                    fields = mapped_values(read, row)
                    hashes = field_hashes(fields)
                    changed = getattr(link, "target_field_hashes", None) != hashes
                    if dry_run:
                        bump("would_update" if changed else "unchanged")
                        continue
                    if changed:
                        frappe.db.set_value("File", link.target_name, fields)
                        frappe.db.set_value("HERO Integration Link", link.name, {
                            "target_field_hashes": hashes, "mapping_version": read.mapping_version,
                            "last_synced_on": frappe.utils.now_datetime()})
                    moved = relink(link, target, folders[bool(target[0])])
                    frappe.db.commit()
                    if moved:
                        bump("relinked")
                    if changed or moved:
                        bump("updated")
                    else:
                        bump("unchanged")
                except Exception as error:
                    frappe.db.rollback()
                    bump("failed")
                    frappe.log_error(title="HERO file transfer failed",
                                     message=f"HERO file {row.get('id')} ({row.get('filename')}): {error}")
                continue
            bump("bytes_planned", int(row.get("size") or 0))
            if dry_run:
                mapped_values(read, row)
                bump("would_update" if link else "would_add")
                bump(f"target_{(target[0] or 'none').lower()}")
                continue
            if shutil.disk_usage(partial_dir).free - int(row.get("size") or 0) < min_free:
                state["last_error"] = "Not enough free disk space; transfer paused."
                save(state)
                return state
            temp = os.path.join(partial_dir, f"{row['id']}.part")
            try:
                size, md5 = downloader.fetch(row["downloadUrl"], temp, row.get("size"))
                commit_file(row, size, md5, temp, target, folders[bool(target[0])], link, source_instance, read)
                bump("updated" if link else "added")
                bump("bytes", size)
            except Exception as error:
                frappe.db.rollback()
                _remove(temp)
                bump("failed")
                frappe.log_error(title="HERO file transfer failed",
                                 message=f"HERO file {row.get('id')} ({row.get('filename')}): {error}")
        state["offset"] += len(page)
        if not dry_run:
            save(state)
            if progress:
                progress(counts)
        if len(page) < read.page_size:
            break
        if time.monotonic() > deadline:
            return state
    state.update(done=True)
    if not dry_run:
        save(state)
    return state


@frappe.whitelist()
def preview(phase: str = "documents") -> dict:
    """Count what a transfer would do; reads HERO, writes nothing."""
    frappe.only_for("System Manager")
    return transfer(phase, dry_run=True, time_budget=24 * 3600)


@frappe.whitelist()
def start(phase: str = "documents") -> str:
    """Start a Document or Picture Migration run; returns the run name.

    Delegates to the source-bound run path, so this entry point has no
    separate queue and no check of its own."""
    frappe.only_for("System Manager")
    if phase not in PHASES:
        frappe.throw(f"Unknown phase {phase!r}.")
    from hero_integration import api

    return api.sync_area_start({"documents": "documents", "images": "pictures"}[phase])["run"]


@frappe.whitelist()
def status() -> dict:
    frappe.only_for("System Manager")
    return {phase: _state(phase) for phase in PHASES}


def run_job(phase: str) -> None:
    """Retired background job. Jobs queued by an earlier version carry no
    source binding and therefore transfer nothing."""
    frappe.log_error(title="HERO file transfer job blocked",
                     message=f"A queued {phase!r} file transfer job has no recorded HERO source binding and was not run. "
                             "Start Document or Picture Migration on the Synchronization page instead.")
