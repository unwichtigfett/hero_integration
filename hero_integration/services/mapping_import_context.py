"""Keep approved snapshot bindings when a newer schema preserves used fields."""
import json

import frappe

from hero_integration.services.mapping_snapshot_rebase import (
    SnapshotRebaseError, _snapshot, _documents, MAPPING, SERVICE, CONTRACT,
)
from hero_integration.services.mapping_schema_compatibility import check_compatibility
from hero_integration.services.personal_mapping_content import unique_json_object


def resolve(snapshot_name, source_instance, payload=None):
    """Return the actual approved import context after proving compatibility.

    A different global schema hash does not itself authorize new approvals or
    rewrite immutable rule provenance. Compatible existing mappings retain their
    approved snapshot; the selected newer snapshot is a validation target only.
    """
    frappe.only_for('System Manager')
    value = None
    if payload is not None:
        if not isinstance(payload, str) or len(payload.encode()) > 5_000_000:
            raise SnapshotRebaseError('The mapping file exceeds the permitted size.')
        try:
            value = json.loads(payload, object_pairs_hook=unique_json_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError) as error:
            raise SnapshotRebaseError('The mapping file is not valid JSON.') from error
        if not isinstance(value, dict):
            raise SnapshotRebaseError('The mapping file is not valid JSON.')
    target, target_schema, target_fp = _snapshot(snapshot_name, source_instance, lock=True, require_compatible=False)
    preliminary = frappe.get_all(MAPPING, filters={'source_instance': source_instance, 'status': 'Active'},
                                 fields=['name', 'schema_snapshot'], limit_page_length=0)
    names = {row.schema_snapshot for row in preliminary}
    result = {'snapshot_name': snapshot_name, 'payload': payload, 'validated_against_snapshot': snapshot_name}
    if not names or names == {snapshot_name}:
        return result
    snapshots = {name: _snapshot(name, source_instance, lock=True) for name in names}
    if all(item[2] == target_fp for item in snapshots.values()):
        return result  # The ordinary reference-only snapshot transition handles this.
    if len(names) != 1:
        raise SnapshotRebaseError('Multiple approved schema contexts require separate mapping imports.')
    canonical = next(iter(names)); old, old_schema, old_fp = snapshots[canonical]
    if not old.get('endpoint_fingerprint') or old.endpoint_fingerprint != target.get('endpoint_fingerprint'):
        raise SnapshotRebaseError('Snapshot rebinding cannot cross different HERO endpoints.')
    from hero_integration.services.mapping_draft_exchange import _lock_context
    for name in sorted(names | {snapshot_name}):
        for direction in ('QUERY_TO_FRAPPE', 'FRAPPE_TO_MUTATION'):
            _lock_context(source_instance, name, direction)
    mappings = _documents(MAPPING, {'source_instance': source_instance, 'status': 'Active'})
    if {(row.name, row.schema_snapshot) for row in mappings} != {(row.name, row.schema_snapshot) for row in preliminary}:
        raise SnapshotRebaseError('Mapping snapshot bindings changed; preview the import again.')
    mapping_names = {row.name for row in mappings if row.direction == 'QUERY_TO_FRAPPE'}
    readers = [row for row in _documents(SERVICE, {'schema_snapshot': canonical,
        'execution_status': 'Approved', 'status': 'Validated'}) if row.get('read_mapping') in mapping_names]
    profiles = [json.loads(row.mapping_json, object_pairs_hook=unique_json_object)
                for row in mappings if row.direction == 'FRAPPE_TO_MUTATION']
    contracts = _documents(CONTRACT, {'schema_snapshot': canonical, 'status': 'Active'})
    mutation_queries = []
    for contract in contracts:
        service = frappe.get_doc(SERVICE, contract.graphql_service, for_update=True)
        if service.status != 'Validated' or service.schema_snapshot != canonical:
            raise SnapshotRebaseError('Snapshot rebinding found an invalid mutation service or contract.')
        mutation_queries.append(service.query_document)
    queries = [row.query_document for row in readers]
    if value is not None:
        fmt = value.get('format_version')
        if fmt == 'hero-personal-mapping-portable-v2':
            groups = value.get('mappings')
            if value.get('direction') != 'FRAPPE_TO_MUTATION' or not isinstance(groups, list):
                raise SnapshotRebaseError('The mapping file is not valid JSON.')
            profiles += groups
        elif fmt == 'hero-integration-mappings-v1':
            services = value.get('read_services')
            if not isinstance(services, list) or any(not isinstance(row, dict) or not isinstance(row.get('query'), str) for row in services):
                raise SnapshotRebaseError('The mapping file is not valid JSON.')
            queries += [row['query'] for row in services]
        else:
            raise SnapshotRebaseError('Import mapping files here. Configuration packages belong in Settings.')
        if value.get('schema_fingerprint') not in {old_fp, target_fp}:
            raise SnapshotRebaseError('Select a validated snapshot matching the mapping file schema.')
    proof = check_compatibility({'__schema': old_schema}, {'__schema': target_schema},
        read_queries=queries, mutation_profiles=profiles, mutation_queries=mutation_queries)
    if value is not None:
        value['schema_fingerprint'] = old_fp
        if value.get('format_version') == 'hero-integration-mappings-v1':
            if not isinstance(value.get('mapping_package'), dict):
                raise SnapshotRebaseError('The mapping file is not valid JSON.')
            if value['mapping_package'].get('schema_fingerprint') not in {old_fp, target_fp}:
                raise SnapshotRebaseError('Select a validated snapshot matching the mapping file schema.')
            value['mapping_package']['schema_fingerprint'] = old_fp
    return {**result, 'snapshot_name': canonical,
            'payload': json.dumps(value, ensure_ascii=False, allow_nan=False) if value is not None else None,
            'dependency_fingerprint': proof['dependency_fingerprint']}
