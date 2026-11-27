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
import os

import frappe
from hero_integration.services import migration_archive
from hero_integration.services import execution_budget
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
FULL_STEPS_WITHOUT_PICTURES = tuple(step for step in NEW_FULL_STEPS if step != "Picture Migration")


def pictures_enabled():
    """Read the current site's durable migration selection."""
    local = getattr(frappe, "local", None)
    conf = getattr(local, "conf", None) or {}
    sites = json.loads(os.environ.get("HERO_PICTURE_EXCLUDED_SITES", "[]"))
    if not isinstance(sites, list) or any(not isinstance(site, str) for site in sites):
        raise ValueError("HERO_PICTURE_EXCLUDED_SITES must be a JSON list of site names.")
    return not (bool(int(conf.get("hero_exclude_picture_migration", 0) or 0))
                or getattr(local, "site", None) in sites)


UNSCOPED_PROJECT_AREAS = {
    PRODUCT_ACTION: 'The product reader has no approved product-to-project relationship.',
    SERVICE_ACTION: 'The service reader has no approved service-to-project relationship.',
}
ACTIVE = ("Queued", "Running")
SUCCESS = ("Completed", "Completed With Conflicts")
FULL_KEY = "hero_full_migration:{}"
CHILD_KEY = "hero_full_migration_child:{}"
WAVE_DELIVERY_KEY = "hero_wave_slice_delivery:{}"


class SourceBindingError(RuntimeError):
    """The HERO source of a run is unconfirmed or no longer the configured one."""


class DirectContractChanged(SourceBindingError):
    """The worker lost its source, mapping or persisted run before confirmation."""


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


@migration_archive.for_run
def prepare_related_test(run):
    """Freeze project identities before the first child of a bounded full run."""
    from hero_integration.services.file_transfer import _client_and_downloader
    from hero_integration.services.migration_testing import prepare_project_cohort, project_reader_fingerprint, record_limit
    config = bound_configuration(run)
    read = direct_runtime.approved_read('HeroProjectMatches', config.source_instance)
    if read is None:
        raise SourceBindingError('Related test selection requires an approved Project read service.')
    version = int(getattr(run, 'budget_contract_version', 0) or 1)
    graph_version = 2 if version >= 2 else 1
    before = project_reader_fingerprint(read, version=graph_version)
    client, _, instance = _client_and_downloader(config)
    if instance != config.source_instance:
        raise SourceBindingError('The related test reader belongs to another HERO source.')
    if version == 3:
        from dataclasses import replace
        from hero_integration.services.migration_testing import project_cohort
        from hero_integration.services.migration_testing_selection import incremental_direct_ids, selected_direct_rows
        budget = execution_budget.for_run(run)

        def active_project(row):
            if type(row.get('is_deleted')) is not bool or str(row.get('id')) != str(read.plan.source_identity(row)):
                raise SourceBindingError('The project reader must prove the project identity and deletion state.')
            return not row['is_deleted'] or migration_archive.includes_archived()

        def verify(reader, fingerprint):
            frappe.db.rollback()
            bound_configuration(run)
            if frappe.db.get_value('HERO Integration Run', run.name, 'state') not in ACTIVE:
                raise SourceBindingError('The full migration no longer owns its selection.')
            current = direct_runtime.approved_read(reader.operation, instance)
            if current is None or project_reader_fingerprint(current, version=2) != fingerprint:
                raise SourceBindingError('The approved reader changed during incremental selection.')

        scoped = bool(_full(run.name).get('scoped_project_reads'))
        if scoped:
            identity_read = replace(read,projection_paths=('project_matches.id','project_matches.is_deleted'),
                                    projection_preserve_paths=())
            from hero_integration.services.scoped_test_reads import project_sample_pages
            def verify_project():
                verify(read,before)
            pages = project_sample_pages(client,identity_read,budget,verify_project)
            projects = budget.incremental_ids('HeroProjectMatches','project',
                        ([read.plan.source_identity(row) for row in rows if active_project(row)] for rows in pages))
            relation_paths = {'id','is_deleted','project_match_assignments.user_id'}
            relation_paths.update(rule.source_path for rule in read.plan.definitions if
                                  rule.transformation in ('HERO Customer Link','User By Partner ID') or rule.purpose=='Customer Relation')
            relation_read = replace(read,projection_paths=tuple('project_matches.'+key for key in sorted(relation_paths)),
                                    projection_preserve_paths=())
            rows = [row for page in project_sample_pages(client,relation_read,budget,verify_project,ids=projects)
                    for row in page]
            frozen = replace(relation_read,testing_rows=tuple(rows))
        else:
            # An earlier root can have selection receipts even before its first discovery checkpoint.
            projects = incremental_direct_ids(client, read, budget, verify=lambda: verify(read, before),
                                              eligible=active_project, scoped=False)
            frozen = selected_direct_rows(client, read, projects, verify=lambda: verify(read, before), scoped=False)
        if any(not active_project(row) for row in frozen.testing_rows):
            raise SourceBindingError('A frozen project was deleted before dependency discovery.')
        cohort = project_cohort(frozen.testing_rows, read.plan, record_limit(run), version=2)
        cohort['additional_cohorts'] = {}
        cohort['projection_reader_fingerprints'] = {}
        for operation, kind in (('HeroProducts', 'supply_product'), ('HeroServices', 'supply_service')):
            area = direct_runtime.approved_read(operation, instance)
            if area is None:
                raise SourceBindingError('Missing approved incremental reader for ' + operation)
            fingerprint = project_reader_fingerprint(area, version=2)
            cohort['additional_cohorts'][kind] = incremental_direct_ids(
                client, area, budget, verify=lambda: verify(area, fingerprint), scoped=scoped)
            verify(area, fingerprint)
            cohort['projection_reader_fingerprints'][operation] = fingerprint
    else:
        overlap = int(getattr(run, "page_overlap", 0) or 0)
        selection = {"page_overlap": overlap} if overlap else {}
        if version == 2:
            selection['version'] = 2
        cohort = prepare_project_cohort(client, read, record_limit(run), **selection)
    bound_configuration(run)
    current = direct_runtime.approved_read('HeroProjectMatches', config.source_instance)
    if current is None or project_reader_fingerprint(current, version=graph_version) != before:
        raise SourceBindingError('Project mapping changed while selecting related test records.')
    cohort.update(source_instance=config.source_instance, source_binding_revision=int(config.source_binding_revision),
                  project_reader_fingerprint=before)
    return cohort


