"""Manual, source-bound HERO Partner -> Employee migration.

Creates active local Users with only the Employee role when needed.
Existing account roles/access, login email and local audit fields stay local. Every record and its checkpoint commit together.
"""
from __future__ import annotations

from hashlib import sha256
from contextlib import contextmanager
from contextvars import ContextVar
import json
import time

import frappe

from hero_integration.integrations.erpnext.company import project_company
from hero_integration.integrations.hero_people import _existing_user, PARTNER_FIELD, USER_ID_FIELD
from hero_integration.services.employee_mapping import preview
from hero_integration.services import direct_runtime as runtime
from hero_integration.services.file_migration import SourceBindingError

JOINING_DATE = "2026-01-01"
VERSION = "employee-migration-v2"
OPERATION = "HeroEmployees"
MAX_RECORDS = 10000
STATE_KEY = "hero_employee_migration:{}"
_historical_users = ContextVar('hero_historical_users', default=('', frozenset()))



class EmployeeMigrationBlocked(SourceBindingError):
    pass


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


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


@contextmanager
def historical_user_scope(source_instance, identities):
    token = _historical_users.set((source_instance, frozenset(str(value) for value in identities)))
    try:
        yield
    finally:
        _historical_users.reset(token)


def historical_users(source_instance):
    source, identities = _historical_users.get()
    return identities if source == source_instance else frozenset()


def _historical_request(read, source_instance, path):
    from hero_integration.services.mapping_contract_v1 import QueryAdapter, QueryDraft
    if path == 'partners':
        draft = QueryDraft('HeroHistoricalEmployees', 'partners', 'partners',
            ('id', 'status', 'user_id', 'user.id'), {'show_deleted': True})
    elif path == 'company.partners':
        draft = QueryDraft('HeroHistoricalEmployees', 'company', 'company',
            ('partners.id', 'partners.status', 'partners.user_id', 'partners.user.id'), {},
            nested_arguments={'partners': {'show_deleted': True}})
    else:
        raise EmployeeMigrationBlocked('The employee reader has no supported historical identity query.')
    try:
        validated = runtime.validated_snapshot(read, source_instance)
        return QueryAdapter().materialize(validated, draft)
    except ValueError as error:
        raise EmployeeMigrationBlocked('The historical employee query cannot be materialized from the approved schema.') from error


