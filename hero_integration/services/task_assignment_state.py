"""Protected provenance for system-owned native Task assignments."""
from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar('hero_managed_assignment', default=False)


@contextmanager
def managed_assignment():
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)


def managed_assignment_active():
    return _active.get()


def assignment_boundary_available():
    """Fail closed if the versioned framework extension or app hooks are absent."""
    import frappe
    from frappe.desk.form import assign_to
    return (getattr(assign_to, 'ASSIGNMENT_INTENT_HOOK_VERSION', None) == 2
            and 'hero_integration.services.task_assignment_state.before_native_assignment' in frappe.get_hooks('assignment_intent_before')
            and 'hero_integration.services.task_assignment_state.after_native_assignment' in frappe.get_hooks('assignment_intent_after')
            and 'hero_integration.services.task_assignment_state.guard_native_assignment' in frappe.get_hooks('assignment_intent_guard'))


def _lock_task_and_states(task):
    import frappe
    frappe.db.sql('SELECT name FROM `tabTask` WHERE name=%s FOR UPDATE', (task,))
    return frappe.db.sql('SELECT name FROM `tabHERO Task Assignment State` WHERE task_name=%s FOR UPDATE',
                         (task,), as_dict=True)


def guard_native_assignment(args):
    import frappe
    if (managed_assignment_active() or not args or args.get('doctype') != 'Task'
            or not frappe.db.exists('DocType', 'HERO Task Assignment State')):
        return False
    return bool(_lock_task_and_states(args.get('name')))


def before_native_assignment(args, recipient, ignore_permissions=False):
    import frappe
    if (managed_assignment_active() or args.get('doctype') != 'Task'
            or not frappe.db.exists('DocType', 'HERO Task Assignment State')):
        return
    _lock_task_and_states(args.get('name'))


def after_native_assignment(args, recipient, ignore_permissions=False):
    import frappe
    if (managed_assignment_active() or args.get('doctype') != 'Task'
            or not frappe.db.exists('DocType', 'HERO Task Assignment State')):
        return
    for row in _lock_task_and_states(args.get('name')):
        state = frappe.get_doc('HERO Task Assignment State', row.name, for_update=True)
        with managed_assignment():
            state.state = 'LOCAL'
            state.state_revision = int(state.state_revision) + 1
            state.save(ignore_permissions=True)


def observe_local_todo(doc, method=None):
    """A native ToDo save or deletion permanently hands ownership to local actors."""
    import frappe
    if managed_assignment_active() or frappe.flags.in_migrate or not frappe.db.exists('DocType', 'HERO Task Assignment State'):
        return
    task = doc.reference_name if doc.reference_type == 'Task' else ''
    if task:
        _lock_task_and_states(task)
    names = frappe.db.sql('SELECT name FROM `tabHERO Task Assignment State` WHERE todo=%s OR task_name=%s FOR UPDATE',
                          (doc.name, task), as_dict=True)
    for row in names:
        state = frappe.get_doc('HERO Task Assignment State', row.name, for_update=True)
        with managed_assignment():
            state.state = 'LOCAL'
            state.state_revision = int(state.state_revision) + 1
            state.save(ignore_permissions=True)
