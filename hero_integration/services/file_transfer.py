"""Transfer HERO files into private Frappe Files: documents first, then images.

Read-only towards HERO. Each file is streamed to disk in 1 MiB chunks and
never held in memory, then attached to its bound Project (or Customer).
One HERO Integration Link per HERO file makes reruns skip unchanged files
and replace regenerated ones. A run stops on its time budget and continues
from the saved offset in a new background job.

Runs are started as Document or Picture Migration (services.file_migration),
which binds them to the confirmed HERO source. Usage (bench execute):
    hero_integration.services.file_transfer.preview --kwargs "{'phase': 'documents'}"
    hero_integration.api.sync_area_start --kwargs "{'area': 'documents'}"
    hero_integration.services.file_transfer.status
"""

from __future__ import annotations

import hashlib
from http.client import IncompleteRead
import json
import logging
import mimetypes
import os
import re
import shutil
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4

import frappe
from hero_integration.services import migration_archive
from hero_integration.services.binding_registry import BindingConflict, RETIRED_LINK_SYSTEM
from hero_integration.services.file_migration import DirectContractChanged

from hero_integration.services import direct_runtime

PHASES = ("documents", "images")
SOURCE_TYPE = "file_upload"
OPERATION = {"documents": "HeroDocuments", "images": "HeroPictures"}
DETAIL_BATCH = 50  # HERO rejects larger uuid lists ("Too many UUIDs given")
CHUNK = 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 5_000_000_000
TIME_BUDGET_SECONDS = 50 * 60
STATE_KEY = "hero_file_transfer:{}"
ROOT_FOLDER = "HERO"
PHASE_FOLDER = {"documents": "Dokumente", "images": "Bilder"}
UNASSIGNED_FOLDER = "Ohne Zuordnung"
DELETED_DOCUMENT_STATUS = 1000

DETAIL_PATHS = (
    'id', 'uuid', 'is_deleted', 'file_upload_matches.project_match_id',
    'file_upload_links.target', 'file_upload_links.target_id', 'file_upload_links.customer_id',
    'file_upload_links.project_match_id', 'customer_document.id', 'customer_document.nr',
    'customer_document.status_code', 'customer_document.project_match_id', 'customer_document.contact_id',
    'customer_document.localized_type', 'customer_document.document_type.name',
)


RELATION_PATHS = ('id','uuid','is_deleted','file_upload_matches.project_match_id',
                  'file_upload_links.customer_id','file_upload_links.project_match_id',
                  'customer_document.status_code','customer_document.project_match_id','customer_document.contact_id')


class TransferError(RuntimeError):
    pass


class FileRelationshipError(TransferError):
    """One identified file has unusable relationship evidence."""


class FileConflict(TransferError):
    pass


# ---------------------------------------------------------------- download

def _byte_count(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    if type(value) is int and value >= 0:
        return value
    return None


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

    def fetch(self, url: str, target: str, expected_size: int | str | None = None, *,
              image_id: str | None = None) -> tuple[int, str]:
        """Write ``url`` to ``target``; returns (bytes, md5). Retries 429/5xx/transport."""
        request = urllib.request.Request(self.check_url(url), headers={"Authorization": f"Bearer {self.token_provider()}"})
        expected = _byte_count(expected_size)
        if image_id is None and expected is not None and expected > self.max_bytes:
            raise TransferError(f"File is larger than {self.max_bytes // 2**20} MB.")
        for attempt in range(self.retries + 1):
            try:
                size, digest = self._stream(request, target, image=image_id is not None)
                if image_id is not None:
                    _validate_image(target)
                    if expected is not None and size != expected:
                        logging.getLogger(__name__).warning(
                            "HERO image %s: received %d bytes; HERO metadata declares %d.", image_id, size, expected)
                    return size, digest
                if expected is None or size == expected:
                    return size, digest
                retry, reason = True, "size differs from HERO metadata"
            except urllib.error.HTTPError as error:
                retry = error.code == 429 or error.code >= 500
                reason = f"HTTP {error.code}"
            except (urllib.error.URLError, OSError, IncompleteRead) as error:
                retry, reason = True, type(error).__name__
            except TransferError:
                _remove(target)
                raise
            _remove(target)
            if not retry or attempt == self.retries:
                raise TransferError(f"HERO download failed ({reason}).")
            time.sleep(2 ** (attempt + 1))
        raise TransferError("HERO download failed.")

    def _stream(self, request, target: str, *, image=False) -> tuple[int, str]:
        digest, size = hashlib.md5(usedforsecurity=False), 0
        with self.opener.open(request, timeout=self.timeout) as response, open(target, "wb") as out:
            declared = _byte_count(response.headers.get("Content-Length"))
            if image and (response.status != 200 or response.headers.get("Content-Range") is not None
                          or (response.headers.get("Content-Length") is not None and declared is None)):
                raise TransferError("HERO did not return a complete image response.")
            if declared is not None and declared > self.max_bytes:
                raise TransferError(f"File is larger than {self.max_bytes // 2**20} MB.")
            while chunk := response.read(CHUNK):
                size += len(chunk)
                if size > self.max_bytes:
                    raise TransferError(f"File is larger than {self.max_bytes // 2**20} MB.")
                digest.update(chunk)
                out.write(chunk)
        if declared is not None and declared != size:
            raise OSError("Download ended before Content-Length.")
        return size, digest.hexdigest()


def _validate_image(path):
    from PIL import Image, ImageSequence

    try:
        with Image.open(path) as image:
            image.verify()
        # verify() checks the container; decoding also catches truncated JPEG data.
        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                frame.load()
    except (OSError, SyntaxError, ValueError, EOFError, Image.DecompressionBombError) as error:
        raise TransferError("HERO returned invalid or incomplete image data.") from error


# ---------------------------------------------------------------- listing

def _detail_request(read, source_instance, uuids, *, paths=DETAIL_PATHS):
    """Compile file relationship reads against the area's approved source snapshot."""
    from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft

    try:
        validated = direct_runtime.validated_snapshot(read, source_instance, frappe_module=frappe)
    except ValueError as error:
        raise TransferError(str(error)) from error
    return QueryAdapter().materialize(validated, QueryDraft('HeroFileDetails', 'file_uploads', 'file_uploads',
        paths, {'uuids': uuids, 'first': len(uuids)}))


def details(client, rows: list[dict], read, source_instance: str, *, allow_missing=False, paths=None) -> dict[str, dict]:
    """Project/customer/document data per uuid, fetched in batches."""
    found = {}
    requested = set()
    for start in range(0, len(rows), DETAIL_BATCH):
        batch = rows[start:start + DETAIL_BATCH]
        expected = {row.get("uuid"): row.get("id") for row in batch}
        if (len(expected) != len(batch) or any(not isinstance(key, str) or not key or
                type(value) not in (int, str) or not str(value) for key, value in expected.items())
                or requested & expected.keys()):
            raise TransferError("File detail request contains missing or duplicate identities.")
        requested.update(expected)
        uuids = list(expected)
        request = _detail_request(read, source_instance, uuids, **({'paths':paths} if paths is not None else {}))
        data = client.query(request.document, request.variables)
        items = data.get("file_uploads") if isinstance(data, dict) else None
        if not isinstance(items, list) or (not allow_missing and len(items) != len(expected)):
            raise TransferError("File detail response is missing requested identities.")
        for item in items:
            key = item.get("uuid") if isinstance(item, dict) else None
            if (not isinstance(key, str) or key not in expected or key in found
                    or type(item.get("id")) not in (int, str) or str(item["id"]) != str(expected[key])):
                raise TransferError("File detail response does not match the requested identities.")
            if migration_archive.active() and "is_deleted" in (DETAIL_PATHS if paths is None else paths) and "is_deleted" not in item:
                raise TransferError("HERO file detail omitted the requested archive flag.")
            found[key] = item
    return found


def resolve_target(detail: dict, projects: dict, customers: dict) -> tuple[str | None, str | None]:
    projects_for_file, customers_for_file, _ = file_references(detail or {})
    for source_id in projects_for_file:
        if source_id in projects:
            return "Project", projects[source_id]
    for source_id in customers_for_file:
        if source_id in customers:
            return "Customer", customers[source_id]
    return None, None


def file_references(detail, *, complete=False):
    """Decode the selected nested GraphQL Int fields. HERO also uses zero for no link."""
    def identity(row, field):
        if complete and field not in row:
            raise FileRelationshipError('File relationship detail omits a requested identity.')
        value = row.get(field)
        if value is None or type(value) is int and value == 0:
            return None
        if type(value) is not int or value < 0:
            raise FileRelationshipError('File detail returned an invalid project or customer identity.')
        return str(value)

    names = ('file_upload_matches', 'file_upload_links', 'customer_document')
    if not isinstance(detail, dict) or complete and any(key not in detail for key in names):
        raise FileRelationshipError('File detail omits the relationships required for the related test sample.')
    matches, links, document = (detail.get(key) for key in names)
    for rows in (matches, links):
        if rows is not None and (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)):
            raise FileRelationshipError('File relationship details have an invalid shape.')
    if document is not None and not isinstance(document, dict):
        raise FileRelationshipError('File relationship details have an invalid shape.')
    pairs = ([(identity(document, 'project_match_id'), identity(document, 'contact_id'))]
             if document is not None else [])
    pairs.extend((identity(row, 'project_match_id'), identity(row, 'customer_id')) for row in links or ())
    projects = [identity(row, 'project_match_id') for row in matches or ()] + [p for p, _ in pairs]
    return [p for p in projects if p], [c for _, c in pairs if c], pairs