def _historical_identities(client, path, wanted_users, wanted_partners, *, read, source_instance):
    """Verify deleted source identities without provisioning local accounts."""
    if path not in ('partners', 'company.partners'):
        raise EmployeeMigrationBlocked('The employee reader has no supported historical identity query.')
    request = _historical_request(read, source_instance, path)
    rows = _records(client.query(request.document, request.variables), path)
    by_user = {}
    historical_partners = set()
    for row in rows:
        nested = row.get('user') if isinstance(row.get('user'), dict) else {}
        values = [value for value in (row.get('user_id'), nested.get('id'))
                  if type(value) is int and value > 0]
        identities = {str(value) for value in values}
        consistent = len(identities) == 1
        deleted = row.get('status') == 'deleted' and consistent
        for identity in identities:
            by_user.setdefault(identity, []).append(deleted)
        if deleted and wanted_partners is not None and str(row['id']) in wanted_partners:
            historical_partners.add(str(row['id']))
    historical_users = {identity for identity in (wanted_users if wanted_users is not None else by_user)
                        if identity in by_user and all(by_user[identity])}
    return sorted(historical_users), sorted(historical_partners)


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
    from hero_integration.services.deferred_references import realizing
    employee_values.update(company=company, date_of_joining=JOINING_DATE)
    if realizing():
        employee_values['user_id'] = user_name
    if not employee_values.get("first_name") or not employee_values.get("status"):
        raise EmployeeMigrationBlocked("HERO employee first name and status are required.")
    if bool(user.enabled) != (employee_values["status"] == "Active"):
        raise EmployeeMigrationBlocked("HERO employee status would change ERPNext login access; review the user first.")
    matches = frappe.get_all("Employee", filters={"user_id": user_name}, pluck="name", limit_page_length=2)
    if len(matches) > 1 or (employee_link and matches and matches != [employee_link.target_name]):
        raise EmployeeMigrationBlocked("The ERPNext employee user relation is ambiguous or changed.")
    employee = frappe.get_doc("Employee", employee_link.target_name if employee_link else matches[0], for_update=True) if (employee_link or matches) else None
    if employee:
        if employee.get('user_id') not in (None, '', user_name):
            raise EmployeeMigrationBlocked('The ERPNext employee user relation is ambiguous or changed.')
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
        if realizing() and _ensure_user_object_binding(read, instance, user_id, user_name):
            from hero_integration.services.deferred_references import binding_updated
            binding_updated(user_link)
        return "unchanged"
    is_new = employee is None
    user_changed = new_user or _values(user, {**user_values, **ids}) != {
        key: str(value or '') for key, value in {**user_values, **ids}.items()}
    employee_changed = is_new or _values(employee, employee_values) != {
        key: str(value or '') for key, value in employee_values.items()}
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
        elif user_changed:
            user.save(ignore_permissions=True)
        if is_new:
            employee.insert(ignore_permissions=True)
        elif employee_changed:
            employee.save(ignore_permissions=True)
    _write_link(user_link, instance, "User", user_id, user, user_values, record, read.mapping_version)
    _write_link(employee_link, instance, "partner", record["id"], employee, employee_values, record, read.mapping_version)
    if realizing():
        _ensure_user_object_binding(read, instance, user_id, user_name)
        from hero_integration.services.deferred_references import binding_updated
        binding_updated(_link(instance, 'User', user_id, 'User'))
    return "added" if is_new else "updated"


def remember_relationships(run_name, instance, company, read, record):
    """Retain the exact source and target identities before committing the row."""
    from hero_integration.services.deferred_references import save
    user_id = record.get('user_id') or (record.get('user') or {}).get('id')
    employee = _link(instance, 'partner', record['id'], 'Employee')
    user = _link(instance, 'User', user_id, 'User')
    if not employee or not user:
        raise EmployeeMigrationBlocked('Employee relationship observations require both imported identity links.')
    save(run_name, instance, OPERATION, record['id'], read.mapping_version,
        {'record':record, 'company':company, 'user_name':user.target_name, 'employee_name':employee.target_name,
         'source_hash':_digest(record)}, [('User', str(user_id)), ('partner', str(record['id']))], defer_empty=True)


