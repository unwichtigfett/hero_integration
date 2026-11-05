"""Native contract upgrades require compatible saved queries without granting execution."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from hero_integration.services.frappe_mapping_draft_storage import FrappeMappingDraftStorage
from hero_integration.services.mapping_draft_service import MappingDraftError
from hero_integration.services.read_contract import from_documents
from hero_integration.sources.hero import project_query


class _Document(SimpleNamespace):
    def as_dict(self):
        return {key: value for key, value in vars(self).items() if key != "check_permission"}

    def save(self, *args, **kwargs):
        raise AssertionError("Publication validation must not persist approval.")


class TestNativePublicationReadContract(unittest.TestCase):
    def setUp(self):
        self.snapshot = _Document(name="SNAPSHOT", schema_fingerprint="fingerprint", source_instance="source")
        current = {
            "target_operation": "HeroProjectMatches", "records_path": "project_matches", "identity_path": "id",
            "mapping_definition_ids": ["project-name"], "project_contract_version": "v1",
            "variables": {"first": "$page_size", "offset": "$offset", "orderBy": "id"},
        }
        self.content = dict(current, project_contract_version="v5")
        self.parent = _Document(name="MAPPING", mapping_revision=4, status="Active", schema_snapshot="SNAPSHOT",
            schema_fingerprint="fingerprint", source_instance="source", operation_kind="QUERY",
            direction="QUERY_TO_FRAPPE", operation_name="project_matches", mapping_json=json.dumps(current))
        common = dict(status="Validated", schema_snapshot="SNAPSHOT", schema_fingerprint="fingerprint",
            operation_name="project_matches", service_revision=2)
        self.old_service = _Document(**common, name="OLD-SERVICE", modified_by="previous-owner",
            read_mapping="MAPPING", execution_status="Approved", approved_service_revision=2,
            approved_mapping_revision=4, query_document=project_query("v1"), check_permission=Mock())
        self.new_service = _Document(**common, name="NEW-SERVICE", modified_by="current-owner",
            read_mapping="", execution_status="Pending Approval", approved_service_revision=0,
            approved_mapping_revision=0, query_document=project_query("v5"), check_permission=Mock())
        self.services = [self.old_service, self.new_service]
        self.sql = Mock(side_effect=self._sql)
        self.frappe = SimpleNamespace(session=SimpleNamespace(user="current-owner"),
            db=SimpleNamespace(sql=self.sql), get_doc=self._get_doc)
        self.storage = FrappeMappingDraftStorage(self.frappe)
        self.storage._definition = self.parent

    def _sql(self, query, parameters, **kwargs):
        self.assertTrue(query.startswith("select "))
        self.assertIn("for update", query)
        if "where read_mapping=%s" in query:
            name, status = parameters
            selected = [row for row in self.services if row.read_mapping == name and row.status == status]
        else:
            self.assertIn("schema_snapshot=%s and status=%s and modified_by=%s", query)
            snapshot, status, actor = parameters
            selected = [row for row in self.services
                if (row.schema_snapshot, row.status, row.modified_by) == (snapshot, status, actor)]
        return [row.as_dict() for row in selected]

    def _get_doc(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            return next(row for row in self.services if row.name == doctype["name"])
        self.assertEqual((doctype, name), ("HERO GraphQL Schema Snapshot", "SNAPSHOT"))
        self.assertTrue(kwargs["for_update"])
        return self.snapshot

    def test_old_approval_does_not_cover_the_new_native_contract(self):
        # The old approval is valid for revision four, but its query omits V5 fields.
        self.assertEqual(from_documents(self.old_service, self.parent, self.snapshot).mapping_revision, 4)
        self.services = [self.old_service]
        with self.assertRaisesRegex(MappingDraftError, "matching validated read service"):
            self.storage._validate_publication_read_contract(self.content)
        self.assertEqual(self.old_service.approved_mapping_revision, 4)

    def test_own_validated_query_allows_publication_without_writing_approval(self):
        before = deepcopy([self.parent.as_dict(), self.old_service.as_dict(), self.new_service.as_dict()])
        self.storage._validate_publication_read_contract(self.content)
        self.assertEqual([self.parent.as_dict(), self.old_service.as_dict(), self.new_service.as_dict()], before)
        self.new_service.check_permission.assert_called_once_with("read")
        self.assertEqual(self.new_service.execution_status, "Pending Approval")
        self.assertEqual(self.new_service.approved_mapping_revision, 0)
        self.assertEqual(self.sql.call_count, 2)

    def test_incompatible_query_actor_snapshot_or_validation_stays_blocked(self):
        changes = {
            "query": {"query_document": project_query("v1")},
            "actor": {"modified_by": "another-administrator"},
            "snapshot": {"schema_snapshot": "OTHER-SNAPSHOT"},
            "fingerprint": {"schema_fingerprint": "other-fingerprint"},
            "validation": {"status": "Draft"},
        }
        original = self.new_service.as_dict()
        for reason, values in changes.items():
            with self.subTest(reason=reason):
                for key, value in original.items():
                    setattr(self.new_service, key, value)
                for key, value in values.items():
                    setattr(self.new_service, key, value)
                with self.assertRaisesRegex(MappingDraftError, "matching validated read service"):
                    self.storage._validate_publication_read_contract(self.content)
                self.assertEqual(self.new_service.execution_status, "Pending Approval")
                self.assertEqual(self.new_service.approved_mapping_revision, 0)

    def test_candidate_scope_is_rechecked_after_loading_the_document(self):
        # A result returned by SQL must not bypass current actor/snapshot checks.
        self.sql.side_effect = lambda query, *args, **kwargs: (
            [self.new_service.as_dict()] if "modified_by=%s" in query else [])
        original = self.new_service.as_dict()
        for field, value in (("modified_by", "other-owner"), ("schema_snapshot", "OTHER"),
                             ("schema_fingerprint", "other-fingerprint"), ("status", "Draft")):
            with self.subTest(field=field):
                for key, current in original.items():
                    setattr(self.new_service, key, current)
                setattr(self.new_service, field, value)
                with self.assertRaisesRegex(MappingDraftError, "matching validated read service"):
                    self.storage._validate_publication_read_contract(self.content)
                self.assertEqual(self.new_service.execution_status, "Pending Approval")

    def test_read_permission_failure_is_not_bypassed(self):
        self.new_service.check_permission.side_effect = PermissionError("private query")
        with self.assertRaisesRegex(PermissionError, "private query"):
            self.storage._validate_publication_read_contract(self.content)
        self.assertEqual(self.new_service.execution_status, "Pending Approval")


if __name__ == "__main__":
    unittest.main()
