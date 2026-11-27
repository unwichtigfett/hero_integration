"""Verify the authority of the manual service or a bound worker update at the wire."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import time
from uuid import UUID

from .graphql import HeroClientError

_CONFIRMATION = ContextVar("hero_confirmed_test_mutation", default=None)
_WORKER_REQUEST = ContextVar("hero_bound_update_request", default=None)
_POLICY = Path(__file__).with_name("test_mutation_policy.json")
_SOURCE = Path("/run/secrets/hero-test-mutation-source.json")
_PERMIT = Path("/run/secrets/hero-test-mutation-permit.json")
_RECEIPTS = Path("/var/lib/hero-test-mutation-receipts")


def _deny():
    raise HeroClientError("This HERO mutation has no valid, unused test authorization.", code="MUTATION_DENIED")


@contextmanager
def confirmed_service(audit, snapshot):
    """Entered only after the manual service RPC verifies confirmation and reserves its audit."""
    token = _CONFIRMATION.set((audit, snapshot))
    try:
        yield
    finally:
        _CONFIRMATION.reset(token)


def _config(path):
    from hero_integration.graphql_protocol.graphql_response import strict_json_loads
    # Source and permit are operator-mounted files outside site backups and web-facing configuration.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            _deny()
        content = source.read(8193)
        if len(content) > 8192:
            _deny()
    return strict_json_loads(content.decode())


def wire_digest(document, variables):
    """Operator helper for the final materialized document and its exact variables."""
    return sha256(json.dumps([document, variables], ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _target(document, variables, policy, snapshot):
    """Prove one explicit, scalar, identity-bound update against a reviewed schema and effect profile."""
    from graphql import (FieldNode, GraphQLInputObjectType, GraphQLObjectType, GraphQLNonNull,
                         OperationDefinitionNode, Undefined, VariableNode, parse, validate)
    from hero_integration.services.graphql_schema_cache import compiled_schema

    def nullable(value):
        return value.of_type if isinstance(value, GraphQLNonNull) else value

    compiled = compiled_schema(snapshot.snapshot_json)
    if compiled.fingerprint != snapshot.schema_fingerprint:
        _deny()
    schema = compiled.core
    tree = parse(document)
    if len(tree.definitions) != 1 or validate(schema, tree):
        _deny()
    op = tree.definitions[0]
    if (not isinstance(op, OperationDefinitionNode) or op.operation.value != "mutation" or op.directives
            or len(op.variable_definitions) != 1 or len(op.selection_set.selections) != 1):
        _deny()
    root, var = op.selection_set.selections[0], op.variable_definitions[0]
    if (not isinstance(root, FieldNode) or root.alias or root.directives or len(root.arguments) != 1
            or not root.selection_set or len(root.selection_set.selections) != 1
            or var.default_value is not None or var.directives):
        _deny()
    response, arg = root.selection_set.selections[0], root.arguments[0]
    if (not isinstance(response, FieldNode) or response.name.value != "id" or response.alias or response.directives
            or response.arguments or response.selection_set or not isinstance(arg.value, VariableNode)
            or arg.value.name.value != var.variable.name.value or set(variables) != {arg.value.name.value}):
        _deny()
    from graphql.execution.values import get_variable_values
    if isinstance(get_variable_values(schema, op.variable_definitions, variables), list):
        _deny()
    shape = schema.mutation_type.fields[root.name.value]
    target = nullable(shape.type)
    if not isinstance(target, GraphQLObjectType):
        _deny()
    input_type = nullable(shape.args[arg.name.value].type)
    if (not isinstance(input_type, GraphQLInputObjectType)
            or any(f.default_value is not Undefined for f in input_type.fields.values())):
        _deny()
    payload = variables[arg.value.name.value]
    if not isinstance(payload, dict) or len(payload) != 2 or "id" not in payload:
        _deny()
    identity = payload["id"]
    field = next(key for key in payload if key != "id")
    if (field not in input_type.fields or "id" not in input_type.fields
            or nullable(input_type.fields["id"].type) not in (schema.get_type("ID"), schema.get_type("Int"))):
        _deny()
    scalar = nullable(input_type.fields[field].type)
    value_type = {"String": str, "Date": str, "Boolean": bool, "Float": float, "Int": int}
    if type(payload[field]) is not value_type.get(getattr(scalar, "name", "")):
        _deny()
    # The schema proves shape. Only a reviewed provider effect contract can establish that
    # the resolver changes this existing object alone, without workflow or related writes.
    profile = {"schema": snapshot.schema_fingerprint, "target": target.name, "root": root.name.value,
               "argument": arg.name.value, "input": input_type.name, "field": field}
    input_arg = shape.args[arg.name.value]
    for reviewed in policy["reviewed_operations"]:
        logical_target = reviewed.get("target")
        if (logical_target not in policy["targets"] or type(identity) not in (int, str)
                or str(identity) != str(policy["targets"][logical_target])):
            continue
        if reviewed == profile:
            if scalar is schema.get_type("String") and set(shape.args) == {arg.name.value} and input_arg.default_value is Undefined:
                return logical_target, str(identity), root.name.value
            continue
        omitted = sorted(set(shape.args) - {arg.name.value})
        expected = {**profile, "result": target.name, "target": logical_target,
                    "value": payload[field], "omitted_arguments": omitted}
        # The non-null input object is supplied explicitly. Its argument default cannot apply.
        # An omitted sibling must be optional and have no default, including an empty list.
        if (reviewed == expected and type(reviewed.get("value")) is type(payload[field]) and all(not isinstance(shape.args[k].type, GraphQLNonNull)
                                         and shape.args[k].default_value is Undefined for k in omitted)):
            return logical_target, str(identity), root.name.value
    _deny()


def _mounted_receipts(directory):
    """A writable container layer or tmpfs loses spent permits when the container is replaced."""
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount, filesystem = line.split(" - ", 1)
        if mount.split()[4] == os.fspath(directory):
            return filesystem.split()[0] not in {"overlay", "tmpfs", "ramfs"}
    return False


def _claim(permit_id):
    """Spend a globally unique permit before transport, even on timeout or process death."""
    if UUID(permit_id).hex != permit_id or not _mounted_receipts(_RECEIPTS):
        _deny()
    descriptor = os.open(_RECEIPTS, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
    try:
        # Provision this directory once on a persistent volume shared by every sending worker.
        # It must survive site restores and container replacement independently of the database.
        with os.fdopen(os.open(permit_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                               0o600, dir_fd=descriptor), "wb") as marker:
            os.fsync(marker.fileno())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _spend_campaign_permit(source, actor, target, identity, operation, document, variables):
    from hero_integration.services.test_environment import environment_generation, require_environment
    permit = _config(_PERMIT)
    expected = {**source, "actor": actor, "target": target, "id": identity, "operation": operation,
                "wire_sha256": wire_digest(document, variables)}
    if (not isinstance(permit, dict) or set(permit) != set(expected) | {"permit_id", "issued_at", "expires_at"}
            or any(permit[k] != v for k, v in expected.items())
            or type(permit["issued_at"]) is not int or type(permit["expires_at"]) is not int
            or not permit["issued_at"] <= time.time() < permit["expires_at"]
            or permit["expires_at"] - permit["issued_at"] > 900):
        _deny()
    if require_environment() != source["environment"] or environment_generation() != source["generation"]:
        _deny()
    _claim(permit["permit_id"])


@contextmanager
def worker_update(request, audit, source, bound_fields):
    """Bind the final materialized request to the current worker event and its receipt."""
    from hero_integration.services.automatic_writeback import dispatch_selection
    plan, selected = dispatch_selection()
    if plan is None or selected is None:
        yield
        return
    token = _WORKER_REQUEST.set((plan, selected, audit, source, bound_fields,
                                 wire_digest(request.document, request.variables)))
    try:
        yield
    finally:
        _WORKER_REQUEST.reset(token)


def _authorize_worker(context, document, variables, endpoint):
    import frappe
    from hero_integration import api
    from hero_integration.services import automatic_writeback
    from hero_integration.services.binding_registry import FrappeBindingStorage
    from hero_integration.services.test_environment import STATE, require_environment, environment_generation
    from hero_integration.services.writeback_source import validate_source

    plan, (event, mapping), audit, source, bound_fields, digest = context
    if automatic_writeback.dispatch_selection() != (plan, (event, mapping)) or digest != wire_digest(document, variables):
        _deny()
    automatic_writeback.validate_dispatch(frappe)
    config = validate_source(source)
    selected = plan.selection
    if (config.endpoint != endpoint or source.source_instance != selected.source_instance
            or selected.site_scope != frappe.local.site or frappe.session.user != "Administrator"
            or event.technical_action not in ("frappe_change_observed", "frappe_related_change_observed")):
        _deny()
    proof = json.loads(audit.intent_json)
    version = proof.get("version")
    if version not in (1, 2, 3) or (version == 3 and proof.get("mode") != "UPDATE"):
        _deny()
    if version == 1:
        if proof["plan"] != plan.plan_key or proof["generation"] != plan.generation:
            _deny()
        mapping_revision = proof["mutation_revision"]
    else:
        mapping_revision = proof["mapping_revision"]
    if ((proof.get("mutation_mapping") or proof.get("mapping")) != mapping
            or proof.get("object") != audit.bound_object_key or not audit.bound_object_key
            or proof.get("snapshot") != selected.schema_fingerprint
            or not frappe.db.exists("HERO GraphQL Mapping", {
                "name": mapping, "mapping_revision": mapping_revision, "status": "Active",
                "direction": "FRAPPE_TO_MUTATION", "operation_kind": "MUTATION",
                "schema_snapshot": selected.schema_snapshot, "source_instance": source.source_instance,
                "operation_name": audit.operation_name})):
        _deny()
    parent, fields = bound_fields()
    storage = FrappeBindingStorage(frappe)
    if (not parent or parent.identity_key != event.correlation_id or parent.lifecycle != "VALID"
            or parent.reconciliation or parent.site_scope != selected.site_scope
            or parent.source_instance != source.source_instance or parent.target_name != event.target_name
            or parent.target_doctype != event.target_doctype or not fields
            or storage.object(selected.site_scope, parent.identity_key) != parent):
        _deny()
    for field in fields:
        if (not field or field.object_binding_key != parent.identity_key or field.reconciliation
                or field.state not in ("SYNCHRONIZED", "LOCAL_CHANGED") or not field.acknowledged_version
                or not field.acknowledged_source_fingerprint or not field.acknowledged_target_fingerprint
                or storage.field(selected.site_scope, field.identity_key) != field):
            _deny()
    if not frappe.db.exists("HERO Mutation Audit", {
            "correlation_id": audit.correlation_id, "site": selected.site_scope, "user": frappe.session.user,
            "status": "Approved", "reservation_key": "active", "origin_event": event.name,
            "event_action_key": api._event_action_key(event.name, mapping), "delivery_stage": "Dispatching",
            "schema_fingerprint": selected.schema_fingerprint, "operation_name": audit.operation_name,
            "bound_object_key": audit.bound_object_key, "pending_object_key": audit.bound_object_key}):
        _deny()
    settings = frappe.get_single("HERO Integration Settings")
    if (settings.testing_enabled or frappe.conf.get("allow_tests") or frappe.conf.get("hero_exclusive_test_environment")
            or selected.site_scope.startswith("hero-test-") or STATE.exists() or _SOURCE.exists() or _PERMIT.exists()):
        campaign = {"environment": require_environment(), "generation": environment_generation(),
                    "site": selected.site_scope, "source_instance": source.source_instance,
                    "binding_revision": source.binding_revision, "endpoint_fingerprint": source.endpoint_fingerprint,
                    "schema": selected.schema_fingerprint}
        target, identity = parent.target_doctype, str(parent.external_object_id)
        if _config(_SOURCE) != {**campaign, "targets": {target: int(identity)}}:
            _deny()
        _spend_campaign_permit(campaign, frappe.session.user, target, identity, audit.operation_name, document, variables)
    return config.api_token


def authorize(document, variables, endpoint):
    """Authorize one serialized call through its worker or manual service authority."""
    context = _CONFIRMATION.get()
    worker = _WORKER_REQUEST.get()
    if worker is not None:
        _WORKER_REQUEST.set(None)  # One materialization admits at most one transport attempt.
        try:
            return _authorize_worker(worker, document, variables, endpoint)
        except Exception:
            _deny()
    if context is None:
        _deny()
    try:
        import frappe
        from hero_integration.services.test_environment import environment_generation, require_environment
        from hero_integration.services.writeback_source import _current

        audit, snapshot = context
        actor = frappe.session.user
        rpc = "hero_integration.api.execute_graphql_service"
        if (frappe.request.method != "POST" or frappe.request.path not in
                {f"{prefix}/method/{rpc}" for prefix in ("/api", "/api/v1", "/api/v2")}):
            _deny()
        frappe.only_for("System Manager")
        environment, generation = require_environment(), environment_generation()
        current, binding = _current()
        if (current.endpoint != endpoint or snapshot.source_instance != binding.source_instance
                or snapshot.endpoint_fingerprint != binding.endpoint_fingerprint
                or snapshot.analysis_status != "Validated" or snapshot.execution_contract_status == "Blocked"):
            _deny()
        policy = json.loads(_POLICY.read_text())
        target, identity, operation = _target(document, variables, policy, snapshot)
        source = {"environment": environment, "generation": generation, "site": frappe.local.site,
                  "source_instance": binding.source_instance, "binding_revision": binding.binding_revision,
                  "endpoint_fingerprint": binding.endpoint_fingerprint, "schema": snapshot.schema_fingerprint}
        if _config(_SOURCE) != {**source, "targets": policy["targets"]}:
            _deny()
        if not frappe.db.exists("HERO Mutation Audit", {"correlation_id": audit.correlation_id, "status": "Approved",
                "reservation_key": "active", "site": frappe.local.site, "user": actor, "schema_fingerprint": snapshot.schema_fingerprint}):
            _deny()
        _spend_campaign_permit(source, actor, target, identity, operation, document, variables)
        return current.api_token
    except HeroClientError:
        raise
    except Exception:
        # Source/schema/configuration/storage errors fail closed, without request payloads in diagnostics.
        _deny()