def related_test_for_run(run):
    """Return only a still-valid sample belonging to this full-run child."""
    if not bool(int(getattr(run, 'testing_enabled', 0) or 0)):
        return None
    contract_version = int(getattr(run, 'budget_contract_version', 0) or 1)
    parent = _parent_run(run.name)
    if not parent:
        if contract_version >= 2 and run.execution_budget_run != run.name:
            raise SourceBindingError('A bounded child has lost its project cohort owner.')
        return None
    state = _full(parent)
    if (contract_version >= 2
            and (not state.get('related_test_ready') or run.execution_budget_run != parent)):
        raise SourceBindingError('A bounded child must wait for its frozen project cohort.')
    if not state.get('related_test_enabled'):
        return None  # a full run accepted before related selection existed
    if not state.get('related_test_digest'):
        raise SourceBindingError('The frozen related test selection has no integrity proof.')
    cohort = _load_related_test(parent, expected_digest=state.get('related_test_digest'))
    config = bound_configuration(run)
    if (cohort.get('version') != (2 if contract_version >= 2 else 1)
            or ('additional_cohorts' in cohort) != (contract_version == 3)
            or cohort.get('source_instance') != config.source_instance or
            cohort.get('source_binding_revision') != int(config.source_binding_revision)):
        raise SourceBindingError('The related test sample no longer matches the confirmed HERO source.')
    from hero_integration.services.migration_testing import project_reader_fingerprint
    version = cohort['version']
    project_reader = direct_runtime.approved_read('HeroProjectMatches', config.source_instance)
    if (project_reader is None or project_reader_fingerprint(project_reader, version=version)
            != cohort.get('project_reader_fingerprint')):
        raise SourceBindingError('The related test Project reader or mapping changed; start a new run.')
    operation = {'Projection Migration': 'HeroTasks', 'Schedule Migration': 'HeroCalendarEvents'}.get(run.action)
    if version == 2:
        operation = {**DIRECT_OPERATIONS, 'Customer Migration': 'HeroCustomers',
                     'Project Migration': 'HeroProjectMatches'}.get(run.action, operation)
        if not operation or (contract_version == 2 and run.action in UNSCOPED_PROJECT_AREAS):
            raise SourceBindingError('This child has no proven project relationship.')
    if operation and (cohort['project_ids'] or run.action in UNSCOPED_PROJECT_AREAS):
        read = project_reader if operation == 'HeroProjectMatches' else direct_runtime.approved_read(operation, config.source_instance)
        expected = (cohort['project_reader_fingerprint'] if operation == 'HeroProjectMatches' else
                    cohort.get('projection_reader_fingerprints', {}).get(operation))
        if read is None or project_reader_fingerprint(read, version=version) != expected:
            raise SourceBindingError('The related test reader or mapping changed; start a new run.')
        if version == 2:
            if (run.graphql_service, run.graphql_mapping, run.service_revision) != (read.origin, read.mapping, read.service_revision):
                raise SourceBindingError('The queued child reader differs from the frozen selection reader.')
            if operation == 'HeroProjectMatches':
                customer = direct_runtime.approved_read('HeroCustomers', config.source_instance)
                if (customer is None or project_reader_fingerprint(customer, version=version)
                        != cohort['projection_reader_fingerprints'].get('HeroCustomers')
                        or (run.customer_service, run.customer_mapping) != (customer.origin, customer.mapping)):
                    raise SourceBindingError('The queued Project customer reader differs from the frozen selection.')
    return cohort


RELATED_DISCOVERY_OPERATIONS = ('HeroTasks', 'HeroCalendarEvents', 'HeroEmployees', 'HeroCustomers',
                                'HeroDocuments', 'HeroPictures')


