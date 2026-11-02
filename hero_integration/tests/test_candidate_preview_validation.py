import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration import api
from hero_integration.services import frappe_mapping_catalog


class CandidatePreviewValidationTest(unittest.TestCase):
    def test_invalid_target_is_reported_as_a_validation_error(self):
        class ValidationError(Exception):
            pass
        def throw(message, *args):
            raise ValidationError(message)
        frappe = SimpleNamespace(only_for=lambda role: None, throw=throw, _=lambda message: message)
        with patch.object(api, 'frappe', frappe), patch.object(api, '_validated_snapshot', return_value=(None, {})), \
             patch.object(api, '_FrappePickerMetadata', return_value=object()), \
             patch.object(frappe_mapping_catalog, 'validate_draft_target', side_effect=ValueError('Target unavailable')):
            with self.assertRaisesRegex(ValidationError, 'Target unavailable'):
                api.preview_mapping_candidate('SYNTHETIC', 'HeroCustomers', 'id', 'customer_name', '{"id": 1}', 'Customer')
