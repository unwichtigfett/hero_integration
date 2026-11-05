"""Reinstall the dedicated test site, retaining identities only in memory.

Run inside the stopped test stack's backend. Credentials arrive on stdin;
the host updater owns locking, maintenance mode and failure recovery.
"""

import json
import os
from pathlib import Path
import secrets
import sys


IDENTITY_TYPES = ("Role", "Role Profile", "Module Profile", "User")
IDENTITY_CHILDREN = {"Has Role", "User Role Profile", "Block Module", "User Social Login"}


def capture_identities(frappe):
    rows = {}
    for doctype in IDENTITY_TYPES:
        rows[doctype] = frappe.db.sql(f"SELECT * FROM `tab{doctype}`", as_dict=True)
        if doctype == "User":
            for record in rows[doctype]:
                record["api_key"] = None
                record["api_secret"] = None
        for field in frappe.get_meta(doctype).get_table_fields():
            if field.options in IDENTITY_CHILDREN:
                rows.setdefault(field.options, []).extend(frappe.get_all(
                    field.options, filters={"parenttype": doctype}, fields=["*"]
                ))
    auth = frappe.db.sql(
        "SELECT * FROM `__Auth` WHERE doctype = 'User' AND fieldname = 'password' AND encrypted = 0",
        as_dict=True,
    )
    names = {row["name"] for row in rows["User"]}
    if not {"Administrator", "Guest"}.issubset(names):
        raise RuntimeError("Built-in users are missing; refusing to reset")
    if not any(row["name"] == "Administrator" and row["fieldname"] == "password" for row in auth):
        raise RuntimeError("Administrator password is missing; refusing to reset")
    return rows, auth


def restore_identities(frappe, rows, auth):
    # Direct persistence avoids welcome emails, Contact creation and password
    # regeneration from User hooks. The database was just installed from scratch.
    for doctype in rows:
        if doctype == "User":
            frappe.db.delete(doctype)
        elif doctype not in IDENTITY_TYPES:
            for parenttype in IDENTITY_TYPES:
                parents = [record["name"] for record in rows[parenttype]]
                if parents:
                    frappe.db.delete(doctype, {"parenttype": parenttype, "parent": ("in", parents)})
    for doctype, records in rows.items():
        for record in records:
            record = dict(record)
            if doctype == "User":
                for field in ("api_key", "api_secret", "user_image", "banner_image", "reset_password_key",
                              "last_reset_password_key_generated_on", "home_settings"):
                    record[field] = None
                workspace = record.get("default_workspace")
                if workspace and not frappe.db.exists("Workspace", workspace):
                    record["default_workspace"] = None
            if doctype not in IDENTITY_TYPES or not frappe.db.exists(doctype, record["name"]):
                frappe.get_doc(dict(record, doctype=doctype)).db_insert()
    frappe.db.sql("DELETE FROM `__Auth` WHERE doctype = 'User'")
    for record in auth:
        frappe.db.sql(
            "INSERT INTO `__Auth` (doctype, name, fieldname, password, encrypted) "
            "VALUES (%s, %s, %s, %s, %s)",
            tuple(record[key] for key in ("doctype", "name", "fieldname", "password", "encrypted")),
        )
    actual = set(frappe.get_all("User", pluck="name"))
    if actual != {row["name"] for row in rows["User"]}:
        raise RuntimeError("User restoration is incomplete")
    actual_auth = frappe.db.sql("SELECT * FROM `__Auth` WHERE doctype = 'User'", as_dict=True)
    if sorted(map(dict, actual_auth), key=lambda row: (row["name"], row["fieldname"])) != sorted(
        map(dict, auth), key=lambda row: (row["name"], row["fieldname"])
    ):
        raise RuntimeError("Credential restoration is incomplete")
    frappe.db.commit()
    frappe.clear_cache()


def reset_site(site, root_password):
    import frappe
    from frappe.installer import _new_site
    from frappe.utils.doctor import purge_pending_jobs

    # No arbitrary site/database may be selected through the SSH boundary.
    if site != "frappe-test":
        raise RuntimeError("Only the dedicated test site may be reset")
    frappe.init(site)
    frappe.connect()
    if not frappe.conf.maintenance_mode:
        raise RuntimeError("Maintenance mode is required")
    apps = frappe.get_installed_apps()
    if not {"frappe", "erpnext", "hero_integration"}.issubset(apps):
        raise RuntimeError("Required test apps are missing")
    rows, auth = capture_identities(frappe)
    db_name = frappe.conf.db_name
    encryption_key = frappe.conf.encryption_key
    site_path = Path(frappe.get_site_path()).resolve()
    file_paths = [site_path / relative for relative in ("public/files", "private/files", "private/backups")]
    for path in file_paths:
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(site_path):
            raise RuntimeError("Unexpected symlink in test-site files")
    # /dev/shm is container-local RAM. Keep only the identities for recovery if
    # installation fails; never create a database, site archive or image backup.
    recovery = Path("/dev/shm/frappe-test-users.json")
    fd = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"rows": rows, "auth": auth, "encryption_key": encryption_key}, out, default=str)
    purge_pending_jobs(site=site)
    frappe.clear_cache()
    frappe.destroy()
    frappe.init(site)
    _new_site(
        db_name, site, force=True, install_apps=apps,
        db_root_username="root", db_root_password=root_password,
        admin_password=secrets.token_urlsafe(32), mariadb_user_host_login_scope="%",
    )
    frappe.connect()
    if frappe.conf.encryption_key != encryption_key:
        raise RuntimeError("Site encryption key changed")
    restore_identities(frappe, rows, auth)
    purge_pending_jobs(site=site)
    # Attachments and exports belong to the discarded database. Remove only
    # these known test-site directories after the identities are restored.
    import shutil

    for path in file_paths:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    print(f"Blank test database ready; {len(rows['User'])} users and login passwords preserved; API keys cleared", flush=True)
    recovery.unlink()
    frappe.destroy()


if __name__ == "__main__":
    request = json.load(sys.stdin)
    reset_site(request["site"], request["db_root_password"])
