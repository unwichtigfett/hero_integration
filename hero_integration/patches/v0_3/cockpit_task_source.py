"""Provision source data for the permission-filtered Cockpit task reader."""


def execute():
    import frappe
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    from hero_integration.services.cockpit_task_source import FIELD

    if frappe.db.exists('DocType', 'Task'):
        create_custom_fields({'Task': [dict(fieldname=FIELD, label='HERO Cockpit Task Source',
            fieldtype='Long Text', hidden=1, read_only=1, no_copy=1, permlevel=9)]})
    if not frappe.db.exists('DocType', 'Employee') or not frappe.get_meta('User').has_field('custom_hero_partner_id'):
        return
    uid = frappe.db.get_value('Custom Field', {'dt':'Employee', 'fieldname':'custom_hero_src_user_id'})
    if uid:
        field = frappe.get_doc('Custom Field', uid)
        field.update({'label':'HERO-Benutzer-ID (user_id)', 'hidden':0, 'insert_after':'user_id'})
        field.save()
    create_custom_fields({'Employee':[dict(fieldname='custom_hero_user_partner_id',
        label='HERO-Partner-ID (user_partner_id)', fieldtype='Data', read_only=1, no_copy=1,
        insert_after='custom_hero_src_user_id' if uid else 'user_id', fetch_from='user_id.custom_hero_partner_id')]})
