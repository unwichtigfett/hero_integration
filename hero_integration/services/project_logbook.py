"""Import HERO project histories into the ERPNext Project timeline.

HERO requires histories nested in project-list queries; batches of 25 avoid
its large-response rejection. Entries become dated Info lines, deduplicated
by project_history Integration Links. No HERO writes occur; remote deletions
retain the ERPNext entry.
"""

from __future__ import annotations

import json

import frappe

PAGE_SIZE = 5
ENTRIES_PER_PROJECT = 100
SOURCE_TYPE = "project_history"
CONTEXT_FIELDS = (
    'user_id', 'modified', 'target_project_match_id', 'target_company_id', 'target', 'target_id',
    'type_code', 'associated_outbox_mail_id', 'role_visibility', 'target_user_id',
    'target_project_id', 'type', 'is_editable', 'additional_file_uploads', 'weather_attachment',
)
HISTORY_FIELDS = (
    'id created custom_title custom_text author_name user_id modified target_project_match_id '
    'target_company_id target target_id type_code associated_outbox_mail_id role_visibility '
    'target_user_id target_project_id type is_editable '
    'additional_file_uploads { id } weather_attachment { main description temp wind emoji icon }'
)
QUERY = (
    'query HeroProjectLogbook($first: Int!, $offset: Int!) { '
    'project_matches(first: $first, offset: $offset, orderBy: "id") { '
    'id histories(first: 100, offset: 0, orderBy: "id") { ' + HISTORY_FIELDS + ' } } }'
)


def _tail_query(pending):
    """Batch independent history cursors without assuming HERO's page-size cap."""
    declarations, fields, variables = [], [], {}
    for index, (identity, offset) in enumerate(pending.items()):
        declarations.extend((f'$id{index}: [Int]', f'$offset{index}: Int!'))
        fields.append(f'p{index}: project_matches(ids: $id{index}, first: 1) {{ id '
                      f'histories(first: 100, offset: $offset{index}, orderBy: "id") {{ {HISTORY_FIELDS} }} }}')
        variables.update({f'id{index}': [int(identity)], f'offset{index}': offset})
    return 'query HeroProjectLogbookPages(' + ', '.join(declarations) + ') { ' + ' '.join(fields) + ' }', variables


def _response(data):
    data = data.get('data', data) if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ValueError('HERO returned an invalid logbook response.')
    return data


def _rows(value):
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError('HERO returned an invalid project/history collection.')
    identities = [row.get('id') for row in value]
    if any(type(key) is not int or key <= 0 for key in identities) or len(set(identities)) != len(identities):
        raise ValueError('HERO returned missing or repeated logbook identities.')
    return value