def belongs_to_test_cohort(detail: dict, cohort: dict) -> bool:
    projects, customers, _ = file_references(detail, complete=True)
    return bool(set(projects).intersection(cohort['project_ids']) or set(customers).intersection(cohort['customer_ids']))


def project_file_proof(detail, cohort):
    """A customer's other projects may share its files. Require a direct project edge."""
    project_refs, customer_refs, pairs = file_references(detail, complete=True)
    projects, customers = set(project_refs), set(customer_refs)
    document = detail['customer_document'] or {}
    selected_projects = projects.intersection(cohort['project_ids'])
    # Only a customer co-identified on an explicit selected project relation
    # can extend the cohort. A customer on another link may serve another project.
    selected_customers = {str(customer) for project, customer in pairs
                          if customer and str(project) in selected_projects}
    return {'projects': sorted(selected_projects), 'customers': sorted(selected_customers),
            'ambiguous_customer': not projects and bool(customers.intersection(cohort['customer_ids'])),
            'uuid': detail['uuid'], 'deleted': detail.get('is_deleted') is True
            or document.get('status_code') == DELETED_DOCUMENT_STATUS}


def skip_reason(row: dict, detail: dict | None) -> str | None:
    if detail is None:
        return "no_detail"
    if detail.get("is_deleted") is True and not migration_archive.includes_archived():
        return "skipped_archived" if migration_archive.active() else "deleted"
    document = detail.get("customer_document") or {}
    if document.get("status_code") == DELETED_DOCUMENT_STATUS:
        return "deleted"
    if not row.get("downloadUrl"):
        return "no_url"
    return None


def source_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps([row.get("uuid"), row.get("modified"), row.get("size")]).encode()).hexdigest()


# ---------------------------------------------------------------- Frappe side

def _bindings(source_type: str, doctype: str, source_instance: str, *, source_ids=None) -> dict[str, str]:
    """Only uniquely bound HERO identities are eligible as attachment targets."""
    if source_ids is not None:
        ids = sorted(set(source_ids))
        result = {}
        for start in range(0, len(ids), 256):
            rows = frappe.db.sql("""SELECT source_id, MIN(target_name) FROM `tabHERO Integration Link`
                WHERE source_type=%s AND source_instance=%s AND source_system='HERO' AND source_id IN %s
                GROUP BY source_id HAVING COUNT(*)=1 AND MIN(target_doctype)=%s AND MIN(target_name)!=''
                AND MIN(sync_status) IN ('Synchronized','Conflict')""",
                (source_type, source_instance, tuple(ids[start:start+256]), doctype))
            result.update(rows)
        return result
    rows = frappe.db.sql("""SELECT source_id, target_doctype, target_name, sync_status FROM `tabHERO Integration Link`
        WHERE source_type=%s AND source_instance=%s AND source_system='HERO'""", (source_type, source_instance))
    grouped = {}
    for identity, target_type, target, status in rows:
        grouped.setdefault(identity, []).append((target_type, target, status))
    return {identity: bindings[0][1] for identity, bindings in grouped.items()
            if len(bindings) == 1 and bindings[0][0] == doctype and bindings[0][1]
            and bindings[0][2] in ('Synchronized', 'Conflict')}


def scoped_file_target(detail, instance, scope):
    from hero_integration.services.deferred_references import file_dependencies
    dependencies = file_dependencies(detail, scope)
    projects = _bindings('project', 'Project', instance, source_ids=[i for k,i in dependencies if k == 'project'])
    customers = _bindings('customer', 'Customer', instance, source_ids=[i for k,i in dependencies if k == 'customer'])
    return resolve_target(detail, projects, customers)


def _known_files(source_instance, identities):
    return direct_runtime.page_links(frappe, source_instance, SOURCE_TYPE, identities, [
        "target_name", "source_payload_hash", "target_field_hashes", "target_payload_hash", "mapping_version", "sync_status"])


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
    has_field_conflict: bool = False


def commit_file(row: dict, size: int, md5: str, temp: str, target: tuple, folder: str, existing,
                source_instance: str, read=None, *, run_name=None, report_outcome=None) -> str:
    """Store and commit one file. Blobs are only removed once the database agrees:
    the replaced blob after a successful commit, the new blob after any failure."""
    stored = store(row, size, md5, temp, target, folder, existing, source_instance, read, run_name=run_name)
    try:
        from hero_integration.services.execution_budget import current_budget
        if budget := current_budget():
            budget.outcome(SOURCE_TYPE, row['id'], 'conflict' if stored.has_field_conflict else 'processed')
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        _discard_unreferenced(stored.file_url)
        raise
    if stored.old_url and stored.old_url != stored.file_url:
        _discard_unreferenced(stored.old_url)
    if report_outcome:
        report_outcome("conflict" if stored.has_field_conflict else "updated" if existing else "added")
    return stored.file_name


