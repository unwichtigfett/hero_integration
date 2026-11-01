from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hero_integration.services.controlled_schema_provisioning import ProvisioningState
from hero_integration.services.frappe_schema_provisioning_audit import FrappeProvisioningAuditStore
from hero_integration.services.schema_provisioning_lifecycle import ProvisioningLifecycleError
from hero_integration.tests.test_schema_provisioning_lifecycle import approval, plan


class FakeFrappe:
    def __init__(self):
        self.local = SimpleNamespace(site="site.test")
        self.session = SimpleNamespace(user="admin")
        self.docs = {}
        self.commits = 0
        self.db = SimpleNamespace(commit=self._commit)

    def _commit(self):
        self.commits += 1

    def has_permission(self, *args, **kwargs):
        return True

    def get_roles(self):
        return ("System Manager",)

    def get_doc(self, *args):
        api = self
        if len(args) == 1:
            value = args[0]
            class NewDoc:
                def __init__(self):
                    self.name = value["name"]
                    self.site_scope = value["site_scope"]
                    self.lifecycle_state = value["lifecycle_state"]
                    self.audit_payload_json = value["audit_payload_json"]
                def save(self):
                    api.docs[self.name] = self
                def insert(self):
                    api.docs[self.name] = self
            return NewDoc()
        return self.docs[args[1]]


def test_source_audit_adapter_reserves_before_outcomes_and_finalization():
    api = FakeFrappe()
    store = FrappeProvisioningAuditStore(api)
    value = plan()
    audit_id = store.reserve(value, approval(value))
    store.record(audit_id, value.operations[0].operation_id, "APPLIED")
    store.finalize(audit_id, ProvisioningState.APPLIED)
    assert api.docs[audit_id].lifecycle_state == "APPLIED"
    assert api.commits == 3


def test_audit_rejects_actor_or_site_substitution_without_payload_leakage():
    api = FakeFrappe()
    store = FrappeProvisioningAuditStore(api)
    value = plan()
    with pytest.raises(ProvisioningLifecycleError):
        store.reserve(value, replace(approval(value), actor="other"))
    assert api.docs == {}
    audit_id = store.reserve(value, approval(value))
    api.local.site = "other.site"
    with pytest.raises(ProvisioningLifecycleError):
        store.record(audit_id, value.operations[0].operation_id, "APPLIED")


def test_audit_reservation_reports_commit_failure():
    api = FakeFrappe()

    def fail_commit():
        raise RuntimeError("synthetic commit failure")

    api.db.commit = fail_commit
    with pytest.raises(ProvisioningLifecycleError, match="transaction boundary failed"):
        FrappeProvisioningAuditStore(api).reserve(plan(), approval(plan()))


def test_finalized_audit_rejects_new_outcomes_and_a_different_terminal_state():
    api = FakeFrappe()
    store = FrappeProvisioningAuditStore(api)
    value = plan()
    audit_id = store.reserve(value, approval(value))
    store.record(audit_id, value.operations[0].operation_id, "APPLIED")
    store.finalize(audit_id, ProvisioningState.APPLIED)
    payload = api.docs[audit_id].audit_payload_json
    commits = api.commits

    with pytest.raises(ProvisioningLifecycleError, match="finalized"):
        store.record(audit_id, "synthetic-late-operation", "APPLIED")
    with pytest.raises(ProvisioningLifecycleError, match="immutable"):
        store.finalize(audit_id, ProvisioningState.PARTIAL)

    assert api.docs[audit_id].lifecycle_state == "APPLIED"
    assert api.docs[audit_id].audit_payload_json == payload
    assert api.commits == commits
