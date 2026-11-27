"""Admission for HERO work sharing a Frappe site's locks volume."""

from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import BoundedSemaphore
import fcntl
import time


MAX_WORKERS = 10
MAX_GRAPHQL = 4
_offline_http = BoundedSemaphore(MAX_GRAPHQL)


def worker_count(value):
    if value is None:
        return 1
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_WORKERS:
        raise ValueError('Polling concurrency must be an integer between 1 and 10.')
    return value


def _site_locks():
    try:
        import frappe
    except ImportError:
        return None
    local = getattr(frappe, 'local', None)
    site = getattr(local, 'site', None)
    sites = getattr(local, 'sites_path', None)
    if not isinstance(site, str) or not isinstance(sites, (str, Path)):
        return None
    return Path(sites) / site / 'locks'


@contextmanager
def _slot(directory, group, limit):
    # The kernel releases flock on close, including a killed RQ workhorse.
    # Never unlink a slot file: existing and new holders must lock one inode.
    directory.mkdir(exist_ok=True)
    while True:
        for index in range(limit):
            with (directory / f'hero-{group}-{index}.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                yield
                return
        time.sleep(0.05)


def graphql_slot():
    directory = _site_locks()
    return _slot(directory, 'graphql', MAX_GRAPHQL) if directory else _offline_http


def shared_width(frappe_module=None):
    if frappe_module is None:
        import frappe as frappe_module
    physical = int((getattr(frappe_module, 'conf', None) or {}).get('hero_shared_workers') or 0)
    if not physical:
        return 0
    if not _site_locks():
        return min(physical, MAX_WORKERS)
    value = frappe_module.db.get_single_value('HERO Integration Settings', 'polling_concurrency', cache=False)
    return min(physical, worker_count(value))


def limited_worker(fn):
    @wraps(fn)
    def execute(*args, **kwargs):
        directory = _site_locks()
        width = shared_width() if directory else 0
        if not width:
            return fn(*args, **kwargs)
        with _slot(directory, 'data-worker', width):
            return fn(*args, **kwargs)
    return execute
