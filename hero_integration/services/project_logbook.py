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
        if project:
            _guard_legacy_outbound(project, source_instance)
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


def _guard_legacy_outbound(project, instance):
    # Serialize with outbound creation; legacy raw-ID bindings cannot consume
    # the source/parent/history identities used by the mapped Project reader.
    from hero_integration.services.project_logbook_writeback import project_marker
    frappe.get_doc('Project', project, for_update=True)
    if project_marker(instance, project, lock=True, frappe_api=frappe):
        raise ValueError('Projects with outbound logbook activities require the approved mapped Project logbook importer.')


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


def sync_mapped_entries(plan, record, project, *, preview=False, conflicts=None):
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
    if len(entries) >= config['limit'] and not (config.get('offset_variable') and record.get('_hero_logbook_complete') is True):
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
        restricted = False
        if config.get('visibility_path'):
            visibility = lookup(entry, config['visibility_path'][len(config['path']) + 1:])
            if visibility is MISSING or (visibility is not None and not isinstance(visibility, str)):
                raise ValueError('Mapped logbook visibility is absent or invalid.')
            restricted = bool((visibility or '').strip())
        if config.get('context_paths'):
            context = {path[len(config['path']) + 1:]: _context_value(entry, path[len(config['path']) + 1:].split('.'))
                       for path in config['context_paths']}
            values['custom_hero_logbook_context'] = json.dumps(context, sort_keys=True, ensure_ascii=False)
        planned.append((key, values, str(identity), restricted, entry.get('custom_text')))
    if preview:
        return len(planned)
    project_doc = frappe.get_doc('Project', project, for_update=True)
    frappe.has_permission('Project', 'write', doc=project_doc, throw=True)
    from hero_integration.services.project_logbook_writeback import assert_project_import_clear, OUTBOUND_VERSION
    assert_project_import_clear(instance, project, frappe_api=frappe)
    added = 0
    for key, values, identity, restricted, outbound_text in planned:
        existing = frappe.db.sql('SELECT name, target_doctype, target_name, mapping_version, sync_status, '
            'source_payload_hash, target_payload_hash FROM `tabHERO Integration Link` '
            'WHERE source_system=%s AND source_type=%s AND source_instance=%s AND source_id=%s FOR UPDATE',
            ('HERO', SOURCE_TYPE, instance, key), as_dict=True)
        if len(existing) > 1:
            raise ValueError('Mapped logbook identity is ambiguous.')
        if existing:
            link = existing[0]
            if link.mapping_version == OUTBOUND_VERSION and link.target_doctype == 'Version':
                if not frappe.db.exists('Version', link.target_name):
                    raise ValueError('Mapped logbook link has an incompatible or missing target.')
                version = frappe.get_doc('Version', link.target_name)
                if version.ref_doctype != 'Project' or version.docname != project:
                    raise ValueError('Mapped logbook comment belongs to another parent.')
                _observe_outbound_activity(link, version, values, conflicts, outbound_text=outbound_text)
                continue
            if link.target_doctype != 'Comment' or not frappe.db.exists('Comment', link.target_name):
                raise ValueError('Mapped logbook link has an incompatible or missing target.')
            comment = frappe.get_doc('Comment', link.target_name)
            public = comment.reference_doctype == 'Project' and comment.reference_name == project
            private = (comment.reference_doctype == 'HERO Integration Link'
                       and comment.reference_name == link.name and getattr(comment, 'custom_hero_project', None) == project)
            if (not public and not private and link.mapping_version == 'project-logbook-v1'
                    and comment.reference_doctype == 'HERO Integration Link'
                    and getattr(comment, 'custom_hero_project', None) == project):
                original = _legacy_identity_alias(project, parent, instance, identity)
                private = original is not None and original.name == comment.reference_name
            if not public and not private:
                raise ValueError('Mapped logbook comment belongs to another parent.')
            if restricted and public:
                # Tightening visibility never waits on content-conflict resolution.
                frappe.db.set_value('Comment', comment.name, {'reference_doctype': 'HERO Integration Link',
                    'reference_name': link.name, 'custom_hero_project': project}, update_modified=False)
            if link.mapping_version == OUTBOUND_VERSION:
                # This is the original ERPNext activity, not a Comment created
                # by import. Preserve its content, author and native timestamps.
                _observe_outbound_activity(link, comment, values, conflicts, outbound_text=outbound_text)
                continue
            if config.get('context_paths') and link.mapping_version == 'project-logbook-v1':
                _enrich_legacy_mapped_comment(comment, values)
            if config.get('context_paths') and link.mapping_version != 'project-logbook-v1':
                observed = {field: getattr(comment, field, None) for field in values}
                for field in ('creation', 'modified'):
                    if observed.get(field) is not None: observed[field] = str(observed[field])
                if _digest(observed) != link.target_payload_hash:
                    # A local edit is retained, while a stricter visibility above still takes effect.
                    if conflicts is not None: conflicts.append(link.name)
                    frappe.db.set_value('HERO Integration Link', link.name, {
                        'sync_status': 'Conflict', 'conflict_detail': 'Logbook comment changed locally; review mapped values.',
                        'observed_source_hash': _digest(values), 'observed_target_hash': _digest(observed),
                        'last_observed_on': frappe.utils.now_datetime()}, update_modified=False)
                    continue
                if (link.sync_status == 'Synchronized' and link.mapping_version == plan.mapping_version
                        and link.source_payload_hash == _digest(values)):
                    continue  # An unchanged entry needs no writes or timestamp churn.
                frappe.db.set_value('Comment', comment.name, values, update_modified=False)
                frappe.db.set_value('HERO Integration Link', link.name, {
                    'sync_status': 'Synchronized', 'conflict_detail': '',
                    'mapping_version': plan.mapping_version, 'source_payload_hash': _digest(values),
                    'target_payload_hash': _digest(values), 'last_synced_on': frappe.utils.now_datetime()}, update_modified=False)
            continue
        legacy = _legacy_identity_alias(project, parent, instance, identity)
        if legacy is not None:
            if config.get('context_paths'):
                _enrich_legacy_mapped_comment(frappe.get_doc('Comment', legacy.target_name), values)
            if restricted:
                frappe.db.set_value('Comment', legacy.target_name, {'reference_doctype': 'HERO Integration Link',
                    'reference_name': legacy.name, 'custom_hero_project': project}, update_modified=False)
            # Retain the original link and Comment. The alias records identity only,
            # never a new content/date acknowledgement under the current mapping.
            frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': SOURCE_TYPE,
                'source_id': key, 'source_instance': instance, 'sync_status': 'Synchronized', 'target_doctype': 'Comment',
                'target_name': legacy.target_name, 'mapping_version': legacy.mapping_version,
                'source_payload_hash': legacy.source_payload_hash, 'target_payload_hash': legacy.target_payload_hash,
                'last_synced_on': legacy.last_synced_on}).insert(ignore_permissions=True)
            continue
        comment = frappe.get_doc({'doctype': 'Comment', 'comment_type': 'Info', 'reference_doctype': None if restricted else 'Project',
                                  'reference_name': None if restricted else project, 'custom_hero_project': project, **{k: v for k, v in values.items() if k not in {'creation', 'modified'}}})
        comment.insert(ignore_permissions=True)
        dates = {key: values[key] for key in ('creation', 'modified') if values.get(key)}
        if dates:
            if 'creation' in dates and 'modified' not in dates: dates['modified'] = dates['creation']
            frappe.db.set_value('Comment', comment.name, dates, update_modified=False)
        link = frappe.get_doc({'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_type': SOURCE_TYPE,
            'source_id': key, 'source_instance': instance, 'sync_status': 'Synchronized', 'target_doctype': 'Comment',
            'target_name': comment.name, 'mapping_version': plan.mapping_version,
            'source_payload_hash': _digest(values), 'target_payload_hash': _digest(values),
            'last_synced_on': frappe.utils.now_datetime()}).insert(ignore_permissions=True)
        if restricted:
            frappe.db.set_value('Comment', comment.name, {'reference_doctype': 'HERO Integration Link',
                'reference_name': link.name}, update_modified=False)
        added += 1
    return added


