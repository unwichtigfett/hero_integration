"""Recognize the cadence of individually addressed HERO calendar occurrences."""
from calendar import monthrange
from datetime import timedelta
from zoneinfo import ZoneInfo

UNAVAILABLE = object()
FIELD = 'custom_hero_recurrence_interval'
DEFINITION = dict(fieldname=FIELD, label='HERO recurrence interval', fieldtype='Select',
                  options='\nWeekly\nMonthly\nQuarterly\nYearly', insert_after='custom_hero_is_recurring',
                  read_only=1, no_copy=1)
QUERY = """query HeroOccurrenceEvidence($ids: [Int]) {
  calendar_events(ids: $ids, first: 100) {
    id start created title type project_match_id category_id provider all_day is_recurring deleted
  }
}"""
SIGNATURE = ('created', 'title', 'type', 'project_match_id', 'category_id', 'all_day')
MAX_ID = 2**31 - 1


def families(identity):
    # HERO gives the first occurrence its base ID. Further occurrences append
    # the 1-based ordinal, zero-padded to at least two digits. Verify references
    # against live records; an apparent numeric prefix alone proves nothing.
    text = str(identity)
    if not text.isascii() or not text.isdigit() or not 0 < int(text) <= MAX_ID:
        return []
    candidates = [(int(text), 1)]
    for split in range(1, len(text) - 1):
        suffix = text[split:]
        ordinal = int(suffix)
        if ordinal >= 2 and suffix == f'{ordinal:02}':
            candidates.append((int(text[:split]), ordinal))
    return candidates


def member_id(root, ordinal):
    return root if ordinal == 1 else int(f'{root}{ordinal:02}')


def _date(value, zone):
    from hero_integration.services.native_projections import _instant
    return _instant(value, zone).astimezone(zone).date()


def cadence(points):
    """Pairs of (HERO ordinal, source-calendar date), including the anchor."""
    if len(points) < 3 or 1 not in points:
        return None
    first = points[1]
    if len(set(points.values())) != len(points):
        return None
    if all(day == first + timedelta(days=7*(n-1)) for n, day in points.items()):
        return 'Weekly'
    for months, label in ((1, 'Monthly'), (3, 'Quarterly'), (12, 'Yearly')):
        if not all((d.year-first.year)*12 + d.month-first.month == (n-1)*months for n,d in points.items()):
            continue
        same_day = all(d.day == min(first.day, monthrange(d.year,d.month)[1]) for d in points.values())
        month_end = all(d.day == monthrange(d.year,d.month)[1] for d in points.values())
        same_weekday = all(d.weekday() == first.weekday() for d in points.values())
        same_week = all((d.day-1)//7 == (first.day-1)//7 for d in points.values())
        last_week = all(d.day + 7 > monthrange(d.year,d.month)[1] for d in points.values())
        if same_day or month_end or (same_weekday and (same_week or last_week)):
            return label
    return None


def _read(client, ids):
    rows = client.query(QUERY, {'ids': sorted(ids)})['calendar_events']
    result = {}
    for row in rows:
        key = row['id']
        if key in ids:
            if key in result and result[key] != row:
                return {}
            result[key] = row
    return result


def _same_series(row, current):
    # Synthetic children may omit the parent's provider and attendee expansion.
    return (row.get('is_recurring') is True and row.get('deleted') is False
            and all(row.get(k) == current.get(k) for k in SIGNATURE)
            and not (row.get('provider') and current.get('provider') and row['provider'] != current['provider']))


def lookup(client, record, timezone):
    candidates = families(record.get('id'))
    if not candidates:
        return None
    identity = int(record['id'])
    zone = ZoneInfo(timezone)
    anchors = _read(client, {root for root, _ in candidates})
    results = []
    for root, ordinal in candidates:
        anchor = anchors.get(root)
        if (not anchor or anchor.get('is_recurring') is not True or anchor.get('deleted') is not False
                or not anchor.get('created') or not (anchor.get('title') or anchor.get('type'))
                or any(anchor.get(key) != record[key] for key in SIGNATURE if key in record)
                or member_id(root, 3) > MAX_ID):
            continue
        ordinals = {1, *range(2, 10), *range(max(2, ordinal-3), ordinal+5)}
        members = {member_id(root, n): n for n in ordinals if member_id(root, n) <= MAX_ID}
        rows = _read(client, members)
        current = rows.get(identity)
        if (not current or rows.get(root) != anchor or not _same_series(anchor, current)
                or any(record[key] != current.get(key) for key in (*SIGNATURE, 'start') if key in record)):
            continue
        active = [r for r in rows.values() if r.get('deleted') is False]
        if any(not _same_series(r, current) for r in active):
            continue
        try:
            pattern = cadence({members[r['id']]: _date(r['start'], zone) for r in active})
        except (ValueError, TypeError, OverflowError):
            continue
        if pattern:
            results.append(pattern)
    return results[0] if len(results) == 1 else None


def for_run(run, record, timezone, instance):
    from hero_integration.client.graphql import HeroClientError
    from hero_integration.services.file_migration import bound_configuration
    from hero_integration.services.file_transfer import _client_and_downloader
    # Limited test cohorts must never extend their authorized source selection.
    if (getattr(run, 'testing_enabled', False) or getattr(run, 'execution_budget_run', None)
            or not getattr(run, 'source_binding_revision', None) or run.source_instance != instance):
        return UNAVAILABLE
    config = bound_configuration(run)
    client, _, source = _client_and_downloader(config)
    if source != instance:
        return UNAVAILABLE
    try:
        return lookup(client, record, timezone)
    except HeroClientError:
        # Keep the recurring occurrence reviewable on outages or rate limits.
        return UNAVAILABLE


def original_pending_occurrence(frappe, technical_link, event, record, instance):
    """Recognize an Event inserted and held by the old single-occurrence importer."""
    from hero_integration.services.migration import _digest
    from hero_integration.services.native_projections import RECURRING_REVIEW

    if (technical_link.sync_status != 'Pending Review' or record.get('is_recurring') is not True
            or record.get('deleted') is not False or not event.creation or event.creation > technical_link.creation
            or str(event.get('custom_hero_source_id')) != technical_link.source_id):
        return False
    link = frappe.get_doc('HERO Integration Link', technical_link.name, for_update=True)
    if link.conflict_detail != RECURRING_REVIEW:
        return False
    if link.target_incarnation:
        from hero_integration.services.binding_registry import matches_incarnation
        return matches_incarnation(link.target_incarnation, event.creation)
    if (link.source_payload_hash != _digest(record)
            or event.get('custom_hero_original_start') != record.get('start')
            or frappe.db.exists('Deleted Document', {'deleted_doctype':'Event', 'deleted_name':event.name})):
        return False
    receipt = frappe.get_all('HERO Integration Record', filters={
        'source_type':'CalendarEvent', 'source_id':link.source_id, 'target_doctype':'Event',
        'target_name':event.name, 'result':'PENDING_REVIEW', 'technical_action':'hero_to_frappe',
        'detail':['like', RECURRING_REVIEW + '%'],
    }, pluck='run', limit_page_length=1, order_by='creation')
    return bool(receipt and frappe.db.get_value('HERO Integration Run', receipt[0], 'source_instance') == instance)
