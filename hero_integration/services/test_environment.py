"""Test admission and the workspace selective reset."""
import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from uuid import UUID

STATE = Path('/run/hero-test-state/sandbox.json')
INBOX = Path('/run/hero-test-requests')


class EnvironmentUnavailable(ValueError):
    """The origin test may not write in the current site or reset generation."""


def _state(frappe, *, allow_pending=False):
    try:
        state = json.loads(STATE.read_text())
        env = UUID(hex=state['environment']).hex
        generation = UUID(hex=state['generation']).hex
        exclusive = getattr(frappe.conf, 'hero_exclusive_test_environment', None)
        named_site = (state['site'] == f'hero-test-{env}.localhost' and state['database'] == 'ht_' + env[:20])
        admitted = named_site if exclusive is None else exclusive == env
        if (not admitted or env != state['environment'] or generation != state['generation']
                or state['phase'] != 'ready' or not re.fullmatch(r'[a-f0-9]{64}', state['baseline'])
                or state['site'] != frappe.local.site
                or state['database'] != frappe.conf.db_name
                or frappe.conf.db_host != 'db' or not INBOX.is_dir()
                or not allow_pending and (INBOX / 'pending.json').exists()):
            raise ValueError
        return state
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise EnvironmentUnavailable('Testing requires the ready, isolated HERO test site and a sealed baseline.') from None


def require_environment(frappe_module=None):
    if frappe_module is None:
        import frappe as frappe_module
    return _state(frappe_module)['environment']


def environment_generation(frappe_module=None):
    if frappe_module is None:
        import frappe as frappe_module
    return _state(frappe_module)['generation']


def guard_run(run):
    if int(getattr(run, 'testing_enabled', 0) or 0):
        require_environment()
        if json.loads(getattr(run, "testing_results_json", None) or "{}").get("reset_retained"):
            raise EnvironmentUnavailable("This run is retained for reset review. Start a new test run.")


def _admin(frappe):
    if frappe.session.user != 'Administrator':
        raise PermissionError('Only Administrator can reset this test site.')


def inventory(frappe):
    from frappe.utils.background_jobs import get_queues, get_redis_conn
    from rq import Worker

    redis = get_redis_conn()
    queues = list(get_queues(connection=redis))
    return {
        'waiting_jobs': sum(queue.count + redis.llen(queue.intermediate_queue_key)
                            + queue.deferred_job_registry.get_job_count(cleanup=False)
                            + queue.scheduled_job_registry.get_job_count(cleanup=False) for queue in queues),
        'running_jobs': max(sum(queue.started_job_registry.get_job_count(cleanup=False) for queue in queues),
                            sum(bool(w.get_current_job_id()) or w.get_state() == 'busy'
                                for w in Worker.all(connection=redis))),
        'migration_runs': frappe.db.count('HERO Integration Run', {'testing_enabled': 1}),
        'active_or_queued': frappe.db.count('HERO Integration Run', {'state': ('in', ['Running', 'Queued'])}),
        'budgets': frappe.db.count('HERO Execution Budget'),
        'outcomes': frappe.db.count('HERO Budget Outcome'),
        'selection_series': frappe.db.count('HERO Test Cohort Series'),
        'monitor_tests': frappe.db.count('HERO Polling Event', {'manual_approval': ['is', 'set']}),
        'bounded_sync': frappe.db.count('HERO Sync Run Ledger', {'request_fingerprint': ['is', 'set']}),
    }


def _key():
    import frappe
    if not frappe.conf.encryption_key:
        raise ValueError('Provision the dedicated site encryption key before sealing the snapshot.')
    return frappe.conf.encryption_key


def _signature(state, digest, expiry):
    data = json.dumps([state["site"], state["environment"], state["generation"], digest, expiry]).encode()
    return hmac.new(_key().encode("utf-8"), data, hashlib.sha256).hexdigest()