@migration_archive.for_run
def discover_related_test_page(run, state, cohort, *, page_budget=10):
    """Discover related Tasks and Events in bounded, restartable read-only slices."""
    import hashlib
    from hero_integration.services.file_transfer import _client_and_downloader
    from hero_integration.services.migration_testing import collect_related_projection, project_reader_fingerprint
    config = bound_configuration(run)
    client, _, instance = _client_and_downloader(config)
    if instance != config.source_instance:
        raise SourceBindingError('Related test discovery belongs to another HERO source.')
    if cohort.get('scoped_project_reads'):
        from hero_integration.services.scoped_test_reads import discover
        def verify_run():
            frappe.db.rollback()
            bound_configuration(run)
            if frappe.db.get_value('HERO Integration Run',run.name,'state') not in ACTIVE:
                raise SourceBindingError('Project cohort discovery is no longer active.')
        return discover(client, instance, state, cohort, page_budget, verify_run)
    if not cohort['project_ids']:
        return True
    operations = RELATED_DISCOVERY_OPERATIONS if cohort.get('version') == 2 else RELATED_DISCOVERY_OPERATIONS[:2]
    if 'Picture Migration' not in state.get('steps', NEW_FULL_STEPS):
        operations = tuple(operation for operation in operations if operation != 'HeroPictures')
    progress = state.setdefault('related_test_discovery', {'operation_index': 0, 'offset': 0, 'pages': 0})
    fingerprints = cohort.setdefault('projection_reader_fingerprints', {})
    readers = {}

    def revalidate_readers():
        # Discard the read snapshot before rechecking approvals after transport.
        frappe.db.rollback()
        bound_configuration(run)
        for operation, (_, fingerprint) in readers.items():
            current = direct_runtime.approved_read(operation, instance)
            if current is None or project_reader_fingerprint(current, version=cohort['version']) != fingerprint:
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
            fingerprint = project_reader_fingerprint(read, version=cohort['version'])
            if operation in fingerprints and fingerprints[operation] != fingerprint:
                raise SourceBindingError('A related test reader changed during discovery; start a new run.')
            fingerprints[operation] = fingerprint
            readers[operation] = (read, fingerprint)
        read, fingerprint = readers[operation]
        if progress['pages'] >= 10000:
            raise SourceBindingError('Related test discovery reached its page budget before the source ended.')
        from hero_integration.services.offset_pagination import advance, validate_overlap
        overlap = int(getattr(run, "page_overlap", 0) or 0)
        if operation == 'HeroEmployees':
            overlap = 0  # The approved employee directory has one bounded response.
        if overlap:
            validate_overlap(overlap, read.page_size, migration=True)
        bound_configuration(run)
        page = [] if operation == 'HeroCustomers' else direct_runtime.fetch_page(client, read, progress['offset'])
        bound_configuration(run)
        progress['pages'] += 1
        if operation == 'HeroEmployees':
            from hero_integration.services.migration_testing import collect_project_employees
            collect_project_employees(cohort, page)
        if operation in ('HeroEmployees', 'HeroCustomers') or not page:
            # Customer identities already come directly from the selected project.
            # Capture its reader proof, never enumerate the customer table here.
            progress.update(operation_index=index + 1, offset=0, pages=0, last_page='')
            progress.pop("offset_window", None)
            continue
        values = [row.get('id') if isinstance(row, dict) else None for row in page]
        if any(not isinstance(value, (int, str)) or isinstance(value, bool) or not str(value).strip() for value in values):
            raise SourceBindingError('Related test discovery encountered an invalid source identity.')
        ids = [str(value) for value in values]
        signature = hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()
        if not all(ids) or (cohort['version'] != 2 and len(set(ids)) != len(ids)) or signature == progress.get('last_page'):
            raise SourceBindingError('Related test discovery encountered a missing or repeated source page.')
        window = None
        if overlap:
            window = advance(page, offset=progress["offset"], size=read.page_size, overlap=overlap, migration=True,
                             previous=progress.get("offset_window"))
            if window.changed:
                raise SourceBindingError("A relation changed during test selection; restart selection.")
            progress["offset_window"] = window.checkpoint
        rows = window.rows if window else page
        if operation in ('HeroDocuments', 'HeroPictures'):
            from hero_integration.services.file_transfer import details
            from hero_integration.services.migration_testing import collect_project_files
            if any('category' not in row for row in rows):
                raise SourceBindingError('The file reader omitted the approved category for project discovery.')
            from hero_integration.services.migration_testing import distinct_rows
            candidates = distinct_rows((row for row in rows if direct_runtime.belongs(operation, row)),
                                       lambda row: row['id'], {})
            found = details(client, candidates, read, instance, allow_missing=True)
            collect_project_files(cohort, operation, candidates, found)
        else:
            collect_related_projection(cohort, operation, rows)
        if len(cohort['task_ids']) + len(cohort['event_ids']) > 50000:
            raise SourceBindingError('Related test selection exceeds 50000 Tasks and Events; reduce the project sample.')
        progress['offset'] = window.next_offset if window else progress['offset'] + len(page)
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
    if action == "Picture Migration" and not pictures_enabled():
        raise SourceBindingError("Picture migration is disabled for this site.")
    steps = list(NEW_FULL_STEPS if pictures_enabled() else FULL_STEPS_WITHOUT_PICTURES)
    config = confirmed_configuration()
    from hero_integration.services.migration_testing import configuration, run_metadata
    parent_run = frappe.get_doc('HERO Integration Run', parent) if parent else None
    if parent_run and getattr(parent_run, 'budget_contract_version', 0) == 2 and action in UNSCOPED_PROJECT_AREAS:
        raise SourceBindingError(UNSCOPED_PROJECT_AREAS[action])
    reads = require_mappings(action, config.source_instance, steps=steps) if action == FULL_ACTION else require_mappings(action, config.source_instance)
    if action == EMPLOYEE_ACTION:
        from hero_integration.services.employee_migration import preflight

        preflight(config.source_instance)
    if action == SERVICE_ACTION:
        from hero_integration.services.service_migration import preflight

        preflight(config.source_instance)
    if action in FILE_ACTIONS or action in {FULL_ACTION, EMPLOYEE_ACTION, SERVICE_ACTION}:
        from hero_integration.services.deferred_references import require_storage
        require_storage(frappe)
    if parent_run:
        bound_configuration(parent_run)  # a chain never switches sources
    testing = configuration(parent_run, frappe_module=frappe)
    if testing["testing_enabled"]:
        from hero_integration.services.test_environment import require_environment
        require_environment()
    if action == FULL_ACTION and testing['testing_enabled'] and not frappe.get_meta('HERO Integration Run').has_field('testing_cohort_json'):
        raise SourceBindingError('Related test selection schema is not installed; migrate the app before starting this run.')
    from hero_integration.services.run_reservation import admit_migration
    admit_migration(frappe, parent=parent, action=action)
    run = frappe.get_doc({"doctype": "HERO Integration Run", "action": action, "mode": "Execute", "state": "Queued",
                          "source_instance": config.source_instance,
                          "source_binding_revision": int(config.source_binding_revision or 0), **testing, **run_metadata(testing)})
    if testing['testing_enabled']:
        run.budget_contract_version = (getattr(parent_run, 'budget_contract_version', 0) or 1
                                       if parent_run else execution_budget.CONTRACT_VERSION)
    if parent:
        run.flags.parent_execution_run = parent
        run.execution_budget_run = getattr(parent_run, 'execution_budget_run', None)
        if parent_run.action == FULL_ACTION and testing['testing_enabled'] and not run.execution_budget_run:
            raise SourceBindingError('The earlier full test has no provable global budget; start a new full test.')
    if action in SLICE_ACTIONS:
        from hero_integration.services.migration import bind_run_checkpoint
        reader = reads[DIRECT_OPERATIONS[action]]
        run.graphql_service, run.graphql_mapping = reader.origin, reader.mapping
        run.service_revision = reader.service_revision
        run.mapping_revision = reader.plan.binding_contract.revision
        bind_run_checkpoint(run)
    run.insert(ignore_permissions=True)
    if not parent and testing['testing_enabled']:
        run.db_set('execution_budget_run', run.name, update_modified=False)
    if action in FILE_ACTIONS and not testing["testing_enabled"]:
        from hero_integration.services.file_transfer import reset_phase

        reset_phase(FILE_ACTIONS[action])
    elif action == PRODUCT_ACTION and not testing["testing_enabled"]:
        from hero_integration.services.product_migration import reset

        reset()
    elif action == FULL_ACTION:
        from hero_integration.services.run_reservation import parallel_migrations
        _save_full(run.name, {"step": 0, "child": "", "notes": [],
                              "steps": steps,
                              **({'parallel_areas': True, 'project_product_wave': not testing['testing_enabled']}
                                 if parallel_migrations(frappe) else {}),
                              **({'scoped_project_reads': True} if testing['testing_enabled']
                                 and run.budget_contract_version == 3 else {})})
    if parent:
        frappe.db.set_default(CHILD_KEY.format(run.name), parent)
    _enqueue(run.name)
    return run.name


def resume(run) -> None:
    """Recover an interrupted slice only after acquiring its worker's own lock."""
    if run.state == "Running" and run.action in SLICE_ACTIONS:
        from frappe.utils.file_lock import LockTimeoutError
        from frappe.utils.synchronization import filelock

        try:
            with filelock(_area_lock(run.action), timeout=1):
                _resume(run, recovering=True)
        except LockTimeoutError as error:
            raise SourceBindingError("The direct migration still has an active worker.") from error
    else:
        _resume(run)


