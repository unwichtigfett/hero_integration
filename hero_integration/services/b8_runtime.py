"""Reconstruct the approved B2/B7 read boundary for B8 without persistence.

The versioned ``b8_compilation`` input contains no credentials, payload or
transport state. Existing B2 ``QueryAdapter`` owns GraphQL compilation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import json

from hero_integration.services.mapping_contract_v1 import (
    MappingContractError,
    QueryAdapter,
    QueryDraft,
    SnapshotAdapter,
    StoredSnapshot,
)
from hero_integration.services.migration_engine import SealedRead
from hero_integration.services.read_contract import universal_from_documents
from hero_integration.services.runtime_plan import FrappeRuntimePlanReferences, RuntimePlanSelection
from hero_integration.services.sync_engine import MaterializedReadPlan, SyncError


class RuntimeReconstructionError(SyncError):
    """An approved plan cannot be reconstructed exactly from current inputs."""


def reconstruct_materialized_read_plan(selection: RuntimePlanSelection, *, frappe_module=None) -> MaterializedReadPlan:
    """Revalidate and rebuild one immutable B2 request before provider creation."""
    frappe = frappe_module or _frappe()
    try:
        FrappeRuntimePlanReferences(frappe).validate_current(selection)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot)
        service = frappe.get_doc("HERO GraphQL Service", selection.graphql_service)
        mapping = frappe.get_doc("HERO GraphQL Mapping", selection.graphql_mapping)
        payload = _mapping_payload(mapping.mapping_json)
        if not {"b8_compilation", "b8_canonical_operation"}.intersection(payload):
            return saved_runtime_read(service, mapping, snapshot)
        draft = _draft(_compilation_input(payload), selection)
        validated = SnapshotAdapter().adapt(StoredSnapshot(
            snapshot.name, snapshot.snapshot_json, snapshot.schema_fingerprint,
            snapshot.source_instance, snapshot.analysis_status,
        ))
        request = QueryAdapter().materialize(validated, draft)
        sealed = _universal_sealed_read(service, mapping, snapshot, selection, request, payload)
        _validate_against_sealed_read(sealed, draft)
    except (MappingContractError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeReconstructionError("Approved B8 materialization inputs cannot be reconstructed.") from error
    if (
        request.schema_fingerprint != validated.ir_fingerprint
        or request.operation_name != selection.operation_name
        or request.protocol_field_name != draft.field
        or not request.result_bindings
    ):
        raise RuntimeReconstructionError("Reconstructed B2 request differs from the approved B7 read.")
    return MaterializedReadPlan(sealed, request, selection.source_instance)


def saved_runtime_read(service, mapping, snapshot):
    """Compile the migration reader with the same projections and its approved native variables."""
    from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
    from hero_integration.services.direct_runtime import page_variables
    from hero_integration.services.graphql_schema_cache import compiled_schema
    from hero_integration.services.mapping_contract_v1 import materialize_saved_query
    from hero_integration.services.read_contract import from_documents

    contract = from_documents(service, mapping, snapshot)
    request = materialize_saved_query(snapshot, service.query_document, page_variables(contract, 0),
        selected_paths=contract.projection_paths, preserve_paths=contract.projection_preserve_paths)
    schema = compiled_schema(snapshot.snapshot_json).core
    request = GraphQLOperationMaterializer.bind_saved_query_result(schema, request, root_name=mapping.operation_name)
    sealed = SealedRead(contract, request.document, snapshot.schema_fingerprint, _hash_text(request.document))
    return MaterializedReadPlan(sealed, request, mapping.source_instance)


def canonical_operation_contract(selection: RuntimePlanSelection, request) -> dict[str, object]:
    """Return the immutable B8 approval payload for a B2 materialization.

    It belongs to the separately approved mapping revision; a changed seal
    invalidates the runtime plan through its revision/generation gate.
    """
    response_paths = [list(binding.response_path) for binding in request.result_bindings]
    payload = {
        "version": 1,
        "source_instance": selection.source_instance,
        "service_revision": selection.service_revision,
        "operation_revision": selection.operation_revision,
        "mapping_revision": selection.mapping_revision,
        "schema_fingerprint": selection.schema_fingerprint,
        "operation_name": selection.operation_name,
        "document_sha256": _hash_text(request.document),
        "variables_sha256": _hash_json(request.variables),
        "response_paths_sha256": _hash_json(response_paths),
        "request_fingerprint": request.request_fingerprint,
    }
    payload["operation_identity"] = _hash_json(payload)
    return payload


def _universal_sealed_read(service, mapping, snapshot, selection, request, payload) -> SealedRead:
    """Validate B8-only approval without relaxing the legacy read contract."""
    expected = canonical_operation_contract(selection, request)
    provided = payload.get("b8_canonical_operation")
    if not isinstance(provided, Mapping) or dict(provided) != expected:
        raise RuntimeReconstructionError("Approved B8 canonical operation seal is absent or stale.")
    if service.query_document != request.document:
        raise RuntimeReconstructionError("Approved B8 service query differs from its canonical materialization.")
    contract = universal_from_documents(service, mapping, snapshot)
    return SealedRead(contract, request.document, snapshot.schema_fingerprint, _hash_text(request.document))


def _mapping_payload(raw: object) -> Mapping[str, object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeReconstructionError("Approved GraphQL mapping JSON is malformed.") from error
    if not isinstance(parsed, Mapping):
        raise RuntimeReconstructionError("Approved GraphQL mapping must be an object.")
    return parsed


def _compilation_input(parsed: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(parsed, Mapping):
        raise RuntimeReconstructionError("Approved GraphQL mapping must be an object.")
    value = parsed.get("b8_compilation")
    if not isinstance(value, Mapping) or set(value) - {
        "version", "operation_name", "alias", "field", "selected_paths", "variables", "pagination", "ordering",
    }:
        raise RuntimeReconstructionError("Approved mapping lacks a canonical B8 compilation input.")
    if value.get("version") != 1 or _contains_secret_key(value):
        raise RuntimeReconstructionError("B8 compilation input is unsupported or contains secret-like data.")
    return value


def _hash_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeReconstructionError("Canonical operation document is invalid.")
    return sha256(value.encode()).hexdigest()


def _hash_json(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as error:
        raise RuntimeReconstructionError("Canonical operation payload is invalid.") from error
    return sha256(encoded.encode()).hexdigest()


def _draft(value: Mapping[str, object], selection: RuntimePlanSelection) -> QueryDraft:
    operation_name, alias, field = (value.get(key) for key in ("operation_name", "alias", "field"))
    selected, variables = value.get("selected_paths"), value.get("variables")
    if (
        operation_name != selection.operation_name
        or not isinstance(alias, str) or not isinstance(field, str)
        or not isinstance(selected, Sequence) or isinstance(selected, (str, bytes))
        or not all(isinstance(path, str) and path for path in selected)
        or not isinstance(variables, Mapping)
        or _contains_dynamic_value(variables)
    ):
        raise RuntimeReconstructionError("B8 compilation input is incomplete or has dynamic arguments.")
    return QueryDraft(
        operation_name=operation_name, alias=alias, field=field,
        selected_paths=tuple(selected), variables=dict(variables),
        pagination=value.get("pagination") if isinstance(value.get("pagination"), str) else None,
        ordering=value.get("ordering") if isinstance(value.get("ordering"), str) else None,
    )


def _validate_against_sealed_read(sealed: SealedRead, draft: QueryDraft) -> None:
    """Bind B2 arguments and result bindings to the B7 read contract.

    Rendered queries hide scalar variable changes. Resolve the two approved
    symbolic arguments and require an exact match with persisted B2 input.
    """
    contract = sealed.contract
    if draft.alias != contract.records_path or contract.identity_path not in draft.selected_paths:
        raise RuntimeReconstructionError("B8 compilation bindings differ from the sealed B7 read contract.")
    expected: dict[str, object] = {}
    for name, value in contract.variables.items():
        if value == "$offset":
            expected[name] = 0
        elif value == "$page_size":
            expected[name] = contract.page_size
        elif isinstance(value, str) and value.startswith("$"):
            raise RuntimeReconstructionError("B8 compilation cannot resolve an unreviewed symbolic argument.")
        else:
            expected[name] = value
    if dict(draft.variables) != expected:
        raise RuntimeReconstructionError("B8 compilation arguments differ from the sealed B7 read contract.")


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any("token" in str(key).lower() or "secret" in str(key).lower() or _contains_secret_key(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return any(_contains_secret_key(item) for item in value)
    return False


def _contains_dynamic_value(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith("$")
    if isinstance(value, Mapping):
        return any(_contains_dynamic_value(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_dynamic_value(item) for item in value)
    return False


def _frappe():
    import frappe
    return frappe


class ApprovedBoundReader:
    """Compare only explicitly bound objects using the approved selection and a schema-proven filter."""

    def __init__(self, materialized, raw_schema, source_revision, rate_limit):
        from hero_integration.services.graphql_schema_cache import compiled_schema
        from hero_integration.services.sync_engine import ChangeCapabilities

        self.plan = materialized
        self.raw_schema = raw_schema
        self.schema = compiled_schema(raw_schema)
        self.source_revision = source_revision
        self.rate_limit = rate_limit
        self.read_options = {}
        self.direct_read = None
        self.product_usage = None
        self.employee_directory = False
        self.history_source = None
        self.historical_people = ((), ())
        try:
            self.identity_request("1")
        except ValueError:
            bounded = False
        else:
            bounded = True
        self.capabilities = ChangeCapabilities(False, False, bounded)
        self.client = None
        # A schema-declared collection filter preserves the approved selection and page bound.
        self.bound_batch_size = 1
        contract = materialized.sealed.contract
        from hero_integration.services.project_identity_read import MAX_ID_FILTER_VALUES
        limit = min(MAX_ID_FILTER_VALUES, getattr(contract, "page_size", 1))
        if bounded and limit > 1:
            if contract.target_operation == "HeroProjectMatches":
                # Some approved snapshots declare only the singular project_match reader.
                try:
                    self.identity_request("1", identities=("1", "2"))
                except ValueError:
                    return
            if contract.target_operation in {"HeroCustomers", "HeroProjectMatches", "HeroTasks", "HeroCalendarEvents", "HeroServices"}:
                self.bound_batch_size = limit

    def inventory_arguments(self):
        """Resolve the two approved positional variables without changing the selection."""
        from graphql import FieldNode, OperationDefinitionNode, VariableNode, Visitor, parse, print_ast, visit
        from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationRequest

        contract, request = self.plan.sealed.contract, self.plan.request
        definitions = parse(request.document).definitions
        if len(definitions) != 1 or not isinstance(definitions[0], OperationDefinitionNode):
            return None
        fields = definitions[0].selection_set.selections
        if len(fields) != 1 or not isinstance(fields[0], FieldNode):
            return None
        root = fields[0]
        if root.directives or (root.alias or root.name).value != contract.records_path.split(".")[0]:
            return None

        class References(Visitor):
            def __init__(self):
                super().__init__()
                self.names = []

            def enter_variable(self, node, *_):
                self.names.append(node.name.value)

        refs = References()
        visit(root, refs)
        args = {arg.name.value: arg.value for arg in root.arguments}
        page_args = {}
        for key, symbol, initial in (("offset", "$offset", 0), ("first", "$page_size", contract.page_size)):
            var = args.get(key)
            if not isinstance(var, VariableNode):
                return None
            name = var.name.value
            binding = key if isinstance(request, GraphQLOperationRequest) else name
            if (contract.variables.get(binding) != symbol or refs.names.count(name) != 1
                    or type(request.variables.get(name)) is not int or request.variables[name] != initial):
                return None
            page_args[key] = name
        order = args.get("orderBy")
        if order is not None:
            value = request.variables.get(order.name.value) if isinstance(order, VariableNode) else getattr(order, "value", None)
            if value == contract.identity_path and not {"orderDirection", "sortings", "last", "before", "after"}.intersection(args):
                return page_args
        elif (contract.identity_path == "id" and "sortings" in args and print_ast(args["sortings"]) == "[ID_ASC]"
              and not {"last", "before", "after"}.intersection(args)):
            return page_args
        return None

    def prepare_inventory(self, selection, frappe, *, complete_history=True):
        """Bind auxiliary reads to the same approved direct migration, including usage scope."""
        from hero_integration.services import direct_operations, direct_runtime
        operation = self.plan.sealed.contract.target_operation
        contract = self.plan.sealed.contract
        if complete_history and operation in {"HeroProjectMatches", "HeroTasks"}:
            from hero_integration.sources.hero import GraphQLServiceSource
            service = frappe.get_doc("HERO GraphQL Service", selection.graphql_service)
            def approved_contract():
                self._configuration()
                current = reconstruct_materialized_read_plan(selection, frappe_module=frappe)
                if current != self.plan:
                    raise RuntimeReconstructionError("Monitoring reader, schema or effective field rules changed during transport.")
                return current.sealed.contract
            source = GraphQLServiceSource(self._client(), service, contract, selection.source_instance,
                                          revalidate=approved_contract)
            from hero_integration.services.migration import _verified_historical_people
            self.historical_people = _verified_historical_people(source)
            if operation == "HeroProjectMatches" and (contract.adapter_options or {}).get("logbook", {}).get("offset_variable"):
                self.history_source = source
        if operation not in direct_operations.OPERATIONS:
            return
        service = frappe.get_doc("HERO GraphQL Service", selection.graphql_service)
        mapping = frappe.get_doc("HERO GraphQL Mapping", selection.graphql_mapping)
        snapshot = frappe.get_doc("HERO GraphQL Schema Snapshot", selection.schema_snapshot)
        self.direct_read = direct_runtime.read_from_documents(operation, service, mapping, snapshot)
        if operation == "HeroProducts":
            from hero_integration.services import product_usage
            self.product_usage = product_usage.load(selection.source_instance)

    def _inventory_details(self, rows):
        """Read child details before reacquiring the runtime locks."""
        from hero_integration.services import direct_runtime
        read = getattr(self, "direct_read", None)
        if not rows or not read:
            return rows
        source = self.plan.source_instance
        if read.operation in {"HeroDocuments", "HeroPictures"}:
            from hero_integration.services import file_transfer, source_history
            if any(not isinstance(row.get("category"), str) for row in rows):
                raise RuntimeReconstructionError("The approved file category is missing.")
            wanted = [row for row in rows if direct_runtime.belongs(read.operation, row)]
            found = file_transfer.details(self._client(), wanted, read, source,
                paths=source_history.FILE_DETAIL_PATHS if source_history.enabled(read, "file_details") else None)
            result = []
            for row in rows:
                if not direct_runtime.belongs(read.operation, row):
                    result.append(row)
                    continue
                detail = found[row["uuid"]]
                if (type(detail.get("is_deleted")) is not bool or any(field in detail and detail[field] != row.get(field)
                        for field in ("category", "modified"))):
                    raise RuntimeReconstructionError("File metadata changed during the approved read.")
                result.append({**row, "is_deleted": detail["is_deleted"], "_hero_file_detail": detail})
            return result
        if read.operation == "HeroEmployees":
            from hero_integration.services import source_history
            if source_history.enabled(read, "partner_absences"):
                absences = source_history.read_absences(self._client(), read, source, [row["id"] for row in rows],
                                                       self._configuration)
                return [{**row, "_hero_absences": absences[row["id"]]} for row in rows]
        if read.operation == "HeroServices":
            from hero_integration.services import service_migration
            snapshot = direct_runtime.validated_snapshot(read, source)
            details = service_migration._composition_details(self._client(), snapshot, [row["id"] for row in rows])
            for row in rows:
                detail = details[row["id"]]
                if any(detail.get(field) != row.get(field) for field in ("modified", "company_id", "is_deleted")):
                    raise RuntimeReconstructionError("Service metadata changed during the approved read.")
            history = service_migration.historical_inventory_evidence(self._client(), source, details)
            return [{**row, "_hero_composition": details[row["id"]],
                     "_hero_historical_products": history.get(row["id"], {"requested": [], "reader": None, "rows": []})}
                    for row in rows]
        return rows

    def inventory_page(self, checkpoint, size, overlap):
        from hero_integration.graphql_protocol.graphql_result_decoder import decode_graphql_result
        from hero_integration.graphql_protocol.transport import Response
        from hero_integration.services.direct_runtime import records
        from hero_integration.services.offset_pagination import InventoryWindowMoved, advance, inventory_evidence
        from hero_integration.services.read_contract import value_at

        contract, request = self.plan.sealed.contract, self.plan.request
        arguments = self.inventory_arguments()
        directory = getattr(self, "employee_directory", False)
        if (not arguments and not directory) or not 0 < size <= contract.page_size:
            raise RuntimeReconstructionError("The approved reader has no bounded identity-ordered inventory contract.")
        offset = checkpoint.get("next_offset", 0)
        if type(offset) is not int or offset < 0:
            raise RuntimeReconstructionError("Invalid inventory checkpoint.")
        usage = getattr(self, "product_usage", None)
        received = None
        if directory:
            from hero_integration.services.employee_migration import _records
            # The approved company.partners field has no server page argument.
            # Retain its existing response bound and apply the native work budget after a stable sort.
            data = self._client().query(request.document, dict(request.variables))
            decode_graphql_result(request, Response(True, json.dumps({"data": data}), True))
            directory_rows = _records(data, contract.records_path)
            received = len(directory_rows)
            rows = directory_rows[offset:offset + size]
        elif usage:
            from hero_integration.services.product_usage import page as usage_page
            rows = usage_page(self._client(), self.direct_read, sorted(usage[1]), offset, size)
        else:
            variables = {**request.variables, arguments["offset"]: offset, arguments["first"]: size}
            data = self._client().query(request.document, variables)
            decode_graphql_result(request, Response(True, json.dumps({"data": data}), True))
            rows = records(contract, data)
            if getattr(self, "history_source", None) is not None:
                from hero_integration.services.project_logbook import complete_mapped_histories
                from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
                def child_request(params):
                    return GraphQLOperationMaterializer.materialize_document(self.schema.core, self.schema.fingerprint,
                                                                          request.document, params)
                rows = complete_mapped_histories(self.history_source, rows, variables, materialize=child_request)
        rows = self._inventory_details(rows)
        product_keys = contract.target_operation == "HeroProducts" and contract.identity_path == "product_id"
        def identity(row):
            value = value_at(row, contract.identity_path)
            if product_keys:
                if not isinstance(value, str) or not value or value.strip() != value:
                    raise RuntimeReconstructionError("Invalid inventory identity.")
                return value
            if type(value) not in (str, int) or not str(value).isascii() or not str(value).isdigit() or str(int(value)) != str(value):
                raise RuntimeReconstructionError("Invalid inventory identity.")
            return int(value)
        ids = [identity(row) for row in rows]
        if ids != sorted(ids):
            raise RuntimeReconstructionError("Inventory response is not ordered by identity.")
        # JSON key order can change on persistence. Compare the identities in the
        # approved numeric order. A short/empty page must also contain the old tail.
        tail = sorted(key if product_keys else int(key) for key in checkpoint.get("tail", {}))
        # A smaller allocation may revisit only the beginning of the previous overlap.
        expected_tail = tail[:size]
        if expected_tail and ids[:len(expected_tail)] != expected_tail:
            raise InventoryWindowMoved()
        # A shifting collection can move a page backwards, including partial repeated windows.
        if ids and checkpoint and not tail and ids[0] <= checkpoint["last_id"]:
            raise InventoryWindowMoved()
        prior_window = {**checkpoint, "size": size, "overlap": overlap} if checkpoint else None
        page = advance(rows, offset=offset, size=size, overlap=overlap, previous=prior_window, identity=identity, evidence=inventory_evidence)
        if ids:
            page.checkpoint["last_id"] = ids[-1]
        if received is not None:
            page.checkpoint["source_rows_read"] = received
            if offset + len(rows) == received:
                from dataclasses import replace
                page = replace(page, next_offset=None)
        if contract.target_operation in {"HeroDocuments", "HeroPictures"}:
            from dataclasses import replace
            from hero_integration.services.direct_runtime import belongs
            page = replace(page, rows=tuple(row for row in page.rows if belongs(contract.target_operation, row)))
        return page

    def identity_request(self, identity, *, identities=None):
        from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
        from hero_integration.services.project_identity_read import CollectionIdentityReader, ProjectIdentityReader

        contract = self.plan.sealed.contract
        original = self.plan.request
        variables = dict(original.variables)
        singular = contract.target_operation == "HeroProjectMatches" and (not identities or len(identities) == 1)
        if singular:
            from hero_integration.services.mapped_update_v2 import MappedUpdateError
            try:
                narrowed, variables = ProjectIdentityReader(None, self.raw_schema, identity).request(
                    original.document, variables, allow_alias=True)
            except MappedUpdateError:
                # The collection can preserve approved filters that a singular root cannot represent.
                singular = False
        if not singular:
            narrowed = CollectionIdentityReader(None, self.raw_schema, contract.target_operation,
                contract.identity_path, identity).request(original.document, allow_alias=True, identities=identities)
            if narrowed == original.document:
                raise RuntimeReconstructionError("No schema-proven identity filter exists for this approved reader.")
        request = GraphQLOperationMaterializer.materialize_document(
            self.schema.core, self.schema.fingerprint, narrowed, variables)
        return request, singular

    def _configuration(self):
        from hero_integration.client.settings import get_configuration

        config = get_configuration(fresh=True)
        if (not config.source_binding_confirmed or config.source_binding_revision != self.source_revision
                or config.source_instance != self.plan.source_instance):
            raise RuntimeReconstructionError("The confirmed source changed before the bound read.")
        return config

    def read_many(self, identities, *, allow_missing=False):
        """Each thread owns its Frappe connection and freshly checks the source."""
        if len(identities) <= self.bound_batch_size:
            return self._read_identities(tuple(identities), self._client(), allow_missing=allow_missing)

        import copy
        import frappe
        from concurrent.futures import ThreadPoolExecutor

        site, sites_path, actor = frappe.local.site, frappe.local.sites_path, frappe.session.user
        def fetch(identity):
            frappe.init(site=site, sites_path=sites_path)
            try:
                frappe.connect()
                frappe.set_user(actor)
                reader = copy.copy(self)
                reader.client = None
                result = reader._read_identities((identity,), reader._client(), allow_missing=allow_missing)
                return result.get(identity)
            finally:
                frappe.destroy()

        with ThreadPoolExecutor(max_workers=min(len(identities), self.concurrency)) as pool:
            return dict(zip(identities, pool.map(fetch, identities)))

    def _client(self):
        from hero_integration.client.graphql import HeroGraphQLClient

        config = self._configuration()
        if self.client is None:
            self.client = HeroGraphQLClient(config.endpoint, lambda: self._configuration().api_token,
                requests_per_minute=self.rate_limit, **({"timeout": config.timeout_seconds, "max_retries": 0} | self.read_options))
        return self.client

    def read_bound(self, identity):
        return self.read_with_client(identity, self._client())

    def read_bound_many(self, identities):
        if (not isinstance(identities, (tuple, list)) or not 1 <= len(identities) <= self.bound_batch_size
                or any(type(key) is not str for key in identities) or len(set(identities)) != len(identities)):
            raise RuntimeReconstructionError("HERO returned an ambiguous or missing bound object.")
        return self._read_identities(tuple(identities), self._client())

    def read_with_client(self, identity, client):
        """Use the same exact identity and typed response contract for writeback readback."""
        return self._read_identities((str(identity),), client)[str(identity)]

    def _read_identities(self, identities, client, *, allow_missing=False):
        from hero_integration.graphql_protocol.graphql_result_decoder import decode_graphql_result
        from hero_integration.graphql_protocol.transport import Response
        from hero_integration.services.read_contract import value_at

        contract = self.plan.sealed.contract
        original = self.plan.request
        request, singular = self.identity_request(identities[0], identities=identities)
        data = client.query(request.document, dict(request.variables))
        if not isinstance(data, Mapping) or data.get("errors"):
            raise RuntimeReconstructionError("HERO returned an incomplete bound response.")
        if singular:
            # B2 decodes the original approved collection shape, including its alias.
            row = data[contract.records_path]
            data = {contract.records_path: [] if row is None else [row]}
        decode_graphql_result(original, Response(True, json.dumps({"data": data}), True))
        records = value_at(data, contract.records_path)
        if not isinstance(records, list) or (not allow_missing and len(records) != len(identities)):
            raise RuntimeReconstructionError("HERO returned an ambiguous or missing bound object.")
        records_by_id = {}
        expected = set(identities)
        for row in records:
            actual = value_at(row, contract.identity_path) if isinstance(row, dict) else None
            if type(actual) not in (str, int) or str(actual) not in expected or str(actual) in records_by_id:
                raise RuntimeReconstructionError("The bound object response has the wrong source identity.")
            records_by_id[str(actual)] = row
        return records_by_id
