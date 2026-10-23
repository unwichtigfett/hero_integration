"""Runtime mapping plans loaded only from explicitly active mapping documents."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS, validate_projection_record

from hero_integration.services.direct_mapping import (
    MappingSpec,
    PURPOSE_CONFIRMED_COMPANY,
    PURPOSE_CUSTOMER_CONTACT,
    PURPOSE_CUSTOMER_HOME_PHONE,
    PURPOSE_CUSTOMER_RELATION,
    PURPOSE_CUSTOMER_TYPE,
    PURPOSE_FIELD,
    PURPOSE_LIFECYCLE_FLAG,
    PURPOSE_NATIVE_BINDING,
    PURPOSE_RECORD_NAME,
    PURPOSE_SOURCE_IDENTITY,
    RELATION_CUSTOMER_PRIMARY_ADDRESS,
    TRANSFORM_TRIM,
    source_value,
    transformed_value,
)


class MappingPlanError(ValueError):
    pass


@dataclass(frozen=True)
class MappingPlan:
    operation: str
    definitions: tuple[MappingSpec, ...]
    target_operation: str = ""

    @property
    def target_doctype(self) -> str:
        if (self.target_operation or self.operation) in PROJECTIONS:
            return PROJECTIONS[self.target_operation or self.operation]['target_doctype']
        return "Customer" if (self.target_operation or self.operation) == "HeroCustomers" else "Project"

    @property
    def mapping_version(self) -> str:
        payload = [definition.__dict__ for definition in self.definitions]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"hero-mapping-{hashlib.sha256(encoded).hexdigest()[:16]}"

    def require_complete(self) -> None:
        operation = self.target_operation or self.operation
        if operation in PROJECTIONS:
            expected = set(PROJECTIONS[operation]['paths'])
            present = {d.source_path for d in self.definitions if d.purpose != 'Project Relation'}
            if present != expected:
                raise MappingPlanError('The projection requires every reviewed selected field.')
            expected_targets = {b['field'] for b in PROJECTIONS[operation]['bindings'].values()} | set(PROJECTIONS[operation].get('native_bindings', {}))
            actual_targets = [d.target_field for d in self.definitions if d.purpose != 'Project Relation']
            if set(actual_targets) != expected_targets or len(actual_targets) != len(expected_targets):
                raise MappingPlanError('The projection requires every reviewed target exactly once.')
            self._one(PURPOSE_SOURCE_IDENTITY)
            if PROJECTIONS[operation]['project_path']:
                self._one('Project Relation')
            return
        required = {
            "HeroCustomers": {
                PURPOSE_SOURCE_IDENTITY, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_TYPE, PURPOSE_LIFECYCLE_FLAG,
            },
            "HeroProjectMatches": {
                PURPOSE_SOURCE_IDENTITY, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_RELATION, PURPOSE_CONFIRMED_COMPANY,
            },
        }[self.target_operation or self.operation]
        present = {definition.purpose for definition in self.definitions}
        missing = sorted(required - present)
        if missing:
            raise MappingPlanError(f"Active mapping is incomplete: {', '.join(missing)}.")

    def source_identity(self, record: dict[str, object]) -> str:
        definition = self._one(PURPOSE_SOURCE_IDENTITY)
        value = transformed_value(definition, source_value(record, definition.source_path))
        if not isinstance(value, str) or not value:
            raise MappingPlanError("Source identity is missing or invalid.")
        return value

    def values(
        self,
        record: dict[str, object],
        customer_link: Callable[[object], str] | None = None,
        confirmed_company: Callable[[], str] | None = None,
        project_title: Callable[[dict[str, object]], str] | None = None,
        project_link: Callable[[object], str] | None = None,
        native_value: Callable[[str], object] | None = None,
    ) -> dict[str, object]:
        values: dict[str, object] = {}
        projection = (self.target_operation or self.operation) in PROJECTIONS
        if projection:
            try:
                validate_projection_record(self.target_operation or self.operation, record)
            except ValueError as error:
                raise MappingPlanError(str(error)) from error
        for definition in self.definitions:
            value = source_value(record, definition.source_path)
            if definition.purpose in {PURPOSE_FIELD, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_TYPE, PURPOSE_CUSTOMER_CONTACT, PURPOSE_SOURCE_IDENTITY}:
                if definition.purpose == PURPOSE_RECORD_NAME and (self.target_operation or self.operation) == "HeroProjectMatches":
                    transformed = project_title(record) if project_title is not None else transformed_value(definition, value)
                else:
                    transformed = transformed_value(definition, value)
                if not projection and not definition.source_path.startswith('current_project_match_status.') and (transformed is None or transformed == ""):
                    continue
                if definition.purpose == PURPOSE_RECORD_NAME and definition.target_field in values:
                    continue
                values[definition.target_field] = transformed
            elif definition.purpose == 'Native Binding':
                # Customer primary-address values are intentionally not fields
                # of the Customer document.  The Customer adapter resolves the
                # selected binding below and persists it once through its
                # native Address relation.  Keep this exception closed to the
                # reviewed M-099 contract; all other native bindings still
                # require their caller-provided resolver.
                if self._is_customer_primary_address_binding(definition):
                    continue
                if native_value is None:
                    raise MappingPlanError('The native field resolver is unavailable.')
                values[definition.target_field] = native_value(definition.target_field)
            elif definition.purpose == 'Project Relation':
                if value is None:
                    values[definition.target_field] = None
                elif project_link is None:
                    raise MappingPlanError('Project relation resolver is unavailable.')
                else:
                    values[definition.target_field] = project_link(value)
            elif definition.purpose == PURPOSE_CUSTOMER_RELATION:
                if customer_link is None:
                    raise MappingPlanError("Customer relation resolver is unavailable.")
                values[definition.target_field] = customer_link(value)
            elif definition.purpose == PURPOSE_CONFIRMED_COMPANY:
                if confirmed_company is None:
                    raise MappingPlanError("Confirmed Company resolver is unavailable.")
                values[definition.target_field] = confirmed_company()
        return values

    def source_deleted(self, record: dict[str, object]) -> bool:
        definitions = self._with_purpose(PURPOSE_LIFECYCLE_FLAG)
        return any(source_value(record, definition.source_path) is True for definition in definitions)

    def home_phone(self, record: dict[str, object]) -> str | None:
        definitions = self._with_purpose(PURPOSE_CUSTOMER_HOME_PHONE)
        if not definitions:
            return None
        value = transformed_value(definitions[0], source_value(record, definitions[0].source_path))
        return value if isinstance(value, str) else None

    def customer_contact_values(self, values: dict[str, object]) -> dict[str, object]:
        """Expose only the explicit Customer Primary Contact bindings."""
        return {
            definition.target_field: values[definition.target_field]
            for definition in self._with_purpose(PURPOSE_CUSTOMER_CONTACT)
            if definition.target_field in values
        }

    def customer_primary_address_line2(self, record: dict[str, object]) -> str | None:
        """Resolve only the selected, reviewed Customer primary-address binding."""

        bindings = tuple(
            definition for definition in self.definitions
            if definition.purpose == PURPOSE_NATIVE_BINDING
            and definition.relation == RELATION_CUSTOMER_PRIMARY_ADDRESS
        )
        if not bindings:
            return None
        if len(bindings) != 1 or not self._is_customer_primary_address_binding(bindings[0]):
            raise MappingPlanError("Customer primary address requires exactly one selected native binding.")
        value = transformed_value(bindings[0], source_value(record, bindings[0].source_path))
        return value if isinstance(value, str) and value else None

    def _is_customer_primary_address_binding(self, definition: MappingSpec) -> bool:
        """Recognize only the reviewed Customer-to-Address native handoff."""

        return (
            (self.target_operation or self.operation) == "HeroCustomers"
            and definition.operation == "HeroCustomers"
            and definition.purpose == PURPOSE_NATIVE_BINDING
            and definition.source_path == "address.line_2"
            and definition.source_type == "string|null"
            and definition.target_doctype == "Address"
            and definition.target_field == "address_line2"
            and definition.transformation == TRANSFORM_TRIM
            and definition.relation == RELATION_CUSTOMER_PRIMARY_ADDRESS
        )

    def _one(self, purpose: str) -> MappingSpec:
        matches = self._with_purpose(purpose)
        if len(matches) != 1:
            raise MappingPlanError(f"Active mapping requires exactly one {purpose} binding.")
        return matches[0]

    def _with_purpose(self, purpose: str) -> tuple[MappingSpec, ...]:
        return tuple(definition for definition in self.definitions if definition.purpose == purpose)


def plan_from_definitions(operation: str, definitions: list[MappingSpec] | tuple[MappingSpec, ...], *, target_operation: str = "") -> MappingPlan:
    target_operation = target_operation or operation
    if target_operation not in {"HeroCustomers", "HeroProjectMatches", *PROJECTIONS}:
        raise MappingPlanError("The selected target adapter is not supported.")
    selected = tuple(sorted((item for item in definitions if item.operation == target_operation), key=lambda item: (item.priority, item.definition_id, item.source_path)))
    if not selected:
        raise MappingPlanError("No active mapping definitions exist for this operation.")
    plan = MappingPlan(operation, selected, target_operation)
    plan.require_complete()
    return plan


def active_mapping_plan(operation: str, *, definition_ids: tuple[str, ...] = (), target_operation: str = "") -> MappingPlan:
    import frappe

    rows = frappe.get_all(
        "HERO Frappe Mapping",
        filters={"operation": target_operation or operation, "status": "Active"},
        fields=[
            "operation", "source_path", "source_type", "target_doctype", "target_field", "purpose",
            "transformation", "relation", "priority", "definition_id",
        ],
        order_by="priority asc, creation asc",
    )
    if definition_ids:
        expected = set(definition_ids)
        rows = [row for row in rows if row["definition_id"] in expected]
        if {row["definition_id"] for row in rows} != expected:
            raise MappingPlanError("The selected active mapping definitions are unavailable or changed.")
    return plan_from_definitions(operation, [MappingSpec(**row) for row in rows], target_operation=target_operation)
