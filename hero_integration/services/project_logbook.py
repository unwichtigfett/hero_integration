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


def sync_mapped_entries(plan, record, project, *, preview=False):
    """Import only child values selected by the approved parent Query mapping."""
    from hero_integration.services.direct_mapping import MISSING, lookup
    from hero_integration.services.query_mapping import transform, _check_value

    config = plan.adapter_options.get('logbook')
    if not config:
        return 0
    entries = lookup(record, config['path'])
    if entries is MISSING:
        raise ValueError('Approved logbook collection is absent from the project response.')
    if entries is None:
        return 0
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError('Mapped logbook response is not a collection of entries.')
    if len(entries) >= config['limit']:
        raise ValueError('Logbook reached its approved collection bound; review pagination before importing.')
    parent = plan.source_identity(record)
    instance = str(record.get('_hero_source_instance') or '').strip()
    if not instance:
        raise ValueError('Mapped logbook requires the approved source instance.')
    identifiers = [lookup(entry, config['identity_path']) for entry in entries]
    if any(type(value) not in {str, int} or not str(value).strip() for value in identifiers) or len(set(map(str, identifiers))) != len(identifiers):
        raise ValueError('Mapped logbook identities are missing or repeated.')
    planned = []
    rules = [spec for spec in plan.definitions if spec.target_doctype == 'Comment']
    for entry, identity in zip(entries, identifiers):
        values = {}
        for spec in rules:
            relative = spec.source_path[len(config['path']) + 1:]
            raw = lookup(entry, relative)
            _check_value(spec, raw)
            value = transform(spec, raw, plan.adapter_options)
            if value is not MISSING and value is not None:
                values[spec.target_field] = value
        body = values.get('content')
        if body is not None and not isinstance(body, str):
            raise ValueError('Mapped logbook content has an incompatible type.')
        from frappe.utils.html_utils import sanitize_html
        values['content'] = sanitize_html(body or '').strip()
        headings = []
        for path in config.get('heading_paths', []):
            value = lookup(entry, path[len(config['path']) + 1:])
            if value is not MISSING and value is not None:
                if not isinstance(value, str): raise ValueError('Mapped logbook heading has an incompatible type.')
                if value.strip(): headings.append(value.strip())
        if headings:
            values['content'] = '<b>' + frappe.utils.escape_html(' · '.join(headings)) + '</b><br>' + values['content']
        if not values['content'].strip():
            raise ValueError('Mapped logbook content and configured headings are empty.')
        # Stable provenance includes source instance, parent identity and child identity.
        key = _digest([instance, parent, str(identity)])
        planned.append((key, values, str(identity)))
    if preview:
        return len(planned)
    project_doc = frappe.get_doc('Project', project)
    frappe.has_permission('Project', 'write', doc=project_doc, throw=True)
    added = 0
    for key, values, identity in planned:
        existing = frappe.get_all('HERO Integration Link', filters={'source_system': 'HERO', 'source_type': SOURCE_TYPE,
            'source_instance': instance, 'source_id': key}, fields=['name', 'target_doctype', 'target_name'])
        if len(existing) > 1:
            raise ValueError('Mapped logbook identity is ambiguous.')
        if existing:
            link = existing[0]
            if link.target_doctype != 'Comment' or not frappe.db.exists('Comment', link.target_name):
                raise ValueError('Mapped logbook link has an incompatible or missing target.')
            comment = frappe.get_doc('Comment', link.target_name)
            if comment.reference_doctype != 'Project' or comment.reference_name != project:
                raise ValueError('Mapped logbook comment belongs to another parent.')
            continue
        legacy = _legacy_identity_alias(project, parent, instance, identity)
        if legacy is not None:
            # Retain the original link and Comment. The alias records identity only,
            # never a new content/date acknowledgement under the current mapping.
            frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': SOURCE_TYPE,
                'source_id': key, 'source_instance': instance, 'sync_status': 'Synchronized', 'target_doctype': 'Comment',
                'target_name': legacy.target_name, 'mapping_version': legacy.mapping_version,
                'source_payload_hash': legacy.source_payload_hash, 'target_payload_hash': legacy.target_payload_hash,
                'last_synced_on': legacy.last_synced_on}).insert(ignore_permissions=True)
            continue
        comment = frappe.get_doc({'doctype': 'Comment', 'comment_type': 'Info', 'reference_doctype': 'Project',
                                  'reference_name': project, 'content': values['content']})
        comment.insert(ignore_permissions=True)
        if values.get('creation'):
            frappe.db.set_value('Comment', comment.name, {'creation': values['creation'], 'modified': values['creation']}, update_modified=False)
        frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': SOURCE_TYPE,
            'source_id': key, 'source_instance': instance, 'sync_status': 'Synchronized', 'target_doctype': 'Comment',
            'target_name': comment.name, 'mapping_version': plan.mapping_version,
            'source_payload_hash': _digest(values), 'target_payload_hash': _digest(values),
            'last_synced_on': frappe.utils.now_datetime()}).insert(ignore_permissions=True)
        added += 1
    return added


def _legacy_identity_alias(project, parent, instance, identity):
    """Bridge a legacy history ID only under a uniquely proven Project binding."""
    legacy = frappe.get_all('HERO Integration Link', filters={
        'source_system': 'HERO', 'source_type': SOURCE_TYPE, 'source_instance': 'default', 'source_id': identity},
        fields=['name', 'target_doctype', 'target_name', 'mapping_version', 'source_payload_hash',
                'target_payload_hash', 'last_synced_on'], limit_page_length=2)
    if not legacy:
        return None
    if len(legacy) != 1:
        raise ValueError('Legacy logbook identity is ambiguous; reconciliation is required.')
    link = legacy[0]
    if link.target_doctype != 'Comment' or not frappe.db.exists('Comment', link.target_name):
        raise ValueError('Legacy logbook target is missing or incompatible.')
    comment = frappe.get_doc('Comment', link.target_name)
    if comment.reference_doctype != 'Project' or comment.reference_name != project:
        raise ValueError('Legacy logbook entry belongs to another Project; no identity was adopted.')
    bindings = frappe.get_all('HERO Integration Link', filters={
        'source_system': 'HERO', 'source_type': 'project', 'target_doctype': 'Project', 'target_name': project},
        fields=['name', 'source_id', 'source_instance'], limit_page_length=2)
    if (len(bindings) != 1 or str(bindings[0].source_id) != str(parent)
            or (bindings[0].source_instance or 'default') != instance):
        raise ValueError('Legacy logbook source and Project identity are not uniquely proven.')
    return link
