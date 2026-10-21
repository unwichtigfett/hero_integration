"""Runtime mapping plans loaded only from explicitly active mapping documents."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from hero_integration.services.direct_mapping import (
    MappingSpec,
    PURPOSE_CONFIRMED_COMPANY,
    PURPOSE_CUSTOMER_CONTACT,
    PURPOSE_CUSTOMER_HOME_PHONE,
    PURPOSE_CUSTOMER_RELATION,
    PURPOSE_CUSTOMER_TYPE,
    PURPOSE_FIELD,
    PURPOSE_LIFECYCLE_FLAG,
    PURPOSE_RECORD_NAME,
    PURPOSE_SOURCE_IDENTITY,
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
        return "Customer" if (self.target_operation or self.operation) == "HeroCustomers" else "Project"

    @property
    def mapping_version(self) -> str:
        payload = [definition.__dict__ for definition in self.definitions]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"hero-mapping-{hashlib.sha256(encoded).hexdigest()[:16]}"

    def require_complete(self) -> None:
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
    ) -> dict[str, object]:
        values: dict[str, object] = {}
        for definition in self.definitions:
            value = source_value(record, definition.source_path)
            if definition.purpose in {PURPOSE_FIELD, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_TYPE, PURPOSE_CUSTOMER_CONTACT, PURPOSE_SOURCE_IDENTITY}:
                if definition.purpose == PURPOSE_RECORD_NAME and (self.target_operation or self.operation) == "HeroProjectMatches":
                    transformed = project_title(record) if project_title is not None else transformed_value(definition, value)
                else:
                    transformed = transformed_value(definition, value)
                if transformed is None or transformed == "":
                    continue
                if definition.purpose == PURPOSE_RECORD_NAME and definition.target_field in values:
                    continue
                values[definition.target_field] = transformed
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

    def _one(self, purpose: str) -> MappingSpec:
        matches = self._with_purpose(purpose)
        if len(matches) != 1:
            raise MappingPlanError(f"Active mapping requires exactly one {purpose} binding.")
        return matches[0]

    def _with_purpose(self, purpose: str) -> tuple[MappingSpec, ...]:
        return tuple(definition for definition in self.definitions if definition.purpose == purpose)


def plan_from_definitions(operation: str, definitions: list[MappingSpec] | tuple[MappingSpec, ...], *, target_operation: str = "") -> MappingPlan:
    target_operation = target_operation or operation
    if target_operation not in {"HeroCustomers", "HeroProjectMatches"}:
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
