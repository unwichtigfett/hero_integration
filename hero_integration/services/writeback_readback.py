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
    from graphql import parse
    from hero_integration.services.graphql_schema_cache import compiled_schema
    from hero_integration.services.read_contract import record_object_type

    schema = compiled_schema(context["snapshot"].snapshot_json).core
    query = parse(plan.request.document).definitions[0]
    root = query.selection_set.selections[0].name.value
    name = record_object_type(schema, plan.request.document, plan.sealed.contract.records_path)
    paths = {item.selection_path for item in plan.request.result_bindings}
    lifecycle_paths = {("is_deleted",)}
    if binding.target_doctype == "Customer":
        lifecycle_paths.add(("is_contact_person",))
    if (not lifecycle_paths <= paths or expected is None or root != expected[0] or name != parent.object_type
            or parent.object_type not in expected[1]
            or plan.source_instance != parent.source_instance
            or binding.source_field != field
            or (field,) not in paths or ('id',) not in paths
            or plan.sealed.contract.identity_path != 'id'
            or binding.mapping_definition_id not in plan.sealed.contract.definition_ids
            or binding.mapping_revision != plan.sealed.contract.mapping_revision):
        raise WritebackContractError('The approved read contract cannot verify this bound writeback field.')
    return plan


def _matches_numeric_uid(observed, external_id):
    if type(observed) not in (str, int):
        return False
    uid = str(observed)
    return uid.isascii() and uid.isdecimal() and not uid.startswith("0") and uid == str(external_id)


def active_bound_project(record, external_id):
    """A ProjectMatch update requires its exact positive UID and an explicit GraphQL Boolean."""
    return (isinstance(record, Mapping) and _matches_numeric_uid(record.get("id"), external_id)
            and record.get("is_deleted") is False)


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
    matches = [row for row in records if isinstance(row, Mapping) and _matches_numeric_uid(row.get('id'), parent.external_object_id)]
    field = mapping['target']['field']
    if len(matches) != 1 or field not in matches[0]:
        raise WritebackContractError('HERO readback did not prove the unique bound record and field.')
    from hero_integration.services.mapping_runtime import source_lifecycle
    doctype = context['binding'].target_doctype
    if ((doctype == "Project" and not active_bound_project(matches[0], parent.external_object_id))
            or (doctype == "Customer" and source_lifecycle("HeroCustomers", matches[0]))):
        raise WritebackContractError("The HERO record is no longer eligible for this bound write.")
    return matches[0][field]


def read_bound_record(context, client):
    from hero_integration.graphql_protocol.graphql_result_decoder import GraphQLResultDecodeError
    from hero_integration.services.b8_runtime import ApprovedBoundReader

    plan = context["readback_plan"]
    reader = ApprovedBoundReader(plan, context["snapshot"].snapshot_json, context["source_context"].binding_revision, 60)
    try:
        row = reader.read_with_client(context["parent"].external_object_id, client)
        if (context["parent"].target_doctype == "Project" and not active_bound_project(row, context["parent"].external_object_id)
                or context["read_plan"].source_deleted(row)) or (context["parent"].target_doctype == "Customer"
                and row.get("is_contact_person") is True):
            raise WritebackContractError("The approved reader marks the bound source as deleted.")
        return row
    except (ValueError, GraphQLResultDecodeError) as error:
        raise WritebackContractError("HERO readback did not prove the unique bound record and field.") from error


def observed_field(context, client):
    """Return wire and native projections from one identity-verified observation."""
    from hero_integration.services.binding_registry import native_fingerprint
    from hero_integration.services.sync_engine import FrappeBoundProcessor

    record = read_bound_record(context, client)
    raw = value_at(record, context["mapping"]["target"]["field"])
    projected = FrappeBoundProcessor.project_scalar(context["read_plan"], context["rule"], record,
                                                   context["parent"].source_instance)
    if projected is None:
        raise WritebackContractError("The approved readback did not establish a field baseline.")
    return state_fingerprint(raw, present=True), native_fingerprint(projected, context["fieldtype"])


def expected_wire(context, variables):
    target = context["mapping"]["target"]
    expected = variables[context["variable"]][target["field"]]
    return state_fingerprint(expected, present=True)


def verify_readback(context, client, variables):
    wire, canonical = observed_field(context, client)
    if wire != expected_wire(context, variables):
        raise WritebackContractError("HERO did not store the expected field value; the binding remains unconfirmed.")
    return canonical



def intent_evidence(context):
    """Persist only identifiers, contract revisions and fingerprints needed for recovery."""
    from hero_integration.services.frappe_writeback import object_write_scope
    binding, parent, executable = context["binding"], context["parent"], context["executable"]
    return {
        "native_target": object_write_scope(binding.site_scope, "", binding.target_doctype, binding.target_name),
        "version": 1, "site": binding.site_scope, "object": context["object_scope"],
        "source": context["source_context"].fingerprint, "parent": parent.identity_key,
        "parent_revision": parent.revision, "binding": binding.identity_key, "revision": binding.revision,
        "definition": binding.mapping_definition_id, "mapping_revision": binding.mapping_revision,
        "ack_version": binding.acknowledged_version, "prior_source": binding.acknowledged_source_fingerprint,
        "prior_target": binding.acknowledged_target_fingerprint, "plan": executable.plan_key,
        "generation": executable.generation, "snapshot": executable.selection.schema_fingerprint,
        "rules": context["rules_fingerprint"], "mutation_mapping": context["mapping_name"],
        "mutation_revision": context["mutation_revision"], "contract": context["mutation_contract"],
        "fieldtype": context["fieldtype"], "authority": context.get("field_authority"), "local": context["intent"].local_fingerprint,
        "wire": expected_wire(context, context["intent"].variables),
    }
