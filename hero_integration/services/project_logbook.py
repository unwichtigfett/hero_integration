"""Import HERO project histories into the ERPNext Project timeline.

HERO requires histories nested in project-list queries; batches of 25 avoid
its large-response rejection. Entries become dated Info lines, deduplicated
by project_history Integration Links. No HERO writes occur; remote deletions
retain the ERPNext entry.
"""

from __future__ import annotations

import frappe

PAGE_SIZE = 25
ENTRIES_PER_PROJECT = 500
SOURCE_TYPE = "project_history"
QUERY = (
    "query HeroProjectLogbook($first: Int!, $offset: Int!) { project_matches(first: $first, offset: $offset, "
    "orderBy: \"id\") { id histories(first: " + str(ENTRIES_PER_PROJECT) + ") { id created custom_title "
    "custom_text author_name user_id } } }"
)


def sync_logbook(client, user_emails: dict[int, str] | None = None, max_pages: int = 200) -> dict:
    """Read every page once and add the entries not yet in ERPNext; returns counts."""
    projects = dict(frappe.db.sql("""SELECT source_id, target_name FROM `tabHERO Integration Link`
        WHERE source_type='project' AND target_doctype='Project' AND IFNULL(target_name, '') != ''"""))
    known = set(frappe.get_all("HERO Integration Link", filters={"source_type": SOURCE_TYPE}, pluck="source_id",
                               limit_page_length=0))
    counts = {"requests": 0, "added": 0, "known": 0, "without_project": 0}
    offset, size = 0, PAGE_SIZE
    for _ in range(max_pages):
        try:
            data = client.query(QUERY, {"first": size, "offset": offset})
        except Exception:
            counts["requests"] += 1
            if size <= 5:
                raise
            size = max(5, size // 2)  # HERO rejected the page size; retry this offset smaller
            continue
        counts["requests"] += 1
        data = data.get("data", data) if isinstance(data, dict) else {}
        rows = (data or {}).get("project_matches") or []
        for row in rows:
            project = projects.get(str(row.get("id")))
            entries = row.get("histories") or []
            if len(entries) >= ENTRIES_PER_PROJECT:
                frappe.log_error(title="HERO project logbook truncated",
                                 message=f"HERO project {row.get('id')} has at least {ENTRIES_PER_PROJECT} logbook entries.")
            for entry in entries:
                key = str(entry.get("id"))
                if key in known:
                    counts["known"] += 1
                elif not project:
                    counts["without_project"] += 1
                else:
                    add_entry(project, entry, user_emails or {})
                    known.add(key)
                    counts["added"] += 1
        frappe.db.commit()
        if len(rows) < size:
            break
        offset += size
    return counts


def add_entry(project: str, entry: dict, user_emails: dict[int, str]) -> str:
    from frappe.utils.html_utils import sanitize_html

    title = (entry.get("custom_title") or "").strip()
    text = sanitize_html(entry.get("custom_text") or "").strip()
    author = (entry.get("author_name") or "").strip() or user_emails.get(entry.get("user_id")) or ""
    heading = " · ".join(filter(None, ("HERO Logbuch", title, author)))
    content = f"<b>{frappe.utils.escape_html(heading)}</b>" + (f"<br>{text}" if text else "")
    comment = frappe.get_doc({"doctype": "Comment", "comment_type": "Info", "reference_doctype": "Project",
                              "reference_name": project, "content": content})
    comment.flags.ignore_permissions = True
    comment.insert(ignore_permissions=True)
    created = _local(entry.get("created"))
    if created:
        frappe.db.set_value("Comment", comment.name, {"creation": created, "modified": created}, update_modified=False)
    frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                    "source_id": str(entry["id"]), "source_instance": "default", "sync_status": "Synchronized",
                    "target_doctype": "Comment", "target_name": comment.name, "mapping_version": "project-logbook-v1",
                    "source_payload_hash": _digest(entry), "target_payload_hash": _digest({"content": content}),
                    "last_synced_on": frappe.utils.now_datetime()}).insert(ignore_permissions=True)
    return comment.name


def _digest(value) -> str:
    from hero_integration.services.migration import _digest as digest

    return digest(value)


def _local(value):
    from hero_integration.services.migration import _external_task_deadline

    return _external_task_deadline(value)
