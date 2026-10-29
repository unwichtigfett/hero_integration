"""Narrow, schema-bound Frappe-to-HERO writeback contracts.

Only the reviewed Customer and Project fields are admitted.  The module is
deliberately not a generic mutation mapper: HERO identity always comes from
an app-owned integration link, never from the browser.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from hero_integration.services.graphql_contract import (
    GraphQLContractError,
    parse_mutation,
    validate_mutation,
)
from hero_integration.services.graphql_graph import GraphSnapshotError, normalize_schema


class WritebackContractError(ValueError):
    """A mapping attempted to widen the reviewed MVP write surface."""


@dataclass(frozen=True)
class WritebackDefinition:
    doctype: str
    source_type: str
    operation: str
    argument: str
    input_type: str
    fields: Mapping[str, str]


DEFINITIONS = {
    "Customer": WritebackDefinition(
        doctype="Customer", source_type="customer", operation="update_contact",
        argument="contact", input_type="CustomerInput",
        fields={
            "custom_hero_customer_number": "nr", "first_name": "first_name",
            "last_name": "last_name", "customer_name": "full_name",
            "email_id": "email", "mobile_no": "phone_mobile",
        },
    ),
    "Project": WritebackDefinition(
        doctype="Project", source_type="project", operation="update_project_match",
        argument="project_match", input_type="ProjectMatchInput",
        fields={
            "project_name": "project_title", "custom_hero_project_number": "project_nr",
            "custom_hero_project_type": "project_type", "custom_hero_partner_notes": "partner_notes",
            "custom_hero_display_id": "display_id",
        },
    ),
}
_TRANSFORMS = frozenset({"Identity", "Trim"})


def definition(doctype: str) -> WritebackDefinition:
    try:
        return DEFINITIONS[doctype]
    except KeyError as error:
        raise WritebackContractError("Only Customer and Project writeback are supported.") from error


def candidate_fields(doctype: str) -> dict[str, str]:
    return dict(definition(doctype).fields)


def reviewed_input_target(doctype: str, source_field: str, schema: object) -> dict[str, str]:
    """Return the one schema-proven HERO input target for an approved source.

    This is deliberately a catalogue helper, not a generic field mapper.  The
    returned choice is the same exact relationship enforced by
    :func:`normalise_mapping` and is safe to render as a picker option.
    """
    contract = definition(doctype)
    target_field = contract.fields.get(source_field)
    if not target_field:
        raise WritebackContractError("The selected Frappe field is not approved for HERO writeback.")
    input_field = _schema_input_field(schema, contract, target_field)
    return {
        "operation": contract.operation,
        "argument": contract.argument,
        "input_type": contract.input_type,
        "field": target_field,
        "graphql_type": _type_label(input_field.get("type")),
    }


def normalise_mapping(value: object, schema: object) -> dict[str, object]:
    """Validate exactly one approved Frappe field to one HERO input field."""
    if not isinstance(value, Mapping):
        raise WritebackContractError("Writeback mapping must be an object.")
    if (set(value) - {'version', 'source', 'target', 'transformation'}
            or ('version' in value and (type(value['version']) is not int or value['version'] != 1))):
        raise WritebackContractError('Active writeback normalization would lose mapping parameters.')
    source, target = value.get("source"), value.get("target")
    transform = value.get("transformation", "Identity")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise WritebackContractError("Writeback mapping needs source and target objects.")
    if set(source) - {'doctype', 'field'} or set(target) - {'operation', 'argument', 'input_type', 'field'}:
        raise WritebackContractError('Active writeback normalization would lose mapping parameters.')
    doctype, field = source.get("doctype"), source.get("field")
    if not isinstance(doctype, str) or not isinstance(field, str):
        raise WritebackContractError("Writeback source DocType and field are required.")
    contract = definition(doctype)
    expected_field = contract.fields.get(field)
    if not expected_field:
        raise WritebackContractError("The selected Frappe field is not approved for HERO writeback.")
    expected = {
        "operation": contract.operation, "argument": contract.argument,
        "input_type": contract.input_type, "field": expected_field,
    }
    if any(target.get(key) != item for key, item in expected.items()):
        raise WritebackContractError("The HERO target is not the reviewed target for this Frappe field.")
    if transform not in _TRANSFORMS:
        raise WritebackContractError("Only Identity and Trim transformations are approved for writeback.")
    _schema_input_field(schema, contract, expected_field)
    return {
        "version": 1,
        "source": {"doctype": contract.doctype, "field": field},
        "target": expected,
        "transformation": transform,
    }


def normalise_draft_mapping(value: object, schema: object) -> dict[str, object]:
    """Validate a schema-owned Draft without widening executable writeback.

    Generic Drafts are intentionally rejected by :func:`normalise_mapping`,
    which is still the sole gate used by request compilation and activation.
    """
    if not isinstance(value, Mapping):
        raise WritebackContractError("Writeback mapping must be an object.")
    source, target = value.get("source"), value.get("target")
    transform = value.get("transformation", "Identity")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise WritebackContractError("Writeback mapping needs source and target objects.")
    doctype, field = source.get("doctype"), source.get("field")
    operation, argument = target.get("operation"), target.get("argument")
    path = target.get("field_path", target.get("field"))
    if not all(isinstance(value, str) for value in (doctype, field, operation, argument, path)):
        raise WritebackContractError("Writeback source and exact schema target are required.")
    if transform not in _TRANSFORMS:
        raise WritebackContractError("Only Identity and Trim transformations are approved for writeback.")
    input_type, graphql_type = _schema_draft_input_field(schema, operation, argument, path)
    return {
        "version": 1,
        "source": {"doctype": doctype, "field": field},
        "target": {
            "operation": operation, "argument": argument, "input_type": input_type,
            "field": path, "field_path": path, "graphql_type": graphql_type,
        },
        "transformation": transform,
        "draft_only": True,
    }


def draft_source_type_compatible(frappe_type: str, graphql_type: str) -> bool:
    """Allow only direct scalar/enum pairs in a non-executable Draft."""
    scalar = graphql_type.rstrip("!")
    if scalar in {"String", "ID"}:
        return frappe_type in {"Data", "Small Text", "Long Text", "Text", "Code", "Select"}
    if scalar == "Int":
        return frappe_type == "Int"
    if scalar == "Float":
        return frappe_type in {"Int", "Float"}
    if scalar == "Boolean":
        return frappe_type == "Check"
    # Schema enums retain their named type, while Frappe Select remains the
    # only direct enum source; arbitrary custom scalars fail closed.
    return frappe_type == "Select"


def build_request_checked(mapping: Mapping[str, object], schema: object, source_id: object, field_value: object) -> tuple[str, dict[str, object]]:
    """Compile a typed one-field mutation from server-owned data only."""
    normalised = normalise_mapping(mapping, schema)
    target = normalised["target"]
    assert isinstance(target, Mapping)
    identity = _positive_int(source_id)
    value = _transform(field_value, str(normalised["transformation"]))
    argument, input_type, operation, field = (
        str(target["argument"]), str(target["input_type"]),
        str(target["operation"]), str(target["field"]),
    )
    variables = {argument: {"id": identity, field: value}}
    document = f"mutation HeroWriteback(${argument}: {input_type}!) {{ {operation}({argument}: ${argument}) {{ id }} }}"
    # Validate the compiled value against the snapshot as a second boundary.
    try:
        validate_mutation(schema, parse_mutation(document), variables)
    except GraphQLContractError as error:
        raise WritebackContractError("The exact HERO mutation variables are not schema-valid.") from error
    return document, variables


def fingerprint_payload(mapping_name: str, mapping_revision: int, document_version: object, source_instance: str, source_id: object, document: str, variables: Mapping[str, object]) -> str:
    """Canonical preview inputs; the caller hashes them without auditing values."""
    return json.dumps(
        {"mapping": mapping_name, "revision": int(mapping_revision),
         "document_version": str(document_version or ""), "source_instance": source_instance,
         "source_id": str(source_id), "document": document, "variables": variables},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _schema_input_field(schema: object, contract: WritebackDefinition, field: str) -> Mapping[str, object]:
    try:
        normalised = normalize_schema(schema)
        types = {item.get("name"): item for item in normalised.get("types", []) if isinstance(item, Mapping)}
        root = types.get((normalised.get("mutationType") or {}).get("name"))
        operation = next((item for item in root.get("fields", []) if item.get("name") == contract.operation), None)
        argument = next((item for item in operation.get("args", []) if item.get("name") == contract.argument), None)
        input_type = types.get(_named_type(argument.get("type")))
        input_fields = {item.get("name"): item for item in input_type.get("inputFields", [])}
        selected = input_fields.get(field)
        if _named_type(argument.get("type")) != contract.input_type or not {"id", field} <= set(input_fields) or not isinstance(selected, Mapping):
            raise ValueError
        return selected
    except (AttributeError, GraphSnapshotError, TypeError, ValueError) as error:
        raise WritebackContractError("The saved schema does not prove this approved HERO write target.") from error


def _schema_draft_input_field(schema: object, operation_name: str, argument_name: str, path: str) -> tuple[str, str]:
    """Resolve a scalar/enum input path exactly from the selected mutation."""
    try:
        normalised = normalize_schema(schema)
        types = {item.get("name"): item for item in normalised.get("types", []) if isinstance(item, Mapping)}
        root = types.get((normalised.get("mutationType") or {}).get("name"))
        operation = next((item for item in root.get("fields", []) if item.get("name") == operation_name), None)
        argument = next((item for item in operation.get("args", []) if item.get("name") == argument_name), None)
        input_type = _named_type(argument.get("type"))
        current_ref = argument.get("type")
        if _has_list(current_ref):
            raise ValueError
        if not path:
            named = _named_type(current_ref)
            kind = _named_kind(current_ref)
            if kind not in {"SCALAR", "ENUM"}:
                raise ValueError
            return input_type, _type_label(current_ref)
        current = types.get(input_type)
        for index, segment in enumerate(part for part in path.split(".") if part):
            selected = next((item for item in current.get("inputFields", []) if item.get("name") == segment), None)
            current_ref = selected.get("type")
            if _has_list(current_ref):
                raise ValueError
            if index == len(path.split(".")) - 1:
                if _named_kind(current_ref) not in {"SCALAR", "ENUM"}:
                    raise ValueError
                return input_type, _type_label(current_ref)
            current = types.get(_named_type(current_ref))
            if not isinstance(current, Mapping) or current.get("kind") != "INPUT_OBJECT":
                raise ValueError
    except (AttributeError, GraphSnapshotError, TypeError, ValueError) as error:
        raise WritebackContractError("The selected HERO target is not a scalar or enum input field in the saved schema.") from error
    raise WritebackContractError("The selected HERO target is not a scalar or enum input field in the saved schema.")


def _named_type(reference: object) -> str:
    current = reference
    while isinstance(current, Mapping):
        name = current.get("name")
        if isinstance(name, str) and name:
            return name
        current = current.get("ofType")
    return ""


def _named_kind(reference: object) -> str:
    current = reference
    while isinstance(current, Mapping):
        if current.get("name"):
            return str(current.get("kind") or "")
        current = current.get("ofType")
    return ""


def _has_list(reference: object) -> bool:
    current = reference
    while isinstance(current, Mapping):
        if current.get("kind") == "LIST":
            return True
        current = current.get("ofType")
    return False


def _type_label(reference: object) -> str:
    if not isinstance(reference, Mapping):
        return "Unknown"
    kind = reference.get("kind")
    name = reference.get("name")
    if kind == "NON_NULL":
        return f"{_type_label(reference.get('ofType'))}!"
    if kind == "LIST":
        return f"[{_type_label(reference.get('ofType'))}]"
    return str(name or kind or "Unknown")


def _positive_int(value: object) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as error:
        raise WritebackContractError("The HERO Integration Link has no safe numeric external identity.") from error
    if number <= 0:
        raise WritebackContractError("The HERO Integration Link has no safe positive external identity.")
    return number


def _transform(value: object, transform: str) -> object:
    if transform == "Identity":
        return value
    if transform == "Trim" and isinstance(value, str):
        return value.strip()
    if transform == "Trim":
        raise WritebackContractError("Trim is only valid for a text Frappe field.")
    raise WritebackContractError("Unsupported writeback transformation.")
