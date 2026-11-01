"""Runtime mapping plans loaded only from explicitly active mapping documents."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable
from hero_integration.services.projection_contracts import CONTRACTS as PROJECTIONS, validate_projection_record
from hero_integration.services.project_naming import project_title_missing

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
    TRANSFORM_POSITIVE_INT_TO_TEXT,
    MISSING,
    lookup,
    source_value,
    transformed_value,
)


class MappingPlanError(ValueError):
    pass


class MappedValues(dict):
    """Writable target values plus the fields HERO did not supply a value for.

    ``missing`` targets were absent from the response and carry no evidence;
    ``empty`` targets were explicitly null/empty and are not written under the
    established no-clear policy, but still take part in change comparison.
    """

    def __init__(self, *args, missing=(), empty=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.missing = set(missing)
        self.empty = set(empty)


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
        allow_partial: bool = False,
    ) -> dict[str, object]:
        values = MappedValues()
        projection = (self.target_operation or self.operation) in PROJECTIONS
        if projection:
            try:
                validate_projection_record(self.target_operation or self.operation, record, allow_partial=allow_partial)
            except ValueError as error:
                raise MappingPlanError(str(error)) from error
        if (self.target_operation or self.operation) == "HeroProjectMatches":
            for field in ("customer", "address", "current_project_match_status"):
                nested = record.get(field)
                if nested is not None and not isinstance(nested, dict):
                    raise MappingPlanError("Project response has an incompatible object: " + field)
        for definition in self.definitions:
            raw = lookup(record, definition.source_path)
            operation = self.target_operation or self.operation
            if operation == "HeroProjectMatches" and raw is not MISSING:
                kind = definition.source_type.split("|")[0]
                valid = (raw is None and "|null" in definition.source_type) or (
                    (kind == "string" and isinstance(raw, str)) or
                    (kind == "int" and type(raw) is int) or
                    (kind == "boolean" and type(raw) is bool) or
                    (kind == "float" and type(raw) in (int, float))
                )
                # Project naming also accepts the established structured trade
                # representation used by existing contracts and test fixtures.
                # A missing salutation is no block (user decision 2026-10-31);
                # the stored rule keeps its approved "string" type.
                if definition.source_path == "customer.title" and raw is None:
                    valid = True
                if definition.source_path == "project_type" and isinstance(raw, dict):
                    valid = isinstance(raw.get("code"), str)
                if definition.transformation == TRANSFORM_POSITIVE_INT_TO_TEXT and raw is not None:
                    valid = type(raw) is int and raw > 0
                if not valid:
                    raise MappingPlanError("Project response has an incompatible field: " + definition.source_path)
            value = None if raw is MISSING else raw
            if (self.target_operation or self.operation) == 'HeroCalendarEvents' and (
                value is None or (definition.source_path in {'description', 'full_address'}
                                  and isinstance(value, str) and not value.strip())
                # An existing Event keeps its subject when HERO's title is blank.
                or (allow_partial and definition.source_path == 'title'
                    and isinstance(value, str) and not value.strip())
                or (definition.source_path in {'partners', 'resources'} and value == [])
            ):
                # Missing, NULL and unconfirmed empty values do not clear Event fields.
                continue
            if definition.purpose in {PURPOSE_FIELD, PURPOSE_RECORD_NAME, PURPOSE_CUSTOMER_TYPE, PURPOSE_CUSTOMER_CONTACT, PURPOSE_SOURCE_IDENTITY}:
                project_name = definition.purpose == PURPOSE_RECORD_NAME and (self.target_operation or self.operation) == "HeroProjectMatches"
                if project_name and project_title is not None and project_title_missing(record):
                    values.missing.add(definition.target_field)
                    continue
                if raw is MISSING and not projection and not project_name:
                    # An unselected or omitted response field is no evidence
                    # of an empty HERO value (e.g. a missing company_name must
                    # not turn a Company into an Individual).
                    if definition.target_field not in values:
                        values.missing.add(definition.target_field)
                    continue
                if project_name:
                    transformed = project_title(record) if project_title is not None else transformed_value(definition, value)
                else:
                    transformed = transformed_value(definition, value)
                if not projection and not definition.source_path.startswith('current_project_match_status.') and (transformed is None or transformed == ""):
                    if definition.target_field not in values:
                        values.empty.add(definition.target_field)
                    continue
                if definition.purpose == PURPOSE_RECORD_NAME and definition.target_field in values:
                    continue
                values[definition.target_field] = transformed
                values.missing.discard(definition.target_field)
                values.empty.discard(definition.target_field)
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
                try:
                    values[definition.target_field] = native_value(definition.target_field)
                except KeyError:
                    if not (allow_partial and (self.target_operation or self.operation) == 'HeroCalendarEvents'
                            and definition.target_field in {'starts_on', 'ends_on', 'all_day', 'custom_hero_original_start', 'custom_hero_original_end'}):
                        raise
            elif definition.purpose == 'Project Relation':
                if value is None:
                    values[definition.target_field] = None
                elif project_link is None:
                    raise MappingPlanError('Project relation resolver is unavailable.')
                else:
                    values[definition.target_field] = project_link(value)
            elif definition.purpose == PURPOSE_CUSTOMER_RELATION:
                if raw is MISSING:
                    values.missing.add(definition.target_field)
                    continue
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

    def has_home_phone(self) -> bool:
        return bool(self._with_purpose(PURPOSE_CUSTOMER_HOME_PHONE))

    def customer_contact_fields(self) -> set[str]:
        """Every selected Customer Primary Contact target, empty or not."""
        return {definition.target_field for definition in self._with_purpose(PURPOSE_CUSTOMER_CONTACT)}

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


def plan_from_definitions(operation: str, definitions: list[MappingSpec] | tuple[MappingSpec, ...], *, target_operation: str = "", native_task: bool = False) -> MappingPlan:
    target_operation = target_operation or operation
    if target_operation not in {"HeroCustomers", "HeroProjectMatches", *PROJECTIONS}:
        raise MappingPlanError("The selected target adapter is not supported.")
    selected = tuple(sorted((item for item in definitions if item.operation == target_operation), key=lambda item: (item.priority, item.definition_id, item.source_path)))
    if not selected:
        raise MappingPlanError("No active mapping definitions exist for this operation.")
    plan = MappingPlan(operation, selected, target_operation)
    if native_task:
        from hero_integration.services.direct_mapping import validate_native_task_rule
        if target_operation != 'HeroTasks' or len(selected) not in {2, 3, 4, 5, 6}:
            raise MappingPlanError('Native Task requires identity, title, and optional comment, assignment, project and deadline rules.')
        paths = [item.source_path for item in selected]
        if len(set(paths)) != len(paths) or not {'id', 'title'} <= set(paths):
            raise MappingPlanError('Native Task requires exactly one identity and title rule.')
        for item in selected:
            validate_native_task_rule(item)
    else:
        plan.require_complete()
    return plan


def active_mapping_plan(operation: str, *, definition_ids: tuple[str, ...] = (), target_operation: str = "", mapping_name: str = "", native_task: bool = False, lock_rules: bool = False) -> MappingPlan:
    import frappe

    if definition_ids and (not mapping_name or len(set(definition_ids)) != len(definition_ids)):
        raise MappingPlanError('Explicit rule IDs require an explicit active mapping and unique bindings.')
    if native_task and (not definition_ids or not mapping_name or len(set(definition_ids)) != len(definition_ids)):
        raise MappingPlanError('Native Task requires explicitly selected approved rules.')
    if lock_rules and not native_task:
        raise MappingPlanError('Locking rule reads are restricted to the native Task contract.')
    rows = frappe.get_all(
        "HERO Frappe Mapping",
        filters={"operation": target_operation or operation, "status": "Active"},
        fields=[
            "operation", "source_path", "source_type", "target_doctype", "target_field", "purpose",
            "transformation", "relation", "priority", "definition_id", "personal_graphql_mapping",
            "schema_snapshot", "schema_fingerprint",
        ],
        order_by="priority asc, creation asc",
    ) if not lock_rules else []
    if lock_rules:
        for identity in sorted(definition_ids):
            current = frappe.db.sql(
                'select operation, source_path, source_type, target_doctype, target_field, purpose, '
                'transformation, relation, priority, definition_id, personal_graphql_mapping, '
                'schema_snapshot, schema_fingerprint, status from `tabHERO Frappe Mapping` '
                'where definition_id=%s for update', (identity,), as_dict=True)
            if len(current) != 1 or current[0]['status'] != 'Active' or current[0]['operation'] != (target_operation or operation):
                raise MappingPlanError('A selected active Task rule is unavailable or changed.')
            rows.append({key: value for key, value in current[0].items() if key != 'status'})
    if definition_ids:
        expected = set(definition_ids)
        rows = [row for row in rows if row["definition_id"] in expected]
        if len(rows) != len(expected) or {row["definition_id"] for row in rows} != expected:
            raise MappingPlanError("The selected active mapping definitions are unavailable or changed.")
        if any(row.get('personal_graphql_mapping') and row.get('personal_graphql_mapping') != mapping_name for row in rows):
            raise MappingPlanError('A selected rule version belongs to another read mapping.')
    else:
        rows = [row for row in rows if not row.get('personal_graphql_mapping')]
    if mapping_name and definition_ids:
        mapping = frappe.get_doc('HERO GraphQL Mapping', mapping_name)
        if mapping.status != 'Active' or mapping.direction != 'QUERY_TO_FRAPPE':
            raise MappingPlanError('The selected read mapping is not an active Query definition.')
        try:
            from hero_integration.services.personal_mapping_content import unique_json_object
            config = json.loads(mapping.mapping_json, object_pairs_hook=unique_json_object)
        except (ValueError, TypeError, AttributeError):
            raise MappingPlanError('The approved mapping binding is invalid.') from None
        if not isinstance(config, dict) or config.get('target_operation') != (target_operation or operation):
            raise MappingPlanError('The selected mapping target operation does not match the requested plan.')
        selected = config.get('mapping_definition_ids') if isinstance(config, dict) else None
        if (not isinstance(selected, list) or any(not isinstance(value, str) or not value for value in selected)
                or len(set(selected)) != len(selected) or tuple(selected) != tuple(definition_ids)):
            raise MappingPlanError('Selected rules do not match the approved mapping binding.')
        if any(row.get('schema_snapshot') != mapping.schema_snapshot or row.get('schema_fingerprint') != mapping.schema_fingerprint for row in rows):
            raise MappingPlanError('Selected rule snapshot does not match the approved read mapping.')
        scoped = [row for row in rows if row.get('personal_graphql_mapping')]
        has_column = getattr(frappe.db, 'has_column', None) if scoped else None
        if scoped and callable(has_column) and has_column('HERO Frappe Mapping', 'portable_lineage_json'):
            from hero_integration.services.mapping_rule_lineage import parse_lineage, semantic_fingerprint
            from hero_integration.services.personal_mapping_content import RULE_FIELDS
            for row in scoped:
                metadata = frappe.db.get_value('HERO Frappe Mapping', {'definition_id':row['definition_id']},
                                               ['portable_definition_id', 'portable_lineage_json', 'schema_manifest', 'schema_dependencies'], as_dict=True)
                if metadata and not metadata.get('portable_lineage_json'):
                    continue  # Pre-lineage scoped revisions retain their explicit legacy binding.
                try:
                    lineage = parse_lineage(metadata.get('portable_lineage_json') if metadata else None)
                    fields = {key:row.get(key) for key in RULE_FIELDS}
                    if (lineage['revision_id'] != metadata.get('portable_definition_id')
                            or lineage['content_hash'] != semantic_fingerprint(row['operation'], row['schema_fingerprint'], fields,
                                schema_manifest=metadata.get('schema_manifest'), schema_dependencies=metadata.get('schema_dependencies'))):
                        raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise MappingPlanError('Scoped rule lineage is missing or inconsistent; reconcile the revision.') from None
    definitions = [MappingSpec(**{k: v for k, v in row.items() if k not in {'personal_graphql_mapping', 'schema_snapshot', 'schema_fingerprint'}}) for row in rows]
    # Recheck native Field grants against the currently installed server
    # contract before executing a previously approved mapping revision.
    # Revoking a binding must not leave an executable stale rule behind.
    from hero_integration.services.direct_mapping import validate_spec
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _FrappeMetadata
    metadata = _FrappeMetadata()
    for spec in definitions:
        if native_task:
            try:
                validate_spec(spec, metadata)
            except ValueError as error:
                raise MappingPlanError('Native Task field authorization is no longer valid.') from error
        if spec.operation in {'HeroCustomers', 'HeroProjectMatches'} and spec.purpose == PURPOSE_FIELD:
            field = metadata.field(spec.target_doctype, spec.target_field)
            if field is None or not getattr(field, 'is_custom_field', False):
                if not definition_ids or not mapping_name:
                    raise MappingPlanError('Native field mappings require an explicit approved read mapping.')
                try:
                    validate_spec(spec, metadata)
                except ValueError as error:
                    raise MappingPlanError('Native field authorization is no longer valid.') from error
    return plan_from_definitions(operation, definitions, target_operation=target_operation, native_task=native_task)


def effective_rule_fingerprint(definition_ids, mapping_name, *, lock=False, frappe_module=None):
    """Hash exactly the selected active rule content, not unrelated mapping edits.

    A locking caller must hold the existing mapping-root exchange lock first.
    Current reads fence publication/disablement until the checkpoint transaction
    ends; ordinary reads are for lock-free transport preflight only.
    """
    if frappe_module is None:
        import frappe as frappe_module
    ids = tuple(sorted(definition_ids))
    if not ids or len(ids) != len(set(ids)) or not mapping_name:
        raise MappingPlanError("A checkpoint requires explicit unique mapping rule identities.")
    fields = ("definition_id", "operation", "source_path", "source_type", "target_doctype", "target_field",
              "purpose", "transformation", "relation", "priority", "personal_graphql_mapping", "status",
              "schema_snapshot", "schema_fingerprint", "mapping_revision", "schema_manifest", "schema_dependencies")
    if lock:
        placeholders = ", ".join(["%s"] * len(ids))
        rows = frappe_module.db.sql(
            "SELECT " + ", ".join(fields) + " FROM `tabHERO Frappe Mapping` "
            "WHERE definition_id IN (" + placeholders + ") ORDER BY definition_id FOR UPDATE", ids, as_dict=True)
    else:
        rows = frappe_module.get_all("HERO Frappe Mapping", filters={"definition_id": ["in", list(ids)]},
                                     fields=list(fields), limit_page_length=0)
    if (len(rows) != len(ids) or {row.get("definition_id") for row in rows} != set(ids)
            or any(row.get("status") != "Active" or row.get("personal_graphql_mapping") not in
                   (None, "", mapping_name) for row in rows)):
        raise MappingPlanError("Selected checkpoint field rules are missing, inactive or belong to another mapping.")
    content = [{field: row.get(field) for field in fields} for row in sorted(rows, key=lambda row: row['definition_id'])]
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