def sync_logbook(client, user_emails: dict[int, str] | None = None, max_pages: int = 10000, *,
                 source_instance: str, max_history_pages: int = 200, progress=None, verify_source=None) -> dict:
    """Exhaust both nested collections; fail visibly if a bound/cursor is broken.

    Short pages are not completion evidence. Each project is read to an empty
    history page, including projects below 500 entries. Existing comments are
    immutable and must belong to the same source and parent. A failed batch is
    retryable: links and comments are committed together after the whole batch.
    """
    if not isinstance(source_instance, str) or not source_instance.strip():
        raise ValueError("A HERO source instance is required.")
    projects = dict(frappe.db.sql("""SELECT source_id, target_name FROM `tabHERO Integration Link`
        WHERE source_system='HERO' AND source_type='project' AND target_doctype='Project'
          AND IFNULL(target_name, '') != ''
          AND IFNULL(NULLIF(source_instance, ''), 'default')=%s""", (source_instance,)))
    known = dict(frappe.db.sql("""SELECT l.source_id,
        CASE WHEN c.reference_doctype='Project' THEN c.reference_name ELSE c.custom_hero_project END
        FROM `tabHERO Integration Link` l
        LEFT JOIN `tabComment` c ON l.target_doctype='Comment' AND c.name=l.target_name
          AND c.reference_doctype IN ('Project', 'HERO Integration Link')
        WHERE l.source_system='HERO' AND l.source_type='project_history'
          AND IFNULL(NULLIF(l.source_instance, ''), 'default')=%s""", (source_instance,)))
    counts = {"requests": 0, "added": 0, "known": 0, "without_project": 0, "projects": 0, "complete": False, "restricted": 0, "file_references": 0, "weather_entries": 0}
    seen_projects = set()
    metadata = _metadata(source_instance)

    def request(document, variables):
        if verify_source:
            verify_source()
        counts['requests'] += 1
        return _response(client.query(document, variables))

    def consume(row, seen):
        identity = str(row['id'])
        entries = _rows(row.get('histories'))
        project = projects.get(identity)
        for entry in entries:
            key = str(entry['id'])
            counts['restricted'] += bool((entry.get('role_visibility') or '').strip())
            counts['file_references'] += len(entry.get('additional_file_uploads') or [])
            counts['weather_entries'] += bool(entry.get('weather_attachment'))
            if key in seen:
                raise ValueError('HERO history pagination repeated an identity; completion cannot be confirmed.')
            seen.add(key)
            if not project:
                counts['without_project'] += 1
            elif key in known:
                if known[key] != project:
                    raise ValueError('An existing logbook binding has a missing or different Project target.')
                enrich_entry(project, entry, user_emails or {}, source_instance, current=metadata.get(key))
                counts['known'] += 1
            else:
                add_entry(project, entry, user_emails or {}, source_instance)
                known[key] = project
                counts['added'] += 1
        return len(entries)

    offset, size = 0, PAGE_SIZE
    for _ in range(max_pages):
        try:
            rows = _rows(request(QUERY, {'first': size, 'offset': offset}).get('project_matches'))
        except Exception:
            if size <= 1:
                raise
            size = max(1, size // 2)
            continue
        if not rows:
            counts['complete'] = True
            return counts
        pending, seen = {}, {}
        for row in rows:
            identity = str(row['id'])
            if identity in seen_projects:
                raise ValueError('HERO project pagination repeated an identity.')
            seen_projects.add(identity)
            counts['projects'] += 1
            seen[identity] = set()
            length = consume(row, seen[identity])
            if length and identity in projects:
                pending[identity] = length
        for _ in range(max_history_pages):
            if not pending:
                break
            document, variables = _tail_query(pending)
            data = request(document, variables)
            next_pending = {}
            for index, (identity, cursor) in enumerate(pending.items()):
                tail = _rows(data.get(f'p{index}'))
                if len(tail) != 1 or str(tail[0]['id']) != identity:
                    raise ValueError('HERO history pagination lost its parent Project.')
                length = consume(tail[0], seen[identity])
                if length:
                    next_pending[identity] = cursor + length
            pending = next_pending
        if pending:
            raise ValueError('HERO history page bound reached; logbook import is incomplete.')
        frappe.db.commit()
        if progress:
            progress(dict(counts))
        offset += len(rows)
    raise ValueError('HERO project page bound reached; logbook import is incomplete.')


def add_entry(project: str, entry: dict, user_emails: dict[int, str], source_instance: str) -> str:
    from frappe.utils.html_utils import sanitize_html

    title = (entry.get("custom_title") or "").strip()
    text = sanitize_html(entry.get("custom_text") or "").strip()
    author = (entry.get("author_name") or "").strip() or user_emails.get(entry.get("user_id")) or ""
    heading = " · ".join(filter(None, ("Logbuch", title, author)))
    content = f"<b>{frappe.utils.escape_html(heading)}</b>" + (f"<br>{text}" if text else "")
    restricted = bool((entry.get('role_visibility') or '').strip())
    comment = frappe.get_doc({"doctype": "Comment", "comment_type": "Info",
                              "reference_doctype": None if restricted else "Project",
                              "reference_name": None if restricted else project, "content": content,
                              "subject": title, "comment_by": author[:140] or None,
                              "comment_email": user_emails.get(entry.get('user_id')),
                              "custom_hero_project": project, "custom_hero_logbook_context": _context(entry)})
    comment.flags.ignore_permissions = True
    comment.insert(ignore_permissions=True)
    created = _local(entry.get("created"))
    if created:
        frappe.db.set_value("Comment", comment.name, {"creation": created, "modified": created}, update_modified=False)
    link = frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_type": SOURCE_TYPE,
                    "source_id": str(entry["id"]), "source_instance": source_instance, "sync_status": "Synchronized",
                    "target_doctype": "Comment", "target_name": comment.name, "mapping_version": "project-logbook-v1",
                    "source_payload_hash": _digest(entry), "target_payload_hash": _digest({"content": comment.content}),
                    "last_synced_on": frappe.utils.now_datetime()}).insert(ignore_permissions=True)
    if restricted:
        frappe.db.set_value('Comment', comment.name, {'reference_doctype':'HERO Integration Link',
            'reference_name':link.name}, update_modified=False)
    return comment.name