def _reset_activity(frappe):
    """Only HERO jobs block the HERO reset. Other apps can continue to work."""
    from frappe.utils.background_jobs import get_queues, get_redis_conn
    from rq import Worker
    from rq.job import Job

    redis = get_redis_conn()
    waiting, running = set(), set()
    for queue in get_queues(connection=redis):
        waiting.update(queue.get_job_ids())
        waiting.update(redis.lrange(queue.intermediate_queue_key, 0, -1))
        waiting.update(queue.deferred_job_registry.get_job_ids(cleanup=False))
        waiting.update(queue.scheduled_job_registry.get_job_ids(cleanup=False))
        running.update(queue.started_job_registry.get_job_ids(cleanup=False))
    running.update(w.get_current_job_id() for w in Worker.all(connection=redis))
    def scoped(ids):
        ids = sorted({i.decode() if isinstance(i, bytes) else i for i in ids if i})
        return sum(bool(j and j.kwargs.get("site") == frappe.local.site
                   and str(j.kwargs.get("method") or j.func_name).startswith("hero_integration."))
                   for j in Job.fetch_many(ids, connection=redis))
    return {"waiting_jobs": scoped(waiting), "running_jobs": scoped(running),
            "active_or_queued": frappe.db.count("HERO Integration Run", {"state": ["in", ["Queued", "Running"]]}),
            "deletion_enabled": bool(frappe.db.get_single_value("HERO Integration Settings", "allow_deletion")),
            "external_send_enabled": bool(frappe.db.get_single_value("HERO Integration Settings", "allow_graphql_mutation_execution")),
            "synchronization_enabled": frappe.db.get_single_value("HERO Integration Settings", "polling_mode") not in ("OFF", None, "")}


def preview(frappe):
    _admin(frappe)
    state = _state(frappe)
    from hero_integration.services import selective_test_reset as reset
    manifest, counts = reset.plan(frappe)
    counts.update(_reset_activity(frappe))
    expiry = int(time.time()) + 300
    return {"site": state["site"], "inventory": counts,
            "token": f"{expiry}.{_signature(state, reset.fingerprint(manifest), expiry)}"}


def submit(frappe, token, confirmed):
    _admin(frappe)
    state = _state(frappe)
    if str(confirmed) not in ("1", "True"):
        raise ValueError("Explicit reset confirmation is required.")
    expires, _, signature = str(token).partition(".")
    if not re.fullmatch(r"[0-9]{10}", expires) or not int(time.time()) <= int(expires) <= int(time.time()) + 300:
        raise ValueError("The reset preview has expired. Open a new preview.")
    from hero_integration.services import run_reservation, selective_test_reset as reset
    if not run_reservation.acquire(frappe):
        raise ValueError("Another HERO operation is running.")
    try:
        slot = run_reservation.locked_legacy_slot(frappe)
        counts = _reset_activity(frappe)
        if (any(counts[k] for k in ("active_or_queued", "waiting_jobs", "running_jobs", "synchronization_enabled", "external_send_enabled"))
                or (slot and run_reservation.legacy_slot_is_active(frappe, slot))
                or run_reservation.locked_pending_migrations(frappe)
                or run_reservation.locked_b8_runs(frappe, frappe.local.site)):
            raise ValueError("Turn off HERO polling and wait for HERO background work to finish before resetting.")
        manifest, result = reset.plan(frappe, lock=True)
        if _state(frappe) != state or not hmac.compare_digest(signature, _signature(state, reset.fingerprint(manifest), int(expires))):
            raise ValueError("The reset selection changed. Open a new preview.")
        if result["active_mutations"]:
            raise ValueError("Resolve pending HERO mutations before resetting.")
        reset.apply(frappe, manifest)
        if slot in manifest["artifacts"].get("HERO Integration Run", ()):
            frappe.db.set_single_value(run_reservation.SLOT_DOCTYPE, run_reservation.SLOT_FIELD, "")
        return {"status": "completed", "site": state["site"], "inventory": result}
    finally:
        run_reservation.release(frappe)


def baseline_inventory():
    """Host-only check on the sealed, exclusive site before or after restoring it."""
    import frappe
    from frappe.utils import get_assets_json
    filename = 'hero_workspace.bundle.js'
    disk = json.loads(Path('assets/assets.json').read_text())[filename]
    if (get_assets_json().get(filename) != disk or not disk.startswith('/assets/hero_integration/dist/js/')
            or not Path(disk.lstrip('/')).is_file()):
        raise ValueError('The cached HERO workspace bundle differs from the installed image. Clear the sandbox cache before sealing.')
    return inventory(frappe)