def store(row: dict, size: int, md5: str, temp: str, target: tuple, folder: str, existing, source_instance: str, read=None, *, run_name=None, caller_owns_transaction=False) -> StoredFile:
    """Move the downloaded blob into place and create or update the File and its link (not committed)."""
    operation = "HeroPictures" if row.get("category") == "images" else "HeroDocuments"
    read = read or direct_runtime.resolve_read(operation, source_instance)
    file_url = f"/private/files/hero-{row['id']}-{uuid4().hex}-{safe_name(row.get('filename'))}"
    stored = None
    def write():
        nonlocal stored
        _assert_target_baseline(existing, mapped_values(read, row), lock=True)
        os.replace(temp, _disk_path(file_url))
        stored = _record(row, size, md5, file_url, target, folder, existing, source_instance, read)
        return stored

    try:
        from hero_integration.services.execution_runtime import process_migration_record
        if read.validate_write:
            read.validate_write()
        outcome = process_migration_record(read.plan, row, source_instance, SOURCE_TYPE, write,
            evidence=run_name or "file-storage", successful=lambda result: isinstance(result, StoredFile))
        if stored is None:
            raise FileConflict("The source lifecycle does not authorize storing this file.")
        stored.has_field_conflict = outcome == "conflict"
        return stored
    except Exception:
        if not caller_owns_transaction:
            frappe.db.rollback()  # The previous blob remains available until commit.
        _remove(temp)
        _remove(_disk_path(file_url))
        raise


def inventory_blob_present(target_name):
    url = frappe.db.get_value("File", target_name, "file_url") or ""
    return url.startswith("/private/files/") and os.path.isfile(_disk_path(url))


def _restore_inventory_blob(link, fields, staged):
    """Restore only the exact previously acknowledged bytes at their established URL."""
    if staged is None:
        return False
    size, md5, path = staged
    document = frappe.get_doc("File", link.target_name, for_update=True)
    if size != document.file_size or md5 != link.target_payload_hash:
        raise FileConflict("Downloaded content differs from the verified file baseline.")
    _assert_target_baseline(link, fields, lock=True, replacement=path)
    # Both paths are on the site's private filesystem. Do not overwrite a
    # concurrently restored file. The ordinary metadata guard rechecks it.
    try:
        os.link(path, _disk_path(document.file_url))
    except FileExistsError:
        pass
    return True


def prepare_inventory_blobs(read, rows, instance, configuration, cleanup, *, allow_new):
    """Stage changed bytes before acquiring target locks. The page owns their lifetime."""
    from tempfile import TemporaryDirectory
    from hero_integration.services.deferred_references import require_storage

    require_storage(frappe)
    phase = "images" if read.operation == "HeroPictures" else "documents"
    if phase == "images":
        from hero_integration.services.file_migration import pictures_enabled
        if not pictures_enabled():
            raise TransferError("Picture migration is disabled for this site.")
    known = _known_files(instance, [read.plan.source_identity(row) for row in rows]) if rows else {}
    partial_dir = None
    downloader = None
    prepared = {}
    for row in rows:
        identity = read.plan.source_identity(row)
        link = known.get(identity)
        if skip_reason(row, row.get("_hero_file_detail")) or not link and not allow_new:
            continue
        if link and (link.sync_status != "Synchronized" or link.source_payload_hash == source_hash(row)
                     and inventory_blob_present(link.target_name)):
            continue
        if partial_dir is None:
            directory = frappe.get_site_path("private", "files", ".hero-partial")
            os.makedirs(directory, exist_ok=True)
            partial_dir = cleanup.enter_context(TemporaryDirectory(dir=directory, prefix="inventory-"))
            _, downloader, configured = _client_and_downloader(configuration(), token_provider=lambda: configuration().api_token)
            if configured != instance:
                raise TransferError("The configured HERO source instance changed; queue a new run.")
        planned = _byte_count(row.get("size")) or 0
        if shutil.disk_usage(partial_dir).free - (planned if phase == "documents" else 0) < MIN_FREE_BYTES:
            raise TransferError("Not enough free disk space; transfer paused.")
        path = os.path.join(partial_dir, uuid4().hex)
        size, md5 = downloader.fetch(row["downloadUrl"], path, row.get("size"),
            **({"image_id": identity} if phase == "images" else {}))
        if shutil.disk_usage(partial_dir).free < MIN_FREE_BYTES:
            raise TransferError("Not enough free disk space; transfer paused.")
        prepared[identity] = size, md5, path
    return prepared


def update_bound_metadata(read, link, fields, target, folder):
    """Refresh the native File and its verified metadata baseline in the caller's transaction."""
    _assert_target_baseline(link, fields, lock=True)
    baseline = json.loads(frappe.db.get_value("HERO Integration Link", link.name, "target_field_hashes"))
    changed = baseline.get("fields") != json.loads(field_hashes(fields))["fields"]
    moved = relink(link, target, folder)
    if changed:
        _save_file(link.target_name, fields, fields, link.name)
    if changed or moved:
        frappe.db.set_value("HERO Integration Link", link.name, {
            "target_field_hashes": _target_baseline(link.target_name, fields, link),
            "mapping_version": read.mapping_version, "sync_status": "Synchronized",
            "conflict_detail": "", "last_synced_on": frappe.utils.now_datetime()})
    return ("updated" if changed or moved else "unchanged"), moved


def apply_inventory_file(read, row, run, instance, staged):
    """Store a single private file and its existing deferred attachment in the page transaction."""
    from functools import partial
    from hero_integration.services import deferred_references as deferred
    from hero_integration.services.sync_engine import SyncStatus
    from hero_integration.services.migration_replay import count_result
    from hero_integration.services.migration import Result
    from hero_integration.services.integration_events import record_output

    identity = read.plan.source_identity(row)
    detail = row["_hero_file_detail"]
    from hero_integration.services.execution_runtime import locked_provenance
    source_link = locked_provenance(instance, SOURCE_TYPE, identity, "File")
    link = frappe.get_doc("HERO Integration Link", source_link.name, for_update=True) if source_link else None
    phase = "images" if read.operation == "HeroPictures" else "documents"
    folder = "/".join(("Home", ROOT_FOLDER, PHASE_FOLDER[phase], UNASSIGNED_FOLDER))
    reason = skip_reason(row, detail)
    outcome = "CONVERGED"
    if reason == "deleted":
        if link and detail["is_deleted"] is True:
            direct_runtime.process_record(read, row, instance, SOURCE_TYPE, run.name, lambda: "unchanged")
        deferred.invalidate(instance, read.operation, identity)
        outcome = "SKIPPED_SOURCE_DELETED"
    elif reason:
        return SyncStatus.PARTIAL
    else:
        file_references(detail, complete=True)
        if link and _legacy_fields_digest(link.target_field_hashes) is not None:
            target = scoped_file_target(detail, instance, None)
            target_folder = folder if not target[0] else "/".join(("Home", ROOT_FOLDER, PHASE_FOLDER[phase]))
            def verify_legacy():
                return reconcile_legacy_file(link, read, row, target, target_folder, instance)
            if direct_runtime.process_record(read, row, instance, SOURCE_TYPE, run.name, verify_legacy) == "conflict":
                return SyncStatus.CONFLICT
            link = frappe.get_doc("HERO Integration Link", link.name, for_update=True)
        unchanged = link and link.source_payload_hash == source_hash(row) and frappe.db.exists("File", link.target_name)
        if unchanged:
            fields = mapped_values(read, row)
            if not inventory_blob_present(link.target_name) and not _restore_inventory_blob(link, fields, staged):
                return SyncStatus.PARTIAL
            def update():
                return update_bound_metadata(read, link, fields, (None, None), folder)[0]
            status = direct_runtime.process_record(read, row, instance, SOURCE_TYPE, run.name, update)
            if status == "conflict":
                return SyncStatus.CONFLICT
            outcome = "UPDATED" if status == "updated" else "CONVERGED"
        elif staged is None:
            return SyncStatus.PARTIAL
        else:
            folder = _folder(ROOT_FOLDER, PHASE_FOLDER[phase], UNASSIGNED_FOLDER)
            size, md5, path = staged
            stored = store(row, size, md5, path, (None, None), folder, link, instance, read, run_name=run.name,
                           caller_owns_transaction=True)
            frappe.db.after_rollback.add(partial(_remove, _disk_path(stored.file_url)))
            if stored.old_url and stored.old_url != stored.file_url:
                frappe.db.after_commit.add(partial(_discard_unreferenced, stored.old_url))
            outcome = "CONFLICT" if stored.has_field_conflict else "UPDATED" if link else "CREATED"
        if link and not deferred.file_dependencies(detail) and frappe.db.get_value("File", link.target_name, "attached_to_name"):
            # The source removed every relationship. Without an approved detach
            # contract the native attachment requires independent review.
            return SyncStatus.PARTIAL
        deferred.remember_file(run.name, instance, read.operation, read,
            {key: value for key, value in row.items() if key != "downloadUrl"}, detail)
    count_result(run, Result(outcome, ""))
    record_output(run=run.name, source_type=SOURCE_TYPE, source_id=identity, result=outcome, action="hero_to_frappe")
    return SyncStatus.CONFLICT if outcome == "CONFLICT" else True


