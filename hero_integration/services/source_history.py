"""Additional source observations owned by the selected read contract."""
import json
from datetime import date
from math import isfinite

ABSENCE_FIELD = 'custom_hero_absence_history'
FILE_FIELD = 'custom_hero_file_detail'
ABSENCE_PATHS = ('id', 'partner_id', 'start', 'end', 'duration', 'budget', 'comment', 'type',
                 'status', 'start_budget', 'end_budget', 'created', 'modified')
DOC_SCALARS = ('id', 'nr', 'status_code', 'status_name', 'type', 'localized_type', 'date', 'value',
               'vat', 'currency', 'booking_relevant', 'is_gaeb', 'source', 'source_id',
               'document_type_id', 'project_match_id', 'company_id', 'contact_id', 'partner_id',
               'customer_invoice_id', 'file_upload_id', 'file_upload_folder_id',
               'published_customer_document_draft_id', 'selected_document_id', 'created', 'modified')
FILE_DETAIL_PATHS = (
    'id', 'uuid', 'project_id', 'category', 'section', 'image_category', 'filename', 'size',
    'type', 'icon', 'created', 'modified', 'preview_available', 'is_deleted',
    *('customer_document.' + key for key in DOC_SCALARS),
    *('customer_document.document_type.' + key for key in ('id','name','base_type','booking_relevant')),
    'customer_document.metadata.is_signed', 'customer_document.file_upload_folder.name',
    *('customer_document.customer_document_booking.' + key for key in ('id', 'status', 'status_name',
            'is_open', 'due_date', 'paid_date', 'balance', 'discount_rate', 'discount_time', 'discount_date')),
    'customer_document.published_customer_document_draft.id', 'customer_document.partner.id',
    'customer_document.contact.id', 'customer_document.project_match.id', 'customer_document.company.id',
    'customer_document.customer.id', 'customer_document.file_upload.uuid',
    'customer_document.file_upload_folder.id',
    *('customer_document.document_links.' + key for key in ('id','target','target_id','target_relation')),
    *('file_upload_links.' + key for key in ('id','file_upload_id','target','target_id','target_relation',
                                             'project_match_id','customer_id','created','modified')),
    *('file_upload_matches.' + key for key in ('id','file_upload_id','project_match_id','created','modified')),
)

def enabled(read, kind):
    return (getattr(getattr(read, 'plan', None), 'adapter_options', None) or {}).get('source_history') == kind

def file_values(detail):
    values = {FILE_FIELD: json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}
    doc = detail.get('customer_document') or {}
    for key in DOC_SCALARS:
        value = doc.get(key)
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        elif value is not None:
            value = str(value)
        values['custom_hero_doc_' + key] = value
    return values

def read_absences(client, read, instance, partner_ids, verify=None):
    from hero_integration.services.direct_runtime import validated_snapshot
    from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft
    ids = sorted(set(partner_ids))
    result = {key: [] for key in ids}
    seen = set()
    if not ids:
        return result
    snapshot = validated_snapshot(read, instance)
    for start in range(0, len(ids), 25):
        batch = ids[start:start + 25]
        offset = 0
        previous = 0
        while True:
            if verify:
                verify()
            request = QueryAdapter().materialize(snapshot, QueryDraft('HeroPartnerAbsences', 'absences',
                'absences', ABSENCE_PATHS, {'first': 100, 'offset': offset, 'partner_ids': batch,
                'orderBy': 'id', 'show_all_partners': True}))
            page = client.query(request.document, request.variables).get('absences')
            if verify:
                verify()
            if not isinstance(page, list) or len(page) > 100:
                raise ValueError('Invalid HERO absence page.')
            if not page:
                break
            for row in page:
                if not isinstance(row, dict) or not set(ABSENCE_PATHS) <= row.keys():
                    raise ValueError('Incomplete HERO absence metadata.')
                key, partner = row['id'], row['partner_id']
                if (type(key) is not int or key <= previous or key in seen or
                        type(partner) is not int or partner not in batch):
                    raise ValueError('Unexpected or duplicated HERO absence identity.')
                if not row['start'] or not row['end'] or date.fromisoformat(row['end']) < date.fromisoformat(row['start']):
                    raise ValueError('Invalid HERO absence period.')
                if any(v is not None and (type(v) not in (float, int) or not isfinite(v) or v < 0)
                       for v in (row['duration'], row['budget'])):
                    raise ValueError('Invalid HERO absence duration.')
                previous = key
                seen.add(key)
                result[partner].append({f:row[f] for f in ABSENCE_PATHS})
            offset += len(page)
            if offset > 10000:
                raise ValueError('HERO absence history exceeds the supported batch size.')
    return result

def merge_absences(document, rows, instance, *, changed_rows=None):
    """Update source-owned child rows, preserving earlier entries outside the source result."""
    existing = {}
    for child in document.get(ABSENCE_FIELD) or []:
        key = (child.source_instance, str(child.absence_id))
        if key in existing:
            raise ValueError('Duplicate local HERO absence identity.')
        existing[key] = child
    changed = False
    for row in rows:
        values = {'source_instance':instance, 'absence_id':str(row['id']), 'partner_id':str(row['partner_id']),
                  'from_date':row['start'], 'to_date':row['end'], 'duration_days':row['duration'] or 0,
                  'budget_days':row['budget'] or 0, 'has_budget':int(row['budget'] is not None),
                  'has_duration':int(row['duration'] is not None), 'absence_type':row['type'], 'source_status':row['status'],
                  'source_comment':row['comment'], 'start_budget':row['start_budget'], 'end_budget':row['end_budget'],
                  'source_created':row['created'], 'source_modified':row['modified']}
        child = existing.get((instance, values['absence_id']))
        if child is None:
            child = document.append(ABSENCE_FIELD, values)
        elif any((child.get(k) != v if k in ('budget_days', 'duration_days', 'has_duration', 'has_budget')
                  else str(child.get(k) or '') != str(v or '')) for k, v in values.items()):
            child.update(values)
        else:
            continue
        if changed_rows is not None:
            changed_rows.append(child)
        changed = True
    return changed


def store_absences(employee, rows, instance):
    """Persist only private child data while the employee row is locked by the importer.

    Employee.save() would copy absence comments into the ordinary Version history.
    Updating the parent timestamp also prevents a stale form from replacing these rows.
    """
    import frappe
    changed_rows = []
    if not merge_absences(employee, rows, instance, changed_rows=changed_rows):
        return False
    now = frappe.utils.now_datetime()
    for child in changed_rows:
        child.modified = now
        child.modified_by = frappe.session.user
        child.db_update()
    frappe.db.set_value('Employee', employee.name, {'modified': now, 'modified_by': frappe.session.user},
                        update_modified=False)
    return True