def _resume(run, *, recovering=False):
    """Continue the original contract, budget and checkpoint."""
    from hero_integration.services.migration_testing import record_limit
    if run.action == "Picture Migration" and not pictures_enabled():
        raise SourceBindingError("Picture migration is disabled for this site.")
    if record_limit(run) is not None and not execution_budget.for_run(run):
        raise SourceBindingError('Testing imports cannot resume; start a new bounded run.')
    config = bound_configuration(run)
    steps = _full(run.name).get("steps", list(FULL_STEPS)) if run.action == FULL_ACTION else None
    if steps and getattr(run, 'budget_contract_version', 0) == 2:
        steps = [step for step in steps if step not in UNSCOPED_PROJECT_AREAS]
    if run.action in SLICE_ACTIONS:
        _check_direct_contract(run, lock=True)
    else:
        require_mappings(run.action, config.source_instance, steps=steps)
    from hero_integration.services.run_reservation import admit_migration
    if run.action == FULL_ACTION and _full(run.name).get('wave_children'):
        from hero_integration.services.migration_waves import resume_wave
        return resume_wave(run)
    admit_migration(frappe, parent=_parent_run(run.name), resume=run.name, action=run.action)
    if recovering:
        current = frappe.get_doc("HERO Integration Run", run.name, for_update=True)
        if current.state != "Running" or current.started_on != run.started_on:
            raise SourceBindingError("The direct migration changed before recovery.")
        run = current
    run.state, run.started_on, run.error_message, run.finished_on = "Queued", None, "", None
    run.save(ignore_permissions=True)
    if run.action == FULL_ACTION:
        state = _full(run.name)
        child = state.get("child")
        if child and frappe.db.get_value("HERO Integration Run", child, "state") not in SUCCESS:
            if getattr(run, 'execution_budget_run', None) or _sealed_native_child(run, state, child):
                from hero_integration.api import resume_run
                resume_run(child)
                return  # the same frozen child notifies this parent on completion
            state["child"] = ""  # legacy full runs restart a stopped step as a new child
            _save_full(run.name, state)
    _enqueue(run.name)


def _sealed_native_child(parent, state, child):
    """Reuse the original unbounded child when the chain and native checkpoint agree."""
    action = (state.get('steps') or FULL_STEPS)[state['step']]
    if action not in {'Customer Migration', 'Project Migration'}:
        return False
    row = frappe.db.get_value('HERO Integration Run', child,
        ['action', 'mode', 'source_instance', 'source_binding_revision', 'checkpoint_contract_json', 'testing_enabled'],
        as_dict=True, for_update=True)
    if not row or not row.checkpoint_contract_json:
        return False
    if (_parent_run(child) != parent.name or row.action != action or row.mode != parent.mode
            or row.source_instance != parent.source_instance or row.source_binding_revision != parent.source_binding_revision
            or row.testing_enabled):
        raise SourceBindingError('The frozen child does not belong to this full migration checkpoint.')
    return True

def _clear_relationship_notes(state):
    state['notes'] = [note for note in state.get('notes', []) if not note.startswith('Relationship reconciliation:')
                      and not note.endswith('selected primary record(s) have incomplete outcomes or relationships.')]


def _omit_cancelled_picture(run, state, picture):
    """Accept only a finished scalar cohort, apart from its explicitly named cancelled child."""
    from hero_integration.services.migration_waves import wave_plan

    wave = state.get('wave_children') or {}
    step = state.get('step')
    if (run.state != 'Blocked' or run.action != FULL_ACTION or run.mode != 'Execute'
            or run.testing_enabled or state.get('steps') != list(NEW_FULL_STEPS)
            or not state.get('parallel_areas') or type(step) != int or state.get('child')
            or set(wave) != set(wave_plan(state).get(step, ())) or step + len(wave) != len(NEW_FULL_STEPS)
            or not picture or wave.get('Picture Migration') != picture):
        raise SourceBindingError('The checkpoint has no matching final wave with an omitted picture run.')
    finished = list(state.get('completed_children') or [])
    actions = set()
    all_children = finished + list(wave.values())
    if len(all_children) != len(set(all_children)) or len(all_children) != len(NEW_FULL_STEPS):
        raise SourceBindingError('The full migration child set is incomplete or ambiguous.')
    for name in all_children:
        child = frappe.get_doc('HERO Integration Run', name, for_update=True)
        if (_parent_run(name) != run.name or child.source_instance != run.source_instance
                or child.mode != 'Execute' or child.action in actions or child.testing_enabled
                or (name in wave.values() and wave.get(child.action) != name)
                or (child.state != 'Cancelled' if name == picture else child.state not in SUCCESS)):
            raise SourceBindingError('The original child scope or terminal status changed.')
        actions.add(child.action)
        if name in wave.values() and name != picture:
            finished.append(name)
            if child.state == 'Completed With Conflicts':
                state['notes'].append(f'{child.action}: conflicts in {name}')
    if actions != set(NEW_FULL_STEPS):
        raise SourceBindingError('The remaining migration areas have not all finished.')
    state.update(step=len(NEW_FULL_STEPS), completed_children=finished,
                 omitted_children={'Picture Migration': picture})
    state['notes'].append(f'Partial completion: Picture Migration ({picture}) remains Cancelled and was omitted.')
    del state['wave_children']
    state.pop('wave_watch_token', None)


def retry_full_relationships(run_name: str, *, omit_cancelled_picture: str | None = None):
    """Queue only the final binding pass of an already imported full run."""
    frappe.only_for('System Manager')
    run=frappe.get_doc('HERO Integration Run',run_name,for_update=True)
    run.check_permission('write')
    from hero_integration.services.test_environment import guard_run
    guard_run(run)
    state = _full(run_name, lock=True) if omit_cancelled_picture else _full(run_name)
    partial = bool(omit_cancelled_picture and run.state == 'Blocked' and state.get('wave_children'))
    if partial:
        _omit_cancelled_picture(run, state, omit_cancelled_picture)
    elif omit_cancelled_picture:
        if (state.get('omitted_children') or {}).get('Picture Migration') != omit_cancelled_picture:
            raise SourceBindingError('The picture omission does not match this checkpoint.')
        if run.state in (*ACTIVE, *SUCCESS):
            return {'run':run_name,'phase':'relationships'}
    if (run.action!=FULL_ACTION or run.mode=='Preview' or (run.state not in SUCCESS and not partial)
            or state.get('steps')!=list(NEW_FULL_STEPS) or state.get('step')!=len(NEW_FULL_STEPS)
            or state.get('child') or state.get('wave_children') or not state.get('completed_children')):
        raise SourceBindingError('Only a finished full import can restart its relationship pass.')
    from hero_integration.services.run_reservation import admit_migration
    bound_configuration(run)
    for child in state['completed_children']:
        if (_parent_run(child)!=run_name or
                frappe.db.get_value('HERO Integration Run',child,'state') not in SUCCESS or
                frappe.db.get_value('HERO Integration Run',child,'source_instance')!=run.source_instance):
            raise SourceBindingError('The full import child scope no longer matches its relationship pass.')
    admit_migration(frappe, resume=run_name)
    for key in ('reconciliation_done','reconciliation_stage','reconciliation_after',
                'reconciliation_final_after','reconciliation_conflicts'):
        state.pop(key,None)
    _clear_relationship_notes(state)
    _save_full(run_name,state)
    run.state,run.finished_on,run.error_message='Queued',None,''
    run.save(ignore_permissions=True)
    _enqueue(run_name)
    return {'run':run_name,'phase':'relationships'}


