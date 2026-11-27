"""Freeze the bounded standalone sample before a native writer receives any rows."""
from dataclasses import replace
from hashlib import sha256

from hero_integration.services import migration_archive
from hero_integration.services import direct_runtime
from hero_integration.services.direct_mapping import MISSING, lookup
from hero_integration.services.migration_testing import RecordLimitedSource
from hero_integration.services.sync_page import BY_OPERATION
from hero_integration.sources.hero import SourcePage

DIRECT_KINDS = {
    'HeroEmployees': 'partner', 'HeroProducts': 'supply_product', 'HeroServices': 'supply_service',
    'HeroDocuments': 'file_upload', 'HeroPictures': 'file_upload',
}
MAX_SCAN_PAGES = 1000


class SelectionBlocked(ValueError):
    """The bound reader cannot prove the membership or completeness of the sample."""


def _scan(reader, identity, limit, *, wanted=None, eligible=lambda row: True):
    """Read bounded pages into an ordered map; replay requests the original identities."""
    selected = {}
    pages = received = offset = 0
    if wanted == set():
        return selected, pages, received
    previous = None
    for _ in range(MAX_SCAN_PAGES):
        page = reader(offset)
        pages += 1
        received += (page.offset_checkpoint or {}).get('count', len(page.records))
        keys = []
        for row in page.records:
            if not eligible(row):
                continue
            key = identity(row)
            if type(key) not in (int, str) or not str(key).strip():
                raise SelectionBlocked('Source identity is missing in the standalone sample.')
            key = str(key)
            keys.append(key)
            if wanted is not None and key not in wanted:
                continue
            if key in selected:
                if selected[key] != row:
                    raise SelectionBlocked(f'Conflicting rows for selected source identity {key}.')
                continue
            if len(selected) < limit:
                selected[key] = row
        if len(selected) == limit or page.next_offset is None:
            return selected, pages, received
        if type(page.next_offset) is not int or page.next_offset <= offset or (keys and keys == previous):
            raise SelectionBlocked('Standalone selection pagination repeated a page or failed to advance.')
        previous = keys
        offset = page.next_offset
    raise SelectionBlocked('Standalone selection exceeded its bounded page scan.')


def _order_and_verify(budget, main_type, records, existing):
    if not existing:
        return list(records)
    if existing['main_type'] != main_type:
        raise SelectionBlocked('Frozen source type differs from the standalone reader.')
    order = existing['evidence']['order']
    missing = set(order) - set(records)
    if missing:
        _report_missing(budget, main_type, missing)
    return order


def _report_missing(budget, kind, missing, *, reason='frozen identities missing from the approved read'):
    import frappe
    for identity in sorted(missing):
        budget.outcome(kind, identity, 'missing')
    # Preflight runs before the first write. Retain the missing identities even
    # when the worker rolls back the failed attempt and blocks the run.
    frappe.db.commit()
    raise SelectionBlocked(f'{kind}: {reason}: ' + ', '.join(sorted(missing)))


def _evidence(checkpoint, order):
    return {'contract': sha256(checkpoint.encode()).hexdigest(), 'order': order}


def _direct_page(client, read, verify):
    operation = read.operation
    def page(offset):
        if verify:
            verify()
        rows = direct_runtime.fetch_page(client, read, offset)
        if operation == 'HeroEmployees':
            rows = sorted(rows, key=lambda r: r.get('id') or 0)
        if migration_archive.active() and not migration_archive.includes_archived() and operation in ("HeroDocuments", "HeroPictures"):
            from hero_integration.services.file_transfer import details
            members = [row for row in rows if direct_runtime.belongs(operation, row)]
            flags = details(client, members, read, read.snapshot.source_instance, paths=("id", "uuid", "is_deleted"))
            rows = [{**row, "is_deleted": flags[row["uuid"]]["is_deleted"]} if row["uuid"] in flags else row for row in rows]
        if verify:
            verify()
        following = offset + len(rows) if rows and operation != 'HeroEmployees' else None
        return SourcePage(tuple(rows), following)
    return page


def _file_member(operation, row):
    if operation in ('HeroDocuments', 'HeroPictures') and 'category' not in row:
        raise SelectionBlocked('The file reader omitted the category proving its migration type.')
    return direct_runtime.belongs(operation, row) and not migration_archive.excluded(operation, row)


