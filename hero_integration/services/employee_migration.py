"""Manual, source-bound HERO Partner -> Employee migration.

Creates active local Users with only the Employee role when needed.
Existing account roles/access, login email and local audit fields stay local. Every record and its checkpoint commit together.
"""
from __future__ import annotations

from hashlib import sha256
import json
import time

import frappe

from hero_integration.integrations.erpnext.company import project_company
from hero_integration.integrations.hero_people import _existing_user, PARTNER_FIELD, USER_ID_FIELD
from hero_integration.services.employee_mapping import preview
from hero_integration.services import direct_runtime as runtime
from hero_integration.services.file_migration import SourceBindingError
from hero_integration.services.graphql_graph import normalize_schema

JOINING_DATE = "2026-01-01"
VERSION = "employee-migration-v2"
OPERATION = "HeroEmployees"
MAX_RECORDS = 10000
STATE_KEY = "hero_employee_migration:{}"



class EmployeeMigrationBlocked(SourceBindingError):
    pass


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def read_query(schema):
    """Select the supported partners root from this source's validated schema."""
    from graphql import build_client_schema, parse, validate

    schema = build_client_schema({"__schema": normalize_schema(schema)})
    if schema.query_type is None:
        raise EmployeeMigrationBlocked("The HERO schema has no supported employee query.")
    fields = schema.query_type.fields
    if "partners" in fields:
        path = "partners"
    elif "company" in fields:
        path = "company.partners"
    else:
        raise EmployeeMigrationBlocked("The HERO schema has no supported employee query.")
    selection = "id user_id first_name last_name full_name email phone mobile role status account_type user { id email role locale last_login created modified }"
    body = f"partners {{ {selection} }}"
    if path == "company.partners":
        body = f"company {{ {body} }}"
    document = f"query HeroEmployees {{ {body} }}"
    if validate(schema, parse(document)):
        raise EmployeeMigrationBlocked("The HERO employee query does not validate against its source schema.")
    return document, path


def preflight(source_instance, read=None):
    """Read-only checks against the approved mapping; never create fields."""
    read = read or runtime.resolve_read(OPERATION, source_instance)
    company = project_company()
    if not company.is_valid:
        raise EmployeeMigrationBlocked(company.reason)
    for rule in read.plan.definitions:
        if rule.target_doctype in {"Employee", "User"} and not frappe.get_meta(rule.target_doctype).has_field(rule.target_field):
            raise EmployeeMigrationBlocked("Install the selected employee mapping fields before starting migration.")
    return company.name, read.mapping_version, read.document, read.records_path


def _records(data, path):
    from hero_integration.services.direct_mapping import lookup, MISSING

    rows = lookup(data, path)
    if rows is MISSING or not isinstance(rows, list) or len(rows) > MAX_RECORDS or any(not isinstance(r, dict) for r in rows):
        raise EmployeeMigrationBlocked("HERO returned an invalid or oversized employee list.")
    ids = [r.get("id") for r in rows]
    if any(type(i) is not int or i <= 0 for i in ids) or len(ids) != len(set(ids)):
        raise EmployeeMigrationBlocked("HERO returned missing or duplicate employee identities.")
    return sorted(rows, key=lambda row: row["id"])


def _link(instance, source_type, source_id, target_doctype):
    names = frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "source_instance": instance,
                           "source_type": source_type, "source_id": str(source_id)}, pluck="name", limit_page_length=2)
    if len(names) > 1:
        raise EmployeeMigrationBlocked("HERO employee binding is ambiguous.")
    if not names:
        return None
    link = frappe.get_doc("HERO Integration Link", names[0], for_update=True)
    if link.target_doctype != target_doctype or not frappe.db.exists(target_doctype, link.target_name):
        raise EmployeeMigrationBlocked("The HERO employee binding target is unavailable.")
    if link.sync_status in {"Rejected", "Pending Review", "Source Deleted"}:
        raise EmployeeMigrationBlocked("The HERO employee binding requires review.")
    return link


def _values(doc, fields):
    # Frappe stores empty Data as either null or empty string. Compare both alike.
    return {field: str(doc.get(field) or "") for field in fields}


def _assert_unchanged(link, doc):
    if not link:
        return
    baseline = json.loads(link.target_field_hashes or "{}")
    if not baseline or _digest(_values(doc, baseline)) != link.target_payload_hash:
        raise EmployeeMigrationBlocked("The imported employee or user was changed in ERPNext; review the conflict.")


