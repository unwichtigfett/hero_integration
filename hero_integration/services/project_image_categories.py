"""Native category configuration and guarded local File classification."""
from __future__ import annotations

import frappe
import re

from hero_integration.services.project_workspace import IMAGE_TYPES, _text


DOCTYPE = "Project Image Category"
FIELD = "custom_project_image_category"
DEFAULTS = ("Dach", "Elektro")


def ensure_field():
    """Create only the app-owned File Link; refuse a conflicting definition."""
    field = frappe.get_meta("File").get_field(FIELD)
    if field:
        if field.fieldtype != "Link" or field.options != DOCTYPE or field.read_only or field.permlevel:
            frappe.throw(frappe._("The project image category field has an incompatible definition."))
        return
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields({"File": [{
        "fieldname": FIELD, "label": "Project Image Category", "fieldtype": "Link",
        "options": DOCTYPE, "insert_after": "file_name", "no_copy": 1,
        "description": "Local category for a project image. Categories are managed by a System Manager.",
    }]}, update=False)


def install_defaults():
    """Called once by installation/patch, never re-seed on ordinary migrations."""
    ensure_field()
    for name in DEFAULTS:
        if not frappe.db.exists(DOCTYPE, name):
            frappe.get_doc({"doctype": DOCTYPE, "category_name": name}).insert()


def prepare_import_category(document, mapped_fields):
    """Classify a new/relinked imported image after approved mapping evaluation."""
    if (document.get("attached_to_doctype") != "Project" or not document.get("attached_to_name")
            or str(document.get("file_type") or "").upper() not in IMAGE_TYPES):
        return
    previous = document.get_doc_before_save()
    relinked = previous and any(previous.get(key) != document.get(key)
        for key in ("attached_to_doctype", "attached_to_name"))
    # Existing, untouched local classifications (including legacy empty values)
    # remain outside the import's authority when this field is not mapped.
    if previous and not relinked and FIELD not in mapped_fields:
        return
    value = document.get(FIELD)
    if value and frappe.db.exists(DOCTYPE, {"name": value, "disabled": 0}):
        return
    # Respect disabled categories. Reuse an active fallback, or create a small
    # dedicated category idempotently through its native controller.
    name = "HERO-Import"
    index = 1
    while frappe.db.exists(DOCTYPE, name):
        if frappe.db.exists(DOCTYPE, {"name": name, "disabled": 0}):
            document.set(FIELD, name)
            return
        index += 1
        name = "HERO-Import-" + str(index)
    frappe.only_for("System Manager")
    category = frappe.get_doc({"doctype": DOCTYPE, "category_name": name}).insert()
    document.set(FIELD, category.name)


def ensure_upload_folder(category):
    """Bind an actual native File folder; never infer categories from filenames."""
    frappe.only_for("System Manager")
    if category.get("upload_folder"):
        folder = frappe.get_doc("File", category.get("upload_folder"))
        if not folder.get("is_folder"):
            frappe.throw(frappe._("The image category upload folder is invalid."))
        return folder.name
    if not frappe.db.get_value("File", "Home/Attachments", "is_folder"):
        frappe.throw(frappe._("The native attachments folder is unavailable."))
    label = re.sub(r"[^\w -]", "_", category.name)[:40]
    folder = frappe.get_doc({
        "doctype": "File", "is_folder": 1, "is_private": 0, "folder": "Home/Attachments",
        "file_name": "Project images - " + label + " - " + frappe.generate_hash(length=8),
    }).insert()
    # Internal, create-only linkage. Native uploads still create the real File,
    # enforce limits/privacy and run normal validation and rollback cleanup.
    category.db_set("upload_folder", folder.name, update_modified=False)
    return folder.name


def ensure_upload_folders():
    frappe.only_for("System Manager")
    for row in frappe.get_all(DOCTYPE, fields=["name"], limit_page_length=5000):
        ensure_upload_folder(frappe.get_doc(DOCTYPE, row.name))


