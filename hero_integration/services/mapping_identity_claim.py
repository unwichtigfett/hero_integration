"""Durable immutable identity evidence, independent of Draft or activation authority.

This service is not an endpoint. Callers must hold their context lock and retain
ownership of the encompassing transaction. A claim records evidence only; it
never admits a foreign Draft or a published rule.
"""
from contextvars import ContextVar
from hashlib import sha256
import json
from uuid import UUID, uuid4

from hero_integration.services.mapping_draft_service import DraftConflict, MappingDraftError


_DOCTYPE = 'HERO Mapping Identity Claim'
_insert_scope = ContextVar('hero_mapping_claim_insert', default=None)
_batch_scope = ContextVar('hero_mapping_claim_batch', default=False)
_KEYS = {'site_scope', 'source_instance', 'direction', 'schema_fingerprint', 'operation',
         'identity_kind', 'portable_id', 'logical_id', 'predecessor_id', 'content_hash'}


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MappingDraftError('Claim identities must be canonical UUIDs.') from None
    return value


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def canonical_claim(value, site):
    if not isinstance(value, dict) or set(value) != _KEYS:
        raise MappingDraftError('Mapping identity claim has unknown or missing fields.')
    claim = dict(value)
    if (claim['site_scope'] != site or not isinstance(site, str) or not site
            or not isinstance(claim['source_instance'], str) or not claim['source_instance'].strip()
            or claim['source_instance'] != claim['source_instance'].strip()
            or claim['direction'] not in {'QUERY_TO_FRAPPE', 'FRAPPE_TO_MUTATION'}
            or not isinstance(claim['operation'], str) or not claim['operation'].strip()
            or claim['operation'] != claim['operation'].strip()
            or not _digest(claim['schema_fingerprint']) or not _digest(claim['content_hash'])):
        raise MappingDraftError('Mapping identity claim context is invalid.')
    if claim['identity_kind'] not in {'LOGICAL_ROOT', 'RULE_REVISION'}:
        raise MappingDraftError('Mapping identity claim kind is invalid.')
    identity, root = _uuid(claim['portable_id']), _uuid(claim['logical_id'])
    predecessor = claim['predecessor_id']
    if predecessor is not None:
        _uuid(predecessor)
        if predecessor == identity:
            raise MappingDraftError('A revision cannot precede itself.')
    if claim['identity_kind'] == 'LOGICAL_ROOT' and (identity != root or predecessor is not None):
        raise MappingDraftError('Logical root identity is inconsistent.')
    if claim['identity_kind'] == 'RULE_REVISION' and ((identity == root) != (predecessor is None)):
        raise MappingDraftError('Rule revision ancestry is inconsistent.')
    return claim


def claim_key(claim):
    # Identity is global to the site, not to an individual source or actor:
    # a conflicting context must fail rather than create a second meaning.
    raw = json.dumps([claim['site_scope'], claim['identity_kind'], claim['portable_id']],
                     ensure_ascii=False, separators=(',', ':')).encode()
    return sha256(raw).hexdigest()