def _assert_adoptable(doc, values):
    if any(doc.get(field) not in (None, "") and str(doc.get(field)) != str(value or "") for field, value in values.items()):
        raise EmployeeMigrationBlocked("Existing ERPNext employee or user values differ; review before linking.")


def _assert_new_fields(link, doc, values):
    if link:
        from hero_integration.services.change_detection import new_mapped_field_conflicts
        if new_mapped_field_conflicts(doc, values, json.loads(link.target_field_hashes or '{}')):
            raise EmployeeMigrationBlocked("Newly mapped employee or user fields contain different local values; review before importing.")


def _write_link(link, instance, source_type, source_id, doc, values, record, mapping_version):
    previous = json.loads(link.target_field_hashes or "{}") if link else {}
    baseline = _values(doc, set(values) | set(previous))
    changes = {"target_doctype": doc.doctype, "target_name": doc.name, "sync_status": "Synchronized",
               "source_payload_hash": _digest(record), "target_payload_hash": _digest(baseline),
               "target_field_hashes": json.dumps(baseline, sort_keys=True), "mapping_version": mapping_version,
               "last_synced_on": frappe.utils.now_datetime(), "conflict_detail": ""}
    if link:
        link.update(changes)
        link.save(ignore_permissions=True)
    else:
        frappe.get_doc({"doctype": "HERO Integration Link", "source_system": "HERO", "source_instance": instance,
                        "source_type": source_type, "source_id": str(source_id), **changes}).insert(ignore_permissions=True)


def _ensure_user_object_binding(read, instance, source_id, user_name):
    """Expose the same verified User identity to the native assignment boundary."""
    if not read.mapping:
        return False  # Builtin plans are templates; live execution requires an approved reader.
    from hero_integration.services.binding_registry import BindingRegistryService, FrappeBindingStorage
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    site = frappe.local.site
    _lock_active_target('employee-user-binding', site, _digest([instance, str(source_id)]))
    existing = frappe.get_all('HERO Object Binding', filters={
        'site_scope': site, 'source_instance': instance, 'object_type': 'User',
        'external_object_id': str(source_id), 'target_doctype': 'User', 'lifecycle': 'VALID'},
        fields=['name', 'target_name'], limit_page_length=2)
    if existing:
        if len(existing) != 1 or existing[0].target_name != user_name:
            raise EmployeeMigrationBlocked('The employee User object binding is ambiguous or points to another account.')
        return False
    mapping = frappe.get_doc('HERO GraphQL Mapping', read.mapping)
    if mapping.source_instance != instance or mapping.status != 'Active':
        raise EmployeeMigrationBlocked('The employee mapping no longer belongs to the approved source.')
    BindingRegistryService(FrappeBindingStorage(frappe)).create_or_resolve_object_binding(
        site_scope=site, source_instance=instance, object_type='User', external_object_id=str(source_id),
        target_app='frappe', target_doctype='User', target_name=user_name,
        mapping_definition_id=mapping.definition_id, mapping_revision=int(mapping.mapping_revision))
    return True


