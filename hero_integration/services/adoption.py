"""Persist and resume explicit schema-and-mapping adoption requests."""
from __future__ import annotations

import hashlib
import hmac
import json

import frappe

from hero_integration.hero_integration.doctype.hero_mapping_adoption.hero_mapping_adoption import service_write_context
from hero_integration.services.schema_provisioning import (
    SchemaProvisioningError, adopt_package, preview_package, revalidate_adoption_preview,
)


def start(snapshot_name: str, payload_json: str, selected_ids: list[str], preview_revision: str) -> dict[str, object]:
    frappe.only_for("System Manager")
    preview = preview_package(snapshot_name, payload_json)
    if not preview_revision or preview["preview_revision"] != preview_revision:
        raise SchemaProvisioningError("Package preview is stale or missing; preview again.")
    selection = {"ids": selected_ids, "preview": preview}
    _binding(snapshot_name, payload_json, selection, "")  # Fail before insert if the site key is unavailable.
    adoption = frappe.get_doc({
        "doctype": "HERO Mapping Adoption",
        "schema_snapshot": snapshot_name,
        "selected_ids_json": json.dumps({**selection, "binding": ""}, sort_keys=True, separators=(",", ":")),
        "package_json": payload_json,
        "status": "Queued",
    })
    with service_write_context():
        adoption.insert()
        binding = _binding(snapshot_name, payload_json, selection, adoption.name)
        adoption.selected_ids_json = json.dumps({**selection, "binding": binding}, sort_keys=True, separators=(",", ":"))
        adoption.save()
    return execute(adoption.name)


def execute(adoption_name: str) -> dict[str, object]:
    frappe.only_for("System Manager")
    adoption = frappe.get_doc("HERO Mapping Adoption", adoption_name)
    if adoption.owner != frappe.session.user:
        raise SchemaProvisioningError("Only the adoption owner may resume it.")
    try:
        selection = json.loads(adoption.selected_ids_json)
    except (TypeError, ValueError) as error:
        raise SchemaProvisioningError("Legacy adoption requires a new preview.") from error
    if adoption.status == "Completed" and isinstance(selection, list) and all(isinstance(item, str) for item in selection):
        outcome = json.loads(adoption.outcome_json or "{}")
        if not isinstance(outcome, dict):
            raise SchemaProvisioningError("Legacy adoption outcome is invalid.")
        return {**outcome, "adoption_name": adoption.name, "confirmation_binding": "LEGACY_UNVERIFIED"}
    if not isinstance(selection, dict) or set(selection) != {"ids", "preview", "binding"}:
        raise SchemaProvisioningError("Legacy adoption requires a new preview.")
    expected = _binding(adoption.schema_snapshot, adoption.package_json, {"ids": selection["ids"], "preview": selection["preview"]}, adoption.name)
    if not isinstance(selection["binding"], str) or not hmac.compare_digest(selection["binding"], expected):
        raise SchemaProvisioningError("Adoption confirmation is invalid; preview again.")
    if adoption.status == "Completed":
        outcome = json.loads(adoption.outcome_json or "{}")
        return {"adoption_name": adoption.name, **outcome}
    try:
        revision = revalidate_adoption_preview(adoption.schema_snapshot, adoption.package_json, selection["ids"], selection["preview"])
        result = adopt_package(adoption.schema_snapshot, adoption.package_json, selection["ids"], confirmed=True, preview_revision=revision)
    except Exception as error:
        adoption.status = "Failed"
        adoption.error_message = str(error)
        with service_write_context():
            adoption.save()
        raise
    adoption.status = "Completed"
    adoption.outcome_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
    adoption.error_message = ""
    with service_write_context():
        adoption.save()
    return {"adoption_name": adoption.name, **result}


def _binding(snapshot_name, payload_json, selection, adoption_name):
    key = frappe.local.conf.get("encryption_key")
    if not isinstance(key, str) or not key:
        raise SchemaProvisioningError("Site encryption key is required for adoption confirmation.")
    payload = {"site": frappe.local.site, "actor": frappe.session.user, "adoption": adoption_name, "snapshot": snapshot_name,
               "package_sha256": hashlib.sha256(payload_json.encode()).hexdigest(), "selection": selection}
    message = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key.encode(), b"hero-mapping-adoption-v1\0" + message, hashlib.sha256).hexdigest()
