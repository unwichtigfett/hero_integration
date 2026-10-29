from __future__ import annotations

import hashlib
import json
from uuid import UUID

import frappe
from frappe.model.document import Document

from hero_integration.services.frappe_mapping_catalog import FrappePickerMetadata, validate_draft_writeback_source
from hero_integration.services.frappe_writeback import (
    WritebackContractError, draft_source_type_compatible, normalise_draft_mapping, normalise_mapping,
)
from hero_integration.services.schema_operations import catalog


class HEROGraphQLMapping(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        from hero_integration.services.mapping_draft_access import protect_managed_mapping
        protect_managed_mapping(self)
        previous = self.get_doc_before_save() if not self.is_new() else None
        if previous and previous.status == 'Active' and previous.direction != self.direction:
            frappe.throw('An active mapping definition direction is immutable; create a separate definition for the other direction.')
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", self.schema_snapshot)
        if snapshot.analysis_status != "Validated":
            frappe.throw("A validated schema snapshot is required.")
        # All supported membership changes in a directional exchange context
        # participate in the same narrowly scoped application lock.
        scopes = set()
        if previous and previous.status == 'Active':
            scopes.add((previous.source_instance or '', previous.schema_snapshot, previous.direction))
        if self.status == 'Active':
            scopes.add((getattr(snapshot, 'source_instance', '') or '', self.schema_snapshot, self.direction))
        context_fields = ('status', 'source_instance', 'schema_snapshot', 'direction', 'operation_kind',
                          'operation_name', 'mapping_json', 'definition_id')
        context_changed = bool(scopes) and (not previous or any(self.get(field) != previous.get(field) for field in context_fields))
        if context_changed:
            for scope in sorted(scopes):
                if previous:
                    require_exchange_context(*scope)
                else:
                    lock_exchange_context(*scope)
        lossy_writeback = False
        try:
            from hero_integration.services.personal_mapping_content import unique_json_object
            mapping = json.loads(self.mapping_json, object_pairs_hook=unique_json_object)
            if not isinstance(mapping, dict):
                raise ValueError
            if self.direction == "FRAPPE_TO_MUTATION":
                schema = json.loads(snapshot.snapshot_json)
                from hero_integration.api import _mapping_values_preserved
                original = mapping
                if self.status == "Active":
                    mapping = normalise_mapping(original, schema)
                else:
                    try:
                        mapping = normalise_mapping(mapping, schema)
                    except WritebackContractError:
                        mapping = normalise_draft_mapping(mapping, schema)
                        source_field = validate_draft_writeback_source(
                            mapping["source"]["doctype"], mapping["source"]["field"], FrappePickerMetadata(),
                        )
                        if not draft_source_type_compatible(source_field.fieldtype, mapping["target"]["graphql_type"]):
                            frappe.throw("The selected Frappe source and HERO scalar or enum input types are incompatible.")
                lossy_writeback = not _mapping_values_preserved(original, mapping)
                source = mapping["source"]
                if not frappe.get_meta(source["doctype"]).get_field(source["field"]):
                    frappe.throw("The configured Frappe writeback field no longer exists.")
                if self.operation_kind != "MUTATION" or self.operation_name != mapping["target"]["operation"]:
                    frappe.throw("Writeback direction, operation kind and reviewed HERO target must agree.")
                self.mapping_json = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
            available = {item["operation_name"] for item in catalog(json.loads(snapshot.snapshot_json), self.operation_kind)}
        except WritebackContractError as error:
            frappe.throw(str(error))
        except (ValueError, TypeError):
            frappe.throw("The GraphQL mapping must contain a valid JSON object and schema operation.")
        if lossy_writeback:
            frappe.throw('Writeback normalization would lose mapping parameters.')
        if self.operation_name not in available:
            frappe.throw("The selected operation is not present in the selected schema snapshot.")
        self.schema_fingerprint = snapshot.schema_fingerprint
        self.source_instance = getattr(snapshot, "source_instance", "") or ""
        if self.direction == "FRAPPE_TO_MUTATION" and self.status == "Active":
            contracts = frappe.get_all(
                "HERO Mutation Contract",
                filters={"schema_snapshot": snapshot.name, "operation_name": self.operation_name, "status": "Active"},
                pluck="name",
            )
            if len(contracts) != 1:
                frappe.throw("Exactly one active schema-bound HERO Mutation Contract is required for writeback activation.")
            if context_changed:
                _check_active_writeback_conflicts(snapshot.name, mapping["source"], self.name or "")
        if not self.definition_id:
            self.definition_id = self.name or ""
        if not previous and self.definition_id:
            try:
                portable_candidate = str(UUID(self.definition_id))
            except (ValueError, TypeError, AttributeError):
                portable_candidate = ''
            if portable_candidate == self.definition_id:
                from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
                _lock_active_target('personal-new-definition', frappe.local.site, portable_candidate)
                matches = frappe.db.sql('select name from `tabHERO GraphQL Mapping` '
                                        'where portable_definition_id=%s for update', (portable_candidate,))
                if matches:
                    frappe.throw('Definition identity is already enrolled by another mapping.')
                from hero_integration.services.frappe_mapping_draft_storage import (
                    lock_definition_drafts, lock_identity_drafts, personal_publication_parent, first_publication_admits,
                )
                drafts = lock_definition_drafts(frappe, [portable_candidate])
                if any(not first_publication_admits('definition', portable_candidate, row) for row in drafts):
                    frappe.throw('Definition identity is already used by a personal draft.')
                for row in lock_identity_drafts(frappe, [portable_candidate], 'child'):
                    parent = personal_publication_parent()
                    if not ((parent and row.owner_user == frappe.session.user and row.base_active_mapping == parent)
                            or first_publication_admits('definition', portable_candidate, row)):
                        frappe.throw('Definition identity is already used by a personal draft.')
        if previous and previous.definition_id and self.definition_id != previous.definition_id:
            frappe.throw("GraphQL mapping definition identity is immutable.")
        portable = self.get('portable_definition_id') or ''
        from hero_integration.services.frappe_mapping_draft_storage import first_publication_admits
        if portable != ((previous.get('portable_definition_id') or '') if previous else '') and not (
                not previous and portable == self.definition_id and first_publication_admits('definition', portable)):
            frappe.throw('Portable identity may only be enrolled through the identity service.')
        for field in ('portable_schema_manifest', 'portable_schema_dependencies'):
            value = self.get(field) or ''
            old = (previous.get(field) or '') if previous else ''
            if value != old and not (not previous and self.direction == 'QUERY_TO_FRAPPE'
                                     and first_publication_admits('definition', self.definition_id)):
                frappe.throw('Portable mapping schema provenance is immutable and service-managed.')
        self.content_hash = hashlib.sha256(
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        semantic_fields = ("schema_snapshot", "operation_kind", "operation_name", "direction", "mapping_json")
        content_changed = bool(previous and any(self.has_value_changed(field) for field in semantic_fields))
        writeback_scope = bool(previous and (previous.direction == "FRAPPE_TO_MUTATION" or self.direction == "FRAPPE_TO_MUTATION"))
        review_changed = bool(writeback_scope and any(self.has_value_changed(field) for field in ("suggestion_reason", "confidence")))
        status_changed = bool(writeback_scope and previous.status != self.status)
        draft_editor_changed = bool(writeback_scope and (previous.status == "Draft" or self.status == "Draft")
                                    and previous.modified_by != self.modified_by)
        if content_changed or review_changed or status_changed or draft_editor_changed:
            self.mapping_revision = int(previous.mapping_revision or 0) + 1
        from hero_integration.services.frappe_mapping_draft_storage import is_personal_publication
        if content_changed and not is_personal_publication(self):
            self.status = "Draft"
        if self.status == "Active" and previous and previous.status == "Draft":
            if previous.modified_by != frappe.session.user:
                frappe.throw("Only the administrator who last changed this Draft may activate it.")

    def on_trash(self):
        if self.status == 'Active':
            require_exchange_context(self.source_instance or '', self.schema_snapshot, self.direction)


def lock_exchange_context(source_instance, snapshot_name, direction):
    """Serialize supported membership changes for this exact source context."""
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    _lock_active_target('personal-draft-exchange', source_instance or '',
                        json.dumps([snapshot_name, direction], ensure_ascii=False, separators=(',', ':')))


def require_exchange_context(source_instance, snapshot_name, direction):
    """Existing Document.save already holds the parent row; never acquire context after it."""
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target
    target = json.dumps([snapshot_name, direction], ensure_ascii=False, separators=(',', ':'))
    key = json.dumps([frappe.local.site, 'personal-draft-exchange', source_instance or '', target],
                     ensure_ascii=False, separators=(',', ':'))
    name = 'hero-map:' + hashlib.sha256(key.encode()).hexdigest()[:52]
    if name not in (getattr(frappe.db, '_hero_mapping_target_locks', None) or set()):
        frappe.throw('Mapping context must be locked before saving an active definition; use the mapping service.')
    _lock_active_target('personal-draft-exchange', source_instance or '', target)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _check_active_writeback_conflicts(snapshot_name, source, name):
    from hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping import _lock_active_target

    direction = "FRAPPE_TO_MUTATION"
    _lock_active_target("writeback:" + snapshot_name, source["doctype"], direction + ":" + source["field"])
    # This locking read must run after acquiring the shared connection lock.
    # On a stale snapshot MariaDB may abort instead of returning newer rows.
    conflicts = frappe.db.sql(
        "SELECT name, mapping_json FROM `tabHERO GraphQL Mapping` "
        "WHERE direction = %s AND status = %s AND schema_snapshot = %s AND name != %s FOR UPDATE",
        (direction, "Active", snapshot_name, name), as_dict=True,
    )
    for other in conflicts:
        try:
            value = json.loads(other.mapping_json, object_pairs_hook=_unique_json_object)
            other_source = value.get("source") if isinstance(value, dict) else None
        except (TypeError, ValueError):
            other_source = None
        if not isinstance(other_source, dict) or not all(isinstance(other_source.get(key), str) for key in ("doctype", "field")):
            frappe.throw("An active writeback source cannot be safely inspected.")
        if all(other_source[key] == source[key] for key in ("doctype", "field")):
            frappe.throw("An active writeback mapping already owns this Frappe field.")
