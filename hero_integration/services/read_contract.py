"""Saved, explicit read contracts for the existing HERO reader and mapper."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256

from hero_integration.client.graphql import assert_read_only
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS, approved_query


class ReadContractError(ValueError):
    pass


@dataclass(frozen=True)
class ReadContract:
    service_name: str
    service_revision: int
    mapping_name: str
    mapping_revision: int
    target_operation: str
    records_path: str
    identity_path: str
    definition_ids: tuple[str, ...]
    variables: dict[str, object]
    page_size: int
    materialized_request: object | None = None


def task_materialization(snapshot, config, plan):
    """Rebuild a native Task request and its mapping-bound approval evidence."""
    from dataclasses import asdict
    from hero_integration.services.mapping_contract_v1 import QueryAdapter, SnapshotAdapter, StoredSnapshot
    rules = {}
    for spec in plan.definitions:
        rule = asdict(spec)
        rule.pop('operation')
        rule.pop('definition_id')
        rules[spec.definition_id] = rule
    content = dict(config)
    content['field_rules'] = rules
    validated = SnapshotAdapter().adapt(StoredSnapshot(snapshot.name, snapshot.snapshot_json,
        snapshot.schema_fingerprint, snapshot.source_instance, snapshot.analysis_status))
    request = QueryAdapter().materialize_personal_task(validated, content)
    seal = {'version': 1, 'mapping_version': plan.mapping_version,
            'schema_fingerprint': snapshot.schema_fingerprint, 'source_instance': snapshot.source_instance,
            'request_fingerprint': request.request_fingerprint,
            'document_sha256': sha256(request.document.encode('utf-8')).hexdigest()}
    return request, seal


def from_documents(service, mapping, snapshot) -> ReadContract:
    """Validate the immutable legacy B7 saved-query contract.

    This remains the only contract accepted by the legacy migration executor.
    Universal B8 materialization must opt into ``universal_from_documents``
    and present its own canonical-operation seal.
    """
    contract, config = _common_contract(service, mapping, snapshot)
    target = contract.target_operation
    records_path, identity_path = contract.records_path, contract.identity_path
    variables, definition_ids = contract.variables, contract.definition_ids
    onboarding = config.get('native_task_onboarding')
    if onboarding is not None:
        if (target != 'HeroTasks' or not isinstance(onboarding, dict)
                or set(onboarding) != {'source_instance', 'source_type', 'confirmed'}
                or onboarding.get('confirmed') is not True
                or onboarding.get('source_type') != 'Task'
                or not isinstance(onboarding.get('source_instance'), str)
                or not onboarding['source_instance'].strip()
                or onboarding['source_instance'] != getattr(snapshot, 'source_instance', '')
                or onboarding['source_instance'] != getattr(mapping, 'source_instance', '')
                or getattr(snapshot, 'analysis_status', '') != 'Validated'
                or getattr(service, 'operation_name', '') not in {'HeroTasks', 'tasks'}
                or getattr(mapping, 'operation_name', '') != 'tasks'
                or getattr(mapping, 'operation_kind', '') != 'QUERY'
                or getattr(mapping, 'direction', '') != 'QUERY_TO_FRAPPE'):
            raise ReadContractError('Native Task onboarding requires an explicit matching source and Task type.')
    if target in PROJECTIONS:
        projection = PROJECTIONS[target]
        from hero_integration.services.native_projections import validate_options
        validate_options(projection, config.get('native_options'))
        if snapshot.schema_fingerprint != projection['fingerprint']:
            raise ReadContractError('Projection requires its reviewed schema fingerprint.')
        if service.query_document.strip() != approved_query(target).strip():
            if target != 'HeroTasks' or onboarding is None or not isinstance(config.get('generated_task_seal'), dict):
                raise ReadContractError('Projection requires the exact reviewed read-only query.')
            from hero_integration.services.mapping_runtime import active_mapping_plan
            try:
                plan = active_mapping_plan(service.operation_name, definition_ids=definition_ids,
                    target_operation=target, mapping_name=mapping.name, native_task=True, lock_rules=True)
                request, seal = task_materialization(snapshot, config, plan)
            except (ValueError, TypeError, KeyError) as error:
                raise ReadContractError('Generated Task mapping cannot be reconstructed.') from error
            if config['generated_task_seal'] != seal or service.query_document != request.document:
                raise ReadContractError('Generated Task plan or approved document changed.')
            contract = replace(contract, materialized_request=request)
        if records_path != projection['root'] or identity_path != 'id' or dict(variables) != {'first': '$page_size', 'offset': '$offset'}:
            raise ReadContractError('Projection pagination, root or identity path is invalid.')
    elif target in {"HeroCustomers", "HeroProjectMatches"}:
        # The two native adapters also have closed read contracts.  Do not let
        # a saved service silently omit a field required by its active plan.
        from hero_integration.sources.hero import (
            CUSTOMER_CONTRACT_V1,
            CUSTOMER_CONTRACT_V2,
            CUSTOMER_CONTRACT_V3,
            PROJECT_QUERY_V1,
            customer_query,
        )

        customer_contract_version = str(config.get("customer_contract_version", CUSTOMER_CONTRACT_V1))
        if target == "HeroCustomers":
            try:
                expected_query = customer_query(customer_contract_version)
            except ValueError as error:
                raise ReadContractError("Customer read-contract version is not supported.") from error
            line2_definition = "customer-primary-address-line2"
            legal_form_definition = "customer-company-legal-form"
            selected_literals = _customer_selected_literals(definition_ids, mapping)
            if customer_contract_version == CUSTOMER_CONTRACT_V3:
                if line2_definition not in selected_literals or legal_form_definition not in selected_literals:
                    raise ReadContractError("Customer V3 requires the selected primary-address line_2 and company legal-form bindings.")
            elif customer_contract_version == CUSTOMER_CONTRACT_V2:
                if line2_definition not in selected_literals:
                    raise ReadContractError("Customer V2 requires the selected primary-address line_2 binding.")
                if legal_form_definition in selected_literals:
                    raise ReadContractError("Customer V2 cannot select the company legal-form binding.")
            elif line2_definition in selected_literals or legal_form_definition in selected_literals:
                raise ReadContractError("Customer V1 cannot select V2 or V3 bindings.")
        else:
            expected_query = PROJECT_QUERY_V1
        expected_variables = (
            {"first": "$page_size", "offset": "$offset", "orderBy": "id", "showDeleted": True}
            if target == "HeroCustomers"
            else {"first": "$page_size", "offset": "$offset", "orderBy": "id"}
        )
        expected_root = "contacts" if target == "HeroCustomers" else "project_matches"
        if (
            service.query_document.strip() != expected_query.strip()
            or records_path != expected_root
            or identity_path != "id"
            or dict(variables) != expected_variables
        ):
            raise ReadContractError("Native adapter requires its exact reviewed read-only query and variables.")
    return contract


def _customer_selected_literals(definition_ids, mapping):
    from hero_integration.services.mapping_rule_lineage import customer_legacy_literals
    import frappe
    if not frappe.db.has_column('HERO Frappe Mapping', 'portable_lineage_json'):
        # A restored or rolled-back site can contain scoped UUID revisions
        # while missing their provenance column. Do not reinterpret those as
        # ordinary Customer rules, particularly for the V1 exclusion gate.
        from hero_integration.services.personal_mapping_content import RULE_FIELDS
        fields = ['definition_id', 'personal_graphql_mapping', 'status', 'operation', 'schema_snapshot',
                  'schema_fingerprint', *sorted(RULE_FIELDS)]
        def old_rule(identity):
            return frappe.get_all('HERO Frappe Mapping', filters={'definition_id':identity}, fields=fields, limit=2)
        try:
            return customer_legacy_literals(definition_ids, mapping, old_rule, lambda _value: [])
        except (ValueError, TypeError, KeyError) as error:
            raise ReadContractError('Customer rule lineage schema is missing or its revision is ambiguous; reconcile before approval.') from error
    from hero_integration.services.personal_mapping_content import RULE_FIELDS
    fields = ['definition_id', 'portable_definition_id', 'portable_lineage_json', 'operation', 'status',
              'schema_snapshot', 'schema_fingerprint', 'personal_graphql_mapping', 'schema_manifest',
              'schema_dependencies', *sorted(RULE_FIELDS)]
    def find(key, value):
        return frappe.get_all('HERO Frappe Mapping', filters={key:value}, fields=fields, limit=2)
    try:
        return customer_legacy_literals(definition_ids, mapping,
            lambda value:find('definition_id', value), lambda value:find('portable_definition_id', value))
    except (ValueError, TypeError, KeyError) as error:
        raise ReadContractError('Customer rule lineage cannot be verified for the selected mapping.') from error


def universal_from_documents(service, mapping, snapshot) -> ReadContract:
    """Validate shared B7 identities without adopting the legacy query bytes.

    Only B8 calls this helper, after the separately approved canonical
    operation seal has been recomputed.  The caller owns that additional
    universal-contract validation; this helper deliberately cannot make a
    legacy executor accept a materialized query.
    """
    contract, _config = _common_contract(service, mapping, snapshot)
    return contract


def _common_contract(service, mapping, snapshot) -> tuple[ReadContract, Mapping[str, object]]:
    if service.status != "Validated":
        raise ReadContractError("The selected GraphQL service is not validated.")
    if getattr(service, "execution_status", "Pending Approval") != "Approved":
        raise ReadContractError("The selected GraphQL service has no explicit execution approval.")
    if int(getattr(service, "approved_service_revision", 0) or 0) != int(service.service_revision or 0):
        raise ReadContractError("The selected GraphQL service approval is stale.")
    if service.schema_snapshot != snapshot.name or service.schema_fingerprint != snapshot.schema_fingerprint:
        raise ReadContractError("The selected GraphQL service no longer matches its schema snapshot.")
    if mapping.name != getattr(service, "read_mapping", "") or mapping.status != "Active":
        raise ReadContractError("The selected read mapping is not active.")
    if getattr(mapping, 'direction', None) != 'QUERY_TO_FRAPPE' or getattr(mapping, 'operation_kind', None) != 'QUERY':
        raise ReadContractError('The selected read mapping is not an active Query definition.')
    if mapping.schema_snapshot != snapshot.name or mapping.schema_fingerprint != snapshot.schema_fingerprint:
        raise ReadContractError("The selected read mapping no longer matches its schema snapshot.")
    source = getattr(snapshot, 'source_instance', None)
    if (not isinstance(source, str) or not source.strip() or source != source.strip()
            or getattr(mapping, 'source_instance', None) != source):
        raise ReadContractError('The selected read mapping no longer matches the snapshot source.')
    if int(getattr(service, "approved_mapping_revision", 0) or 0) != int(mapping.mapping_revision or 0):
        raise ReadContractError("The selected read mapping approval is stale.")
    assert_read_only(service.query_document)
    try:
        from hero_integration.services.personal_mapping_content import unique_json_object
        config = json.loads(mapping.mapping_json, object_pairs_hook=unique_json_object)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReadContractError("The selected read mapping is not valid JSON.") from error
    if not isinstance(config, Mapping):
        raise ReadContractError("The selected read mapping must be an object.")
    target = config.get("target_operation")
    records_path, identity_path = config.get("records_path"), config.get("identity_path")
    definition_ids, variables, page_size = config.get("mapping_definition_ids"), config.get("variables", {}), config.get("page_size", 100)
    if target not in {"HeroCustomers", "HeroProjectMatches", *PROJECTIONS}:
        raise ReadContractError("The selected target adapter is not supported.")
    if not isinstance(records_path, str) or not records_path or not isinstance(identity_path, str) or not identity_path:
        raise ReadContractError("The selected read mapping requires record and external identity paths.")
    if (not isinstance(definition_ids, list) or not definition_ids
            or not all(isinstance(value, str) and value for value in definition_ids)
            or len(set(definition_ids)) != len(definition_ids)):
        raise ReadContractError("The selected read mapping requires explicit active mapping definitions.")
    if not isinstance(variables, Mapping) or not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
        raise ReadContractError("The selected variables or page size are invalid.")
    return (
        ReadContract(
            service.name, int(service.service_revision), mapping.name,
            int(mapping.mapping_revision), target, records_path, identity_path,
            tuple(definition_ids), dict(variables), page_size,
        ),
        config,
    )


def value_at(value: object, path: str) -> object:
    current = value
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise ReadContractError(f"The selected response path {path!r} is absent.")
        current = current[segment]
    return current