def _identity_pages(reader, identity, eligible):
    offset = 0
    previous = None
    for _ in range(MAX_SCAN_PAGES):
        page = reader(offset)
        keys = []
        seen = {}
        for row in page.records:
            if not eligible(row):
                continue
            key = identity(row)
            if key in seen and row != seen[key]:
                raise SelectionBlocked(f'Conflicting source rows for identity {key}.')
            if key not in seen:
                seen[key] = row
                keys.append(key)
        yield keys
        if page.next_offset is None:
            return
        if type(page.next_offset) is not int or page.next_offset <= offset or (keys and keys == previous):
            raise SelectionBlocked('Incremental selection pagination repeated a page or failed to advance.')
        previous = keys
        offset = page.next_offset
    raise SelectionBlocked('Incremental selection exceeded its bounded page scan.')


def incremental_direct_ids(client, read, budget, *, verify=None, eligible=None, scoped=True):
    operation = read.operation
    kind = DIRECT_KINDS[operation] if operation in DIRECT_KINDS else BY_OPERATION[operation].source_type
    if scoped and operation in ('HeroProducts','HeroServices'):
        from hero_integration.services.scoped_test_reads import catalog_pages
        return budget.incremental_group(operation,kind,(),catalog=catalog_pages(client,read,verify))['ids']
    def belongs(row):
        return _file_member(operation, row) and (eligible is None or eligible(row))
    pages = _identity_pages(_direct_page(client, read, verify), read.plan.source_identity, belongs)
    return budget.incremental_ids(operation, kind, pages)


def selected_direct_rows(client, read, ids, *, verify=None, scoped=True):
    if scoped and read.operation in ('HeroProducts','HeroServices','HeroProjectMatches'):
        from hero_integration.services.scoped_test_reads import exact_rows
        return replace(read,testing_rows=tuple(exact_rows(client,read,ids,verify=verify)))
    wanted = set(ids)
    records, _, _ = _scan(_direct_page(client, read, verify), read.plan.source_identity, len(wanted),
                          wanted=wanted, eligible=lambda row: _file_member(read.operation, row))
    if missing := wanted - set(records):
        raise SelectionBlocked('Frozen source identities missing: ' + ', '.join(sorted(missing)))
    return replace(read, testing_rows=tuple(records[key] for key in ids))


def prepare_direct(client, read, budget, checkpoint, *, verify=None):
    """Materialize selected rows in memory for the existing per-record writers."""
    operation = read.operation
    kind = DIRECT_KINDS[operation]
    existing = budget.selection()
    wanted = set(existing['ids']) if existing else None
    page = _direct_page(client, read, verify)
    def eligible(row):
        return _file_member(operation, row)
    if budget.version == 3:
        order = incremental_direct_ids(client, read, budget, verify=verify)
        wanted = set(order)
    if budget.version == 3 and wanted is not None and operation in ('HeroProducts','HeroServices'):
        rows = {str(read.plan.source_identity(row)):row for row in
                selected_direct_rows(client,read,sorted(wanted),verify=verify).testing_rows}
    else:
        rows, _, _ = _scan(page, read.plan.source_identity, len(wanted) if wanted is not None else budget.limit,
                            wanted=wanted, eligible=eligible)
    if budget.version == 3 and not existing:
        if missing := wanted - set(rows):
            raise SelectionBlocked('Preselected identities disappeared: ' + ', '.join(sorted(missing)))
    else:
        order = _order_and_verify(budget, kind, rows, existing)
    evidence = _evidence(checkpoint, order)
    if operation == 'HeroEmployees':
        edges = []
        for identity in order:
            row = rows[identity]
            nested = row.get('user') or {}
            user = row.get('user_id') or nested.get('id')
            if user:
                edges.append([kind, identity, 'User', str(user)])
        evidence['edges'] = edges
    budget.freeze(kind, order, evidence=evidence)
    return replace(read, testing_rows=tuple(rows[key] for key in order))


class FrozenNativeSource(RecordLimitedSource):
    """Replay only source rows checked against the frozen selection for this attempt."""
    def __init__(self, source, maximum, pages_by_operation, pages_read, rows_read):
        super().__init__(source, maximum)
        self.pages_by_operation = pages_by_operation
        self.executed_actions = pages_read
        self.records_received = rows_read

    def _page(self, operation, reader, offset):
        rows = tuple(self.pages_by_operation[operation][offset:])
        self.results.setdefault(operation, {}).update(processed=len(rows), status='COMPLETED')
        return SourcePage(rows, None)


