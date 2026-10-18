"""Persist and resume explicit schema-and-mapping adoption requests."""
from __future__ import annotations

import json

import frappe

from hero_integration.services.schema_provisioning import adopt_package


def start(snapshot_name: str, payload_json: str, selected_ids: list[str]) -> dict[str, object]:
    adoption = frappe.get_doc({
        "doctype": "HERO Mapping Adoption",
        "schema_snapshot": snapshot_name,
        "selected_ids_json": json.dumps(selected_ids, sort_keys=True, separators=(",", ":")),
        "package_json": payload_json,
        "status": "Queued",
    })
    adoption.insert()
    return execute(adoption.name)


def execute(adoption_name: str) -> dict[str, object]:
    adoption = frappe.get_doc("HERO Mapping Adoption", adoption_name)
    frappe.only_for("System Manager")
    if adoption.status == "Completed":
        outcome = json.loads(adoption.outcome_json or "{}")
        return {"adoption_name": adoption.name, **outcome}
    try:
        selected_ids = json.loads(adoption.selected_ids_json)
        result = adopt_package(adoption.schema_snapshot, adoption.package_json, selected_ids, confirmed=True)
    except Exception as error:
        adoption.status = "Failed"
        adoption.error_message = str(error)
        adoption.save()
        raise
    adoption.status = "Completed"
    adoption.outcome_json = json.dumps(result, sort_keys=True, separators=(",", ":"))
    adoption.error_message = ""
    adoption.save()
    return {"adoption_name": adoption.name, **result}