def _discard_unreferenced(file_url: str) -> None:
    if file_url and not frappe.db.exists("File", {"file_url": file_url}):
        _remove(_disk_path(file_url))


def _attachment(target: tuple, folder: str) -> dict:
    doctype, name = target
    return {"attached_to_doctype": doctype, "attached_to_name": name, "folder": folder} if doctype else {}


def _mapped_source_values(read, row: dict) -> dict:
    values = direct_runtime.record_values(read.plan, row, "File")
    from hero_integration.services.source_history import enabled, file_values
    if enabled(read, 'file_details'):
        detail = row.get('_hero_file_detail')
        if not isinstance(detail, dict) or detail.get('uuid') != row.get('uuid') or str(detail.get('id')) != str(row.get('id')):
            raise TransferError('Missing verified HERO file metadata.')
        values.update(file_values(detail))
    values["file_name"] = values.get("file_name") or safe_name("")
    return values


def _native_file_type(filename):
    # Same native coercion as Frappe File.set_file_type; MIME strings are not
    # the extension labels used by File.file_type and its image filters.
    mime = mimetypes.guess_type(filename)[0]
    extension = mimetypes.guess_extension(mime) if mime else None
    return extension.lstrip('.').upper() if extension else None


def mapped_values(read, row: dict) -> dict:
    values = _mapped_source_values(read, row)
    if 'file_type' in values and (native := _native_file_type(values['file_name'])):
        values['file_type'] = native
    return values


def field_hashes(values: dict) -> str:
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return json.dumps({"fields": digest})


FILE_CONTEXT = ('file_url', 'file_size', 'content_hash', 'is_private', 'folder', 'creation',
                'attached_to_doctype', 'attached_to_name', 'attached_to_field')


