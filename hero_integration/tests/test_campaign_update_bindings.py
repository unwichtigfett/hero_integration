from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import mapped_update_v2 as update
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.sync_engine import FrappeBoundProcessor


class CampaignUpdateBindings(TestCase):
    def supported(self, rule, fieldtype, read_only):
        field = SimpleNamespace(fieldtype=fieldtype, read_only=read_only, fetch_from=None,
                                is_virtual=False, permlevel=0)
        doc = SimpleNamespace(doctype=rule.target_doctype, meta=SimpleNamespace(get_field=lambda k: field),
                              get_permlevel_access=lambda mode: {0})
        return FrappeBoundProcessor.supports_scalar(rule, doc)

    def test_project_notes_use_current_editor_transform(self):
        rule = MappingSpec("HeroProjectMatches", "partner_notes", "Project", "notes", "string",
                           transformation="Safe HTML")
        self.assertTrue(self.supported(rule, "Text Editor", False))
        self.assertFalse(self.supported(replace(rule, source_path="unrelated"), "Text Editor", False))

    def test_file_label_excludes_storage_fields_and_unreviewed_mappings(self):
        rule = MappingSpec("HeroDocuments", "filename", "File", "file_name", "string",
                           purpose="Record Name", transformation="Trim")
        self.assertTrue(self.supported(rule, "Data", True))
        for changed in (replace(rule, target_field="file_url"), replace(rule, operation="HeroTasks"),
                        replace(rule, source_path="downloadUrl"), replace(rule, relation="Other")):
            self.assertFalse(self.supported(changed, "Data", True))

    def test_ambiguous_address_remains_held(self):
        local = SimpleNamespace(name="billing")
        ctx = SimpleNamespace(document=SimpleNamespace(doctype="Customer"),
                              proof=SimpleNamespace(source_instance="default"), source_documents={})
        item = {"id": "city", "source": {"doctype": "Address", "field": "city"}}
        with patch.object(update.send_helpers, "_source_document", return_value=local), \
                patch.object(update.frappe, "get_all", return_value=["first", "second"]), \
                self.assertRaises(update._SharedAddressBinding):
            update._source(ctx, item, {})
        self.assertIs(ctx.source_documents["city"], local)
