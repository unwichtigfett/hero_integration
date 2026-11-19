"""Durable admission of distinct HERO identities before bounded execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json

_MAXIMUM = 50
_current = ContextVar('hero_execution_budget', default=None)
current_budget = _current.get


class BudgetReached(RuntimeError):
    """The next source identity would exceed this run's frozen capacity."""


@dataclass(frozen=True)
class ExecutionBudget:
    scope: str
    run_id: str
    source_instance: str
    limit: int = _MAXIMUM

    def __post_init__(self):
        if (self.scope not in ('migration', 'sync') or not self.run_id or not self.source_instance
                or type(self.limit) is not int or not 1 <= self.limit <= _MAXIMUM):
            raise ValueError('Invalid HERO execution budget contract.')

    @property
    def budget_id(self):
        return sha256(json.dumps([self.scope, self.run_id]).encode()).hexdigest()

    def reserve(self, object_type, external_id):
        return self.reserve_many([(object_type, external_id)])

    def reserve_many(self, identities):
        """Admit an identity plus its identified dependencies as one unit.

        Commit on the site's primary using a separate connection. A business
        rollback retains the reservation, and reservation never commits caller writes.
        """
        requested = set()
        for kind, identity in identities:
            if (not isinstance(kind, str) or not kind.strip() or type(identity) not in (int, str)
                    or not str(identity).strip()):
                raise ValueError('A bounded run requires a stable source identity.')
            # Service compositions and file readers expose the same FileUpload.id.
            kind = 'file_upload' if kind == 'service_file' else kind
            requested.add((kind, str(identity).strip()))
        if not requested:
            return False
        from hero_integration.client.settings import _configuration_connection
        db = _configuration_connection()
        try:
            db.sql("""INSERT INTO `tabHERO Execution Budget`
                (name,creation,modified,owner,modified_by,docstatus,idx,scope,run_id,source_instance,record_limit,identities_json,used)
                VALUES (%s,NOW(6),NOW(6),'Administrator','Administrator',0,0,%s,%s,%s,%s,'[]',0)
                ON DUPLICATE KEY UPDATE name=VALUES(name)""",
                (self.budget_id,self.scope,self.run_id,self.source_instance,self.limit))
            source, limit, raw, used = db.sql("""SELECT source_instance, record_limit, identities_json, used
                FROM `tabHERO Execution Budget` WHERE name=%s FOR UPDATE""", (self.budget_id,))[0]
            if source != self.source_instance or limit != self.limit:
                raise ValueError('The durable execution budget belongs to another run contract.')
            existing = {tuple(item) for item in json.loads(raw)}
            if len(existing) != used or used > limit:
                raise ValueError('Invalid durable HERO identity reservations.')
            combined = existing | requested
            if len(combined) > limit:
                raise BudgetReached(f'The next identity or relationship would exceed the global limit of {limit} ({used} reserved).')
            if combined != existing:
                db.sql("""UPDATE `tabHERO Execution Budget` SET identities_json=%s, used=%s, modified=NOW(6)
                    WHERE name=%s""", (json.dumps(sorted(combined)), len(combined), self.budget_id))
            db.commit()
            return combined != existing
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def used(self):
        from hero_integration.client.settings import _configuration_connection
        db = _configuration_connection()
        try:
            rows = db.sql('SELECT used FROM `tabHERO Execution Budget` WHERE name=%s', (self.budget_id,))
            return rows[0][0] if rows else 0
        finally:
            db.close()


def for_run(run):
    root_name = getattr(run, 'execution_budget_run', None)
    if not root_name:
        if getattr(run, 'testing_enabled', 0):
            from hero_integration.services.file_migration import _parent_run
            if run.action == 'Full Migration' or _parent_run(run.name):
                raise ValueError('A legacy full test has no verifiable global budget; start a new run.')
        return None
    import frappe
    root = run if root_name == run.name else frappe.get_doc('HERO Integration Run', root_name)
    if (not root.testing_enabled or root.execution_budget_run != root.name
            or (root.name != run.name and root.action != 'Full Migration')
            or root.source_instance != run.source_instance or root.source_binding_revision != run.source_binding_revision):
        raise ValueError('Invalid full migration budget binding.')
    return ExecutionBudget('migration', root_name, root.source_instance, min(_MAXIMUM, int(root.testing_max_actions)))


@contextmanager
def run_scope(run):
    budget = for_run(run)
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


def mapped_dependencies(plan, record, parent_doctype):
    """Use the writer's validated relation identities, including composite price keys."""
    if not (getattr(plan, 'adapter_options', None) or {}).get('native_relations'):
        return []
    from hero_integration.services.mapped_relations import prepare, source_identities
    try:
        items = prepare(plan, record, parent_doctype, '')
    except ValueError:
        # The native writer classifies incomplete addresses and unsupported
        # collection rows. Its own pre-write guard also applies on replay.
        return []
    return list(source_identities(items))