def value_hash(value):
    from hero_integration.services.change_detection import check_value
    return hashlib.sha256(json.dumps(check_value(value), sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _target_hash(name, fields, *, document=None, replacement=None):
    names = sorted(set(fields) | set(FILE_CONTEXT))
    values = ({field: getattr(document, field, None) for field in names} if document is not None
              else frappe.db.get_value('File', name, names, as_dict=True))
    if not values or not (values.get('file_url') or '').startswith('/private/files/'):
        raise FileConflict('The imported file target requires review before synchronization.')
    try:
        digest = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        path = _disk_path(values["file_url"])
        if replacement and not os.path.lexists(path):
            path = replacement
        with open(path, 'rb') as stream:
            while chunk := stream.read(CHUNK):
                digest.update(chunk)
                md5.update(chunk)
    except OSError as error:
        raise FileConflict('The imported file content is unavailable and requires review.') from error
    return {**{field: value_hash(value) for field, value in values.items()},
            '_blob_sha256': digest.hexdigest(), '_blob_md5': md5.hexdigest()}


def _target_baseline(name, fields, existing=None):
    from hero_integration.services.execution_runtime import field_decisions
    current = {**json.loads(field_hashes(fields)), 'version': 2,
               'mapped_fields': sorted(fields), 'target': _target_hash(name, fields)}
    held = {key for key, action in field_decisions('File', name).items() if action in (None, 'local')}
    if held and existing:
        prior = json.loads(frappe.db.get_value('HERO Integration Link', existing.name, 'target_field_hashes'))
        current['fields'] = prior['fields']
        for field in held & prior['target'].keys():
            current['target'][field] = prior['target'][field]
    return json.dumps(current)


def _legacy_fields_digest(raw):
    try:
        data = json.loads(raw or '{}')
    except (ValueError, TypeError):
        return None
    if (not isinstance(data, dict) or data.get('version') not in (None, 1)
            or set(data) - {'fields', 'version'} or not isinstance(data.get('fields'), str)
            or not re.fullmatch('[0-9a-f]{64}', data['fields'])):
        return None
    return data['fields']


def reconcile_legacy_file(existing, read, row, target, folder, source_instance, *, dry_run=False):
    """Upgrade only proven unchanged legacy files; never replace their blobs."""
    if not existing or _legacy_fields_digest(getattr(existing, 'target_field_hashes', None)) is None:
        return None
    from hero_integration.services.conflict_report import RECHECK_PREFIX
    link = frappe.get_doc('HERO Integration Link', existing.name, for_update=not dry_run)
    digest = _legacy_fields_digest(getattr(link, 'target_field_hashes', None))
    if digest is None:
        return None  # Another worker may already have established a v2 baseline.
    reason = (getattr(link, 'conflict_detail', '') or '').removeprefix(RECHECK_PREFIX)
    permitted = {'The imported file has no verified target baseline and requires review.',
                 'The imported file target requires review before synchronization.'}
    if (link.sync_status not in {'Synchronized', 'Conflict'}
            or link.sync_status == 'Conflict' and reason not in permitted):
        return None
    identity = direct_runtime.identity(read, row)
    if (link.source_system != 'HERO' or link.source_type != SOURCE_TYPE or link.source_instance != source_instance
            or str(link.source_id) != identity or link.target_doctype != 'File' or link.target_name != existing.target_name):
        raise FileConflict('The imported file target requires review before synchronization.')
    source_values = _mapped_source_values(read, row)
    if link.source_payload_hash != source_hash(row) or digest != json.loads(field_hashes(source_values))['fields']:
        raise FileConflict('The legacy file source or mapping changed; review is required before updating.')
    document = frappe.get_doc('File', link.target_name, for_update=not dry_run)
    bindings = frappe.get_all('HERO Integration Link', filters={'source_system': ['!=', RETIRED_LINK_SYSTEM],
        'target_doctype': 'File', 'target_name': link.target_name},
                             fields=['name'], limit_page_length=2)
    if len(bindings) != 1 or bindings[0].name != existing.name:
        raise FileConflict('The imported file target requires review before synchronization.')
    if (not getattr(document, 'is_private', 0) or
            not (document.file_url or '').startswith('/private/files/hero-' + identity + '-')):
        raise FileConflict('The imported file target requires review before synchronization.')
    if (document.folder != folder or (document.attached_to_doctype or None) != (target[0] or None)
            or (document.attached_to_name or None) != (target[1] or None)
            or getattr(document, 'attached_to_field', None)):
        raise FileConflict('The legacy file attachment or folder requires review before establishing a baseline.')
    fields = mapped_values(read, row)
    repairs = {}
    for name, expected in fields.items():
        current = getattr(document, name, None)
        if value_hash(current) == value_hash(expected):
            continue
        if name == 'file_type' and expected == _native_file_type(document.file_name) and value_hash(current) == value_hash(source_values[name]):
            repairs[name] = expected  # Repair only the old importer's proven MIME value.
            continue
        raise FileConflict('The imported file was changed locally and requires review.')
    current = _target_hash(link.target_name, fields, document=document)
    if (current['_blob_md5'] != link.target_payload_hash or document.content_hash != link.target_payload_hash
            or document.file_size != row.get('size')):
        raise FileConflict('The imported file was changed locally and requires review.')
    created = (row.get('created') or '').replace('T', ' ')[:19]
    if created and value_hash(document.creation) != value_hash(created):
        raise FileConflict('The imported file was changed locally and requires review.')
    if dry_run:
        return 'would_update'
    if repairs:
        document = _save_file(link.target_name, repairs, repairs, existing.name)
    baseline = {**json.loads(field_hashes(fields)), 'version': 2, 'mapped_fields': sorted(fields),
                'target': _target_hash(link.target_name, fields, document=document),
                'legacy': {'fields': digest, 'mapping_version': getattr(link, 'mapping_version', '')}}
    frappe.db.set_value('HERO Integration Link', existing.name, {'target_field_hashes': json.dumps(baseline),
        'mapping_version': read.mapping_version, 'sync_status': 'Synchronized', 'conflict_detail': '',
        'last_synced_on': frappe.utils.now_datetime()})
    return 'updated' if repairs else 'unchanged'


def _assert_target_baseline(existing, fields, *, lock=False, replacement=None):
    if not existing:
        return
    if not frappe.db.exists('File', existing.target_name):
        raise FileConflict('The imported file target requires review before synchronization.')
    target = None
    if lock:
        link = frappe.get_doc('HERO Integration Link', existing.name, for_update=True)
        if link.target_name != existing.target_name:
            raise FileConflict('The imported file target requires review before synchronization.')
        target = frappe.get_doc('File', existing.target_name, for_update=True)
        raw = getattr(link, 'target_field_hashes', None)
        status, content_hash = getattr(link, 'sync_status', None), getattr(link, 'target_payload_hash', None)
    else:
        raw = frappe.db.get_value('HERO Integration Link', existing.name, 'target_field_hashes')
        status = frappe.db.get_value('HERO Integration Link', existing.name, 'sync_status')
        content_hash = frappe.db.get_value('HERO Integration Link', existing.name, 'target_payload_hash')
    if status in {'Conflict', 'Pending Review', 'Rejected', 'Source Deleted'}:
        raise FileConflict('The imported file target requires review before synchronization.')
    try:
        baseline = json.loads(raw or '{}')
        names = baseline.get('mapped_fields')
        valid = (baseline.get('version') == 2 and isinstance(names, list) and bool(names)
                 and all(isinstance(name, str) for name in names) and len(set(names)) == len(names)
                 and isinstance(baseline.get('target'), dict))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise FileConflict('The imported file has no verified target baseline and requires review.')
    current_hashes = (_target_hash(existing.target_name, names, document=target, replacement=replacement) if replacement
                      else _target_hash(existing.target_name, names, document=target))
    from hero_integration.services.execution_runtime import field_decisions
    bound = field_decisions('File', existing.target_name).keys() - set(FILE_CONTEXT)
    prior = baseline['target']
    if (set(current_hashes) != set(prior) or current_hashes['_blob_md5'] != content_hash
            or any(current_hashes[key] != prior[key] for key in current_hashes.keys() - bound)):
        raise FileConflict('The imported file was changed locally and requires review.')
    additions = set(fields) - set(names)
    if additions:
        current = ({name: getattr(target, name, None) for name in additions} if target is not None
                   else frappe.db.get_value('File', existing.target_name, sorted(additions), as_dict=True) or {})
        if any(current.get(name) not in (None, '', fields[name]) for name in additions):
            raise FileConflict('A newly mapped file field already contains a local value and requires review.')


def _record_conflict(existing, reason, *, dry_run=False):
    if not dry_run:
        frappe.db.rollback()
        if not existing:
            frappe.log_error(title="HERO file binding requires review", message=str(reason))
            return
        status = frappe.db.get_value('HERO Integration Link', existing.name, 'sync_status')
        values = {'conflict_detail': str(reason)}
        if status not in {'Pending Review', 'Rejected', 'Source Deleted'}:
            values['sync_status'] = 'Conflict'
        frappe.db.set_value('HERO Integration Link', existing.name, values)
        frappe.db.commit()


def _record(row, size, md5, file_url, target, folder, existing, source_instance, read=None) -> StoredFile:
    operation = "HeroPictures" if row.get("category") == "images" else "HeroDocuments"
    read = read or direct_runtime.resolve_read(operation, source_instance)
    fields = mapped_values(read, row)
    doctype, name = target
    old_url = None
    created = (row.get("created") or "").replace("T", " ")[:19]
    metadata = {"creation": created} if created else {}
    if existing:
        _assert_target_baseline(existing, fields, lock=True)
        old_url = frappe.db.get_value("File", existing.target_name, "file_url")
        # The old blob stays until the commit succeeded (see commit_file).
        _save_file(existing.target_name, {"file_url": file_url, "file_size": size, "content_hash": md5,
                                          **fields, **_attachment(target, folder)}, fields, existing.name)
        file_name = existing.target_name
    else:
        doc = frappe.get_doc({"doctype": "File", **fields, **metadata, "file_url": file_url,
                              "is_private": 1, "folder": folder, "file_size": size, "content_hash": md5,
                              "attached_to_doctype": doctype, "attached_to_name": name})
        from hero_integration.services.project_image_categories import prepare_import_category
        doc.set("file_type", _native_file_type(doc.file_name))
        prepare_import_category(doc, fields)
        doc.flags.copy_from_existing_file = True  # blob is already on disk; skip reading it into memory
        doc.flags.ignore_duplicate_entry_error = True
        doc.insert(ignore_permissions=True)
        file_name = doc.name
    values = {"sync_status": "Synchronized", "target_name": file_name, "source_payload_hash": source_hash(row),
              "target_payload_hash": md5, "target_field_hashes": _target_baseline(file_name, fields, existing),
              "mapping_version": read.mapping_version, "last_synced_on": frappe.utils.now_datetime()}
    if existing:
        frappe.db.set_value("HERO Integration Link", existing.name, values)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                        "source_id": direct_runtime.identity(read, row), "source_instance": source_instance, "target_doctype": "File",
                        **values}).insert(ignore_permissions=True)
    return StoredFile(file_name, file_url, old_url)


