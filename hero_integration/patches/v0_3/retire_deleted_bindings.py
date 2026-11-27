"""Release historical native claims and remove their fields from active synchronization."""
import frappe

from hero_integration.services.binding_registry import RETIRED_LINK_SYSTEM


def execute():
    frappe.db.sql("""UPDATE `tabHERO Field Binding` f JOIN `tabHERO Object Binding` b
        ON f.object_binding=b.name AND f.site_scope=b.site_scope
        SET f.sync_state='INACTIVE', f.reconciliation=b.reconciliation, f.binding_revision=f.binding_revision+1
        WHERE b.site_scope=%s AND b.lifecycle='LEGACY' AND NOT (f.sync_state <=> 'INACTIVE')""", frappe.local.site)
    frappe.db.sql("""UPDATE `tabHERO Integration Link` SET source_system=%s, source_key=CONCAT('retired:', name)
        WHERE source_system='HERO' AND sync_status='Source Deleted'
          AND conflict_detail='ERPNext document deleted; historical link.'""", RETIRED_LINK_SYSTEM)