def _enqueue(run_name: str, *, intake=False, continuation=False, receipt=None) -> None:
    import time
    from hero_integration.services.run_reservation import in_wave_intake
    from uuid import uuid4

    intake = intake or in_wave_intake()
    if receipt:
        job_id = receipt
    elif intake:
        job_id = f"hero-wave-intake-{run_name}"
    elif continuation and _stored_default(WAVE_DELIVERY_KEY.format(run_name)):
        job_id = f"hero-wave-slice-{run_name}-{uuid4().hex}"
    else:
        job_id = f"hero-file-run:{run_name}:{int(time.time() * 1000)}"
    tracked = job_id.startswith("hero-wave-")
    if tracked and not receipt:
        frappe.db.set_default(WAVE_DELIVERY_KEY.format(run_name), job_id)

    frappe.enqueue("hero_integration.services.file_migration.execute", queue="long", timeout=_job_timeout(),
                   enqueue_after_commit=True, job_id=job_id, deduplicate=tracked,
                   run_name=run_name)


def _job_timeout() -> int:
    from hero_integration.services.file_transfer import TIME_BUDGET_SECONDS
    from hero_integration.services.migration import RUN_JOB_TIMEOUT

    return max(RUN_JOB_TIMEOUT, TIME_BUDGET_SECONDS + 600)


# ---------------------------------------------------------------- background jobs

def execute(run_name: str) -> None:
    from hero_integration.services.execution_runtime import execute as dispatch
    return dispatch('migration', run_name=run_name)


def _execute(run_name: str) -> None:
    # A sibling or the publication observer may finish the same parent between
    # opening this transaction and acquiring the initial row lock. No work has
    # been written by this delivery yet. Recheck ownership on a fresh read view.
    for attempt in range(3):
        try:
            run = frappe.get_doc("HERO Integration Run", run_name, for_update=True)
            break
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as error:
            frappe.db.rollback()
            if attempt == 2:
                raise frappe.RetryBackgroundJobError from error
    from hero_integration.services.test_environment import guard_run
    guard_run(run)
    if run.state not in ACTIVE:
        return
    if run.action == "Picture Migration" and not pictures_enabled():
        _finish(run.name, "Blocked", "Picture migration is disabled for this site.")
        return
    frappe.set_user(run.owner)
    if run.state == "Queued":
        run.state = "Running"
        run.started_on = run.started_on or now_datetime()
        run.save(ignore_permissions=True)
    frappe.db.commit()
    try:
        config = bound_configuration(run)
        with migration_archive.run_selection(run), execution_budget.run_scope(run):
            if run.action == FULL_ACTION:
                advance_full(run.name)
            else:
                _file_slice(run, config)
    except getattr(frappe, 'RetryBackgroundJobError', ()):
        frappe.db.rollback()
        raise  # Frappe retries this job from its last committed child or relationship page.
    except execution_budget.BudgetReached as error:
        frappe.db.rollback()
        _finish(run.name, 'Budget Reached', str(error), started_on=getattr(run, "started_on", None))
    except SourceBindingError as error:
        frappe.db.rollback()
        _finish(run.name, "Blocked", str(error), started_on=getattr(run, "started_on", None))
    except Exception as error:
        frappe.db.rollback()
        frappe.log_error(title=f"HERO {run.action} failed")
        _finish(run.name, "Failed", f"{type(error).__name__}: {str(error)[:300]}", started_on=getattr(run, "started_on", None))


def _area_lock(action):
    return {
        EMPLOYEE_ACTION: 'hero_employee_migration', PRODUCT_ACTION: 'hero_product_migration',
        SERVICE_ACTION: 'hero_service_migration',
    }.get(action, 'hero_file_transfer')


def _file_slice(run, config) -> None:
    """Hold the area lock through its checkpoint and terminal decision."""
    from frappe.utils.file_lock import LockTimeoutError
    from frappe.utils.synchronization import filelock
    lock_name = _area_lock(run.action)
    acquired = False
    try:
        with filelock(lock_name, timeout=5):
            acquired = True
            frappe.db.rollback()
            if frappe.db.get_value('HERO Integration Run', run.name, 'state') != 'Running':
                return
            _process_file_slice(run, config)
    except LockTimeoutError:
        if acquired:
            raise  # an inner operation failed after this worker took ownership
        # Another file phase can still hold the shared lock during its terminal
        # callback. Redeliver a wave child in that case; it may be the only job
        # for a different run and must not silently disappear while Running.
        parent = _parent_run(run.name)
        if parent and run.name in (_full(parent).get('wave_children') or {}).values():
            raise frappe.RetryBackgroundJobError
        return


def _check_direct_contract(run, *, lock=False):
    from hero_integration.services.migration import _assert_checkpoint_contract
    from hero_integration.services.mapping_runtime import MappingPlanError

    try:
        _assert_checkpoint_contract(run, lock=lock)
    except (MappingPlanError, ValueError) as error:
        raise DirectContractChanged(str(error)) from error


def _direct_reader(run, *, expected_state="Running", operation=None):
    from dataclasses import replace

    if operation and DIRECT_OPERATIONS.get(run.action) != operation:
        raise DirectContractChanged("Deferred operation differs from its originating migration.")
    _check_direct_contract(run)
    service = frappe.get_doc("HERO GraphQL Service", run.graphql_service)
    mapping = frappe.get_doc("HERO GraphQL Mapping", run.graphql_mapping)
    snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", service.schema_snapshot)
    read = direct_runtime.read_from_documents(DIRECT_OPERATIONS[run.action], service, mapping, snapshot)

    def validate_write():
        current = frappe.get_doc("HERO Integration Run", run.name, for_update=True)
        if (current.state != expected_state or current.started_on != run.started_on
                or current.owner != frappe.session.user or current.checkpoint_contract_json != run.checkpoint_contract_json):
            raise DirectContractChanged("The direct migration no longer owns its approved checkpoint.")
        _check_direct_contract(run, lock=True)

    return replace(read, validate_write=validate_write)