def upsert(record, instance, company, languages, read=None):
    """All checks precede writes. Caller rolls back the entire record on failure."""
    read = read or runtime.resolve_read(OPERATION, instance)
    try:
        proposed = preview(record, languages=languages, plan=read.plan)
    except ValueError as error:
        raise EmployeeMigrationBlocked(str(error)) from error
    if proposed["blockers"]:
        raise EmployeeMigrationBlocked(proposed["blockers"][0])
    user_id = record.get("user_id") or (record.get("user") or {}).get("id")
    if not user_id:
        raise EmployeeMigrationBlocked("The HERO employee has no user identity; review before importing.")
    employee_link = _link(instance, "partner", record["id"], "Employee")
    user_link = _link(instance, "User", user_id, "User")
    mapped = {(field["target_doctype"], field["target_field"]): field["value"] for field in proposed["fields"]}
    login_email = (mapped.get(("User", "email")) or "").strip().lower()
    # Exact login email identifies an existing account on first adoption only.
    # Future runs use the source link, never an edited email as a rename request.
    user_name = user_link.target_name if user_link else (_existing_user(login_email, str(record["id"]), source_instance=instance) or login_email)
    if not user_name or user_name.casefold() in {"administrator", "guest"}:
        raise EmployeeMigrationBlocked("A valid employee login email is required.")
    new_user = not frappe.db.exists("User", user_name)
    if new_user:
        # Only the explicitly requested Employee role. HERO roles/account types
        # are observations and can never become local permission grants.
        user = frappe.get_doc({"doctype": "User", "email": login_email, "enabled": 1,
                               "first_name": mapped.get(("Employee", "first_name")), "last_name": mapped.get(("Employee", "last_name")),
                               "user_type": "System User", "send_welcome_email": 0,
                               "roles": [{"role": "Employee"}]})
    else:
        user = frappe.get_doc("User", user_name, for_update=True)
    ids = {PARTNER_FIELD: str(record["id"]), USER_ID_FIELD: str(user_id)}
    if any(user.get(field) and str(user.get(field)) != value for field, value in ids.items()):
        raise EmployeeMigrationBlocked("Stored person identities differ from HERO.")
    other_users = frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "target_doctype": "User",
                                 "target_name": user_name}, fields=["name"], limit_page_length=2)
    if any(row.name != (user_link.name if user_link else None) for row in other_users):
        raise EmployeeMigrationBlocked("The ERPNext user is already bound to another HERO identity.")
    employee_values, user_values = {}, {}
    for field in proposed["fields"]:
        target, name = field["target_doctype"], field["target_field"]
        if target == "Employee" and name != "employee_name":
            employee_values[name] = field["value"]
        elif target == "User" and name != "email":
            user_values[name] = field["value"]
    employee_values.update(company=company, date_of_joining=JOINING_DATE, user_id=user_name)
    if not employee_values.get("first_name") or not employee_values.get("status"):
        raise EmployeeMigrationBlocked("HERO employee first name and status are required.")
    if bool(user.enabled) != (employee_values["status"] == "Active"):
        raise EmployeeMigrationBlocked("HERO employee status would change ERPNext login access; review the user first.")
    matches = frappe.get_all("Employee", filters={"user_id": user_name}, pluck="name", limit_page_length=2)
    if len(matches) > 1 or (employee_link and matches != [employee_link.target_name]):
        raise EmployeeMigrationBlocked("The ERPNext employee user relation is ambiguous or changed.")
    employee = frappe.get_doc("Employee", employee_link.target_name if employee_link else matches[0], for_update=True) if (employee_link or matches) else None
    if employee:
        other_employees = frappe.get_all("HERO Integration Link", filters={"source_system": "HERO", "target_doctype": "Employee",
                                         "target_name": employee.name}, fields=["name"], limit_page_length=2)
        if any(row.name != (employee_link.name if employee_link else None) for row in other_employees):
            raise EmployeeMigrationBlocked("The ERPNext employee is already bound to another HERO identity.")
    _assert_unchanged(user_link, user)
    _assert_new_fields(user_link, user, user_values)
    if not user_link and not new_user:
        _assert_adoptable(user, user_values)
    if employee:
        _assert_unchanged(employee_link, employee)
        _assert_new_fields(employee_link, employee, employee_values)
        if not employee_link:
            _assert_adoptable(employee, employee_values)
    if employee and mapped.get(("Employee", "employee_name")) and employee.get("middle_name"):
        raise EmployeeMigrationBlocked("The ERPNext employee middle name requires review before importing the HERO name.")
    # Include every mapped field in the baseline even when the upstream record
    # stays unchanged, so a local edit is reported rather than silently skipped.
    if employee_link and user_link and employee_link.source_payload_hash == _digest(record) and user_link.source_payload_hash == _digest(record) and (
            getattr(employee_link, "mapping_version", "") == read.mapping_version and
            getattr(user_link, "mapping_version", "") == read.mapping_version and
            _values(employee, employee_values) == {key: str(value or "") for key, value in employee_values.items()} and
            _values(user, user_values) == {key: str(value or "") for key, value in user_values.items()}):
        if _ensure_user_object_binding(read, instance, user_id, user_name):
            from hero_integration.services.deferred_references import binding_updated
            binding_updated(user_link)
        return "unchanged"
    is_new = employee is None
    if is_new:
        employee_values["employee_number"] = f"HERO-{_digest(instance)[:8]}-{record['id']}"
        employee = frappe.get_doc({"doctype": "Employee", "create_user_permission": 0, "create_user_automatically": 0, **employee_values})
    else:
        employee.update(employee_values)
    user.update({**user_values, **ids})
    from hero_integration.services.change_detection import managed_target_update
    # Employee.on_update also saves its User. Both owned links must stay managed
    # across that nested native save, after the baseline checks above succeeded.
    with managed_target_update('User', user.name or user_name, user_link.name if user_link else None), \
         managed_target_update('Employee', employee.name, employee_link.name if employee_link else None):
        if new_user:
            user.insert(ignore_permissions=True)
        else:
            user.save(ignore_permissions=True)
        if is_new:
            employee.insert(ignore_permissions=True)
        else:
            employee.save(ignore_permissions=True)
    _write_link(user_link, instance, "User", user_id, user, user_values, record, read.mapping_version)
    _write_link(employee_link, instance, "partner", record["id"], employee, employee_values, record, read.mapping_version)
    _ensure_user_object_binding(read, instance, user_id, user_name)
    return "added" if is_new else "updated"


