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

FILE_ACTIONS = {"Document Migration": "documents", "Picture Migration": "images"}
PRODUCT_ACTION = "Product Migration"
FULL_ACTION = "Full Migration"
SLICE_ACTIONS = frozenset(FILE_ACTIONS) | {PRODUCT_ACTION}
RUN_ACTIONS = SLICE_ACTIONS | {FULL_ACTION}
# Project Migration processes the referenced contacts first in the same run.
FULL_STEPS = ("Project Migration", "Schedule Migration", "Projection Migration", "Product Migration",
              "Document Migration", "Picture Migration")
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


def confirmed_configuration():
    config = _configuration()
    if not config.source_binding_confirmed:
        raise SourceBindingError("The HERO endpoint, token or source instance changed; "
                                 "an administrator must confirm the source binding.")
    return config


def bound_configuration(run):
    """The configuration a run may use: the confirmed binding it was queued under."""
    config = confirmed_configuration()
    recorded = str(getattr(run, "source_instance", "") or "")
    if not recorded or recorded != config.source_instance:
        raise SourceBindingError("The run belongs to another HERO source instance; queue a new run.")
    if int(getattr(run, "source_binding_revision", 0) or 0) != int(config.source_binding_revision or 0):
        raise SourceBindingError("The run was queued under an earlier HERO source binding; queue a new run.")
    return config


# ---------------------------------------------------------------- starting

def start(action: str, parent: str = "") -> str:
    """Create and queue a file or full run; returns the run name."""
    if action not in RUN_ACTIONS:
        raise ValueError("Unknown file migration action.")
    config = confirmed_configuration()
    if parent:
        bound_configuration(frappe.get_doc("HERO Integration Run", parent))  # a chain never switches sources
    run = frappe.get_doc({"doctype": "HERO Integration Run", "action": action, "mode": "Execute", "state": "Queued",
                          "source_instance": config.source_instance,
                          "source_binding_revision": int(config.source_binding_revision or 0)})
    run.insert(ignore_permissions=True)
    if action in FILE_ACTIONS:
        from hero_integration.services.file_transfer import reset_phase

        reset_phase(FILE_ACTIONS[action])
    elif action == PRODUCT_ACTION:
        from hero_integration.services.product_migration import reset

        reset()
    else:
        _save_full(run.name, {"step": 0, "child": "", "notes": []})
    if parent:
        frappe.db.set_default(CHILD_KEY.format(run.name), parent)
    _enqueue(run.name)
    return run.name


def resume(run) -> None:
    """Continue a failed, cancelled or blocked file/full run where it stopped."""
    bound_configuration(run)
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

    client, downloader, instance = _client_and_downloader(config)
    if run.action == PRODUCT_ACTION:
        from hero_integration.services.product_migration import sync_products

        with filelock("hero_product_migration", timeout=5):
            state = sync_products(client, source_instance=instance, should_stop=cancelled)
    else:
        with filelock("hero_file_transfer", timeout=5):
            state = transfer(FILE_ACTIONS[run.action], source_instance=instance, client=client,
                             downloader=downloader, should_stop=cancelled)
    counts = state.get("counts") or {}
    skipped = counts.get("unchanged", 0) + sum(v for k, v in counts.items() if k.startswith("skipped_"))
    problems = counts.get("failed", 0) + counts.get("conflict", 0)
    values = {"created_records": counts.get("added", 0) + counts.get("updated", 0), "skipped_records": skipped,
              "conflict_records": problems, "error_message": summary(counts)}
    values["processed"] = values["created_records"] + skipped + values["conflict_records"]
    frappe.db.set_value("HERO Integration Run", run.name, values)
    frappe.db.commit()
    if state.get("stopped"):
        return  # cancelled by the user; the run keeps its Cancelled state
    if state.get("last_error"):
        _finish(run.name, "Blocked", f"{state['last_error']} {summary(counts)}")
    elif not state.get("done"):
        _enqueue(run.name)
        frappe.db.commit()
    else:
        _finish(run.name, "Completed With Conflicts" if problems else "Completed", summary(counts))


def summary(counts: dict) -> str:
    skipped = sum(v for k, v in counts.items() if k.startswith("skipped_"))
    text = (f"{counts.get('added', 0)} added, {counts.get('updated', 0)} updated, {counts.get('unchanged', 0)} unchanged, "
            f"{skipped} skipped, {counts.get('conflict', 0)} conflicts, {counts.get('failed', 0)} failed")
    if "bytes" in counts or "bytes_planned" in counts:
        text += f", {counts.get('bytes', 0) / 2**30:.2f} GB transferred"
    return text + "."


def _finish(run_name: str, state: str, message: str) -> None:
    if frappe.db.get_value("HERO Integration Run", run_name, "state") == "Cancelled":
        return
    frappe.db.set_value("HERO Integration Run", run_name, {"state": state, "finished_on": now_datetime(),
                                                           "error_message": message[:1000]})
    frappe.db.commit()
    on_run_finished(run_name)


