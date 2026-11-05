"""Explicit multi-source field values; source dependencies remain part of query approval."""
from hero_integration.services.direct_mapping import MISSING, lookup

TRANSFORMS = {'Customer Display Name', 'Coalesce'}


def validate_options(name, options):
    paths = options.get('source_paths')
    from hero_integration.services.query_mapping import PATH
    if not isinstance(paths, list) or not 2 <= len(paths) <= 8 or len(paths) != len(set(paths)) or any(
            not isinstance(path, str) or not PATH.fullmatch(path) for path in paths):
        raise ValueError('Multi-source mappings require explicit unique source paths.')
    if name == 'Customer Display Name' and len(paths) != 4:
        raise ValueError('Customer Display Name requires company, full, first and last name paths.')


def source_value(spec, record, options):
    validate_options(spec.transformation, options)
    values = [lookup(record, path) for path in options['source_paths']]
    if spec.transformation == 'Customer Display Name':
        for value in values:
            if value is not MISSING and value is not None and not isinstance(value, str):
                raise ValueError('Mapped customer name components must be text.')
        company, full, first, last = values
        for value in (company, full):
            if isinstance(value, str) and value.strip(): return value.strip()
        parts = [value.strip() for value in (first,last) if isinstance(value,str) and value.strip()]
        if parts: return ' '.join(parts)
    else:
        for value in values:
            if value is not MISSING and value is not None and not (isinstance(value,str) and not value.strip()):
                return value
    return MISSING if all(value is MISSING for value in values) else None


def country_name(value):
    """Resolve an ISO country code against native Country records without defaults."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) != 2 or not value.strip().isascii() or not value.strip().isalpha():
        raise ValueError('Country Code requires an explicit two-letter country code.')
    import frappe
    rows = frappe.get_all('Country', filters={'code': value.strip().lower()}, fields=['name'], limit_page_length=2)
    if len(rows) != 1:
        raise ValueError('Country code must resolve to exactly one native Country.')
    frappe.has_permission('Country', 'read', doc=frappe.get_doc('Country', rows[0].name), throw=True)
    return rows[0].name


def bound_user(value, source_instance):
    """Resolve only the source-scoped User binding used by the assignment boundary."""
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError('User identity must be a positive integer.')
    import frappe
    rows = frappe.db.sql('''SELECT target_name FROM `tabHERO Object Binding`
        WHERE site_scope=%s AND source_instance=%s AND object_type=%s AND external_object_id=%s
          AND target_doctype=%s AND lifecycle=%s ORDER BY name LIMIT 2''',
        (frappe.local.site, source_instance, 'User', str(value), 'User', 'VALID'), as_dict=True)
    if len(rows) != 1 or not frappe.db.get_value('User', rows[0].target_name, 'enabled'):
        return None
    frappe.has_permission('User', 'read', doc=frappe.get_doc('User', rows[0].target_name), throw=True)
    return rows[0].target_name
