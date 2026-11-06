"""Direct run actions: Product, Document, Picture and Full Migration.

Each action is an ordinary HERO Integration Run, so the Synchronization page
lists, cancels and continues it like the mapping areas. Product and file runs
work in time-boxed background slices (services.product_migration,
services.file_transfer).
Full Migration runs the mapping areas and both file areas one after another;
every step is its own child run, and a finished child advances the chain.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

from hero_integration.services import direct_runtime

FILE_ACTIONS = {"Document Migration": "documents", "Picture Migration": "images"}
PRODUCT_ACTION = "Product Migration"
SERVICE_ACTION = "Service Migration"
EMPLOYEE_ACTION = "Employee Migration"
FULL_ACTION = "Full Migration"
SLICE_ACTIONS = frozenset(FILE_ACTIONS) | {PRODUCT_ACTION, SERVICE_ACTION, EMPLOYEE_ACTION}
RUN_ACTIONS = SLICE_ACTIONS | {FULL_ACTION}
DIRECT_OPERATIONS = {EMPLOYEE_ACTION: "HeroEmployees", PRODUCT_ACTION: "HeroProducts", SERVICE_ACTION: "HeroServices",
                     "Document Migration": "HeroDocuments", "Picture Migration": "HeroPictures"}
# Project Migration processes the referenced contacts first in the same run.
FULL_STEPS = ("Project Migration", "Schedule Migration", "Projection Migration", "Product Migration",
              "Document Migration", "Picture Migration")
NEW_FULL_STEPS = (EMPLOYEE_ACTION, "Customer Migration", "Project Migration", "Schedule Migration",
                  "Projection Migration", "Document Migration", "Picture Migration", PRODUCT_ACTION, SERVICE_ACTION)
ACTIVE = ("Queued", "Running")
SUCCESS = ("Completed", "Completed With Conflicts")
FULL_KEY = "hero_full_migration:{}"
CHILD_KEY = "hero_full_migration_child:{}"


class SourceBindingError(RuntimeError):
    """The HERO source of a run is unconfirmed or no longer the configured one."""


# ---------------------------------------------------------------- source binding

def _configuration():
    from hero_integration.client.settings import get_configuration

    return get_configuration()


def confirmed_configuration(configuration=None):
    config = _configuration() if configuration is None else configuration
    if not config.source_binding_confirmed:
        raise SourceBindingError("The HERO endpoint, token or source instance changed; "
                                 "an administrator must confirm the source binding.")
    return config


def bound_configuration(run, *, configuration=None):
    """Validate the queued binding; callers may supply a freshly read proof."""
    config = confirmed_configuration() if configuration is None else confirmed_configuration(configuration)
    recorded = str(getattr(run, "source_instance", "") or "")
    if not recorded or recorded != config.source_instance:
        raise SourceBindingError("The run belongs to another HERO source instance; queue a new run.")
    if int(getattr(run, "source_binding_revision", 0) or 0) != int(config.source_binding_revision or 0):
        raise SourceBindingError("The run was queued under an earlier HERO source binding; queue a new run.")
    return config


def prepare_related_test(run):
    """Freeze project identities before the first child of a bounded full run."""
    from hero_integration.services.file_transfer import _client_and_downloader
    from hero_integration.services.migration_testing import prepare_project_cohort, project_reader_fingerprint, record_limit
    config = bound_configuration(run)
    read = direct_runtime.approved_read('HeroProjectMatches', config.source_instance)
    if read is None:
        raise SourceBindingError('Related test selection requires an approved Project read service.')
    before = project_reader_fingerprint(read)
    client, _, instance = _client_and_downloader(config)
    if instance != config.source_instance:
        raise SourceBindingError('The related test reader belongs to another HERO source.')
    cohort = prepare_project_cohort(client, read, record_limit(run))
    bound_configuration(run)
    current = direct_runtime.approved_read('HeroProjectMatches', config.source_instance)
    if current is None or project_reader_fingerprint(current) != before:
        raise SourceBindingError('Project mapping changed while selecting related test records.')
    cohort.update(source_instance=config.source_instance, source_binding_revision=int(config.source_binding_revision),
                  project_reader_fingerprint=before)
    return cohort


def related_test_for_run(run):
    """Return only a still-valid sample belonging to this full-run child."""
    if not bool(int(getattr(run, 'testing_enabled', 0) or 0)):
        return None
    parent = frappe.db.get_default(CHILD_KEY.format(run.name))
    if not parent:
        return None
    state = _full(parent)
    if not state.get('related_test_enabled'):
        return None  # a full run accepted before related selection existed
    if not state.get('related_test_digest'):
        raise SourceBindingError('The frozen related test selection has no integrity proof.')
    cohort = _load_related_test(parent, expected_digest=state.get('related_test_digest'))
    config = bound_configuration(run)
    if (cohort.get('version') != 1 or cohort.get('source_instance') != config.source_instance or
            cohort.get('source_binding_revision') != int(config.source_binding_revision)):
        raise SourceBindingError('The related test sample no longer matches the confirmed HERO source.')
    from hero_integration.services.migration_testing import project_reader_fingerprint
    read = direct_runtime.approved_read('HeroProjectMatches', config.source_instance)
    if read is None or project_reader_fingerprint(read) != cohort.get('project_reader_fingerprint'):
        raise SourceBindingError('The related test Project reader or mapping changed; start a new run.')
    operation = {'Projection Migration': 'HeroTasks', 'Schedule Migration': 'HeroCalendarEvents'}.get(run.action)
    if operation and cohort.get('project_ids'):
        read = direct_runtime.approved_read(operation, config.source_instance)
        if read is None or project_reader_fingerprint(read) != cohort.get('projection_reader_fingerprints', {}).get(operation):
            raise SourceBindingError('The related test reader or mapping changed; start a new run.')
    return cohort


def discover_related_test_page(run, state, cohort, *, page_budget=10):
    """Discover related Tasks and Events in bounded, restartable read-only slices."""
    import hashlib
    from hero_integration.services.file_transfer import _client_and_downloader
    from hero_integration.services.migration_testing import collect_related_projection, project_reader_fingerprint
    config = bound_configuration(run)
    client, _, instance = _client_and_downloader(config)
    if instance != config.source_instance:
        raise SourceBindingError('Related test discovery belongs to another HERO source.')
    if not cohort['project_ids']:
        return True
    operations = ('HeroTasks', 'HeroCalendarEvents')
    progress = state.setdefault('related_test_discovery', {'operation_index': 0, 'offset': 0, 'pages': 0})
    fingerprints = cohort.setdefault('projection_reader_fingerprints', {})
    readers = {}

    def revalidate_readers():
        # Discard the read snapshot before rechecking approvals after transport.
        frappe.db.rollback()
        bound_configuration(run)
        for operation, (_, fingerprint) in readers.items():
            current = direct_runtime.approved_read(operation, instance)
            if current is None or project_reader_fingerprint(current) != fingerprint:
                raise SourceBindingError('A related test reader changed during discovery; start a new run.')

    for _ in range(page_budget):
        index = progress['operation_index']
        if index >= len(operations):
            revalidate_readers()
            return True
        operation = operations[index]
        if operation not in readers:
            read = direct_runtime.approved_read(operation, instance)
            if read is None:
                raise SourceBindingError('Related test discovery requires an approved ' + operation + ' read service.')
            fingerprint = project_reader_fingerprint(read)
            if operation in fingerprints and fingerprints[operation] != fingerprint:
                raise SourceBindingError('A related test reader changed during discovery; start a new run.')
            fingerprints[operation] = fingerprint
            readers[operation] = (read, fingerprint)
        read, fingerprint = readers[operation]
        if progress['pages'] >= 10000:
            raise SourceBindingError('Related test discovery reached its page budget before the source ended.')
        bound_configuration(run)
        page = direct_runtime.fetch_page(client, read, progress['offset'])
        bound_configuration(run)
        progress['pages'] += 1
        if not page:
            progress.update(operation_index=index + 1, offset=0, pages=0, last_page='')
            continue
        values = [row.get('id') if isinstance(row, dict) else None for row in page]
        if any(not isinstance(value, (int, str)) or isinstance(value, bool) or not str(value).strip() for value in values):
            raise SourceBindingError('Related test discovery encountered an invalid source identity.')
        ids = [str(value) for value in values]
        signature = hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()
        if not all(ids) or len(set(ids)) != len(ids) or signature == progress.get('last_page'):
            raise SourceBindingError('Related test discovery encountered a missing or repeated source page.')
        collect_related_projection(cohort, operation, page)
        if len(cohort['task_ids']) + len(cohort['event_ids']) > 50000:
            raise SourceBindingError('Related test selection exceeds 50000 Tasks and Events; reduce the project sample.')
        progress['offset'] += len(page)
        progress['last_page'] = signature
    revalidate_readers()
    return progress['operation_index'] >= len(operations)


# ---------------------------------------------------------------- starting

def require_mappings(action: str, instance: str, *, steps=None) -> dict:
    """Check all required direct readers before creating or resuming a run."""
    operations = ([DIRECT_OPERATIONS.get(step) for step in steps] if steps is not None else DIRECT_OPERATIONS.values()) \
        if action == FULL_ACTION else [DIRECT_OPERATIONS.get(action)]
    reads = {}
    for operation in operations:
        if operation:
            reads[operation] = direct_runtime.resolve_read(operation, instance)
    return reads


def start(action: str, parent: str = "") -> str:
    """Create and queue a file or full run; returns the run name."""
    if action not in RUN_ACTIONS:
        raise ValueError("Unknown file migration action.")
    config = confirmed_configuration()
    from hero_integration.services.migration_testing import configuration, run_metadata
    parent_run = frappe.get_doc('HERO Integration Run', parent) if parent else None
    require_mappings(action, config.source_instance)
    if action == EMPLOYEE_ACTION:
        from hero_integration.services.employee_migration import preflight

        preflight(config.source_instance)
    if action == SERVICE_ACTION:
        from hero_integration.services.service_migration import preflight

        preflight(config.source_instance)
    if action in FILE_ACTIONS or action in {FULL_ACTION, EMPLOYEE_ACTION, SERVICE_ACTION}:
        from hero_integration.services.deferred_references import require_storage
        require_storage(frappe)
    if parent:
        bound_configuration(frappe.get_doc("HERO Integration Run", parent))  # a chain never switches sources
    testing = configuration(parent_run, frappe_module=frappe)
    if action == FULL_ACTION and testing['testing_enabled'] and not frappe.get_meta('HERO Integration Run').has_field('testing_cohort_json'):
        raise SourceBindingError('Related test selection schema is not installed; migrate the app before starting this run.')
    run = frappe.get_doc({"doctype": "HERO Integration Run", "action": action, "mode": "Execute", "state": "Queued",
                          "source_instance": config.source_instance,
                          "source_binding_revision": int(config.source_binding_revision or 0), **testing, **run_metadata(testing)})
    run.insert(ignore_permissions=True)
    if action in FILE_ACTIONS and not testing["testing_enabled"]:
        from hero_integration.services.file_transfer import reset_phase

        reset_phase(FILE_ACTIONS[action])
    elif action == PRODUCT_ACTION and not testing["testing_enabled"]:
        from hero_integration.services.product_migration import reset

        reset()
    elif action == FULL_ACTION:
        _save_full(run.name, {"step": 0, "child": "", "notes": [],
                              "steps": list(NEW_FULL_STEPS)})
    if parent:
        frappe.db.set_default(CHILD_KEY.format(run.name), parent)
    _enqueue(run.name)
    return run.name


def resume(run) -> None:
    """Continue a failed, cancelled or blocked file/full run where it stopped."""
    from hero_integration.services.migration_testing import record_limit
    if record_limit(run) is not None:
        raise SourceBindingError('Testing imports cannot resume; start a new bounded run.')
    config = bound_configuration(run)
    steps = _full(run.name).get("steps", list(FULL_STEPS)) if run.action == FULL_ACTION else None
    require_mappings(run.action, config.source_instance, steps=steps)
    run.state, run.error_message, run.finished_on = "Queued", "", None
    run.save(ignore_permissions=True)
    if run.action == FULL_ACTION:
        state = _full(run.name)
        child = state.get("child")
        if child and frappe.db.get_value("HERO Integration Run", child, "state") not in SUCCESS:
            state["child"] = ""  # the stopped step starts again as a new child run
            _save_full(run.name, state)
    _enqueue(run.name)


def _enqueue(run_name: str) -> None:
    import time

    frappe.enqueue("hero_integration.services.file_migration.execute", queue="long", timeout=_job_timeout(),
                   enqueue_after_commit=True, job_id=f"hero-file-run:{run_name}:{int(time.time() * 1000)}",
                   run_name=run_name)


def _job_timeout() -> int:
    from hero_integration.services.file_transfer import TIME_BUDGET_SECONDS
    from hero_integration.services.migration import RUN_JOB_TIMEOUT

    return max(RUN_JOB_TIMEOUT, TIME_BUDGET_SECONDS + 600)


# ---------------------------------------------------------------- background jobs

def execute(run_name: str) -> None:
    run = frappe.get_doc("HERO Integration Run", run_name)
    if run.state not in ACTIVE:
        return
    frappe.set_user(run.owner)
    if run.state == "Queued":
        run.state = "Running"
        run.started_on = run.started_on or now_datetime()
        run.save(ignore_permissions=True)
        frappe.db.commit()
    try:
        config = bound_configuration(run)
        if run.action == FULL_ACTION:
            advance_full(run.name)
        else:
            _file_slice(run, config)
    except SourceBindingError as error:
        frappe.db.rollback()
        _finish(run.name, "Blocked", str(error))
    except Exception as error:
        frappe.db.rollback()
        frappe.log_error(title=f"HERO {run.action} failed")
        _finish(run.name, "Failed", f"{type(error).__name__}: {str(error)[:300]}")


def _file_slice(run, config) -> None:
    """One slice, reading from exactly the source the run is bound to."""
    from frappe.utils.synchronization import filelock

    from hero_integration.services.file_transfer import _client_and_downloader, transfer

    def cancelled() -> bool:
        return frappe.db.get_value("HERO Integration Run", run.name, "state") == "Cancelled"

    def progress(counts: dict) -> None:
        # Called after every page, so the Synchronization page shows the counts while the slice runs.
        frappe.db.set_value("HERO Integration Run", run.name, run_counts(counts))
        frappe.db.commit()

    from hero_integration.services.migration_testing import record_limit, persist
    maximum = record_limit(run)
    cohort = related_test_for_run(run) if maximum is not None and run.action in {*FILE_ACTIONS, EMPLOYEE_ACTION} else None
    client, downloader, instance = _client_and_downloader(config)
    if run.action == SERVICE_ACTION:
        from hero_integration.services.service_migration import sync_services

        with filelock("hero_service_migration", timeout=5):
            state = sync_services(client, source_instance=instance, run_name=run.name, downloader=downloader,
                                  should_stop=cancelled, progress=progress, verify_source=lambda: bound_configuration(run),
                                  record_limit=maximum, continue_on_conflict=True)
    elif run.action == EMPLOYEE_ACTION:
        from hero_integration.services.employee_migration import sync_employees

        with filelock("hero_employee_migration", timeout=5):
            args = {'cohort_user_ids': cohort['user_ids'], 'cohort_partner_ids': cohort['partner_ids']} if cohort is not None else {}
            state = sync_employees(client, source_instance=instance, run_name=run.name,
                                   should_stop=cancelled, progress=progress, verify_source=lambda: bound_configuration(run),
                                   record_limit=maximum, continue_on_conflict=True, **args)
    elif run.action == PRODUCT_ACTION:
        from hero_integration.services.product_migration import sync_products

        with filelock("hero_product_migration", timeout=5):
            state = sync_products(client, source_instance=instance, should_stop=cancelled, progress=progress,
                                  record_limit=maximum, run_name=run.name)
    else:
        with filelock("hero_file_transfer", timeout=5):
            args = {'cohort': cohort} if cohort is not None else {}
            state = transfer(FILE_ACTIONS[run.action], source_instance=instance, client=client,
                             downloader=downloader, should_stop=cancelled, progress=progress,
                             record_limit=maximum, run_name=run.name, **args)
    counts = state.get("counts") or {}
    if maximum is not None:
        persist(run.name, DIRECT_OPERATIONS[run.action], state)
    problems = counts.get("failed", 0) + counts.get("conflict", 0) + int(any(
        state.get(key) for key in ('testing_missing_user_ids', 'testing_ambiguous_user_ids', 'testing_missing_partner_ids')))
    frappe.db.set_value("HERO Integration Run", run.name, run_counts(counts))
    frappe.db.commit()
    if state.get("stopped"):
        return  # cancelled by the user; the run keeps its Cancelled state
    if state.get("last_error"):
        detail = (f"{state['blocked_source_id']}: " if state.get("blocked_source_id") else "") + state["last_error"]
        _finish(run.name, "Blocked", f"{detail} {summary(counts)}")
    elif not state.get("done"):
        _enqueue(run.name)
        frappe.db.commit()
    else:
        unresolved = sum(len(state.get(key, [])) for key in
            ('testing_missing_user_ids', 'testing_ambiguous_user_ids', 'testing_missing_partner_ids'))
        detail = summary(counts)
        if unresolved:
            detail += ' ' + frappe._('{0} related employee identities could not be resolved uniquely.').format(unresolved)
        historical = len(state.get('testing_historical_user_ids', []))
        if historical:
            detail += ' ' + frappe._('{0} deleted HERO users are retained as historical references; no accounts were created for them.').format(historical)
        _finish(run.name, "Completed With Conflicts" if problems else "Completed", detail)


def run_counts(counts: dict) -> dict:
    """Run fields for the counts of a product or file pass."""
    skipped = counts.get("updated", 0) + counts.get("unchanged", 0) + sum(v for k, v in counts.items() if k.startswith("skipped_"))
    problems = counts.get("failed", 0) + counts.get("conflict", 0)
    values = {"created_records": counts.get("added", 0), "skipped_records": skipped,
              "conflict_records": problems, "error_message": summary(counts)}
    values["processed"] = values["created_records"] + skipped + values["conflict_records"]
    return values


def summary(counts: dict) -> str:
    skipped = sum(v for k, v in counts.items() if k.startswith("skipped_"))
    text = (f"{counts.get('added', 0)} added, {counts.get('updated', 0)} updated, {counts.get('unchanged', 0)} unchanged, "
            f"{skipped} skipped, {counts.get('conflict', 0)} conflicts, {counts.get('failed', 0)} failed")
    if "bytes" in counts or "bytes_planned" in counts:
        text += f", {counts.get('bytes', 0) / 2**30:.2f} GB transferred"
    if counts.get('pending_review'):
        text += f", {counts['pending_review']} related record(s) awaiting review"
    return text + "."


def _finish(run_name: str, state: str, message: str) -> None:
    if frappe.db.get_value("HERO Integration Run", run_name, "state") == "Cancelled":
        return
    frappe.db.set_value("HERO Integration Run", run_name, {"state": state, "finished_on": now_datetime(),
                                                           "error_message": message[:1000]})
    frappe.db.commit()
    on_run_finished(run_name)


def on_run_finished(run_name: str) -> None:
    """Notify relationship realization and advance an originating Full Migration."""
    from hero_integration.services.deferred_references import notify_completed_run
    notify_completed_run(run_name)
    parent = frappe.db.get_default(CHILD_KEY.format(run_name))
    if parent and frappe.db.get_value("HERO Integration Run", parent, "state") in ACTIVE:
        _enqueue(parent)
        frappe.db.commit()


def advance_full(run_name: str) -> None:
    """Check the current child; start the next step or finish the Full Migration."""
    from frappe.utils.synchronization import filelock

    with filelock('hero_full_migration_' + run_name, timeout=30):
        # Refresh after acquiring the lock: another worker may have advanced or
        # finished the chain while this worker was waiting.
        frappe.db.rollback()
        if frappe.db.get_value('HERO Integration Run', run_name, 'state') not in ACTIVE:
            return
        _advance_full_locked(run_name)


def _advance_full_locked(run_name: str) -> None:
    state = _full(run_name)
    steps = state.get("steps") or FULL_STEPS  # preserve pre-upgrade run checkpoints
    if (steps == list(NEW_FULL_STEPS) and state.get('step') == 0 and not state.get('child')
            and frappe.db.get_value('HERO Integration Run', run_name, 'testing_enabled')
            and not state.get('related_test_enabled')):
        cohort = prepare_related_test(frappe.get_doc('HERO Integration Run', run_name))
        state['related_test_digest'] = _save_related_test(run_name, cohort)
        state['related_test_enabled'] = True
        _save_full(run_name, state)
        frappe.db.commit()
    if (steps == list(NEW_FULL_STEPS) and state.get('step') == 0 and not state.get('child')
            and state.get('related_test_enabled') and not state.get('related_test_ready')):
        if not state.get('related_test_digest'):
            raise SourceBindingError('The frozen related test selection has no integrity proof.')
        cohort = _load_related_test(run_name, expected_digest=state.get('related_test_digest'))
        state['related_test_ready'] = discover_related_test_page(frappe.get_doc('HERO Integration Run', run_name), state, cohort)
        state['related_test_digest'] = _save_related_test(run_name, cohort)
        _save_full(run_name, state)
        if not state['related_test_ready']:
            _enqueue(run_name)
            frappe.db.commit()
            return
        frappe.db.commit()
    while True:
        child = state.get("child")
        if child:
            child_state = frappe.db.get_value("HERO Integration Run", child, "state")
            if child_state in ACTIVE:
                return  # the child advances the chain when it finishes
            step = steps[state["step"]]
            if child_state not in SUCCESS:
                _save_full(run_name, state)
                _finish(run_name, "Blocked", f"{step} ({child}) ended as {child_state}. Continue this run to retry the step.")
                return
            if child_state == "Completed With Conflicts":
                state["notes"].append(f"{step}: conflicts in {child}")
            state.setdefault("completed_children", []).append(child)
            state.update(step=state["step"] + 1, child="")
        if state["step"] >= len(steps):
            if steps == list(NEW_FULL_STEPS) and not state.get("reconciliation_done"):
                from hero_integration.services.deferred_references import reconcile_source_page
                source = bound_configuration(frappe.get_doc("HERO Integration Run", run_name)).source_instance
                testing = bool(frappe.db.get_value("HERO Integration Run", run_name, "testing_enabled"))
                origins = state.get("completed_children", []) if testing else None
                cursor, more = reconcile_source_page(source, state.get("reconciliation_after", ""), origin_runs=origins)
                state["reconciliation_after"] = cursor
                state["reconciliation_done"] = not more
                _save_full(run_name, state)
                if more:
                    _enqueue(run_name)
                    frappe.db.commit()
                    return
            _save_full(run_name, state)
            notes = "; ".join(state["notes"])
            _finish(run_name, "Completed With Conflicts" if notes else "Completed", notes or "All steps completed.")
            return
        action = steps[state["step"]]
        child = _start_step(action, run_name)
        if not child:
            if steps == list(NEW_FULL_STEPS):
                _save_full(run_name, state)
                _finish(run_name, "Blocked", f"{action}: no approved read service. Approve it before continuing.")
                return
            state["notes"].append(f"{action}: skipped, no approved read service")
            state["step"] += 1
            continue
        state["child"] = child
        _save_full(run_name, state)
        frappe.db.set_value("HERO Integration Run", run_name, {
            "resume_phase": action, "error_message": f"Step {state['step'] + 1}/{len(steps)}: {action} ({child})"})
        frappe.db.commit()
        return


def _start_step(action: str, parent: str) -> str:
    if action in SLICE_ACTIONS:
        return start(action, parent=parent)
    from hero_integration import api
    from hero_integration.services.sync_page import BY_RUN_ACTION
    from hero_integration.services.migration_testing import configuration

    item = BY_RUN_ACTION[action]
    instance = bound_configuration(frappe.get_doc("HERO Integration Run", parent)).source_instance
    services = api._sync_area_read_services(instance).get(item.operation) or []
    if not services:
        return ""
    result = api._start_migration(action=action, dry_run=0, scheduled=False,
                                  graphql_service=services[0]["service"], source_instance=instance,
                                  testing_configuration=configuration(
                                      frappe.get_doc('HERO Integration Run', parent)))
    frappe.db.set_default(CHILD_KEY.format(result["run"]), parent)
    return result["run"]


# ---------------------------------------------------------------- state helpers

def _full(run_name: str) -> dict:
    raw = frappe.db.get_default(FULL_KEY.format(run_name))
    return json.loads(raw) if raw else {"step": 0, "child": "", "notes": []}


def _load_related_test(run_name: str, *, expected_digest=None) -> dict:
    import hashlib
    raw = frappe.db.get_value('HERO Integration Run', run_name, 'testing_cohort_json')
    if expected_digest is not None and (not isinstance(raw, str) or
            hashlib.sha256(raw.encode()).hexdigest() != expected_digest):
        raise SourceBindingError('The frozen related test selection changed; start a new run.')
    try:
        cohort = json.loads(raw or '')
    except (TypeError, ValueError):
        raise SourceBindingError('The frozen related test selection is missing or invalid.') from None
    if not isinstance(cohort, dict) or cohort.get('version') != 1:
        raise SourceBindingError('The frozen related test selection is missing or invalid.')
    return cohort


def _save_related_test(run_name: str, cohort: dict) -> str:
    import hashlib
    raw = json.dumps(cohort, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    frappe.db.set_value('HERO Integration Run', run_name, 'testing_cohort_json',
                        raw)
    return hashlib.sha256(raw.encode()).hexdigest()


def _save_full(run_name: str, state: dict) -> None:
    frappe.db.set_default(FULL_KEY.format(run_name), json.dumps(state))


def active_child(run_name: str) -> str:
    child = _full(run_name).get("child") or ""
    return child if child and frappe.db.get_value("HERO Integration Run", child, "state") in ACTIVE else ""


# ---------------------------------------------------------------- Synchronization page

AREA_TEXT = {
    "employees": ("Employees", EMPLOYEE_ACTION, EMPLOYEE_ACTION,
                  "Import employees into your ERPNext company.",
                  "Joining date 2026-01-01. New active users receive only the Employee role; local changes require review."),
    "services": ("Services", SERVICE_ACTION, SERVICE_ACTION,
                 "Import services with selling prices and material/labor calculations.",
                 "Non-stock articles. Local edits require review; unresolved material references remain visible."),
    "products": ("Products", "Product Migration", PRODUCT_ACTION,
                 "Import all HERO articles (own articles and supplier catalogs) as ERPNext Items with prices.",
                 "Item code, name, description, unit, catalog as Item Group, manufacturer as Brand, EAN, purchase "
                 "and sales price. Items changed in ERPNext are reported as conflicts, not overwritten."),
    "documents": ("Documents", "Document Migration", "Document Migration",
                  "Transfer HERO documents (invoices, offers, confirmations, letters …) as private files.",
                  "Every HERO file except pictures, attached to its Project or Customer. Unchanged files are skipped."),
    "pictures": ("Pictures", "Picture Migration", "Picture Migration",
                 "Transfer HERO project pictures as private files.",
                 "All HERO pictures, attached to their Project. Unchanged files are skipped."),
    "full": ("Everything", "Full Migration", FULL_ACTION,
             "Run all migrations one after another: employees, contacts, projects, events, tasks, documents, pictures, products, services.",
             "Each step is its own run in the history. A stopped step blocks the chain; continue the run to retry it."),
}
AREA_KEYS = {key: text[2] for key, text in AREA_TEXT.items()}


def page_areas(active: list[dict], instance: str) -> list[dict]:
    from hero_integration.services.sync_page import mapping_scope

    rows = []
    for key, (label, action_label, action, description, note) in AREA_TEXT.items():
        executed = {"action": action, "mode": "Execute", "source_instance": instance}
        last = frappe.get_all("HERO Integration Run", filters={**executed, "state": ["in", list(SUCCESS)]},
                              fields=["finished_on"], order_by="finished_on desc", limit_page_length=1)
        running = next((row for row in active if row.get("action") == action), None)
        blocker, read = "", None
        try:
            read = require_mappings(action, instance).get(DIRECT_OPERATIONS.get(action))
        except Exception as exc:
            blocker = str(exc) or "The mapping and read service could not be validated."
        rows.append({
            "key": key, "label": label, "action_label": action_label, "description": description, "scope_note": note,
            "scope": mapping_scope([spec.__dict__ for spec in read.plan.definitions] if read else []),
            "button": "Update" if frappe.db.exists("HERO Integration Run", executed) else "Start",
            "last_successful": last[0]["finished_on"] if last else None,
            "running_run": running["name"] if running else "", "ready": not blocker, "blocker": blocker,
            "service": {"service": read.origin, "mapping": read.mapping} if read else None,
            "sync_enabled": False, "polled": False, "kind": "full" if key == "full" else "direct",
            "confirm": f"{action_label}: {description}",
        })
    return rows