def _process_file_slice(run, config):
    from hero_integration.services.file_transfer import _client_and_downloader, transfer

    def cancelled() -> bool:
        return frappe.db.get_value("HERO Integration Run", run.name, "state") == "Cancelled"

    page_identities = set()
    settled_counts = None

    def observe_identity(identity, counts):
        nonlocal settled_counts
        page_identities.add(identity)
        settled_counts = counts

    def publish_counts(counts):
        if page_identities:
            from hero_integration.services.integration_events import record_processed_page
            kind = "file_upload" if run.action in FILE_ACTIONS else "supply_product"
            record_processed_page(run.name, kind, page_identities)
            page_identities.clear()
        frappe.db.set_value("HERO Integration Run", run.name, run_counts(counts, action=run.action))

    def progress(counts: dict) -> None:
        read.validate_write()
        # Called after every page, so the Synchronization page shows the counts while the slice runs.
        publish_counts(counts)
        frappe.db.commit()

    from hero_integration.services.migration_testing import record_limit, persist
    maximum = record_limit(run)
    needs_project_sample = run.action in {*FILE_ACTIONS, EMPLOYEE_ACTION} or getattr(run, 'budget_contract_version', 0) == 3
    cohort = related_test_for_run(run) if maximum is not None and needs_project_sample else None
    from hero_integration.client.settings import get_configuration
    read = _direct_reader(run)
    def token():
        return bound_configuration(run, configuration=get_configuration(fresh=True)).api_token
    client, downloader, instance = _client_and_downloader(config, token_provider=token)
    budget = execution_budget.current_budget()
    standalone = budget and budget.version >= 2 and run.name == run.execution_budget_run
    additional = budget and budget.version == 3 and cohort is not None and run.action in UNSCOPED_PROJECT_AREAS
    if standalone or additional:
        from hero_integration.services.migration_testing_selection import prepare_direct, SelectionBlocked

        def verify_selection():
            frappe.db.rollback()
            if cancelled():
                raise DirectContractChanged("Selection was cancelled.")
            _check_direct_contract(run)

        try:
            if additional:
                from hero_integration.services.migration_testing_selection import DIRECT_KINDS, selected_direct_rows
                kind = DIRECT_KINDS[DIRECT_OPERATIONS[run.action]]
                read = selected_direct_rows(client, read, cohort['additional_cohorts'][kind], verify=verify_selection,
                                            scoped=bool(cohort.get('scoped_project_reads')))
            else:
                read = prepare_direct(client, read, budget, run.checkpoint_contract_json, verify=verify_selection)
        except SelectionBlocked as error:
            raise SourceBindingError(str(error)) from error
    overlap = int(getattr(run, "page_overlap", 0) or 0)
    paging = {"read": read, **({"page_overlap": overlap} if overlap else {})}
    try:
        if run.action == SERVICE_ACTION:
            from hero_integration.services.service_migration import sync_services

            state = sync_services(client, source_instance=instance, run_name=run.name, downloader=downloader,
                                  should_stop=cancelled, progress=progress, verify_source=lambda: bound_configuration(run),
                                  record_limit=maximum, continue_on_conflict=True, **paging)
        elif run.action == EMPLOYEE_ACTION:
            from hero_integration.services.employee_migration import sync_employees

            args = {'cohort_user_ids': cohort['user_ids'], 'cohort_partner_ids': cohort['partner_ids']} if cohort is not None else {}
            if cohort is not None and cohort.get('version') == 2:
                args['frozen_employees'] = cohort['employees']
            state = sync_employees(client, source_instance=instance, run_name=run.name,
                                   should_stop=cancelled, progress=progress, verify_source=lambda: bound_configuration(run),
                                   record_limit=maximum, continue_on_conflict=True, read=read, **args)
        elif run.action == PRODUCT_ACTION:
            from hero_integration.services.product_migration import sync_products

            state = sync_products(client, source_instance=instance, should_stop=cancelled, progress=progress,
                                  record_limit=maximum, run_name=run.name, observe_identity=observe_identity, **paging)
        else:
            args = {'cohort': cohort} if cohort is not None else {}
            state = transfer(FILE_ACTIONS[run.action], source_instance=instance, client=client,
                             downloader=downloader, should_stop=cancelled, progress=progress,
                             record_limit=maximum, run_name=run.name, observe_identity=observe_identity, **args, **paging)
    except execution_budget.BudgetReached:
        # Earlier items in this page may have committed individually. Publish
        # their outcomes after discarding the rejected item's transaction. The
        # completed-page offset stays where the reader last confirmed it.
        frappe.db.rollback()
        if page_identities:
            read.validate_write()
            publish_counts(settled_counts)
            frappe.db.commit()
        raise
    if state.get("stopped"):
        return
    read.validate_write()
    counts = state.get("counts") or {}
    if maximum is not None:
        persist(run.name, DIRECT_OPERATIONS[run.action], state)
    problems = sum(counts.get(key, 0) for key in ('failed', 'conflict', 'incomplete', 'pending_review')) + int(any(
        state.get(key) for key in ('testing_missing_user_ids', 'testing_ambiguous_user_ids', 'testing_missing_partner_ids')))
    counts["confirmed_offset"] = state.get("offset")
    publish_counts(counts)
    frappe.db.commit()
    if state.get("last_error"):
        detail = (f"{state['blocked_source_id']}: " if state.get("blocked_source_id") else "") + state["last_error"]
        _finish(run.name, "Blocked", f"{detail} {summary(counts)}", started_on=getattr(run, "started_on", None))
    elif not state.get("done"):
        _enqueue(run.name, continuation=True)
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
        from hero_integration.services.deferred_references import finish_standalone_cohort
        incomplete = finish_standalone_cohort(run)
        if incomplete:
            detail += ' ' + incomplete
        terminal = "Blocked" if incomplete else "Completed With Conflicts" if problems else "Completed"
        _finish(run.name, terminal, detail, started_on=getattr(run, "started_on", None))


def run_counts(counts: dict, *, action=None) -> dict:
    """Run fields for the counts of a product or file pass."""
    skipped = counts.get("updated", 0) + counts.get("unchanged", 0) + sum(v for k, v in counts.items() if k.startswith("skipped_"))
    problems = counts.get("failed", 0) + counts.get("conflict", 0)
    values = {"created_records": counts.get("added", 0), "skipped_records": skipped,
              "conflict_records": problems, "error_message": summary(counts)}
    values["processed"] = values["created_records"] + skipped + values["conflict_records"]
    if action:
        values["progress_counts_json"] = json.dumps({
            "rows_read": counts.get("rows_read"), "created": counts.get("added", 0),
            "updated": counts.get("updated", 0), "unchanged": counts.get("unchanged", 0),
            "skipped": sum(v for k, v in counts.items() if k.startswith("skipped_")),
            "skipped_archived": counts.get("skipped_archived", 0),
            "conflicts": counts.get("conflict", 0), "errors": counts.get("failed", 0),
            "processed": counts.get("unique_processed"), "offset": counts.get("confirmed_offset"),
        })
    return values


def summary(counts: dict) -> str:
    skipped = sum(v for k, v in counts.items() if k.startswith("skipped_"))
    text = (f"{counts.get('added', 0)} added, {counts.get('updated', 0)} updated, {counts.get('unchanged', 0)} unchanged, "
            f"{skipped} skipped, {counts.get('conflict', 0)} conflicts, {counts.get('failed', 0)} failed")
    if counts.get("skipped_archived"):
        text += f", {counts['skipped_archived']} archived"
    if "bytes" in counts or "bytes_planned" in counts:
        text += f", {counts.get('bytes', 0) / 2**30:.2f} GB transferred"
    if counts.get('pending_review'):
        text += f", {counts['pending_review']} related record(s) awaiting review"
    return text + "."


