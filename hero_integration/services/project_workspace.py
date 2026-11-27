"""Bounded, permission-checked reads for the native Project form extension."""
from __future__ import annotations

import frappe


PAGE_SIZE = 20
IMAGE_TYPES = ("JPG", "JPEG", "PNG", "GIF", "WEBP", "BMP", "AVIF")
TASK_STATUSES = ("Open", "Working", "Pending Review", "Overdue", "Template", "Completed", "Cancelled")
SECTIONS = frozenset({"details", "documents", "images", "appointments", "tasks", "assignments"})


@frappe.whitelist(methods=["GET"])
def get_panel(project: str, section: str, start: int | str = 0, search: str = "", status: str = "",
        category: str = "", uncategorized: int | str = 0):
    """Read one saved Project panel; never change documents or contact HERO."""
    if not getattr(frappe.session, "user", None) or frappe.session.user == "Guest":
        frappe.throw(frappe._("Log in to view project information."), frappe.PermissionError)
    project = _text(project, 140)
    section = _text(section, 30)
    search = _text(search, 120, required=False)
    status = _text(status, 30, required=False)
    category = _text(category, 140, required=False)
    if str(uncategorized) not in ("0", "1") or (category and str(uncategorized) == "1"):
        frappe.throw(frappe._("Unsupported project view or filter."))
    if section != "images" and (category or str(uncategorized) == "1"):
        frappe.throw(frappe._("Unsupported project view or filter."))
    if section not in SECTIONS or (status and (section != "tasks" or status not in TASK_STATUSES)):
        frappe.throw(frappe._("Unsupported project view or filter."))
    if (isinstance(start, bool) or len(str(start)) > 6
            or not str(start).isascii() or not str(start).isdigit()):
        frappe.throw(frappe._("Invalid project page."))
    start = int(start)
    if start > 100000:
        frappe.throw(frappe._("Invalid project page."))

    # Loading a document alone is not authorization. Field-level filtering also
    # removes protected links before following them and masks sensitive values.
    document = _read("Project", project)
    result = {
        "project": document.name,
        "modified": str(document.get("modified") or ""),
        "section": section,
        "state": "ready",
        "rows": [],
        "next_start": None,
    }
    try:
        if section == "details":
            result["context"] = _context(document)
        else:
            result.update(_items(document, section, start, search, status, category, int(uncategorized)))
            if section == "images":
                categories = frappe.get_list("Project Image Category", fields=["name"],
                    filters={"disabled": 0}, order_by="name", limit_page_length=201)
                result["categories"] = [row["name"] for row in categories[:200]]
                result["categories_truncated"] = len(categories) > 200
    except frappe.PermissionError:
        # A readable Project must not grant access to another DocType.
        result["state"] = "permission_denied"
    return result