def realize_relationships(pending):
    """Recheck the saved import and realize only its own Employee/User relation."""
    from hero_integration.services.deferred_references import available, realizing
    if not realizing():
        raise EmployeeMigrationBlocked('Employee relationships are owned by the post-success realization service.')
    observation = json.loads(pending.record_json)
    record = observation['record']
    if str(record['id']) != pending.source_id:
        raise EmployeeMigrationBlocked('The deferred employee source identity changed.')
    read = runtime.resolve_read(OPERATION, pending.source_instance)
    company, _snapshot, _document, _path = preflight(pending.source_instance, read=read)
    if not read.mapping or read.mapping_version != pending.mapping_version or company != observation['company']:
        raise EmployeeMigrationBlocked('The deferred employee mapping or company changed.')
    user_id = record.get('user_id') or (record.get('user') or {}).get('id')
    employee = _link(pending.source_instance, 'partner', record['id'], 'Employee')
    user = _link(pending.source_instance, 'User', user_id, 'User')
    if (not employee or not user or employee.target_name != observation['employee_name']
            or user.target_name != observation['user_name']
            or any(link.source_payload_hash != observation['source_hash'] or link.mapping_version != pending.mapping_version
                   for link in (employee, user))):
        raise EmployeeMigrationBlocked('The imported employee observation changed before relationship realization.')
    employee_doc = frappe.get_doc('Employee',employee.target_name,for_update=True)
    user_doc = frappe.get_doc('User',user.target_name,for_update=True)
    _assert_unchanged(employee,employee_doc)
    _assert_unchanged(user,user_doc)
    if (employee_doc.user_id not in (None,'',user_doc.name) or
            bool(user_doc.enabled) != (employee_doc.status=='Active')):
        raise EmployeeMigrationBlocked('The ERPNext employee user relation or login access changed.')
    identities = {PARTNER_FIELD:str(record['id']),USER_ID_FIELD:str(user_id)}
    if any(user_doc.get(field) and str(user_doc.get(field)) != identity for field,identity in identities.items()):
        raise EmployeeMigrationBlocked('Stored person identities differ from HERO.')
    matches = frappe.get_all('Employee',filters={'user_id':user_doc.name},pluck='name',limit_page_length=2)
    if matches and matches != [employee_doc.name]:
        raise EmployeeMigrationBlocked('The ERPNext employee user relation is ambiguous or changed.')
    for link,doc in ((employee,employee_doc),(user,user_doc)):
        others = frappe.get_all('HERO Integration Link',filters={'source_system':'HERO',
            'target_doctype':doc.doctype,'target_name':doc.name},pluck='name',limit_page_length=2)
        if any(name != link.name for name in others):
            raise EmployeeMigrationBlocked('The ERPNext employee or user is bound to another HERO identity.')
    created = _ensure_user_object_binding(read,pending.source_instance,user_id,user_doc.name)
    if employee_doc.user_id != user_doc.name:
        from hero_integration.services.change_detection import managed_target_update
        with managed_target_update('User',user_doc.name,user.name), \
             managed_target_update('Employee',employee_doc.name,employee.name):
            employee_doc.user_id = user_doc.name
            employee_doc.save(ignore_permissions=True)
        employee_doc.reload();user_doc.reload()
        _write_link(employee,pending.source_instance,'partner',record['id'],employee_doc,
                    {'user_id':user_doc.name},record,read.mapping_version)
        _write_link(user,pending.source_instance,'User',user_id,user_doc,{},record,read.mapping_version)
    elif created:
        from hero_integration.services.deferred_references import binding_updated
        binding_updated(user)
    if not available(pending.source_instance, 'User', user_id):
        raise EmployeeMigrationBlocked('The employee User object binding was not realized.')
    return 'UPDATED'


