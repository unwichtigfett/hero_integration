"""Install binding lookup indexes on existing sites and hold unproven historical claims."""
import frappe


def execute():
    from hero_integration.hero_integration.doctype.hero_field_binding.hero_field_binding import on_doctype_update as index_fields
    from hero_integration.hero_integration.doctype.hero_object_binding.hero_object_binding import on_doctype_update as index_objects

    index_objects()
    index_fields()
    reason = "Historical binding has no verified native incarnation."
    frappe.db.sql("""UPDATE `tabHERO Object Binding` SET lifecycle='REVIEW', reconciliation=%s,
        binding_revision=binding_revision+1 WHERE lifecycle='VALID' AND COALESCE(target_incarnation,'')=''""", reason)
    frappe.db.sql("""UPDATE `tabHERO Integration Link` l JOIN `tabHERO Object Binding` b
        ON b.site_scope=%s AND b.source_instance=l.source_instance AND b.target_doctype=l.target_doctype
        AND b.target_name=l.target_name SET l.sync_status='Conflict', l.conflict_detail=%s
        WHERE l.source_system='HERO' AND b.lifecycle='REVIEW' AND b.reconciliation=%s
        AND l.sync_status='Synchronized'""", (frappe.local.site, reason, reason))