def _observe_outbound_activity(link, activity, values, conflicts, *, outbound_text=None):
    from hero_integration.services.project_logbook_writeback import _hash, _activity_raw
    from hero_integration.services.task_send import plain_text
    from hero_integration.services.project_logbook_writeback import readback_texts
    candidates = readback_texts(outbound_text)
    observed_source = next((_hash(text) for text in candidates if _hash(text) == link.source_payload_hash),
                           _hash(plain_text(outbound_text)))
    observed_target = _hash(_activity_raw(activity))
    changed = observed_source != link.source_payload_hash or observed_target != link.target_payload_hash
    frappe.db.set_value('HERO Integration Link', link.name, {
        'sync_status': 'Conflict' if changed else link.sync_status, 'observed_source_hash': observed_source,
        'observed_target_hash': observed_target, 'last_observed_on': frappe.utils.now_datetime()}, update_modified=False)
    if changed and conflicts is not None: conflicts.append(link.name)


def _legacy_identity_alias(project, parent, instance, identity):
    """Bridge a legacy history ID only under a uniquely proven Project binding."""
    legacy = []
    for candidate in dict.fromkeys((instance, 'default')):
        legacy.extend(frappe.get_all('HERO Integration Link', filters={
            'source_system': 'HERO', 'source_type': SOURCE_TYPE, 'source_instance': candidate, 'source_id': identity},
            fields=['name', 'target_doctype', 'target_name', 'mapping_version', 'source_payload_hash',
                    'target_payload_hash', 'last_synced_on'], limit_page_length=2))
    if not legacy:
        return None
    if len(legacy) != 1:
        raise ValueError('Legacy logbook identity is ambiguous; reconciliation is required.')
    link = legacy[0]
    if link.target_doctype != 'Comment' or not frappe.db.exists('Comment', link.target_name):
        raise ValueError('Legacy logbook target is missing or incompatible.')
    comment = frappe.get_doc('Comment', link.target_name)
    public = comment.reference_doctype == 'Project' and comment.reference_name == project
    private = (comment.reference_doctype == 'HERO Integration Link' and comment.reference_name == link.name
               and getattr(comment, 'custom_hero_project', None) == project)
    if not public and not private:
        raise ValueError('Legacy logbook entry belongs to another Project; no identity was adopted.')
    bindings = frappe.get_all('HERO Integration Link', filters={
        'source_system': 'HERO', 'source_type': 'project', 'target_doctype': 'Project', 'target_name': project},
        fields=['name', 'source_id', 'source_instance'], limit_page_length=2)
    if (len(bindings) != 1 or str(bindings[0].source_id) != str(parent)
            or (bindings[0].source_instance or 'default') != instance):
        raise ValueError('Legacy logbook source and Project identity are not uniquely proven.')
    return link


