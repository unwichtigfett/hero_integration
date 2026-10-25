"""Personal mapping document access for native Frappe list/detail paths."""
import frappe


def personal_mapping_query(user=None, doctype=None):
    user = user or frappe.session.user
    if "System Manager" not in frappe.get_roles(user):
        return "1=0"
    column = "owner_user" if doctype == "HERO Mapping Draft" else "actor"
    return f"`tab{doctype}`.`{column}` = {frappe.db.escape(user)}"


def personal_mapping_permission(doc, user=None, permission_type=None):
    user = user or frappe.session.user
    owner = doc.owner_user if doc.doctype == "HERO Mapping Draft" else doc.actor
    return owner == user and "System Manager" in frappe.get_roles(user)


def check_personal_document(doc):
    # Document.check_permission also covers Administrator, whose native role
    # check bypasses normal has_permission hooks in Frappe v16.
    if not personal_mapping_permission(doc):
        frappe.throw("Personal mapping document access denied.", frappe.PermissionError)


def personal_version_query(user=None, doctype=None):
    user = frappe.db.escape(user or frappe.session.user)
    return ("(`tabVersion`.`ref_doctype` not in ('HERO Mapping Draft','HERO Mapping Revision')"
            " or (`tabVersion`.`ref_doctype`='HERO Mapping Draft' and exists "
            "(select 1 from `tabHERO Mapping Draft` d where d.name=`tabVersion`.docname and d.owner_user=" + user + "))"
            " or (`tabVersion`.`ref_doctype`='HERO Mapping Revision' and exists "
            "(select 1 from `tabHERO Mapping Revision` r where r.name=`tabVersion`.docname and r.actor=" + user + ")))" )


def personal_version_permission(doc, user=None, permission_type=None):
    if doc.ref_doctype not in ("HERO Mapping Draft", "HERO Mapping Revision"):
        return None
    column = "owner_user" if doc.ref_doctype == "HERO Mapping Draft" else "actor"
    owner = frappe.db.get_value(doc.ref_doctype, doc.docname, column)
    user = user or frappe.session.user
    return owner == user and "System Manager" in frappe.get_roles(user)


class PersonalMappingVersionAccess:
    def check_permission(self, *args, **kwargs):
        if personal_version_permission(self) is False:
            frappe.throw("Personal mapping version access denied.", frappe.PermissionError)
        return super().check_permission(*args, **kwargs)


def protect_managed_mapping(doc):
    """Keep existing active configuration separate from personal draft writes."""
    if doc.is_new():
        return
    previous = doc.get_doc_before_save()
    if not previous:
        return
    definition = previous.get("definition_id") or previous.name
    if not frappe.db.exists("DocType", "HERO Mapping Draft"):
        return
    if not frappe.db.exists("HERO Mapping Draft", {"definition_id": definition}):
        return
    fields = ("mapping_json", "schema_snapshot", "operation_kind", "operation_name", "direction", "status", "mapping_revision", "definition_id")
    if any(doc.get(field) != previous.get(field) for field in fields):
        frappe.throw("PERSONAL_DRAFT_REQUIRED: Use the personal mapping draft API.", frappe.PermissionError)
