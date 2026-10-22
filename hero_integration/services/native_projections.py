"""Finite Item/Event adapters; no accounting, stock, sharing or outbound actions."""
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
        if set(options) != {'confirmed', 'item_group', 'uom_map', 'is_stock_item'}:
            raise ValueError('Item configuration has unknown or missing options.')
        if not isinstance(options['item_group'], str) or not options['item_group'].strip():
            raise ValueError('Item Group must be explicitly configured.')
        if not isinstance(options['uom_map'], dict) or not options['uom_map'] or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in options['uom_map'].items()):
            raise ValueError('An explicit HERO unit_type to native UOM map is required.')
        if type(options['is_stock_item']) is not bool or (kind == 'service' and options['is_stock_item']):
            raise ValueError('Stock behavior must be explicit; services cannot be stock items.')


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


def values(contract, record, identity, instance, options, *, lookup, item_link=None):
    """Resolve native values before _upsert; lookup must enforce native read permission."""
    kind = contract.get('native_kind')
    validate_options(contract, options)
    if not kind:
        product_id = record.get('product_id')
        if not isinstance(product_id, str) or not product_id.strip() or item_link is None:
            raise ValueError('Product version requires a verified source-product Item binding.')
        return {'custom_hero_item': item_link(product_id)}
    if kind in {'product', 'service'}:
        group = lookup('Item Group', options['item_group'])
        if group.get('is_group'):
            raise ValueError('Item Group must be a leaf group.')
        source_unit = record.get('unit_type')
        if source_unit not in options['uom_map']:
            raise ValueError('HERO unit_type has no explicit native UOM binding.')
        uom_name = options['uom_map'][source_unit]
        if not lookup('UOM', uom_name).get('enabled'):
            raise ValueError('Configured UOM is not enabled.')
        return {'item_code': 'HERO-' + key_for(instance, contract['source_type'], identity),
                'item_name': _text(record.get('name'), 'Item name', required=True, limit=140),
                'description': _html(_text(record.get('description'), 'Item description')),
                'stock_uom': uom_name, 'item_group': options['item_group']}
    if record.get('is_recurring') is True:
        raise ValueError('Recurring HERO event has no reviewed recurrence rule; native recurrence is blocked.')
    if type(record.get('all_day')) is not bool:
        raise ValueError('Calendar all_day must be explicit; null is not false.')
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
    resources = [{'custom_hero_source_id': str(r['id']), 'custom_hero_resource_name': _text(r.get('name'), 'Resource name', limit=140),
                  'custom_hero_resource_type': _text(r.get('type'), 'Resource type', limit=140), 'custom_hero_type_id': str(r['type_id']) if r.get('type_id') is not None else None}
                 for r in record.get('resources') or []]
    partners = [{'custom_hero_source_id': str(r['id']), 'custom_hero_partner_name': _text(r.get('full_name'), 'Partner name', limit=140)} for r in record.get('partners') or []]
    return {'subject': _text(record.get('title'), 'Event title', required=True), 'description': _html(_text(record.get('description'), 'Event description')),
            'starts_on': starts_on, 'ends_on': ends_on, 'all_day': int(record['all_day']),
            'location': _text(record.get('full_address'), 'Event location', limit=140),
            'custom_hero_resources': resources, 'custom_hero_partners': partners,
            'custom_hero_original_start': record.get('start'), 'custom_hero_original_end': record.get('end')}


def creation_defaults(contract, options):
    """Creation-only guardrails; never overwrite a locally changed existing document."""
    if contract.get('native_kind') == 'event':
        return {'event_type': 'Private', 'event_category': 'Event', 'status': 'Open',
                'repeat_this_event': 0, 'send_reminder': 0, 'sync_with_google_calendar': 0,
                'add_video_conferencing': 0, 'event_participants': [], 'notifications': []}
    if contract.get('native_kind') in {'product', 'service'}:
        return {'is_stock_item': int(options['is_stock_item']), 'is_fixed_asset': 0, 'auto_create_assets': 0,
                'opening_stock': 0, 'standard_rate': 0, 'valuation_rate': 0, 'has_serial_no': 0, 'has_batch_no': 0,
                'is_sales_item': 1, 'is_purchase_item': int(contract['native_kind'] == 'product')}
    return {}