def complete_mapped_histories(source, records, variables):
    """Exhaust the explicitly approved child cursor before any Project is written."""
    from hero_integration.services.read_contract import value_at, ReadContractError
    config = (source.contract.adapter_options or {}).get('logbook', {})
    variable = config.get('offset_variable')
    if not variable or not records:
        return records
    if source.contract.page_size != 1 or len(records) != 1 or not callable(source.revalidate):
        raise ReadContractError('Paginated logbook requires one Project and source revalidation.')
    row = dict(records[0])
    parent = value_at(row, source.contract.identity_path)
    path = config['path']
    entries = list(value_at(row, path) or [])
    seen = set()
    def check(page):
        if not isinstance(page, list) or not all(isinstance(entry, dict) for entry in page):
            raise ReadContractError('Logbook page is not a collection.')
        for entry in page:
            identity = value_at(entry, config['identity_path'])
            if type(identity) not in {int, str} or not str(identity).strip() or str(identity) in seen:
                raise ReadContractError('Logbook pagination repeated or omitted an identity.')
            seen.add(str(identity))
    check(entries)
    page = entries
    for _ in range(config['max_pages']):
        if not page:
            result = dict(row)
            current = result
            parts = path.split('.')
            for part in parts[:-1]:
                current[part] = dict(current[part]); current = current[part]
            current[parts[-1]] = entries
            result['_hero_logbook_complete'] = True
            return [result]
        if source.revalidate() != source.contract:
            raise ReadContractError('Read approval changed before logbook transport.')
        params = {**variables, variable: len(entries)}
        data = source.client.query(source.service.query_document, params)
        if source.revalidate() != source.contract:
            raise ReadContractError('Read approval changed during logbook transport.')
        parents = value_at(data, source.contract.records_path)
        if (not isinstance(parents, list) or len(parents) != 1
                or value_at(parents[0], source.contract.identity_path) != parent):
            raise ReadContractError('Logbook pagination lost its parent Project.')
        page = value_at(parents[0], path)
        if page is None: page = []
        check(page)
        entries.extend(page)
    if page:
        raise ReadContractError('Logbook page bound reached; Project import is incomplete.')
    result = dict(row)
    current = result
    parts = path.split('.')
    for part in parts[:-1]:
        current[part] = dict(current[part]); current = current[part]
    current[parts[-1]] = entries
    result['_hero_logbook_complete'] = True
    return [result]


def _context_value(value, parts):
    if value is None: return None
    if not parts: return value
    if isinstance(value, list): return [_context_value(item, parts) for item in value]
    if not isinstance(value, dict) or parts[0] not in value:
        raise ValueError('Mapped logbook context field is absent from its source response.')
    return _context_value(value[parts[0]], parts[1:])


def _enrich_legacy_mapped_comment(comment, values):
    """Add approved metadata while preserving historical text, author and dates."""
    fields = {name: values[name] for name in ('subject', 'comment_by', 'comment_email')
              if values.get(name) and not getattr(comment, name, None)}
    if values.get('custom_hero_logbook_context'):
        current = getattr(comment, 'custom_hero_logbook_context', None)
        old = json.loads(current) if current else {}
        if not isinstance(old, dict):
            raise ValueError('Existing logbook context is not an object; review before enrichment.')
        old.update(json.loads(values['custom_hero_logbook_context']))
        fields['custom_hero_logbook_context'] = json.dumps(old, sort_keys=True, ensure_ascii=False)
    if fields:
        frappe.db.set_value('Comment', comment.name, fields, update_modified=False)