def _text(value, maximum, *, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        frappe.throw(frappe._("Invalid project view input."))
    return value.strip()


def _read(doctype, name):
    document = frappe.get_doc(doctype, name)
    document.check_permission("read")
    document.apply_fieldlevel_read_permissions()
    return document


def _linked(doctype, name):
    if not isinstance(name, str) or not name:
        return None
    try:
        return _read(doctype, name)
    except (frappe.PermissionError, frappe.DoesNotExistError):
        return None


def _fields(document, names):
    return {field: document.get(field) for field in names}


def _context(project):
    customer = _linked("Customer", project.get("customer"))
    if not customer:
        return {"customer": None, "address": None, "contact": None}
    contact = _linked("Contact", customer.get("customer_primary_contact"))
    return {
        "customer": {"name": customer.name, **_fields(customer, ("customer_name",))},
        # Kept for response compatibility; the redundant customer-address card
        # is gone and its private Address document is no longer queried.
        "address": None,
        "contact": None if not contact else {
            "name": contact.name,
            **_fields(contact, ("first_name", "last_name", "email_id", "phone", "mobile_no")),
        },
    }


def _items(project, section, start, search, status, category="", uncategorized=0):
    filters, or_filters = {}, []
    if section in ("documents", "images"):
        doctype, order = "File", "creation desc, name desc"
        filters = {
            "attached_to_doctype": "Project", "attached_to_name": project.name, "is_folder": 0,
            "file_type": ["in", IMAGE_TYPES],
        }
        if section == "documents":
            # NULL file types must not disappear from the document list.
            filters.pop("file_type")
            or_filters = [
                ["File", "file_type", "not in", IMAGE_TYPES],
                ["File", "file_type", "is", "not set"],
            ]
        if search:
            filters["file_name"] = ["like", "%" + search + "%"]
        if section == "images":
            if category:
                filters["custom_project_image_category"] = category
            elif uncategorized:
                filters["custom_project_image_category"] = ["is", "not set"]
    elif section == "tasks":
        doctype, order = "Task", "exp_end_date asc, name asc"
        filters = {"project": project.name}
        if status:
            filters["status"] = status
        if search:
            or_filters = [["subject", "like", "%" + search + "%"], ["name", "like", "%" + search + "%"]]
    elif section == "assignments":
        doctype, order = "ToDo", "date asc, name asc"
        filters = {"reference_type": "Project", "reference_name": project.name}
        if search:
            filters["description"] = ["like", "%" + search + "%"]
    else:
        doctype, order = "Event", "starts_on desc, name desc"
        # Frappe supplies the permission-aware parent query and child-table join.
        # A same-named reference to another DocType is rejected again below.
        or_filters = [
            ["Event", "reference_docname", "=", project.name],
            ["Dynamic Link", "link_name", "=", project.name],
            ["Event Participants", "reference_docname", "=", project.name],
        ]
        if frappe.get_meta("Event").has_field("custom_hero_project"):
            or_filters.append(["Event", "custom_hero_project", "=", project.name])
        if search:
            filters["subject"] = ["like", "%" + search + "%"]

    candidates = frappe.get_list(
        doctype, fields=["name"], filters=filters, or_filters=or_filters,
        order_by=order, limit_start=start, limit_page_length=PAGE_SIZE + 1,
        distinct=True,
    )
    rows = []
    for candidate in candidates[:PAGE_SIZE]:
        try:
            document = _read(doctype, candidate["name"])
        except (frappe.PermissionError, frappe.DoesNotExistError):
            # Access can change between the list query and document load.
            continue
        row = _row(document, project.name, section)
        if row is not None:
            rows.append(row)
    return {
        "rows": rows,
        # Advance over candidates, not returned rows; no duplicates or skipped
        # pages when field/document permissions suppress individual candidates.
        "next_start": start + PAGE_SIZE if len(candidates) > PAGE_SIZE else None,
    }


def _row(document, project, section):
    if section in ("documents", "images"):
        if document.get("attached_to_doctype") != "Project" or document.get("attached_to_name") != project:
            return None
        is_image = str(document.get("file_type") or "").upper() in IMAGE_TYPES
        if is_image != (section == "images"):
            return None
        values = _fields(document, ("file_name", "file_type", "file_size", "is_private", "creation"))
        url = document.get("file_url")
        # Never turn an external attachment into a remote preview or an active
        # URL. Unsupported files can still be opened in the native File form.
        values["file_url"] = url if _local_file_url(url) else None
        values["modified"] = str(document.get("modified") or "")
        if section == "images":
            values["category"] = document.get("custom_project_image_category")
    elif section == "tasks":
        if document.get("project") != project:
            return None
        values = _fields(document, ("subject", "status", "priority", "exp_end_date", "progress", "_assign"))
    elif section == "assignments":
        if document.get("reference_type") != "Project" or document.get("reference_name") != project:
            return None
        values = _fields(document, ("allocated_to", "description", "date", "status"))
    else:
        if not _event_is_linked(document, project):
            return None
        values = _fields(document, (
            "subject", "starts_on", "ends_on", "status", "event_category", "all_day", "repeat_this_event",
            "custom_hero_recurrence_interval", "repeat_on", "repeat_till",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        ))
        values["participants"] = [
            {"doctype": row.get("reference_doctype"), "name": row.get("reference_docname")}
            for row in document.get("event_participants") or []
            if row.get("reference_doctype") in ("User", "Employee") and row.get("reference_docname")
        ]
    return {"name": document.name, **values}


def _event_is_linked(document, project):
    return (
        document.get("reference_doctype") == "Project" and document.get("reference_docname") == project
    ) or document.get("custom_hero_project") == project or any(
        row.get("reference_doctype") == "Project" and row.get("reference_docname") == project
        for row in document.get("event_participants") or []
    ) or any(
        row.get("link_doctype") == "Project" and row.get("link_name") == project
        for row in document.get("links") or []
    )


def _local_file_url(value):
    if not isinstance(value, str) or not value.startswith(("/files/", "/private/files/")):
        return False
    from urllib.parse import unquote, urlsplit

    decoded = unquote(value)
    return (
        not any(ord(character) < 32 or ord(character) == 127 or character == "\\" for character in decoded)
        and not any(part in (".", "..") for part in urlsplit(decoded).path.split("/"))
        and not urlsplit(decoded).netloc
    )
