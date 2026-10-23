"""Saved, explicit read contracts for the existing HERO reader and mapper."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

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


def from_documents(service, mapping, snapshot) -> ReadContract:
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
    if mapping.schema_snapshot != snapshot.name or mapping.schema_fingerprint != snapshot.schema_fingerprint:
        raise ReadContractError("The selected read mapping no longer matches its schema snapshot.")
    if int(getattr(service, "approved_mapping_revision", 0) or 0) != int(mapping.mapping_revision or 0):
        raise ReadContractError("The selected read mapping approval is stale.")
    assert_read_only(service.query_document)
    try:
        config = json.loads(mapping.mapping_json)
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
    if not isinstance(definition_ids, list) or not definition_ids or not all(isinstance(value, str) and value for value in definition_ids):
        raise ReadContractError("The selected read mapping requires explicit active mapping definitions.")
    if not isinstance(variables, Mapping) or not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
        raise ReadContractError("The selected variables or page size are invalid.")
    if target in PROJECTIONS:
        contract = PROJECTIONS[target]
        from hero_integration.services.native_projections import validate_options
        validate_options(contract, config.get('native_options'))
        if snapshot.schema_fingerprint != contract['fingerprint']:
            raise ReadContractError('Projection requires its reviewed schema fingerprint.')
        if service.query_document.strip() != approved_query(target).strip():
            raise ReadContractError('Projection requires the exact reviewed read-only query.')
        if records_path != contract['root'] or identity_path != 'id' or dict(variables) != {'first': '$page_size', 'offset': '$offset'}:
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
            if customer_contract_version == CUSTOMER_CONTRACT_V3:
                if line2_definition not in definition_ids or legal_form_definition not in definition_ids:
                    raise ReadContractError("Customer V3 requires the selected primary-address line_2 and company legal-form bindings.")
            elif customer_contract_version == CUSTOMER_CONTRACT_V2:
                if line2_definition not in definition_ids:
                    raise ReadContractError("Customer V2 requires the selected primary-address line_2 binding.")
                if legal_form_definition in definition_ids:
                    raise ReadContractError("Customer V2 cannot select the company legal-form binding.")
            elif line2_definition in definition_ids or legal_form_definition in definition_ids:
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
    return ReadContract(service.name, int(service.service_revision), mapping.name, int(mapping.mapping_revision), target, records_path, identity_path, tuple(definition_ids), dict(variables), page_size)


def value_at(value: object, path: str) -> object:
    current = value
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise ReadContractError(f"The selected response path {path!r} is absent.")
        current = current[segment]
    return current
