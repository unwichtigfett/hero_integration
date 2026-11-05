"""Finite Event adapter; no sharing or outbound actions.

HERO articles are imported by services.product_migration (configurable
HeroProducts mapping), not through a projection adapter."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from html import escape
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_options(contract, options):
    kind = contract.get('native_kind')
    if not kind:
        return
    if not isinstance(options, dict) or options.get('confirmed') is not True:
        raise ValueError('Native adapter requires explicitly confirmed local configuration.')
    if kind == 'event':
        if set(options) != {'confirmed', 'source_timezone', 'target_timezone', 'all_day_end'}:
            raise ValueError('Event configuration has unknown or missing options.')
        for name in ('source_timezone', 'target_timezone'):
            if not isinstance(options[name], str) or not options[name]:
                raise ValueError('Event source and target timezones must be explicit.')
            try:
                ZoneInfo(options[name])
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError('Event timezone is not a known IANA zone.') from error
        if options['all_day_end'] not in {'inclusive', 'exclusive'}:
            raise ValueError('Event all-day end semantics must be explicit.')
    else:
        raise ValueError('Unsupported native adapter kind.')


def key_for(instance, entity_type, identity):
    raw = json.dumps([instance, entity_type, identity], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _text(value, field, *, required=False, limit=None):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(field + ' requires nonempty text.')
    value = value.strip()
    if limit and len(value) > limit:
        raise ValueError(field + ' exceeds the native field length.')
    return value


EVENT_SUBJECT_FALLBACK = 'HERO-Termin'


def _event_subject(record, *, creating):
    """HERO allows untitled events; ERPNext requires a subject.

    A new Event without a HERO title is named after its event type. An existing
    Event keeps its subject when the HERO title is empty.
    """
    title = record.get('title')
    if isinstance(title, str) and title.strip():
        return _text(title, 'Event title', limit=140)
    if not creating:
        return _text(title, 'Event title')
    for key in ('localized_type', 'type'):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:140]
    return EVENT_SUBJECT_FALLBACK


def _html(value):
    # Preserve text, not executable HTML or remote attachments.
    return '<p>' + escape(value or '').replace('\n', '<br>') + '</p>'


def _instant(value, zone):
    if not isinstance(value, str) or not value:
        raise ValueError('Calendar start/end requires an ISO datetime.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        return parsed
    first = parsed.replace(tzinfo=zone, fold=0)
    second = parsed.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset() or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != parsed:
        raise ValueError('Ambiguous or nonexistent local calendar time requires an explicit offset.')
    return first


RECURRING_REVIEW = ('HERO marks this event as recurring but supplies no recurrence rule; '
                    'only the delivered occurrence was imported as a single event.')


def values(contract, record, identity, instance, options, *, lookup, allow_partial=False, review=None):
    """Resolve native values before _upsert; lookup must enforce native read permission."""
    kind = contract.get('native_kind')
    validate_options(contract, options)
    if kind != 'event':
        raise ValueError('Unsupported native adapter kind.')
    times = {}
    try:
        if type(record.get('is_recurring')) is not bool:
            raise ValueError('Calendar recurrence flag must be explicit; null is not false.')
        if record['is_recurring']:
            # HERO exposes no recurrence rule: review new single occurrences and keep
            # existing Event times.
            if allow_partial or review is None:
                raise ValueError('Recurring HERO event has no reviewed recurrence rule; native recurrence is blocked.')
            review.append(RECURRING_REVIEW)
        if type(record.get('all_day')) is not bool:
            raise ValueError('Calendar all_day must be explicit; null is not false.')
        if allow_partial and record.get('end') is None:
            raise ValueError('Calendar time group is incomplete; existing time values are preserved.')
        source_zone, target_zone = ZoneInfo(options['source_timezone']), ZoneInfo(options['target_timezone'])
        start = _instant(record.get('start'), source_zone)
        end = _instant(record['end'], source_zone) if record.get('end') is not None else None
        if end is not None and (end < start or (end == start and not record['all_day'])):
            raise ValueError('Calendar end must be after start.')
        if record['all_day']:
            start_date = start.astimezone(source_zone).date()
            end_date = end.astimezone(source_zone).date() if end else start_date
            if end and options['all_day_end'] == 'exclusive':
                end_date -= timedelta(days=1)
            if end_date < start_date:
                raise ValueError('All-day event range is empty after applying end semantics.')
            starts_on = str(datetime.combine(start_date, time.min))
            ends_on = str(datetime.combine(end_date, time(23, 59, 59)))
        else:
            starts_on = str(start.astimezone(target_zone).replace(tzinfo=None))
            ends_on = str(end.astimezone(target_zone).replace(tzinfo=None)) if end else None
        times = {'starts_on': starts_on, 'ends_on': ends_on, 'all_day': int(record['all_day']),
                 'custom_hero_original_start': record.get('start'), 'custom_hero_original_end': record.get('end')}
        if end is None:
            times.pop('ends_on')
            times.pop('custom_hero_original_end')
    except ValueError as error:
        if not allow_partial:
            raise
        if review is not None:
            review.append(str(error))
    resources = [{'custom_hero_source_id': str(r['id']), 'custom_hero_resource_name': _text(r.get('name'), 'Resource name', limit=140),
                  'custom_hero_resource_type': _text(r.get('type'), 'Resource type', limit=140), 'custom_hero_type_id': str(r['type_id']) if r.get('type_id') is not None else None}
                 for r in record.get('resources') or []]
    partners = [{'custom_hero_source_id': str(r['id']), 'custom_hero_partner_name': _text(r.get('full_name'), 'Partner name', limit=140)} for r in record.get('partners') or []]
    # A HERO appointment marked done is a completed native Event.
    status = {'status': 'Completed' if record['is_done'] is True else 'Open'} if 'is_done' in record else {}
    return {'subject': _event_subject(record, creating=not allow_partial), 'description': _html(_text(record.get('description'), 'Event description')),
            'location': _text(record.get('full_address'), 'Event location', limit=140),
            'custom_hero_resources': resources, 'custom_hero_partners': partners,
            **status, **times}


def creation_defaults(contract, options):
    """Creation-only guardrails; never overwrite a locally changed existing document."""
    if contract.get('native_kind') == 'event':
        return {'event_type': 'Private', 'event_category': 'Event', 'status': 'Open',
                'repeat_this_event': 0, 'send_reminder': 0, 'sync_with_google_calendar': 0,
                'add_video_conferencing': 0, 'event_participants': [], 'notifications': []}
    return {}
