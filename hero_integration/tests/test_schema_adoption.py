from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock

from hero_integration.services.runtime_plan import (
    InMemoryRuntimePlanAudit, InMemoryRuntimePlanStorage, RuntimePlanConflict, RuntimePlanService,
)
from hero_integration.tests.test_runtime_plan import selection
from hero_integration.services.schema_adoption import is_confirmed, read_scope, validate_read_document
from hero_integration.tests.test_mapping_schema_compatibility import raw
from hero_integration.services.mapping_schema_compatibility import MappingSchemaCompatibilityError
import json


class SchemaAdoptionTests(TestCase):
    def test_same_contract_renews_once_and_old_generation_fails(self):
        service = RuntimePlanService(InMemoryRuntimePlanStorage(), Mock(), InMemoryRuntimePlanAudit(), Mock())
        first = selection()
        other = replace(first, graphql_service="reader-two", graphql_mapping="map-two")
        old = service.create_draft(first, additional_readers=[other], actor="manager")
        old = service.approve(old.plan_key, expected_revision=old.revision, actor="manager")
        current = service.adopt_validated_schemas(old, {other.graphql_mapping}, "schema-next", actor="manager")
        self.assertEqual(current.selection, first)
        self.assertEqual(current.additional_readers, (replace(other, validated_snapshot="schema-next"),))
        self.assertIs(service.adopt_validated_schemas(current, {other.graphql_mapping}, "schema-next", actor="manager"), current)
        with self.assertRaises(RuntimePlanConflict):
            service.resolve_executable_plan(first.site_scope, first.source_instance, old.generation, True, "manager")
        self.assertEqual(old.additional_readers, (other,))

    def test_no_receipt_for_other_schema_or_other_generation(self):
        db = Mock()
        self.assertFalse(is_confirmed(db, {"name": "new"}, selection()))
        db.get_doc.assert_not_called()

    def test_auxiliary_read_rejects_incompatible_field(self):
        db = Mock()
        from hero_integration.tests.test_mapping_schema_compatibility import SDL
        schemas = [raw(), raw(SDL.replace("extra: String", "extra: Int"))]
        db.get_doc.side_effect = [Mock(snapshot_json=json.dumps(s)) for s in schemas]
        with read_scope(db, replace(selection(), validated_snapshot="schema-new")):
            validate_read_document("{ tasks { title } }")
            with self.assertRaises(MappingSchemaCompatibilityError):
                validate_read_document("{ tasks { extra } }")

    def test_changed_mapping_cannot_inherit_prior_semantic_approval(self):
        from types import SimpleNamespace
        from hero_integration.services.schema_adoption import _receipt_key, _require_same_lineage
        mapping = SimpleNamespace(name="approved-mapping", schema_snapshot="origin")
        inputs = (mapping.name, {"map": "original-rules"}, [["sealed-state", "reader"]])
        db = Mock()
        db.get_all.return_value = [SimpleNamespace(child_snapshot="first-adoption",
            name=_receipt_key("first-adoption", "fingerprint", inputs))]
        db.db.get_value.return_value = "fingerprint"
        _require_same_lineage(db, mapping, inputs)
        with self.assertRaisesRegex(ValueError, "Approved mapping graph changed"):
            _require_same_lineage(db, mapping, (mapping.name, {"map": "different-rules"}, inputs[2]))
