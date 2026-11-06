"""Retired editor RPCs stay absent while configuration exchange remains available."""
import unittest
from pathlib import Path
from hero_integration import api


class CandidatePreviewValidationTest(unittest.TestCase):
    def test_old_editor_endpoints_are_removed(self):
        retired = ('mapping_object_catalog', 'mapping_source_picker', 'mapping_source_picker_page', 'mapping_suggestions', 'mapping_target_catalog', 'mapping_target_catalog_page', 'mapping_target_picker', 'mapping_target_picker_page', 'preview_direct_mapping', 'preview_mapping_candidate', 'writeback_mapper_catalog')
        scripts = Path(__file__).resolve().parents[1] / 'public' / 'js'
        frontend = '\n'.join(path.read_text() for path in scripts.glob('*.js'))
        for name in retired:
            with self.subTest(name=name):
                self.assertFalse(hasattr(api, name))
                self.assertNotIn(name, frontend)

    def test_configuration_import_export_remain_available(self):
        for name in ('simple_mapping_export', 'simple_mapping_import_preview', 'simple_mapping_import_apply',
                     'export_mapping_json', 'preview_mapping_import', 'import_mapping_json'):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(api, name)))
