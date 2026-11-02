"""Read back one bound value using the exact approved runtime read request."""
from __future__ import annotations

from collections.abc import Mapping

from hero_integration.services.binding_registry import state_fingerprint
from hero_integration.services.frappe_writeback import WritebackContractError
from hero_integration.services.read_contract import value_at


def prepare_readback(context, frappe_module):
    from hero_integration.services.b8_runtime import reconstruct_materialized_read_plan

    plan = reconstruct_materialized_read_plan(context['executable'].selection, frappe_module=frappe_module)
    return validate_readback(context, plan)


def validate_readback(context, plan):
    mapping, parent, binding = context['mapping'], context['parent'], context['binding']
    expected = {
        'Customer': ('contacts', {'Contact', 'Customer'}),
        'Project': ('project_matches', {'Project', 'ProjectMatch'}),
    }.get(binding.target_doctype)
    field = mapping['target']['field']
    paths = {item.selection_path for item in plan.request.result_bindings}
    if (expected is None or plan.request.protocol_field_name != expected[0]
            or parent.object_type not in expected[1]
            or plan.source_instance != parent.source_instance
            or binding.source_field != field
            or (field,) not in paths or ('id',) not in paths
            or plan.sealed.contract.identity_path != 'id'
            or binding.mapping_definition_id not in plan.sealed.contract.definition_ids
            or binding.mapping_revision != plan.sealed.contract.mapping_revision):
        raise WritebackContractError('The approved read contract cannot verify this bound writeback field.')
    return plan


def read_bound_value(context, client):
    plan = context['readback_plan']
    mapping, parent = context['mapping'], context['parent']
    result = client.query(plan.request.document, plan.request.variables)
    if not isinstance(result, Mapping) or result.get('errors'):
        raise WritebackContractError('HERO readback did not return a complete response.')
    payload = result.get('data', result)
    records = value_at(payload, plan.sealed.contract.records_path)
    if not isinstance(records, list):
        raise WritebackContractError('HERO readback did not return the approved record list.')
    matches = [row for row in records if isinstance(row, Mapping) and str(row.get('id')) == str(parent.external_object_id)]
    field = mapping['target']['field']
    if len(matches) != 1 or field not in matches[0]:
        raise WritebackContractError('HERO readback did not prove the unique bound record and field.')
    return matches[0][field]


def verify_readback(context, client, variables):
    mapping = context['mapping']
    observed = read_bound_value(context, client)
    expected = variables[mapping['target']['argument']][mapping['target']['field']]
    # Input transforms have already run in build_request_checked. Preserve the
    # stored type and value, including NULL, empty strings, False and zero.
    if state_fingerprint(observed, present=True) != state_fingerprint(expected, present=True):
        raise WritebackContractError('HERO did not store the expected field value; the binding remains unconfirmed.')
    return state_fingerprint(observed, present=True)