@frappe.whitelist(methods=["GET"])
def get_upload_options(project: str, category: str):
    """Read the configured category's folder before opening the native uploader."""
    if not getattr(frappe.session, "user", None) or frappe.session.user == "Guest":
        frappe.throw(frappe._("Log in to categorize project images."), frappe.PermissionError)
    project, category = _text(project, 140), _text(category, 140)
    frappe.get_doc("Project", project).check_permission("write")
    record = frappe.get_doc(DOCTYPE, category)
    record.check_permission("read")
    if record.get("disabled"):
        frappe.throw(frappe._("This image category is disabled. Choose an active category."))
    if not record.get("upload_folder"):
        frappe.throw(frappe._("Ask an administrator to configure this image category's upload folder."))
    folder = frappe.get_doc("File", record.get("upload_folder"))
    folder.check_permission("read")
    if not folder.get("is_folder"):
        frappe.throw(frappe._("The image category upload folder is invalid."))
    return {"project": project, "category": record.name, "folder": folder.name}


def validate_file_category(document, method=None):
    """Apply to native File writes too, not only the custom RPC."""
    value = document.get(FIELD)
    previous = document.get_doc_before_save()
    old = previous.get(FIELD) if previous else None
    relinked = previous and any(previous.get(field) != document.get(field)
        for field in ("attached_to_doctype", "attached_to_name"))
    project_image = (document.get("attached_to_doctype") == "Project" and document.get("attached_to_name")
        and str(document.get("file_type") or "").upper() in IMAGE_TYPES)
    if project_image and not value and (previous is None or relinked):
        category = frappe.db.get_value(DOCTYPE, {"upload_folder": document.get("folder")},
            ["name", "disabled"], as_dict=True) if document.get("folder") else None
        if category and not category.disabled:
            value = category.name
            document.set(FIELD, value)
        else:
            frappe.throw(frappe._("Choose an image category before uploading a project image."))
    if project_image and old and not value:
        frappe.throw(frappe._("An image category is required. Choose another category instead of clearing it."))
    changed = (value or "") != (old or "")
    if not value and not changed:
        return
    if value and (document.get("attached_to_doctype") != "Project"
            or not document.get("attached_to_name")
            or str(document.get("file_type") or "").upper() not in IMAGE_TYPES):
        frappe.throw(frappe._("Categories can only be assigned to project image attachments."))
    if not changed and not relinked:
        return  # Retired categories remain valid on existing images.
    for parent in (previous, document):
        if parent and parent.get("attached_to_doctype") == "Project" and parent.get("attached_to_name"):
            frappe.get_doc("Project", parent.get("attached_to_name")).check_permission("write")
    if value and changed:
        category = frappe.get_doc(DOCTYPE, value)
        category.check_permission("read")
        if category.get("disabled"):
            frappe.throw(frappe._("This image category is disabled. Choose an active category."))


@frappe.whitelist(methods=["POST"])
def set_image_category(project: str, file: str, category: str = "", expected_modified: str = ""):
    """Change one local classification, retaining native File saving and audit."""
    if not getattr(frappe.session, "user", None) or frappe.session.user == "Guest":
        frappe.throw(frappe._("Log in to categorize project images."), frappe.PermissionError)
    project, file = _text(project, 140), _text(file, 140)
    category = _text(category, 140, required=False)
    if not category:
        frappe.throw(frappe._("An image category is required. Choose another category instead of clearing it."))
    expected_modified = _text(expected_modified, 40)
    frappe.get_doc("Project", project).check_permission("write")
    document = frappe.get_doc("File", file)
    document.check_permission("write")
    if (document.get("attached_to_doctype") != "Project" or document.get("attached_to_name") != project
            or str(document.get("file_type") or "").upper() not in IMAGE_TYPES):
        frappe.throw(frappe._("This image does not belong to the selected project."), frappe.PermissionError)
    if str(document.get("modified")) != expected_modified:
        frappe.throw(frappe._("The image changed. Refresh the image list before categorizing it."),
            frappe.TimestampMismatchError)
    document.set(FIELD, category or None)
    document.save()
    return {"file": document.name, "category": document.get(FIELD), "modified": str(document.get("modified"))}