def on_run_finished(run_name: str) -> None:
    """Advance the Full Migration that started ``run_name``, if any."""
    parent = frappe.db.get_default(CHILD_KEY.format(run_name))
    if parent and frappe.db.get_value("HERO Integration Run", parent, "state") in ACTIVE:
        _enqueue(parent)
        frappe.db.commit()


def advance_full(run_name: str) -> None:
    """Check the current child; start the next step or finish the Full Migration."""
    state = _full(run_name)
    while True:
        child = state.get("child")
        if child:
            child_state = frappe.db.get_value("HERO Integration Run", child, "state")
            if child_state in ACTIVE:
                return  # the child advances the chain when it finishes
            step = FULL_STEPS[state["step"]]
            if child_state not in SUCCESS:
                _save_full(run_name, state)
                _finish(run_name, "Blocked", f"{step} ({child}) ended as {child_state}. Continue this run to retry the step.")
                return
            if child_state == "Completed With Conflicts":
                state["notes"].append(f"{step}: conflicts in {child}")
            state.update(step=state["step"] + 1, child="")
        if state["step"] >= len(FULL_STEPS):
            _save_full(run_name, state)
            notes = "; ".join(state["notes"])
            _finish(run_name, "Completed With Conflicts" if notes else "Completed", notes or "All steps completed.")
            return
        action = FULL_STEPS[state["step"]]
        child = _start_step(action, run_name)
        if not child:
            state["notes"].append(f"{action}: skipped, no approved read service")
            state["step"] += 1
            continue
        state["child"] = child
        _save_full(run_name, state)
        frappe.db.set_value("HERO Integration Run", run_name, {
            "resume_phase": action, "error_message": f"Step {state['step'] + 1}/{len(FULL_STEPS)}: {action} ({child})"})
        frappe.db.commit()
        return


def _start_step(action: str, parent: str) -> str:
    if action in SLICE_ACTIONS:
        return start(action, parent=parent)
    from hero_integration import api
    from hero_integration.services.sync_page import BY_RUN_ACTION

    item = BY_RUN_ACTION[action]
    instance = bound_configuration(frappe.get_doc("HERO Integration Run", parent)).source_instance
    services = api._sync_area_read_services(instance).get(item.operation) or []
    if not services:
        return ""
    result = api._start_migration(action=action, dry_run=0, scheduled=False,
                                  graphql_service=services[0]["service"], source_instance=instance)
    frappe.db.set_default(CHILD_KEY.format(result["run"]), parent)
    return result["run"]


# ---------------------------------------------------------------- state helpers

def _full(run_name: str) -> dict:
    raw = frappe.db.get_default(FULL_KEY.format(run_name))
    return json.loads(raw) if raw else {"step": 0, "child": "", "notes": []}


def _save_full(run_name: str, state: dict) -> None:
    frappe.db.set_default(FULL_KEY.format(run_name), json.dumps(state))


def active_child(run_name: str) -> str:
    child = _full(run_name).get("child") or ""
    return child if child and frappe.db.get_value("HERO Integration Run", child, "state") in ACTIVE else ""


# ---------------------------------------------------------------- Synchronization page

AREA_TEXT = {
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
             "Run all migrations one after another: projects with contacts, events, tasks, products, documents, pictures.",
             "Each step is its own run in the history. A stopped step blocks the chain; continue the run to retry it."),
}
AREA_KEYS = {key: text[2] for key, text in AREA_TEXT.items()}
_FILE_FIELDS = ["file_name", "attached_to_doctype", "attached_to_name", "folder"]
SCOPE = {
    "products": [{"doctype": "Item", "fields": ["item_code", "item_name", "description", "item_group", "stock_uom",
                                                "brand", "barcodes", "disabled"]},
                 {"doctype": "Item Price", "fields": ["price_list", "price_list_rate"]}],
    "documents": [{"doctype": "File", "fields": _FILE_FIELDS}],
    "pictures": [{"doctype": "File", "fields": _FILE_FIELDS}],
}


def page_areas(active: list[dict], instance: str) -> list[dict]:
    rows = []
    for key, (label, action_label, action, description, note) in AREA_TEXT.items():
        executed = {"action": action, "mode": "Execute", "source_instance": instance}
        last = frappe.get_all("HERO Integration Run", filters={**executed, "state": ["in", list(SUCCESS)]},
                              fields=["finished_on"], order_by="finished_on desc", limit_page_length=1)
        running = next((row for row in active if row.get("action") == action), None)
        objects = SCOPE.get(key, [])
        rows.append({
            "key": key, "label": label, "action_label": action_label, "description": description, "scope_note": note,
            "scope": {"object_count": len(objects), "field_count": sum(len(o["fields"]) for o in objects), "objects": objects},
            "button": "Update" if frappe.db.exists("HERO Integration Run", executed) else "Start",
            "last_successful": last[0]["finished_on"] if last else None,
            "running_run": running["name"] if running else "", "ready": True, "blocker": "", "service": None,
            "sync_enabled": False, "polled": False, "kind": "full" if key == "full" else "direct",
            "confirm": f"{action_label}: {description}",
        })
    return rows
