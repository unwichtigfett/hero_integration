"""Opt-in aggregate timings without request data or source identities."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import time


_active = ContextVar('hero_performance', default=None)


@contextmanager
def collect():
    """Collect inclusive phase times in memory; nested collectors share the scope."""
    current = _active.get()
    if current is not None:
        yield current
        return
    result = {}
    token = _active.set(result)
    try:
        yield result
    finally:
        _active.reset(token)


@contextmanager
def measure(phase):
    stats = _active.get()
    if stats is None:
        yield
        return
    start = time.perf_counter()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        row = stats.setdefault(phase, {'calls': 0, 'errors': 0, 'total_ms': 0.0})
        row['calls'] += 1
        row['errors'] += int(failed)
        row['total_ms'] += (time.perf_counter() - start) * 1000


def measured(phase):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with measure(phase):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def _enabled():
    try:
        import frappe
        return frappe.conf.get('hero_performance_metrics') in (True, 1, '1')
    except (AttributeError, RuntimeError):
        return False


def profiled(phase):
    """Emit one site-local aggregate per outer operation when explicitly enabled."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if _active.get() is not None or not _enabled():
                with measure(phase):
                    return fn(*args, **kwargs)
            with collect() as stats:
                try:
                    with measure(phase):
                        return fn(*args, **kwargs)
                finally:
                    try:
                        import frappe
                        frappe.logger('hero_performance', allow_site=True).info(
                            json.dumps({'operation': phase, 'phases': stats}, sort_keys=True))
                    except Exception:
                        # Instrumentation must not alter transaction outcomes.
                        pass
        return wrapped
    return decorate