def _finish(run_name: str, state: str, message: str, *, started_on=None) -> None:
    current = frappe.db.get_value("HERO Integration Run", run_name, ["state", "started_on"], for_update=True)
    if not current or current[0] not in ACTIVE or (started_on is not None and current[1] != started_on):
        return
    values = {"state": state, "finished_on": now_datetime(), "error_message": message[:1000]}
    if state == 'Blocked' and frappe.db.get_value('HERO Integration Run', run_name, 'budget_contract_version') in (2, 3):
        values['testing_status'] = 'BLOCKED'
    if state == 'Budget Reached':
        values['testing_status'] = 'LIMIT_REACHED'
    frappe.db.set_value("HERO Integration Run", run_name, values)
    frappe.db.commit()
    on_run_finished(run_name)


def on_run_finished(run_name: str) -> None:
    """Notify relationship realization and advance an originating Full Migration."""
    from hero_integration.services.deferred_references import notify_completed_run
    notify_completed_run(run_name)
    parent = _parent_run(run_name)
    if parent and frappe.db.get_value("HERO Integration Run", parent, "state") in ACTIVE:
        # Publish the child's terminal state and release its reservation before
        # the parent's locked transition. The next child still runs in its own
        # queued job; the handoff itself must not depend on another notification.
        frappe.db.commit()
        execute(parent)


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


def _check_historical_employees(root_name, state):
    """Supplement a frozen selection with deletion evidence without altering its members or digest."""
    from hero_integration.services.employee_migration import _historical_identities
    from hero_integration.services.file_transfer import _client_and_downloader
    from hero_integration.services.migration_testing import missing_employee_refs, project_reader_fingerprint

    digest = state['related_test_digest']
    cohort = _load_related_test(root_name, expected_digest=digest)
    wanted = missing_employee_refs(cohort['unresolved'])
    if not wanted:
        return
    root = frappe.get_doc('HERO Integration Run', root_name)
    config = bound_configuration(root)
    if (config.source_instance, int(config.source_binding_revision)) != (
            cohort['source_instance'], cohort['source_binding_revision']):
        raise SourceBindingError('The historical employee source differs from the frozen cohort.')
    stamp = cohort['projection_reader_fingerprints']['HeroEmployees']
    def reader():
        read = direct_runtime.approved_read('HeroEmployees', config.source_instance)
        if not read or project_reader_fingerprint(read, version=2) != stamp:
            raise SourceBindingError('The approved employee directory changed after cohort selection.')
        return read
    read = reader()
    client, _, _ = _client_and_downloader(config)
    users, partners = _historical_identities(client, read.records_path,
        {i for k, i in wanted if k == 'User'}, {i for k, i in wanted if k == 'partner'},
        read=read, source_instance=config.source_instance)
    bound_configuration(root)
    reader()
    verified = {('User', i) for i in users} | {('partner', i) for i in partners}
    state['historical_employees'] = {'digest': digest, 'source': config.source_instance,
        'revision': cohort['source_binding_revision'], 'users': users, 'partners': partners}
    state['missing_relationships'] = sum(not (missing_employee_refs([entry]) & verified)
                                         for entry in cohort['unresolved'])
    if verified and state.get('reconciliation_done'):
        state.update(reconciliation_stage=0, reconciliation_after='', reconciliation_done=False)
        _clear_relationship_notes(state)
    _save_full(root_name, state)
    frappe.db.commit()


def historical_employee_ids(state, source, revision):
    proof = state.get('historical_employees')
    if not proof:
        return (), ()
    if (proof['digest'], proof['source'], proof['revision']) != (state['related_test_digest'], source, revision):
        raise SourceBindingError('Historical employee proof differs from its project cohort.')
    return proof['users'], proof['partners']


