"""Durable admission of distinct HERO identities before bounded execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from functools import cached_property
import json

# Only persisted v1 runs use the historical all-identities contract.
LEGACY_IDENTITY_LIMIT = 50
CONTRACT_VERSION = 3
# Separate resource ceilings. Exceeding either blocks the run, never truncates a cohort.
MAX_DEPENDENCY_EDGES = 100_000
MAX_CONTRACT_BYTES = 8 * 1024 * 1024
_record = ContextVar('hero_budget_record', default=None)
_current = ContextVar('hero_execution_budget', default=None)
current_budget = _current.get


class BudgetReached(RuntimeError):
    """The next source identity would exceed this run's frozen capacity."""


def _bounded_json(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode()) > MAX_CONTRACT_BYTES:
        raise ValueError('Cohort checkpoint exceeds its storage resource limit.')
    return raw


def _identity(kind, value):
    if (not isinstance(kind, str) or not kind.strip() or len(kind) > 140
            or type(value) not in (int, str) or not str(value).strip() or len(str(value)) > 140):
        raise ValueError('A bounded run requires a stable source identity.')
    return ('file_upload' if kind == 'service_file' else kind, str(value).strip())


def additional_primary_records(evidence):
    return {(kind, identity) for kind, ids in evidence.get('additional_cohorts', {}).items()
            for identity in ids}


def _check_source_revision(db, source, revision):
    # Take the fence only after transport. Source reconfiguration cannot enter
    # between this check and the commit of the test selections.
    values = dict(db.sql("""SELECT field,value FROM `tabSingles`
        WHERE doctype='HERO Integration Settings' AND field IN
        ('hero_source_instance','source_binding_revision','source_binding_confirmed_revision') FOR UPDATE"""))
    if (str(values.get('hero_source_instance') or 'default').strip() != source
            or int(values.get('source_binding_revision') or 0) != revision
            or int(values.get('source_binding_confirmed_revision') or 0) != revision):
        raise ValueError('The confirmed HERO source revision changed during cohort selection.')