def prepare_native(source, budget, checkpoint, plan, customer_plan=None):
    operation = source.contract.target_operation
    area = BY_OPERATION.get(operation)
    if area is None:
        raise SelectionBlocked('Standalone selection has no verified source identity for this operation.')
    reader = (source.projects if operation == 'HeroProjectMatches' else
              source.customers if operation == 'HeroCustomers' else source.projection)
    existing = budget.selection()
    wanted = set(existing['ids']) if existing else None
    paged_logbook = operation == 'HeroProjectMatches' and bool(
        (getattr(source, 'query_adapter', None) or {}).get('logbook', {}).get('offset_variable'))
    if paged_logbook:
        # The parent reader also owns the optional child cursor. Selection
        # reads just its first page and cannot execute auxiliary child reads.
        source.logbook_project_ids = frozenset()
    skipped_archived = 0
    def eligible(row):
        nonlocal skipped_archived
        if migration_archive.excluded(operation, row):
            skipped_archived += 1
            return False
        return True
    if budget.version == 3:
        ids = budget.incremental_ids(operation, area.source_type, _identity_pages(reader, plan.source_identity, eligible))
        wanted = set(ids)
    selection = {} if wanted is not None else {"eligible": eligible}
    records, pages, received = _scan(reader, plan.source_identity,
                                     len(wanted) if wanted is not None else budget.limit, wanted=wanted, **selection)
    if budget.version == 3 and not existing:
        if missing := wanted - set(records):
            raise SelectionBlocked('Preselected identities disappeared: ' + ', '.join(sorted(missing)))
        order = ids
    else:
        order = _order_and_verify(budget, area.source_type, records, existing)
    evidence = _evidence(checkpoint, order)
    by_operation = {operation: [records[key] for key in order]}
    if operation == 'HeroProjectMatches':
        paths = [r.source_path for r in plan.definitions
                 if r.transformation == 'HERO Customer Link' or r.purpose == 'Customer Relation']
        if records and not paths:
            raise SelectionBlocked('The Project reader does not identify its required Customer relation.')
        edges = set()
        unresolved = set()
        for key in order:
            if migration_archive.excluded(operation, records[key]):
                continue
            for path in paths:
                customer = lookup(records[key], path)
                if customer is MISSING or type(customer) is not int or customer <= 0:
                    unresolved.add(key)
                else:
                    edges.add(('project', key, 'customer', str(customer)))
        evidence['edges'] = [list(edge) for edge in sorted(edges)]
        evidence['members'] = {'customer': sorted({edge[3] for edge in edges})}
    budget.freeze(area.source_type, order, evidence=evidence)
    if operation == 'HeroProjectMatches':
        if unresolved:
            _report_missing(budget, 'project', unresolved, reason='required customer relationship missing')
        if paged_logbook:
            source.logbook_project_ids = frozenset(order)
            hydrated, extra_pages, extra_rows = _scan(reader, plan.source_identity, len(order),
                                                     wanted=source.logbook_project_ids)
            if missing := set(order) - set(hydrated):
                _report_missing(budget, 'project', missing)
            for key in order:
                if (not hydrated[key].get('_hero_logbook_complete') or
                        any(lookup(hydrated[key], path) != lookup(records[key], path) for path in paths)):
                    raise SelectionBlocked(f'Project {key} changed or its approved logbook was incomplete.')
            by_operation[operation] = [hydrated[key] for key in order]
            pages += extra_pages
            received += extra_rows
        customers = {edge[3] for edge in evidence['edges']}
        found, customer_pages, customer_rows = _scan(source.customers, customer_plan.source_identity,
                                                    len(customers), wanted=customers)
        missing = customers - set(found)
        if missing:
            _report_missing(budget, 'customer', missing)
        by_operation['HeroCustomers'] = list(found.values())
        pages += customer_pages
        received += customer_rows
    result = FrozenNativeSource(source, budget.limit, by_operation, pages, received)
    if skipped_archived:
        result.results[operation] = {"skipped_archived": skipped_archived}
    return result