def _save_file(name, values, mapped_fields, link_name):
    """Use native File validation inside the existing guarded import transaction."""
    from hero_integration.services.change_detection import managed_target_update
    from hero_integration.services.project_image_categories import prepare_import_category

    document = frappe.get_doc("File", name, for_update=True)
    document.load_doc_before_save(raise_exception=True)
    from hero_integration.services.execution_runtime import field_decisions
    held = {key for key, action in field_decisions('File', name).items() if action in (None, 'local')}
    values = {key: value for key, value in values.items() if key not in held}
    mapped_fields = {key: value for key, value in mapped_fields.items() if key not in held}
    document.update(values)
    if "file_name" in values:
        document.set_file_type()
    prepare_import_category(document, mapped_fields)
    with managed_target_update("File", name, link_name):
        document.save(ignore_permissions=True)
    return document


def relink(existing, target: tuple, folder: str) -> bool:
    """Attach an unchanged file to its now resolvable target; never detaches. Not committed."""
    wanted = _attachment(target, folder)
    if not wanted or not existing or not frappe.db.exists("File", existing.target_name):
        return False
    current = frappe.db.get_value("File", existing.target_name, list(wanted), as_dict=True) or {}
    if all((current.get(key) or "") == value for key, value in wanted.items()):
        return False
    _assert_target_baseline(existing, {}, lock=True)
    _save_file(existing.target_name, wanted, {}, existing.name)
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
    raw = frappe.db.get_value('DefaultValue', {'parent': '__default', 'defkey': STATE_KEY.format(phase)}, 'defvalue')
    return json.loads(raw) if raw else {}


def _save_state(phase: str, state: dict) -> None:
    state["updated"] = str(frappe.utils.now_datetime())
    frappe.db.set_default(STATE_KEY.format(phase), json.dumps(state))
    frappe.db.commit()


def _client_and_downloader(config=None, *, token_provider=None):
    """Client and downloader for ``config``; without one only for the confirmed source binding."""
    from hero_integration.client.graphql import HeroGraphQLClient

    if config is None:
        from hero_integration.services.file_migration import confirmed_configuration

        config = confirmed_configuration()
    token_provider = token_provider or (lambda: config.api_token)
    client = HeroGraphQLClient(config.endpoint, token_provider, config.timeout_seconds, max_retries=2)
    return client, Downloader(config.endpoint.host, token_provider), config.source_instance


def reset_phase(phase: str) -> None:
    """Forget offset and counts so the next transfer starts a fresh pass."""
    # The accepting transaction also owns the run, parent linkage and after-commit enqueue.
    frappe.db.set_default(STATE_KEY.format(phase), "")


