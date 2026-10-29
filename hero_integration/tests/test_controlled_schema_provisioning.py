from __future__ import annotations

from dataclasses import replace

from hero_integration.services.controlled_schema_provisioning import (
    ModuleOwner,
    ProvisioningOperationKind,
    ProvisioningPlanner,
    ProvisioningRequest,
)
from hero_integration.services.frappe_field_registry import FrappeFieldReference, FrappeFieldRegistry
from hero_integration.services.mapping_registry_model import (
    GraphQLReference,
    GraphQLOperationKind,
    MappingDefinition,
    MappingDirection,
    MissingSemantics,
    MissingValuePolicy,
    TransformPolicy,
)
from hero_integration.services.schema_registry import SchemaTypeReference


def _mapping() -> MappingDefinition:
    return MappingDefinition(
        "definition-1", "source", MappingDirection.HERO_TO_FRAPPE,
        GraphQLReference(GraphQLOperationKind.QUERY, "lookup", ("value",)),
        SchemaTypeReference("String", "String", "SCALAR", ()),
        FrappeFieldReference("app", "New Target", "custom_value"), "Data",
        TransformPolicy.IDENTITY,
        MissingSemantics(MissingValuePolicy.OMIT, MissingValuePolicy.EXPLICIT_NULL),
        "snapshot", "a" * 64, 1,
    )


def test_missing_doctype_produces_immutable_custom_doctype_plan_without_writes():
    registry = FrappeFieldRegistry("site.test", ("app",), (), ())
    request = ProvisioningRequest(_mapping(), "Module", "metadata-fingerprint")
    plan = ProvisioningPlanner().prepare(
        "site.test", (request,), registry, (ModuleOwner("Module", "app"),)
    )

    assert [item.kind.value for item in plan.operations] == ["CREATE_CUSTOM_DOCTYPE"]
    assert plan.operations[0].fieldname == "custom_value"
    assert plan.operations[0].mapping_definition_ids == ("definition-1",)
    assert plan.execution_authority == "NONE"


def test_same_missing_doctype_is_grouped_deterministically_with_all_initial_fields():
    first = _mapping()
    second = replace(
        first,
        definition_id="definition-2",
        frappe=FrappeFieldReference("app", "New Target", "other_value"),
    )
    registry = FrappeFieldRegistry("site.test", ("app",), (), ())
    plan = ProvisioningPlanner().prepare(
        "site.test",
        (ProvisioningRequest(second, "Module", "metadata-fingerprint"), ProvisioningRequest(first, "Module", "metadata-fingerprint")),
        registry,
        (ModuleOwner("Module", "app"),),
    )
    assert len(plan.operations) == 1
    assert plan.operations[0].initial_fields[0].fieldname == "custom_value"
    assert plan.operations[0].initial_fields[1].fieldname == "other_value"
    assert plan.operations[0].mapping_definition_ids == ("definition-1", "definition-2")


def test_unresolved_link_and_collection_provisioning_is_explicitly_rejected():
    registry = FrappeFieldRegistry("site.test", ("app",), (), ())
    linked = replace(_mapping(), frappe_field_type="Link", transform=TransformPolicy.LINK)
    plan = ProvisioningPlanner().prepare(
        "site.test", (ProvisioningRequest(linked, "Module", "metadata-fingerprint"),),
        registry, (ModuleOwner("Module", "app"),),
    )
    assert plan.operations[0].kind == ProvisioningOperationKind.REJECT_UNSUPPORTED_OR_UNSAFE
    assert "dependency" in plan.operations[0].reason.lower()