def _advance_full_locked(run_name: str) -> None:
    state = _full(run_name)
    steps = state.get("steps") or FULL_STEPS  # preserve pre-upgrade run checkpoints
    version = frappe.db.get_value('HERO Integration Run', run_name, 'budget_contract_version')
    project_contract = version in (2, 3)
    if (steps in (list(NEW_FULL_STEPS), list(FULL_STEPS_WITHOUT_PICTURES)) and state.get('step') == 0 and not state.get('child')
            and frappe.db.get_value('HERO Integration Run', run_name, 'testing_enabled')
            and not state.get('related_test_enabled')):
        cohort = prepare_related_test(frappe.get_doc('HERO Integration Run', run_name))
        if state.get('scoped_project_reads'):
            cohort['scoped_project_reads'] = True
        state['related_test_digest'] = _save_related_test(run_name, cohort)
        state['related_test_enabled'] = True
        _save_full(run_name, state)
        frappe.db.commit()
    if (steps in (list(NEW_FULL_STEPS), list(FULL_STEPS_WITHOUT_PICTURES)) and state.get('step') == 0 and not state.get('child')
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
    if project_contract and state['step'] == 0 and not state.get('child'):
        from hero_integration.services.migration_testing import project_budget_evidence
        if not state.get('related_test_ready'):
            raise SourceBindingError('The related project cohort must be complete before starting a child.')
        cohort = _load_related_test(run_name, expected_digest=state['related_test_digest'])
        if cohort['version'] != 2:
            raise SourceBindingError('This project cohort belongs to a different budget version.')
        root = frappe.get_doc('HERO Integration Run', run_name)
        evidence = project_budget_evidence(cohort, state['related_test_digest'])
        if version == 3:
            evidence['additional_cohorts'] = cohort['additional_cohorts']
        else:
            evidence['blocked_areas'] = UNSCOPED_PROJECT_AREAS
        execution_budget.for_run(root).freeze('project', cohort['project_ids'], evidence=evidence)
        if cohort['unresolved']:
            state['missing_relationships'] = len(cohort['unresolved'])
    if (version == 3 and state.get('scoped_project_reads') and state.get('missing_relationships')
            and 'historical_employees' not in state):
        _check_historical_employees(run_name, state)
    while True:
        child = state.get("child")
        if child:
            child_state = frappe.db.get_value("HERO Integration Run", child, "state")
            if child_state in ACTIVE:
                return  # the child advances the chain when it finishes
            step = steps[state["step"]]
            if child_state == 'Budget Reached':
                _finish(run_name, 'Budget Reached', f'Global identity budget reached during {step} ({child}).')
                return
            if child_state not in SUCCESS:
                _save_full(run_name, state)
                _finish(run_name, "Blocked", f"{step} ({child}) ended as {child_state}. Continue this run to retry the step.")
                return
            if child_state == "Completed With Conflicts":
                state["notes"].append(f"{step}: conflicts in {child}")
            state.setdefault("completed_children", []).append(child)
            state.update(step=state["step"] + 1, child="")
        from hero_integration.services.migration_waves import advance_wave, wave_plan
        if state.get('parallel_areas') and state['step'] in wave_plan(state):
            if not advance_wave(run_name, state):
                return
            continue
        if state["step"] >= len(steps):
            if steps in (list(NEW_FULL_STEPS), list(FULL_STEPS_WITHOUT_PICTURES)) and not state.get("reconciliation_done"):
                from hero_integration.services.deferred_references import ReferenceRetryExhausted, reconcile_full_page
                source = bound_configuration(frappe.get_doc("HERO Integration Run", run_name)).source_instance
                origins = state.get("completed_children", [])
                try:
                    done = reconcile_full_page(source, origins, state)
                except ReferenceRetryExhausted as error:
                    _save_full(run_name, state)
                    _finish(run_name, 'Blocked', str(error))
                    return
                state['reconciliation_done']=bool(done)
                _save_full(run_name, state)
                if not done:
                    _enqueue(run_name)
                    frappe.db.commit()
                    return
                if state.get('reconciliation_conflicts'):
                    state['notes'].append('Relationship reconciliation: {0} reference(s) require review.'.format(
                        state['reconciliation_conflicts']))
                if state.get('omitted_children') and state.get('reconciliation_waiting'):
                    state['notes'].append('Relationship reconciliation: {0} reference(s) await targets.'.format(
                        state['reconciliation_waiting']))
            if project_contract:
                incomplete = _complete_project_outcomes(run_name, state['completed_children'])
                if incomplete:
                    state['notes'].append(f'{incomplete} selected primary record(s) have incomplete outcomes or relationships.')
            _save_full(run_name, state)
            notes = "; ".join(state["notes"])
            if project_contract and (state.get('blocked_areas') or state.get('missing_relationships')):
                reasons = [f"{action}: {reason}" for action, reason in state.get('blocked_areas', {}).items()]
                if state.get('missing_relationships'):
                    reasons.append(f"{state['missing_relationships']} missing project relationships in frozen cohort.")
                _finish(run_name, "Blocked", " ".join([*reasons, notes]).strip())
            else:
                _finish(run_name, "Completed With Conflicts" if notes else "Completed", notes or "All steps completed.")
            return
        action = steps[state["step"]]
        if version == 2 and action in UNSCOPED_PROJECT_AREAS:
            state.setdefault('blocked_areas', {})[action] = UNSCOPED_PROJECT_AREAS[action]
            state['step'] += 1
            continue
        child = _start_step(action, run_name)
        if not child:
            if steps in (list(NEW_FULL_STEPS), list(FULL_STEPS_WITHOUT_PICTURES)):
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


def _complete_project_outcomes(run_name, origins):
    """Reconcile writer receipts and the final relationship pass in the same transaction."""
    from hero_integration.services.migration_testing import project_completion_issues
    root = frappe.get_doc('HERO Integration Run', run_name)
    budget = execution_budget.for_run(root)
    selection = budget.selection()
    if not selection or selection['main_type'] != 'project':
        raise SourceBindingError('Project completion requires the original frozen selection.')
    outcomes = {(r.object_type, r.external_id): r.outcome for r in frappe.get_all(
        'HERO Budget Outcome', filters={'budget': budget.budget_id, 'phase':'writer'},
        fields=['object_type', 'external_id', 'outcome'], limit_page_length=0)}
    from hero_integration.services.deferred_references import _pending_identity
    pending = frappe.get_all('HERO Pending Reference', filters={'source_instance': root.source_instance,
        'origin_run':['in', origins], 'status':['in', ['Waiting','Conflict']]},
        fields=['name','operation','source_id','status'], limit_page_length=0)
    unresolved = []
    waiting = set()
    for record in pending:
        kind, identity = _pending_identity(record)
        key = kind, str(identity)
        if record.status == 'Waiting' and budget.version == 3:
            # Pending records retain their own writer receipts. A later binding
            # can realize them without reopening or reselecting the project cohort.
            waiting.add(key)
            continue
        unresolved.append(key)
    users, partners = historical_employee_ids(_full(run_name), root.source_instance, root.source_binding_revision)
    historical = {('User', i) for i in users} | {('partner', i) for i in partners}
    issues = project_completion_issues(selection, outcomes, unresolved, waiting=waiting, historical=historical)
    # Writer receipts stay intact. A repaired relationship can therefore clear
    # an earlier incomplete completion on the next reconciliation.
    previous = {(r.object_type, r.external_id): r.outcome for r in frappe.get_all(
        'HERO Budget Outcome', filters={'budget': budget.budget_id, 'phase':'relationships'},
        fields=['object_type', 'external_id', 'outcome'], limit_page_length=0)}
    primary = {('project', key) for key in selection['ids']} | execution_budget.additional_primary_records(selection['evidence'])
    for key in issues.keys() | previous.keys() | primary:
        if waiting and key not in issues and key not in previous:
            continue  # No successful final relationship receipt while observations remain open.
        status = issues.get(key, 'processed')
        if previous.get(key) != status:
            budget.outcome(*key, status, phase='relationships')
    return len(issues.keys() & primary)


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
    result = api._start_migration(action=action, dry_run=0, scheduled=False, parent_run_name=parent,
                                  graphql_service=services[0]["service"], source_instance=instance,
                                  testing_configuration=configuration(
                                      frappe.get_doc('HERO Integration Run', parent)))
    frappe.db.set_default(CHILD_KEY.format(result["run"]), parent)
    return result["run"]


# ---------------------------------------------------------------- state helpers

def _stored_default(key: str, *, lock=False):
    """Read workflow state from its durable row, bypassing Frappe's defaults cache.

    Other workers can populate that cache before a new child or checkpoint
    commits. Cached defaults must not decide sample scope or chain progression.
    """
    return frappe.db.get_value('DefaultValue', {'parent': '__default', 'defkey': key}, 'defvalue',
                               **({'for_update': True} if lock else {}))


def _parent_run(run_name: str):
    return _stored_default(CHILD_KEY.format(run_name))


def _full(run_name: str, *, lock=False) -> dict:
    raw = _stored_default(FULL_KEY.format(run_name), lock=lock)
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
    if not isinstance(cohort, dict) or cohort.get('version') not in (1, 2):
        raise SourceBindingError('The frozen related test selection is missing or invalid.')
    return cohort


def _save_related_test(run_name: str, cohort: dict) -> str:
    import hashlib
    raw = json.dumps(cohort, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if cohort.get('version') == 2 and len(raw.encode()) > execution_budget.MAX_CONTRACT_BYTES:
        raise SourceBindingError('Related identity discovery exceeds the checkpoint storage bound.')
    frappe.db.set_value('HERO Integration Run', run_name, 'testing_cohort_json',
                        raw)
    return hashlib.sha256(raw.encode()).hexdigest()


def _save_full(run_name: str, state: dict) -> None:
    frappe.db.set_default(FULL_KEY.format(run_name), json.dumps(state))


def active_children(run_name: str):
    state = _full(run_name, lock=True)
    children = list(state.get('wave_children', {}).values()) or ([state['child']] if state.get('child') else [])
    return [child for child in children
            if frappe.db.get_value('HERO Integration Run', child, 'state', for_update=True) in ACTIVE]


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
                 "Import HERO articles with a usage count above zero as ERPNext Items with prices.",
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