def _encoded(claim):
    return json.dumps(claim, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def assert_claim_schema(frappe):
    if not frappe.db.table_exists(_DOCTYPE):
        raise MappingDraftError('Mapping identity claim schema is not installed.')
    rows = frappe.db.sql('show index from `tabHERO Mapping Identity Claim`', as_dict=True)
    keys = {row.get('Key_name') for row in rows if row.get('Column_name') == 'claim_key'
            and int(row.get('Seq_in_index') or 0) == 1 and row.get('Non_unique') in (0, '0')
            and row.get('Sub_part') is None and row.get('Key_name')}
    if not any(sum(row.get('Key_name') == key for row in rows) == 1 for key in keys):
        raise MappingDraftError('Mapping identity claim unique index is not installed.')


def reconciliation_marker(site):
    """Expected immutable marker, created only by a completed reconciliation."""
    key = sha256(json.dumps([site, 'identity-reconciliation-v1'], ensure_ascii=False,
                            separators=(',', ':')).encode()).hexdigest()
    encoded = _encoded({'site_scope':site, 'version':1, 'status':'COMPLETE'})
    return key, encoded, sha256(encoded.encode()).hexdigest()


def assert_reconciliation_ready(frappe):
    # The marker is immutable. A stale read may delay availability but cannot
    # admit a reservation against an incomplete reconciliation.
    key, encoded, digest = reconciliation_marker(frappe.local.site)
    rows = frappe.db.sql('select claim_key, claim_json, claim_hash from `tabHERO Mapping Identity Claim` '
                         'where claim_key=%s', (key,), as_dict=True)
    if (len(rows) != 1 or rows[0].get('claim_key') != key or rows[0].get('claim_json') != encoded
            or rows[0].get('claim_hash') != digest):
        raise MappingDraftError('Mapping identity reconciliation is not complete.')


def claim_insert_admitted(doc):
    import frappe
    scope = _insert_scope.get()
    if not scope or scope[:2] != (frappe.local.site, frappe.session.user) or 'System Manager' not in frappe.get_roles():
        return False
    return (doc.claim_key, doc.claim_json, doc.claim_hash) == scope[2:]


def _reserve_claim(frappe, value):
    """Reserve or verify one claim under the established durable UUID mutex.

    No commit is performed. Caller must use a transaction/savepoint and perform
    authorization, history closure, reconciliation readiness and lock ordering.
    """
    if not _batch_scope.get():
        raise PermissionError('Individual claims require a validated reservation batch.')
    if frappe.session.user in (None, 'Guest') or 'System Manager' not in frappe.get_roles():
        raise PermissionError('Mapping identity claim requires System Manager authority.')
    claim = canonical_claim(value, frappe.local.site)
    assert_claim_schema(frappe)
    assert_reconciliation_ready(frappe)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    _lock_active_target('portable-field-rule', frappe.local.site, claim['portable_id'])
    encoded, key = _encoded(claim), claim_key(claim)
    digest = sha256(encoded.encode()).hexdigest()
    other_kind = 'RULE_REVISION' if claim['identity_kind'] == 'LOGICAL_ROOT' else 'LOGICAL_ROOT'
    companion = {**claim, 'identity_kind': other_kind}
    companion_key, companion_json = claim_key(companion), _encoded(companion)
    existing = frappe.db.sql('select claim_key, claim_json, claim_hash from `tabHERO Mapping Identity Claim` '
                             'where claim_key=%s for update', (companion_key,), as_dict=True)
    if (len(existing) > 1 or (existing and (claim['portable_id'] != claim['logical_id']
            or existing[0].get('claim_key') != companion_key or existing[0].get('claim_json') != companion_json
            or existing[0].get('claim_hash') != sha256(companion_json.encode()).hexdigest()))):
        raise DraftConflict('Logical root and root revision contain conflicting retained evidence.')
    rows = frappe.db.sql('select claim_key, claim_json, claim_hash from `tabHERO Mapping Identity Claim` '
                         'where claim_key=%s for update', (key,), as_dict=True)
    if len(rows) > 1:
        raise DraftConflict('Mapping identity claim is ambiguous.')
    if rows:
        row = rows[0]
        if row.get('claim_key') != key or row.get('claim_json') != encoded or row.get('claim_hash') != digest:
            raise DraftConflict('Mapping identity has conflicting retained evidence.')
        return {'claim_key':key, 'unchanged':True}
    token = _insert_scope.set((frappe.local.site, frappe.session.user, key, encoded, digest))
    try:
        frappe.get_doc({'doctype':_DOCTYPE, 'claim_key':key, 'claim_json':encoded, 'claim_hash':digest}).insert()
    finally:
        _insert_scope.reset(token)
    return {'claim_key':key, 'unchanged':False}


def reserve_claims(frappe, values):
    """Atomically reserve a complete root-to-revision closure in UUID order.

    The caller holds the source/direction context lock and is responsible for
    validating the evidence against its schema before calling this service.
    """
    if not isinstance(values, (list, tuple)) or not values or len(values) > 200:
        raise MappingDraftError('Claim reservation requires a bounded complete ancestry.')
    claims = [canonical_claim(value, frappe.local.site) for value in values]
    indexed = {}
    for claim in claims:
        key = (claim['identity_kind'], claim['portable_id'])
        if key in indexed and indexed[key] != claim:
            raise DraftConflict('A claim batch has conflicting identity evidence.')
        indexed[key] = claim
    for claim in indexed.values():
        if claim['identity_kind'] != 'RULE_REVISION':
            continue
        root = indexed.get(('LOGICAL_ROOT', claim['logical_id']))
        first = indexed.get(('RULE_REVISION', claim['logical_id']))
        if not root or not first or {**root, 'identity_kind':'RULE_REVISION'} != first:
            raise MappingDraftError('Claim ancestry requires matching root evidence.')
        context = tuple(claim[key] for key in ('site_scope', 'source_instance', 'direction', 'schema_fingerprint', 'operation'))
        seen, current = set(), claim
        while current['predecessor_id'] is not None:
            if current['portable_id'] in seen:
                raise MappingDraftError('Claim ancestry contains a cycle.')
            seen.add(current['portable_id'])
            current = indexed.get(('RULE_REVISION', current['predecessor_id']))
            if not current or current['logical_id'] != claim['logical_id'] or tuple(current[key] for key in (
                    'site_scope', 'source_instance', 'direction', 'schema_fingerprint', 'operation')) != context:
                raise MappingDraftError('Claim ancestry has a missing or incompatible predecessor.')
        if current['portable_id'] != claim['logical_id']:
            raise MappingDraftError('Claim ancestry does not reach its root.')
    if any(claim['identity_kind'] == 'LOGICAL_ROOT' and ('RULE_REVISION', claim['portable_id']) not in indexed
           for claim in indexed.values()):
        raise MappingDraftError('Claim ancestry requires its immutable root revision.')
    if frappe.session.user in (None, 'Guest') or 'System Manager' not in frappe.get_roles():
        raise PermissionError('Mapping identity claim requires System Manager authority.')
    assert_claim_schema(frappe)
    assert_reconciliation_ready(frappe)
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    for identity in sorted({claim['portable_id'] for claim in indexed.values()}):
        _lock_active_target('portable-field-rule', frappe.local.site, identity)
    savepoint = 'hero_claims_' + uuid4().hex
    frappe.db.savepoint(savepoint)
    token = _batch_scope.set(True)
    try:
        return [_reserve_claim(frappe, value) for value in sorted(indexed.values(),
                key=lambda item:(item['portable_id'], item['identity_kind']))]
    except Exception as error:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception as rollback_error:
            raise RuntimeError('Claim rollback failed; abort the transaction.') from rollback_error
        raise
    finally:
        _batch_scope.reset(token)