def _context(entry):
    """Preserve source-only scalars and relations without duplicating native fields."""
    return json.dumps({key: entry[key] for key in CONTEXT_FIELDS if key in entry}, sort_keys=True, ensure_ascii=False)


def _metadata(source_instance):
    rows = frappe.db.sql("""SELECT l.source_id, l.name AS link_name, c.name, c.subject, c.comment_by,
        c.reference_doctype, c.reference_name, c.custom_hero_project, c.custom_hero_logbook_context
        FROM `tabHERO Integration Link` l JOIN `tabComment` c ON l.target_doctype='Comment' AND c.name=l.target_name
        WHERE l.source_system='HERO' AND l.source_type='project_history'
          AND IFNULL(NULLIF(l.source_instance, ''), 'default')=%s""", (source_instance,), as_dict=True)
    return {row.source_id: row for row in rows}


def enrich_entry(project, entry, user_emails, source_instance, *, current=None):
    """Fill new native metadata, retaining historical content, authors and timestamps.

    HERO role codes are not ERPNext roles. Restricted entries stay accessible to
    System Managers via their Integration Link, outside the Project timeline.
    Once restricted, an entry is never automatically made public by a later read.
    """
    rows = [current] if current is not None else frappe.db.sql("""SELECT l.name AS link_name, c.name, c.subject, c.comment_by,
        c.reference_doctype, c.reference_name, c.custom_hero_project, c.custom_hero_logbook_context
        FROM `tabHERO Integration Link` l JOIN `tabComment` c ON l.target_doctype='Comment' AND c.name=l.target_name
        WHERE l.source_system='HERO' AND l.source_type='project_history'
          AND IFNULL(NULLIF(l.source_instance, ''), 'default')=%s AND l.source_id=%s""",
        (source_instance, str(entry['id'])), as_dict=True)
    if len(rows) != 1:
        raise ValueError('Logbook metadata requires one proven Comment binding.')
    row = rows[0]
    values = {}
    if not row.subject and entry.get('custom_title'):
        values['subject'] = entry['custom_title'].strip()
    if not row.comment_by:
        author = (entry.get('author_name') or '').strip() or user_emails.get(entry.get('user_id')) or ''
        if author:
            values['comment_by'] = author[:140]
    if not row.custom_hero_project:
        values['custom_hero_project'] = project
    context = _context(entry)
    if row.custom_hero_logbook_context != context:
        values['custom_hero_logbook_context'] = context
    if (entry.get('role_visibility') or '').strip() and row.reference_doctype == 'Project':
        values.update(reference_doctype='HERO Integration Link', reference_name=row.link_name)
    if values:
        frappe.db.set_value('Comment', row.name, values, update_modified=False)


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