def sync_employees(client, *, source_instance, run_name, should_stop=None, progress=None, time_budget=1200, verify_source=None, record_limit=None, continue_on_conflict=False, cohort_user_ids=None, cohort_partner_ids=None):
    from hero_integration.services.migration_testing import validate_limit
    validate_limit(record_limit)
    frappe.only_for("System Manager")
    read = runtime.resolve_read(OPERATION, source_instance)
    company, snapshot, document, path = preflight(source_instance, read=read)
    stamp = _digest([company, JOINING_DATE, snapshot, document, read.mapping_version, VERSION])
    has_column = getattr(frappe.db, 'has_column', None)
    use_run_field = bool(has_column and has_column('HERO Integration Run', 'employee_progress_json'))
    raw = (frappe.db.get_value('HERO Integration Run', run_name, 'employee_progress_json') if use_run_field else None)
    if not raw:
        raw = frappe.db.get_default(STATE_KEY.format(run_name))
    def save_progress(value):
        payload = json.dumps(value, separators=(',', ':'))
        if use_run_field:
            frappe.db.set_value('HERO Integration Run', run_name, 'employee_progress_json', payload, update_modified=False)
        else:
            if len(payload.encode('utf-8')) > 60000:
                raise EmployeeMigrationBlocked('Employee progress requires the updated run schema; migrate the app before continuing.')
            frappe.db.set_default(STATE_KEY.format(run_name), payload)
    state = json.loads(raw) if raw else {"counts": {}, "done_ids": []}
    if state.get("stamp") and state["stamp"] != stamp:
        raise EmployeeMigrationBlocked("Employee source schema, company or mapping changed; start a new run.")
    from hero_integration.services.deferred_references import require_storage
    require_storage(frappe)
    state.update(stamp=stamp, done=False, stopped=False)
    rows = _records({'rows': runtime.fetch_page(client, read, 0)}, 'rows')
    if record_limit is not None:
        if state.get('testing_record_limit') not in (None, record_limit):
            raise EmployeeMigrationBlocked('The frozen testing record limit changed; start a new run.')
        state['testing_record_limit'] = record_limit
        state['testing_records_received'] = len(rows)
        if cohort_user_ids is not None:
            cohort_ids = sorted({str(value) for value in cohort_user_ids})
            if state.get('testing_cohort_user_ids', cohort_ids) != cohort_ids:
                raise EmployeeMigrationBlocked('The frozen related project sample changed; start a new run.')
            state['testing_cohort_user_ids'] = cohort_ids
            partner_ids = sorted({str(value) for value in (cohort_partner_ids or [])})
            if state.get('testing_cohort_partner_ids', partner_ids) != partner_ids:
                raise EmployeeMigrationBlocked('The frozen related project sample changed; start a new run.')
            state['testing_cohort_partner_ids'] = partner_ids
        selected = state.get('testing_selected_ids')
        if selected is None:
            selected = [row['id'] for row in rows[:record_limit]]
            if cohort_user_ids is not None:
                wanted = set(state['testing_cohort_user_ids'])
                selected_ids = set(selected)
                matched = {}
                for row in rows:
                    nested = row.get('user') if isinstance(row.get('user'), dict) else {}
                    identities = {str(value) for value in (row.get('user_id'), nested.get('id'))
                                  if type(value) is int and value > 0}
                    matches = identities & wanted
                    for identity in matches:
                        matched.setdefault(identity, []).append(row['id'])
                state['testing_missing_user_ids'] = sorted(wanted - set(matched))
                state['testing_ambiguous_user_ids'] = sorted(identity for identity, ids in matched.items() if len(ids) != 1)
                for identity in sorted(matched):
                    ids = matched[identity]
                    if len(ids) == 1 and ids[0] not in selected_ids:
                        selected.append(ids[0])
                        selected_ids.add(ids[0])
                wanted_partners = set(state['testing_cohort_partner_ids'])
                by_partner = {str(row['id']): row['id'] for row in rows}
                state['testing_missing_partner_ids'] = sorted(wanted_partners - set(by_partner))
                if state['testing_missing_user_ids'] or state['testing_missing_partner_ids']:
                    from hero_integration.client.graphql import HeroClientError
                    if verify_source:
                        verify_source()
                    try:
                        historical_users, historical_partners = _historical_identities(
                            client, path, set(state['testing_missing_user_ids']),
                            set(state['testing_missing_partner_ids']), read=read, source_instance=source_instance)
                    except (HeroClientError, EmployeeMigrationBlocked):
                        # Unknown identities remain unresolved if the source cannot prove deletion.
                        state['testing_historical_audit_unavailable'] = True
                    else:
                        if verify_source:
                            verify_source()
                        state['testing_historical_user_ids'] = historical_users
                        state['testing_historical_partner_ids'] = historical_partners
                        state['testing_missing_user_ids'] = sorted(set(state['testing_missing_user_ids']) - set(historical_users))
                        state['testing_missing_partner_ids'] = sorted(set(state['testing_missing_partner_ids']) - set(historical_partners))
                for identity in sorted(wanted_partners & set(by_partner)):
                    if by_partner[identity] not in selected_ids:
                        selected.append(by_partner[identity])
                        selected_ids.add(by_partner[identity])
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
            remember_relationships(run_name, source_instance, company, read, record)
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
        save_progress(state)
        frappe.db.commit()
        if progress:
            progress(state["counts"])
    completed = set(state["done_ids"])
    state["done"] = all(r["id"] in completed for r in rows)
    if not continue_on_conflict and not state["done"] and state.get("errors"):
        state["last_error"] = next(iter(state["errors"].values()))
    else:
        state.pop("last_error", None)
    save_progress(state)
    frappe.db.commit()
    return state
