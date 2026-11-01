"""Offline checks for the Frappe approval adapter's evidence and replay fences."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hero_integration.services.frappe_schema_provisioning_approval import FrappeProvisioningApprovalStore
from hero_integration.services.schema_provisioning_lifecycle import ProvisioningLifecycleError
from hero_integration.tests.test_schema_provisioning_lifecycle import plan


class FakeFrappe:
    def __init__(self):
        self.local = SimpleNamespace(site="site.test")
        self.session = SimpleNamespace(user="admin")
        self.db = SimpleNamespace(sql=self.sql)
        self.documents = {}
        self.permissions = []
        self.queries = []

    def get_roles(self, *_args):
        return ["System Manager"]

    def has_permission(self, doctype, permission, throw=False):
        self.permissions.append((doctype, permission, throw))
        return True

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            document = SimpleNamespace(**doctype, name="APPROVAL-1")
            document.insert = lambda: self.documents.update({document.name: document})
            document.save = lambda: None
            return document
        return self.documents[name]

    def sql(self, query, values, **_kwargs):
        self.queries.append(query)
        return [SimpleNamespace(name=values[0])] if values[0] in self.documents else []


def test_approval_consumed_once_and_stale_replay_rejected():
    fake = FakeFrappe()
    store = FrappeProvisioningApprovalStore(fake, clock=lambda: 1.0)
    approved = store.issue(plan(), "synthetic-correlation", ttl_seconds=30)
    store.consume(plan(), approved, "admin", 2.0)
    assert fake.documents[approved.approval_id].approval_state == "CONSUMED"
    assert fake.documents[approved.approval_id].consumed_at_epoch == 2.0
    with pytest.raises(ProvisioningLifecycleError, match="not available"):
        store.consume(plan(), approved, "admin", 3.0)
    assert [permission[1] for permission in fake.permissions] == ["create", "write", "write"]
    assert all("for update" in query.lower() for query in fake.queries)


def test_tampered_or_expired_approval_does_not_change_stored_state():
    fake = FakeFrappe()
    store = FrappeProvisioningApprovalStore(fake, clock=lambda: 1.0)
    approved = store.issue(plan(), "synthetic-correlation", ttl_seconds=30)
    with pytest.raises(ProvisioningLifecycleError, match="stale, forged, expired"):
        store.consume(plan(), replace(approved, actor="other"), "admin", 2.0)
    with pytest.raises(ProvisioningLifecycleError, match="stale, forged, expired"):
        store.consume(plan(), approved, "admin", 31.0)
    assert fake.documents[approved.approval_id].approval_state == "APPROVED"


def test_revoked_manager_role_prevents_consumption_before_lock_lookup():
    fake = FakeFrappe()
    store = FrappeProvisioningApprovalStore(fake, clock=lambda: 1.0)
    approved = store.issue(plan(), "synthetic-correlation", ttl_seconds=30)
    fake.get_roles = lambda *_args: []

    with pytest.raises(PermissionError, match="System Manager"):
        store.consume(plan(), approved, "admin", 2.0)

    assert fake.queries == []
    assert fake.documents[approved.approval_id].approval_state == "APPROVED"