def sync_employees(client, *, source_instance, run_name, should_stop=None, progress=None, time_budget=1200, verify_source=None, record_limit=None, continue_on_conflict=False):
    from hero_integration.services.migration_testing import validate_limit
    validate_limit(record_limit)
    frappe.only_for("System Manager")
    read = runtime.resolve_read(OPERATION, source_instance)
    company, snapshot, document, path = preflight(source_instance, read=read)
    stamp = _digest([company, JOINING_DATE, snapshot, document, read.mapping_version, VERSION])
    raw = frappe.db.get_default(STATE_KEY.format(run_name))
    state = json.loads(raw) if raw else {"counts": {}, "done_ids": []}
    if state.get("stamp") and state["stamp"] != stamp:
        raise EmployeeMigrationBlocked("Employee source schema, company or mapping changed; start a new run.")
    state.update(stamp=stamp, done=False, stopped=False)
    rows = _records(client.query(document, runtime.page_variables(read, 0)), path)
    if record_limit is not None:
        if state.get('testing_record_limit') not in (None, record_limit):
            raise EmployeeMigrationBlocked('The frozen testing record limit changed; start a new run.')
        state['testing_record_limit'] = record_limit
        state['testing_records_received'] = len(rows)
        selected = state.get('testing_selected_ids')
        if selected is None:
            selected = [row['id'] for row in rows[:record_limit]]
            state['testing_selected_ids'] = selected
        by_id = {row['id']: row for row in rows}
        if any(identity not in by_id for identity in selected):
            raise EmployeeMigrationBlocked('The HERO employee list changed; start a new run to reconcile it.')
        rows = [by_id[identity] for identity in selected]
        state['testing_limit_reached'] = len(rows) >= record_limit
    inventory = _digest([r["id"] for r in rows])
    if state.get("inventory") and state["inventory"] != inventory:
        raise EmployeeMigrationBlocked("The HERO employee list changed; start a new run to reconcile it.")
    state["inventory"] = inventory
    languages = frappe.get_all("Language", pluck="name", limit_page_length=0)
    deadline = time.monotonic() + time_budget
    seen = set(state["done_ids"])
    for record in rows:
        if record["id"] in seen:
            continue
        if should_stop and should_stop():
            state["stopped"] = True
            break
        if time.monotonic() >= deadline:
            break
        if verify_source:
            verify_source()
        try:
            result = upsert(record, source_instance, company, languages, read=read)
        except Exception as error:
            frappe.db.rollback()
            result = "conflict" if isinstance(error, EmployeeMigrationBlocked) else "failed"
            # Keep the failed identity retryable when this run is resumed.
            state.setdefault("errors", {})[str(record["id"])] = str(error)[:300] if result == "conflict" else "Employee validation failed."
        else:
            state["done_ids"].append(record["id"])
            state.setdefault("errors", {}).pop(str(record["id"]), None)
        if continue_on_conflict and record['id'] not in state['done_ids']:
            state['done_ids'].append(record['id'])
        previous = state.setdefault("outcomes", {}).get(str(record["id"]))
        if previous:
            state["counts"][previous] -= 1
        state["outcomes"][str(record["id"])] = result
        state["counts"][result] = state["counts"].get(result, 0) + 1
        frappe.db.set_default(STATE_KEY.format(run_name), json.dumps(state))
        frappe.db.commit()
        if progress:
            progress(state["counts"])
    completed = set(state["done_ids"])
    state["done"] = all(r["id"] in completed for r in rows)
    if not continue_on_conflict and not state["done"] and state.get("errors"):
        state["last_error"] = next(iter(state["errors"].values()))
    else:
        state.pop("last_error", None)
    frappe.db.set_default(STATE_KEY.format(run_name), json.dumps(state))
    frappe.db.commit()
    return state