def _hash_key(*components):
    return sha256(_bounded_json(components).encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionBudget:
    scope: str
    run_id: str
    source_instance: str
    limit: int | None = None
    version: int = 1
    source_revision: int | None = None

    def __post_init__(self):
        if self.limit is None and self.version == 1:
            object.__setattr__(self, "limit", LEGACY_IDENTITY_LIMIT)
        if (self.scope not in ('migration', 'sync', 'monitor') or self.version not in (1, 2, CONTRACT_VERSION) or not self.run_id or not self.source_instance
                or type(self.limit) is not int or not 1 <= self.limit <= 100):
            raise ValueError('Invalid HERO execution budget contract.')
        if self.version == 3 and (self.scope not in ('migration', 'monitor') or type(self.source_revision) is not int or self.source_revision < 0):
            raise ValueError('Incremental migration requires its accepted source binding revision.')

    @property
    def budget_id(self):
        return sha256(json.dumps([self.scope, self.run_id]).encode()).hexdigest()

    def reserve(self, object_type, external_id):
        return self.reserve_record(object_type, external_id)

    def reserve_record(self, kind, identity, dependencies=(), *, relation="writer"):
        """Admit the writer's source row and its proven inline relationships."""
        if self.version == 1:
            return self.reserve_many([(kind, identity), *dependencies])
        anchor = _identity(kind, identity)
        changed = self._admit(anchor, dependencies, relation)
        _record.set((self.budget_id, anchor))
        return changed

    def allows_record(self, kind, identity):
        return _identity(kind, identity) in self._policy[2]

    @cached_property
    def _policy(self):
        selection = self.selection()
        if selection is None:
            raise ValueError('No frozen cohort for this run.')
        members = {(selection['main_type'], i) for i in selection['ids']} | {
            (k, i) for k, values in selection['evidence'].get('members', {}).items() for i in values}
        members.update(additional_primary_records(selection['evidence']))
        return selection['main_type'], selection['digest'], frozenset(members)

    def _admit(self, anchor, dependencies, relation):
        main_type, _, members = self._policy
        if anchor not in members:
            raise BudgetReached('The source record is outside the frozen cohort.')
        edges = set()
        for item in dependencies:
            child = _identity(*item)
            if child[0] == main_type and child not in members:
                raise BudgetReached('A relationship would introduce an unselected primary record.')
            if child != anchor:
                edges.add((*anchor, *child))
            if len(edges) > MAX_DEPENDENCY_EDGES:
                raise ValueError('Dependency resource limit exceeded.')
        with self._connection() as db:
            _, raw, _ = self._row(db, lock=True)
            saved = json.loads(raw)
            admitted = {tuple(i) for i in saved['admitted']}
            links = {tuple(i) for i in saved['edges']}
            proofs = saved.get('proofs', {})
            proof_key = _bounded_json([*anchor, relation])
            proof = sha256(_bounded_json(sorted(edges)).encode()).hexdigest()
            if proof_key in proofs:
                if proofs[proof_key] != proof:
                    raise ValueError('A frozen dependency set changed. Review the source and start a new run.')
                return False
            proofs[proof_key] = proof
            admitted.add(anchor)
            links.update(edges)
            if len(links) > MAX_DEPENDENCY_EDGES:
                raise ValueError('Dependency resource limit exceeded.')
            payload = _bounded_json({'admitted': sorted(admitted), 'edges': sorted(links), 'proofs': proofs})
            used = sum(kind == main_type for kind, _ in admitted)
            db.sql("""UPDATE `tabHERO Execution Budget` SET identities_json=%s,used=%s,modified=NOW(6)
                      WHERE name=%s""", (payload, used, self.budget_id))
            db.commit()
        return True

    @contextmanager
    def _connection(self):
        from hero_integration.client.settings import _configuration_connection
        db = _configuration_connection()
        try:
            yield db
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _row(self, db, *, lock=False):
        rows = db.sql("""SELECT source_instance,record_limit,contract_version,selection_json,identities_json,used
                       FROM `tabHERO Execution Budget` WHERE name=%s""" + (' FOR UPDATE' if lock else ''),
                      (self.budget_id,))
        if not rows:
            return None
        source, limit, version, selection, state, used = rows[0]
        if (source, limit, version or 1) != (self.source_instance, self.limit, self.version):
            raise ValueError('The durable execution budget belongs to another run contract.')
        return selection, state, used

    def incremental_ids(self, operation, main_type, pages):
        return self.incremental_group(operation, main_type, pages)['ids']

    def _series_stream(self, operation, main_type):
        if self.scope == 'monitor':
            if main_type != operation or operation not in ('HeroCustomers', 'HeroProjectMatches'):
                raise ValueError('Unknown incremental MONITOR stream.')
            return 'monitor:' + operation
        if main_type == 'file_upload' and operation not in ('HeroDocuments', 'HeroPictures'):
            raise ValueError('A file selection requires its approved migration type.')
        return operation if main_type == 'file_upload' else main_type

    def monitor_checkpoint(self, operation):
        """Recover a committed observation after an interruption before cohort freeze."""
        with self._connection() as db:
            if self._row(db) is None:
                return None
            return self._incremental_groups(db).get(self._series_stream(operation, operation))

    def unobserved(self, operation, ids):
        """Read existing receipts without holding the series lock during network I/O."""
        from hero_integration.services.test_environment import require_environment
        series = _hash_key(require_environment(), self._series_stream(operation, operation))
        with self._connection() as db:
            _check_source_revision(db, self.source_instance, self.source_revision)
            return list(self._unclaimed_ids(db, operation, series, ids).values())

    @staticmethod
    def _unclaimed_ids(db, main_type, series, values):
        candidates = dict.fromkeys(_identity(main_type, value)[1] for value in values)
        if not candidates:
            return {}
        names = {_hash_key('selection', series, identity): identity for identity in candidates}
        existing = {name for name, in db.sql("SELECT name FROM `tabHERO Budget Outcome` WHERE name IN %s",
                                             (tuple(names),))}
        return {name: identity for name, identity in names.items() if name not in existing}

    def incremental_group(self, operation, main_type, pages, *, observation=None, catalog=None):
        """Claim the next distinct principals for this migration type, once per root.

        The isolated database serializes simultaneous selectors for this series.
        The claim receipts and ordered group checkpoint commit together, ahead
        of potentially resumable dependency discovery. Dependencies never claim
        a principal slot in another migration type's history.
        """
        from hero_integration.services.test_environment import require_environment
        if self.version != 3:
            raise ValueError('This execution budget has no incremental selection series.')
        environment = require_environment()
        stream = self._series_stream(operation, main_type)
        series = _hash_key(environment, stream)
        with self._connection() as db:
            db.sql("""INSERT INTO `tabHERO Execution Budget`
                (name,creation,modified,owner,modified_by,docstatus,idx,scope,run_id,source_instance,
                 record_limit,contract_version,identities_json,used,incremental_json)
                VALUES (%s,NOW(6),NOW(6),'Administrator','Administrator',0,0,%s,%s,%s,%s,3,%s,0,'{}')
                ON DUPLICATE KEY UPDATE name=VALUES(name)""",
                (self.budget_id,self.scope,self.run_id,self.source_instance,self.limit,'{"admitted":[],"edges":[]}'))
            frozen, _, _ = self._row(db, lock=True)
            groups = self._incremental_groups(db)
            db.sql("""INSERT INTO `tabHERO Test Cohort Series`
                (name,creation,modified,owner,modified_by,docstatus,idx,environment_id,migration_type,source_instance,
                 source_binding_revision) VALUES (%s,NOW(6),NOW(6),'Administrator','Administrator',0,0,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE name=VALUES(name)""",
                (series, environment, stream, self.source_instance, self.source_revision))
            binding = db.sql("""SELECT source_instance,source_binding_revision FROM `tabHERO Test Cohort Series`
                WHERE name=%s FOR UPDATE""", (series,))[0]
            if tuple(binding) != (self.source_instance,self.source_revision):
                raise ValueError('The persisted test series belongs to another source revision. Reset the isolated environment.')
            group = groups.get(stream)
            if group is not None:
                if (group['series'], group['main_type']) != (series, main_type):
                    raise ValueError('This incremental checkpoint belongs to another environment or migration type.')
                _check_source_revision(db, self.source_instance, self.source_revision)
                db.commit()
                return group
            if frozen:
                raise ValueError('Cannot add a selection group to a frozen test run.')
            selected = []
            if catalog is not None:
                if self.scope != 'migration' or operation not in ('HeroProducts','HeroServices'):
                    raise ValueError('Invalid catalog selection scope.')
                fetch, fingerprint = catalog
                raw, = db.sql('SELECT catalog_cursor_json FROM `tabHERO Test Cohort Series` WHERE name=%s', (series,))[0]
                from hero_integration.services import migration_archive
                start = {'offset': 0, 'last': None, 'reader': fingerprint}
                cursors = json.loads(raw) if raw else dict(start)
                # Claims are shared; offsets are not. Enabling archives must revisit excluded identities.
                active_only = migration_archive.active() and not migration_archive.includes_archived()
                cursor = cursors.setdefault('active_only', dict(start)) if active_only else cursors
                excluded_count = 0
                def read_page(position, size):
                    page = fetch(position, size)
                    if isinstance(page, migration_archive.CatalogPage):
                        return page.identities, page.eligible
                    return page, page
                if cursor['reader'] != fingerprint:
                    raise ValueError('The catalog reader differs from the selection series.')
                offset = cursor['offset']
                if offset and read_page(offset-1,1)[0] != [cursor['last']]:
                    raise ValueError('The catalog moved before the saved selection checkpoint.')
                # Previously claimed IDs (including runs predating the cursor) can occupy a prefix.
                # Never turn missing/duplicate catalog records into a full catalog search.
                def catalog_pages():
                    nonlocal excluded_count
                    allowance = max(256, 4*self.limit)
                    consumed = 0
                    while consumed < allowance:
                        size = min(self.limit-len(selected), allowance-consumed)
                        ids, eligible = read_page(cursor['offset'], size)
                        if not ids:
                            return
                        if len(ids)>size or len(set(ids))!=len(ids) or cursor['last'] in ids:
                            raise ValueError('The catalog returned a repeated or oversized page.')
                        consumed += len(ids)
                        cursor.update(offset=cursor['offset']+len(ids), last=ids[-1])
                        excluded_count += len(ids) - len(eligible)
                        yield eligible
                    if migration_archive.active():
                        return  # Resume at the proven position on the next bounded run.
                    raise ValueError('Bounded catalog selection exhausted its read allowance.')
                pages = catalog_pages()
            for page in pages:
                for name, identity in self._unclaimed_ids(db, main_type, series, page).items():
                    db.sql("""INSERT INTO `tabHERO Budget Outcome`
                        (name,creation,modified,owner,modified_by,docstatus,idx,budget,object_type,external_id,outcome,phase)
                        VALUES (%s,NOW(6),NOW(6),'Administrator','Administrator',0,0,%s,%s,%s,'selected','selection')""",
                        (name,self.budget_id,main_type,identity))
                    selected.append(identity)
                    if len(selected) == self.limit:
                        break
                if len(selected) == self.limit:
                    break
            if catalog is not None:
                db.sql('UPDATE `tabHERO Test Cohort Series` SET catalog_cursor_json=%s WHERE name=%s',
                       (json.dumps(cursors),series))
            if require_environment() != environment:
                raise ValueError('The isolated environment changed during incremental selection.')
            _check_source_revision(db, self.source_instance, self.source_revision)
            groups[stream] = {'main_type':main_type,'series':series,'ids':selected,
                              'source_revision':self.source_revision,'environment':environment}
            if catalog is not None and excluded_count:
                groups[stream]['skipped_archived'] = excluded_count
            if observation is not None:
                if self.scope != 'monitor':
                    raise ValueError('Only manual monitoring has observation evidence.')
                groups[stream]['observation'] = observation
            db.sql('UPDATE `tabHERO Execution Budget` SET incremental_json=%s,modified=NOW(6) WHERE name=%s',
                   (_bounded_json(groups),self.budget_id))
            db.commit()
            return groups[stream]

    def _incremental_groups(self, db):
        raw, = db.sql('SELECT incremental_json FROM `tabHERO Execution Budget` WHERE name=%s FOR UPDATE',
                      (self.budget_id,))[0]
        return json.loads(raw or '{}')

    def freeze(self, main_type, ids, *, evidence=None):
        if self.version == 1:
            raise ValueError('A legacy execution budget cannot freeze a new cohort.')
        primary = set()
        for value in ids:
            _, identity = _identity(main_type, value)
            primary.add(identity)
            if len(primary) > self.limit:
                raise BudgetReached('Selection exceeds the frozen primary record limit.')
        if not isinstance(main_type, str) or not main_type.strip():
            raise ValueError('Missing primary object type.')
        evidence = dict(evidence or {})
        extra = evidence.get('additional_cohorts')
        if extra is not None:
            if (self.version != 3 or self.scope != 'migration' or main_type != 'project' or
                    not isinstance(extra, dict) or set(extra) != {'supply_product', 'supply_service'}):
                raise ValueError('Unexpected independent cohorts for this migration.')
            for kind, values in extra.items():
                if not isinstance(values, list):
                    raise ValueError('Expected an ordered list of additional primary identities.')
                canonical = [_identity(kind, identity)[1] for identity in values]
                if len(canonical) != len(set(canonical)) or len(canonical) > self.limit:
                    raise BudgetReached('Additional cohort exceeds its distinct primary record limit.')
                extra[kind] = canonical
        members = {_identity(kind, value) for kind, values in evidence.get('members', {}).items() for value in values}
        edges = {(*_identity(k, i), *_identity(t, j)) for k, i, t, j in evidence.get('edges', ())}
        if len(edges) > MAX_DEPENDENCY_EDGES or len(members) > MAX_DEPENDENCY_EDGES:
            raise ValueError('Dependency resource limit exceeded.')
        if members or edges:
            from collections import defaultdict
            children = defaultdict(set)
            for parent_kind, parent_id, child_kind, child_id in edges:
                children[(parent_kind, parent_id)].add((child_kind, child_id))
            # Only established project edges can justify project dependencies.
            # Product/service roots are authorized by their own frozen selections.
            reachable = {(main_type, i) for i in primary}
            frontier = list(reachable)
            while frontier:
                new = children[frontier.pop()] - reachable
                reachable.update(new)
                frontier.extend(new)
            if extra and any(k in extra for k, _ in members):
                raise ValueError('Independent primary records cannot be admitted as project writer members.')
            if not members <= reachable or any(e[:2] not in reachable for e in edges):
                raise ValueError('Dependency lacks a path to a selected primary record.')
            if any(k == main_type and i not in primary for k,i in reachable):
                raise ValueError('Dependency outside the primary cohort.')
            evidence['members'] = {k: sorted({_identity(k, i)[1] for i in values})
                                   for k, values in evidence.get('members', {}).items()}
            evidence['edges'] = sorted(edges)
        selection = {'main_type': main_type, 'ids':sorted(primary), 'evidence': evidence}
        if self.version == 3:
            from hero_integration.services.test_environment import require_environment
            environment = require_environment()
            selection.update(environment=environment, source_binding_revision=self.source_revision, budget_id=self.budget_id)
        selection['digest'] = sha256(_bounded_json([self.scope, self.source_instance, self.version,
                                                    self.limit, selection]).encode()).hexdigest()
        raw = _bounded_json(selection)
        with self._connection() as db:
            db.sql("""INSERT INTO `tabHERO Execution Budget`
                (name,creation,modified,owner,modified_by,docstatus,idx,scope,run_id,source_instance,
                 record_limit,contract_version,selection_json,identities_json,used)
                VALUES (%s,NOW(6),NOW(6),'Administrator','Administrator',0,0,%s,%s,%s,%s,%s,%s,%s,0)
                ON DUPLICATE KEY UPDATE name=VALUES(name)""",
                (self.budget_id,self.scope,self.run_id,self.source_instance,self.limit,self.version,raw,
                 '{"admitted":[],"edges":[]}'))
            stored, _, _ = self._row(db, lock=True)
            if self.version == 3:
                groups = self._incremental_groups(db)
                requested = {main_type: primary, **{k:set(v) for k,v in evidence.get('additional_cohorts', {}).items()}}
                claimed = {group['main_type']:set(group['ids']) for group in groups.values()}
                if (len(groups) != len(claimed) or requested != claimed or
                        any(group['source_revision'] != self.source_revision or group['environment'] != environment
                            for group in groups.values())):
                    raise ValueError('Frozen cohort differs from the durable incremental claims.')
                _check_source_revision(db, self.source_instance, self.source_revision)
                if not stored:
                    db.sql('UPDATE `tabHERO Execution Budget` SET selection_json=%s,modified=NOW(6) WHERE name=%s',
                           (raw, self.budget_id))
                    stored = raw
            if stored != raw:
                raise ValueError('This run already has a different frozen cohort.')
            db.commit()
        return json.loads(raw)

    def selection(self):
        with self._connection() as db:
            row = self._row(db)
        return json.loads(row[0]) if row and row[0] else None

    def outcome(self, kind, identity, status, *, phase="writer"):
        """Commit the latest result in the same transaction as its business checkpoint."""
        if self.version == 1:
            return
        import frappe
        key = _identity(kind, identity)
        if status not in ('processed','failed','conflict','missing','pending') or key not in self._policy[2]:
            raise ValueError('Invalid outcome for this frozen cohort.')
        if status == 'processed' and _record.get() != (self.budget_id, key):
            with self._connection() as db:
                _, raw, _ = self._row(db)
            if list(key) not in json.loads(raw)['admitted']:
                raise ValueError('A successful outcome requires prior admission.')
        if phase not in ('writer', 'relationships'):
            raise ValueError('Invalid budget outcome phase.')
        name = sha256(_bounded_json([self.budget_id, phase, *key]).encode()).hexdigest()
        frappe.db.sql("""INSERT INTO `tabHERO Budget Outcome`
            (name,creation,modified,owner,modified_by,docstatus,idx,budget,object_type,external_id,outcome,phase)
            VALUES (%s,NOW(6),NOW(6),'Administrator','Administrator',0,0,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE modified=NOW(6),outcome=VALUES(outcome)""",
            (name,self.budget_id,*key,status,phase))

    def report(self):
        from collections import Counter
        import frappe
        with self._connection() as db:
            row = self._row(db)
        if not row or not row[0]:
            return {'selected':0, 'processed':0, 'failed':0, 'conflicts':0, 'missing':0,
                    'dependencies':{}, 'limit':self.limit, 'main_type':None, 'cohort_digest':None}
        raw, state, _ = row
        selection = json.loads(raw)
        saved = json.loads(state)
        main_type = selection['main_type']
        dependencies = {tuple(edge[2:]) for edge in (*saved['edges'], *selection['evidence'].get('edges', ()))
                        if edge[2] != main_type}
        records = frappe.db.sql("""SELECT object_type,external_id,outcome,phase FROM `tabHERO Budget Outcome`
                                 WHERE budget=%s""",(self.budget_id,))
        current = {(k,i):s for k,i,s,phase in records if phase == 'writer'}
        for k,i,s,phase in records:
            if phase == 'relationships' and s != 'processed' and current.get((k,i), 'processed') == 'processed':
                current[k,i] = s
        outcomes = [(k,i,s) for (k,i),s in current.items()]
        counts = Counter(status for kind, _, status in outcomes if kind == main_type)
        evidence = selection['evidence']
        report = {'main_type':main_type, 'selected': len(selection['ids']), 'processed':counts['processed'],
                'pending_records':counts['pending'], 'failed':counts['failed'], 'conflicts':sum(status=='conflict' for _,_,status in outcomes),
                'missing':sum(status=='missing' for _,_,status in outcomes), 'limit':self.limit,
                'missing_relationships':evidence.get('missing_relationships', []),
                'blocked_areas':evidence.get('blocked_areas', {}),
                'cohort_digest':selection['digest'], 'dependencies':dict(sorted(Counter(k for k,_ in dependencies).items())),
                'issues': [{'type':k,'id':i,'outcome':s} for k,i,s in sorted(outcomes) if s != 'processed']}
        if 'additional_cohorts' in evidence:
            report['project_ids'] = selection['ids']
            report['additional_cohorts'] = {}
            for kind, ids in evidence['additional_cohorts'].items():
                by_status = Counter(current.get((kind, i), 'pending') for i in ids)
                report['additional_cohorts'][kind] = {
                    'ids': ids, 'selected': len(ids), 'limit': self.limit,
                    **{state: by_status[state] for state in ('processed', 'failed', 'conflict', 'missing', 'pending')}}
            # Trace just the project graph. A service component can reference an
            # existing Item without admitting a new product for import.
            from collections import defaultdict
            children = defaultdict(set)
            for kind, identity, child, other in (*saved['edges'], *evidence.get('edges', ())):
                children[(kind, identity)].add((child, other))
            reachable = {(main_type, i) for i in selection['ids']}
            queue = list(reachable)
            while queue:
                following = children[queue.pop()] - reachable
                queue.extend(following)
                reachable.update(following)
            report['project_dependencies'] = {
                kind: sorted(i for k, i in reachable if kind == k)
                for kind in sorted({k for k, _ in reachable} - {main_type})}
        return report

    def reserve_many(self, identities, *, relation="related"):
        """Admit an identity plus its identified dependencies as one unit.

        Commit on the site's primary using a separate connection. A business
        rollback retains the reservation, and reservation never commits caller writes.
        """
        if self.version >= 2:
            anchor = _record.get()
            if not anchor or anchor[0] != self.budget_id:
                raise ValueError('Dependency admission requires its originating cohort record.')
            return self._admit(anchor[1], identities, relation)
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
            source, limit, raw, used, version = db.sql("""SELECT source_instance, record_limit, identities_json, used, contract_version
                FROM `tabHERO Execution Budget` WHERE name=%s FOR UPDATE""", (self.budget_id,))[0]
            if source != self.source_instance or limit != self.limit or (version or 1) != self.version:
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
    version = int(getattr(root, 'budget_contract_version', 0) or 1)
    if version != int(getattr(run, 'budget_contract_version', 0) or 1):
        raise ValueError('Child and root budget contract versions differ.')
    if version >= 2 and root.name != run.name:
        from hero_integration.services.file_migration import _parent_run
        if _parent_run(run.name) != root.name:
            raise ValueError('The child has no recorded link to this budget root.')
    limit = int(root.testing_max_actions)
    if version == 1:
        limit = min(LEGACY_IDENTITY_LIMIT, limit)
    elif int(run.testing_max_actions) != limit:
        raise ValueError('Child and root frozen limits differ.')
    return ExecutionBudget('migration', root_name, root.source_instance, limit, version,
                           int(root.source_binding_revision) if version == 3 else None)


@contextmanager
def run_scope(run):
    budget = for_run(run)
    token = _current.set(budget)
    record_token = _record.set(None)
    try:
        yield budget
    finally:
        _record.reset(record_token)
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