def transfer(phase: str, *, source_instance: str | None = None, client=None, downloader=None, dry_run: bool = False,
             time_budget: int = TIME_BUDGET_SECONDS, min_free: int = MIN_FREE_BYTES, should_stop=None,
             progress=None, record_limit=None, run_name=None, cohort=None, page_overlap=0, observe_identity=None, read=None,
             retry_image_ids=None) -> dict:
    """Process one phase from its saved offset until done or the time budget ends.

    ``source_instance`` is the HERO source the client reads from; every lookup,
    new link and the saved offset belong to it."""
    if phase == 'images':
        from hero_integration.services.file_migration import pictures_enabled
        if not pictures_enabled():
            raise TransferError('Picture migration is disabled for this site.')
    if client is None or downloader is None:
        client, downloader, configured = _client_and_downloader()
        if source_instance is not None and configured != source_instance:
            raise TransferError("The configured HERO source instance changed; queue a new run.")
        source_instance = configured
    if not isinstance(source_instance, str) or not source_instance.strip():
        raise TransferError("A HERO source instance is required.")
    if phase not in OPERATION:
        raise TransferError(f"Unknown phase {phase!r}; use one of {PHASES}.")
    if cohort is not None and (record_limit is None or dry_run or not isinstance(cohort, dict) or cohort.get('version') not in (1, 2)):
        raise TransferError('Related file selection requires a frozen test run.')
    operation = OPERATION[phase]
    if not dry_run:
        from hero_integration.services.deferred_references import require_storage
        if not isinstance(run_name, str) or not run_name.strip():
            raise TransferError('File migration requires its originating integration run.')
    read = read or direct_runtime.resolve_read(operation, source_instance)
    from hero_integration.services.offset_pagination import advance, validate_overlap
    if page_overlap:
        validate_overlap(page_overlap, read.page_size, migration=True)
    from hero_integration.services.migration_testing import bounded_rows, processed, validate_checkpoint
    state, save = ({}, None) if dry_run else direct_runtime.run_progress(
        read, run_name, lambda: _state(phase), lambda value: _save_state(phase, value), record_limit=record_limit)
    if retry_image_ids is not None and (phase != "images" or dry_run or record_limit is None
            or not read.validate_write or not state.get("done") or not cohort or not cohort.get("scoped_project_reads")
            or cohort.get("version") != 2):
        raise TransferError("Image retry requires the completed, scoped image run.")
    if retry_image_ids is None and read.validate_write and state.get("done"):
        return state
    if record_limit is not None:
        validate_checkpoint(state, record_limit, source_instance=source_instance, mapping=read.mapping_version)
        if cohort is not None:
            fingerprint = hashlib.sha256(json.dumps(cohort, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if state.get('testing_cohort_fingerprint', fingerprint) != fingerprint:
                raise TransferError('The frozen related project sample changed; start a new run.')
        if retry_image_ids is not None:
            if state.get("testing_cohort_fingerprint") != fingerprint:
                raise TransferError("Image retry requires the original file cohort checkpoint.")
            # Each file commits its own durable receipt. Preserve the finished run's cursor.
            state, save = {}, lambda _state: frappe.db.commit()
        elif state.get('done'):
            return state
    if not dry_run:
        require_storage(frappe)
    if (state.get("done") or state.get("source_instance") != source_instance or "offset" not in state
            or state.get("mapping") != read.mapping_version or state.get("page_overlap", 0) != page_overlap):
        # An offset belongs to its approved mapping: changed filters or field
        # selections require every row to be reconsidered under the new version.
        state = {}  # unchanged content is skipped by its link, without another download
    state.pop("cursor", None)
    state.setdefault("offset", 0)
    state["source_instance"] = source_instance
    state["mapping"] = read.mapping_version
    state["page_overlap"] = page_overlap
    frozen_selection = read.testing_rows is not None or cohort is not None
    if frozen_selection:
        state['testing_record_limit'] = record_limit
    if cohort is not None:
        state['testing_cohort_fingerprint'] = fingerprint
    frozen_files = cohort.get('files', {}).get(operation, {}) if cohort and cohort.get('version') == 2 else None
    scoped = bool(cohort and cohort.get('scoped_project_reads'))
    found_ids = set(state.get('testing_file_ids_found', ()))
    counts = state.setdefault("counts", {})
    from hero_integration.services.execution_budget import BudgetReached, current_budget
    budget = current_budget()
    scoped_lookup = budget is not None and budget.version in (2, 3)
    if scoped and not scoped_lookup:
        raise TransferError("Scoped file execution requires its frozen execution budget.")
    if retry_image_ids is not None:
        ids = set(retry_image_ids)
        if not ids or not ids <= frozen_files.keys():
            raise TransferError("Retry identities must belong to the original image cohort.")
        failed = frappe.get_all("HERO Budget Outcome", filters={"budget": budget.budget_id, "phase": "writer",
            "object_type": SOURCE_TYPE, "outcome": "failed", "external_id": ["in", sorted(ids)]},
            pluck="external_id", limit_page_length=0)
        frozen_files = {i: frozen_files[i] for i in failed}
    scoped_ids = sorted(frozen_files, key=int) if scoped else ()
    if not scoped_lookup:
        projects = _bindings("project", "Project", source_instance)
        customers = _bindings("customer", "Customer", source_instance)
        if cohort is not None:
            projects = {key: value for key, value in projects.items() if key in cohort['project_ids']}
            customers = {key: value for key, value in customers.items() if key in cohort['customer_ids']}
    if not dry_run and not current_budget():
        allow_unlimited_attachments()
    partial_dir = frappe.get_site_path("private", "files", ".hero-partial")
    folders = {True: "/".join(("Home", ROOT_FOLDER, PHASE_FOLDER[phase])),
               False: "/".join(("Home", ROOT_FOLDER, PHASE_FOLDER[phase], UNASSIGNED_FOLDER))}

    def initialize_storage():
        folders.update({True: _folder(ROOT_FOLDER, PHASE_FOLDER[phase]),
                        False: _folder(ROOT_FOLDER, PHASE_FOLDER[phase], UNASSIGNED_FOLDER)})
        frappe.db.commit()
        os.makedirs(partial_dir, exist_ok=True)

    storage_ready = dry_run or not current_budget()
    if storage_ready and not dry_run:
        initialize_storage()
    deadline = time.monotonic() + time_budget
    state.update(phase=phase, done=False, last_error=None, stopped=False)

    current_identity = None
    def bump(key, amount=1):
        counts[key] = counts.get(key, 0) + amount
        if key == 'skipped_archived':
            return
        if current_identity is not None and (key in ('failed', 'conflict') or key.startswith('skipped_')):
            if budget and budget.version in (2, 3):
                status = 'missing' if key in ('skipped_no_detail', 'skipped_no_url') else (
                    key if key in ('failed', 'conflict') else 'processed')
                budget.outcome(SOURCE_TYPE, current_identity, status)
                frappe.db.commit()
        if observe_identity and current_identity is not None and (
                key in {"added", "updated", "unchanged", "failed", "conflict"} or key.startswith("skipped_")):
            observe_identity(current_identity, counts)

    while True:
        if frozen_files is not None:
            accounted = found_ids | set(state.get('testing_missing_file_ids', ())) if scoped else found_ids
            if frozen_files.keys() <= accounted:
                break
            if state.get('testing_scan_pages', 0) >= 10000:
                raise TransferError('File execution reached its bounded scan before finding frozen identities.')
        if record_limit is not None and not frozen_selection and processed(counts) >= record_limit:
            state.update(testing_limit_reached=True)
            break
        absent = set()
        if scoped:
            from hero_integration.services.scoped_test_reads import exact_rows
            keys = scoped_ids[state['offset']:state['offset'] + read.page_size]
            received = exact_rows(client, read, keys, uuids={k: frozen_files[k]['uuid'] for k in keys},
                                  allow_absent=True)
            by_id = {str(row['id']): row for row in received if direct_runtime.belongs(operation, row)}
            absent = set(keys) - by_id.keys()
            # Keep the frozen offsets stable even if a selected upload disappears or changes category.
            # Placeholders participate only in offset fingerprints, never in detail reads or writes.
            page = [by_id.get(key, {'id': int(key)}) for key in keys]
            for key in sorted(absent - accounted):
                budget.outcome(SOURCE_TYPE, key, 'missing')
                state.setdefault('testing_missing_file_ids', []).append(key)
                bump('incomplete')
            bump("rows_read", len(received))
        else:
            page = direct_runtime.fetch_page(client, read, state["offset"])
            bump("rows_read", len(page))
        bump("requests")
        if not page:
            if frozen_files is not None and frozen_files.keys() - found_ids:
                missing = sorted(frozen_files.keys() - found_ids)
                state['testing_missing_file_ids'] = missing
                counts['incomplete'] = counts.get('incomplete', 0) + len(missing)
                if budget:
                    for identity in missing:
                        budget.outcome(SOURCE_TYPE, identity, 'missing')
                # An exhausted, verified reader cannot supply these members.
                # Keep their receipts and let the parent advance to its next area.
            break
        window = None
        if page_overlap:
            window = advance(page, offset=state["offset"], size=read.page_size, overlap=page_overlap, migration=True,
                             previous=state.get("offset_window"), identity=lambda row: direct_runtime.identity(read, row))
        identities = [direct_runtime.identity(read, row) for row in page]
        if len(set(identities)) != len(identities):
            raise TransferError("HERO returned duplicate file identities in a page.")
        page_fingerprint = hashlib.sha256(json.dumps(identities, separators=(',', ':')).encode()).hexdigest()
        if state.get('last_page_fingerprint') == page_fingerprint:
            raise TransferError("HERO repeated a file page; pagination cannot safely continue.")
        known = _known_files(source_instance, identities)
        candidates = [row for row in (window.rows if window else page) if str(row['id']) not in absent and direct_runtime.belongs(operation, row)]
        if frozen_files is not None:
            candidates = [row for row in candidates if str(row['id']) in frozen_files]
        if not frozen_selection:
            if migration_archive.active() and record_limit is not None:
                # Only file_uploads detail has the flag. Bound the writers after that read.
                bounded_rows(state, (), record_limit)
                state["testing_records_received"] += len(candidates)
            else:
                candidates = bounded_rows(state, candidates, record_limit)
        else:
            state['testing_records_received'] = int(state.get('testing_records_received', 0)) + (len(received) if scoped else len(page))
        budget = current_budget()
        batch_size = DETAIL_BATCH if scoped or not budget else 1
        def selected_files():
            for start in range(0, len(candidates), batch_size):
                if not frozen_selection and record_limit is not None and processed(counts) >= record_limit:
                    return
                batch = candidates[start:start + batch_size]
                if scoped and not all(budget.allows_record('file_upload',r['id']) for r in batch):
                    raise TransferError('File detail batch is outside the frozen cohort.')
                if budget and not scoped:
                    try:
                        budget.reserve_record('file_upload', batch[0]['id'])
                    except BudgetReached:
                        if not dry_run:
                            save(state)
                        raise
                from hero_integration.services.source_history import enabled, FILE_DETAIL_PATHS
                history = enabled(read, 'file_details')
                extra = {'paths': FILE_DETAIL_PATHS} if history else {}
                found = details(client, batch, read, source_instance, allow_missing=True, **extra)
                bump('requests')
                for row in batch:
                    if not frozen_selection and record_limit is not None and processed(counts) >= record_limit:
                        return
                    if scoped:
                        budget.reserve_record('file_upload',row['id'])
                    detail = found.get(row['uuid'])
                    if history and detail is not None:
                        row = {**row, '_hero_file_detail': detail}
                    yield row, detail

        for index, (row, detail) in enumerate(selected_files()):
            if should_stop and index % 25 == 0 and should_stop():
                state["stopped"] = True
                if not dry_run:
                    save(state)  # retry this page; committed files are skipped later
                return state
            current_identity = direct_runtime.identity(read, row)
            if frozen_files is not None:
                found_ids.add(str(row['id']))
            try:
                if detail is None:
                    raise FileRelationshipError('Requested file detail is missing.')
                if migration_archive.excluded(operation, detail):
                    bump('skipped_archived')
                    continue
                if frozen_files is not None and project_file_proof(detail, cohort) != frozen_files[str(row['id'])]:
                    raise FileRelationshipError('Frozen file changed its project relationship.')
                if cohort is not None and not belongs_to_test_cohort(detail, cohort):
                    continue
                file_references(detail)
            except FileRelationshipError as error:
                if not dry_run and run_name:
                    from hero_integration.services.integration_events import record_output
                    record_output(run=run_name, source_type=SOURCE_TYPE, source_id=current_identity,
                                  result='BLOCKED_RECORD', action='file_relationship', detail=str(error))
                bump('failed')
                continue
            link = known.get(current_identity)
            unchanged = bool(link) and link.source_payload_hash == source_hash(row)
            unchanged = unchanged and bool(frappe.db.exists("File", link.target_name))
            reason = skip_reason(row, detail)
            if reason:
                if reason == 'deleted' and not dry_run:
                    from hero_integration.services.deferred_references import installed, invalidate
                    if link and detail.get('is_deleted') is True and read.plan.binding_contract:
                        # The exact ID+UUID detail describes this FileUpload. A deleted
                        # customer_document alone only invalidates its attachment.
                        direct_runtime.process_record(read, {**row, 'is_deleted': True}, source_instance,
                                                      SOURCE_TYPE, run_name, lambda: 'unchanged')
                    if installed():
                        invalidate(source_instance, operation, direct_runtime.identity(read, row))
                if budget and budget.version in (2, 3) and reason in ('no_detail', 'no_url'):
                    bump('incomplete')
                    bump(f'skipped_{reason}')
                elif budget and budget.version in (2, 3) and unchanged:
                    budget.outcome(SOURCE_TYPE, current_identity, 'processed')
                    frappe.db.commit()
                    bump('unchanged')
                else:
                    bump("unchanged" if unchanged else f"skipped_{reason}")
                continue
            scope = cohort
            if frozen_files is not None:
                proof = frozen_files[str(row['id'])]
                scope = {'project_ids': proof['projects'], 'customer_ids': proof['customers']}
            if budget:
                from hero_integration.services.deferred_references import file_dependencies
                budget.reserve_many(file_dependencies(detail, scope), relation='file_targets')
            if not storage_ready:
                initialize_storage()
                storage_ready = True
            resolved_target = (scoped_file_target(detail, source_instance, scope) if scoped_lookup else
                               resolve_target(detail, projects, customers))
            # Files are imported privately without realizing a new attachment.
            # The run-scoped observation retains every eligible target identity.
            target = resolved_target if dry_run else (None, None)
            fields = mapped_values(read, row)
            def remember_attachment():
                from hero_integration.services.deferred_references import remember_file
                remember_file(run_name, source_instance, operation, read, row, detail, cohort=scope)

            try:
                if link and _legacy_fields_digest(link.get('target_field_hashes') if isinstance(link, dict)
                                                  else getattr(link, 'target_field_hashes', None)) is not None:
                    def upgrade():
                        return reconcile_legacy_file(link, read, row, resolved_target,
                            folders[bool(resolved_target[0])], source_instance, dry_run=dry_run)
                    reconciled = upgrade() if dry_run else direct_runtime.process_record(
                        read, row, source_instance, SOURCE_TYPE, run_name, upgrade)
                    if reconciled:
                        if not dry_run:
                            remember_attachment()
                            if budget:
                                budget.outcome(SOURCE_TYPE, current_identity, 'conflict' if reconciled == 'conflict' else 'processed')
                            frappe.db.commit()
                            bump('baselines_verified')
                        bump(reconciled)
                        continue
                if dry_run or not read.plan.binding_contract:
                    _assert_target_baseline(link, fields, lock=not dry_run and unchanged)
            except BudgetReached:
                raise
            except DirectContractChanged:
                raise
            except (FileConflict, BindingConflict) as error:
                _record_conflict(link, error, dry_run=dry_run)
                bump('conflict')
                continue
            except Exception as error:
                frappe.db.rollback()
                bump('failed')
                frappe.log_error(title="HERO file transfer failed", message=f"HERO file {row.get('id')}: {error}")
                continue
            if unchanged:
                try:
                    if dry_run:
                        hashes = json.loads(field_hashes(fields))["fields"]
                        baseline = json.loads(frappe.db.get_value("HERO Integration Link", link.name, "target_field_hashes"))
                        bump("would_update" if baseline.get("fields") != hashes else "unchanged")
                        continue
                    moved = False
                    def update_metadata():
                        nonlocal moved
                        status, moved = update_bound_metadata(read, link, fields, target, folders[bool(target[0])])
                        return status

                    status = direct_runtime.process_record(read, row, source_instance, SOURCE_TYPE,
                                                           run_name, update_metadata)
                    remember_attachment()
                    if budget:
                        budget.outcome(SOURCE_TYPE, current_identity, 'conflict' if status == 'conflict' else 'processed')
                    frappe.db.commit()
                    if moved:
                        bump("relinked")
                    bump(status)
                except DirectContractChanged:
                    raise
                except (FileConflict, BindingConflict) as error:
                    _record_conflict(link, error)
                    bump('conflict')
                except Exception as error:
                    frappe.db.rollback()
                    bump("failed")
                    frappe.log_error(title="HERO file transfer failed",
                                     message=f"HERO file {row.get('id')} ({row.get('filename')}): {error}")
                continue
            planned = _byte_count(row.get("size")) or 0
            bump("bytes_planned", planned)
            if dry_run:
                mapped_values(read, row)
                bump("would_update" if link else "would_add")
                bump(f"target_{(target[0] or 'none').lower()}")
                continue
            if shutil.disk_usage(partial_dir).free - (0 if phase == "images" else planned) < min_free:
                state["last_error"] = "Not enough free disk space; transfer paused."
                save(state)
                return state
            temp = os.path.join(partial_dir, f"{row['id']}.part")
            try:
                size, md5 = downloader.fetch(row["downloadUrl"], temp, row.get("size"),
                                             **({"image_id": current_identity} if phase == "images" else {}))
                if phase == "images" and shutil.disk_usage(partial_dir).free < min_free:
                    _remove(temp)
                    state["last_error"] = "Not enough free disk space; transfer paused."
                    save(state)
                    return state
                remember_attachment()
                commit_file(row, size, md5, temp, target, folders[bool(target[0])], link, source_instance, read,
                            run_name=run_name, report_outcome=bump)
                bump("bytes", size)
            except DirectContractChanged:
                raise
            except (FileConflict, BindingConflict) as error:
                _remove(temp)
                _record_conflict(link, error)
                bump('conflict')
            except Exception as error:
                frappe.db.rollback()
                _remove(temp)
                bump("failed")
                frappe.log_error(title="HERO file transfer failed",
                                 message=f"HERO file {row.get('id')} ({row.get('filename')}): {error}")
        if frozen_files is not None:
            state['testing_file_ids_found'] = sorted(found_ids)
            state['testing_scan_pages'] = state.get('testing_scan_pages', 0) + 1
        if window:
            state["offset"] = window.next_offset
            state["offset_window"] = window.checkpoint
        else:
            state["offset"] += len(page)
        state['last_page_fingerprint'] = page_fingerprint
        if not dry_run:
            save(state)
            if progress:
                counts["confirmed_offset"] = state.get("offset")
                progress(counts)
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
    with migration_archive.run_selection(frappe.get_single("HERO Integration Settings")):
        return transfer(phase, dry_run=True, time_budget=24 * 3600)


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
