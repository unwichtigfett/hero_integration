"""Cross-layer language smoke test using Frappe's real Python and JS translators."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1]


class TestFrappeTranslationBridge(unittest.TestCase):
    def test_notification_language_uses_recipient_override_or_system_default(self):
        try:
            import frappe
            from frappe.translate import get_translation_dict_from_file
        except ModuleNotFoundError:
            self.skipTest('Frappe is not installed')
        from hero_integration.services.conflict_notifications import recipient_text

        catalog = get_translation_dict_from_file(str(APP / 'translations' / 'de.csv'), 'de', 'hero_integration', throw=True)
        with patch.object(frappe, 'local', SimpleNamespace(lang='en')), \
             patch.object(frappe, 'get_system_settings', return_value='de'), \
             patch.object(frappe, 'get_cached_value', side_effect=lambda _doctype, user, _field: 'en' if user == 'en@example.invalid' else None), \
             patch('frappe.translate.get_all_translations', side_effect=lambda lang: catalog if lang == 'de' else {}):
            self.assertEqual(recipient_text('de@example.invalid', 'HERO polling status'), 'HERO-Polling-Status')
            self.assertEqual(recipient_text('en@example.invalid', 'HERO polling status'), 'HERO polling status')

    def test_framework_catalog_reaches_workspace_controls_in_both_languages(self):
        if not shutil.which('node'):
            self.skipTest('Node.js is not installed')
        try:
            import frappe
            from frappe.boot import load_translations
            from frappe.translate import get_translations_from_apps, get_user_lang
        except ModuleNotFoundError:
            self.skipTest('Frappe is not installed')
        # Resolve the same precedence used by Frappe session startup: a user
        # override wins; an empty user language falls back to System Settings.
        with patch.object(frappe, 'local', SimpleNamespace(lang='en')), \
             patch.object(frappe, 'get_system_settings', return_value='de'):
            with patch.object(frappe, 'get_cached_value', return_value=None):
                global_language = get_user_lang('synthetic@example.invalid')
            with patch.object(frappe, 'get_cached_value', return_value='en'):
                user_language = get_user_lang('synthetic@example.invalid')
        self.assertEqual((global_language, user_language), ('de', 'en'))
        languages = [global_language, user_language, 'de-DE']
        from hero_integration.services.business_numbers import BusinessNumberError, business_number
        from hero_integration.services.project_naming import ProjectTitleValidationError, project_trade

        try:
            business_number('bad/123')
        except BusinessNumberError as error:
            number_error = str(error)
        else:
            self.fail('Invalid HERO number was unexpectedly accepted')
        try:
            project_trade({'project_type': 'NEW'})
        except ProjectTitleValidationError as error:
            trade_error = str(error)
        else:
            self.fail('Unknown project trade was unexpectedly accepted')
        boot_catalogs = {}
        for language in languages:
            catalog = get_translations_from_apps(language, apps=['hero_integration'])
            boot = {}
            # Use the real Frappe boot assembly and the real app catalog; the
            # cache boundary is supplied in memory, so no site is contacted.
            with patch.object(frappe, 'local', SimpleNamespace(lang=language)), \
                 patch.object(frappe, 'lang', language), \
                 patch('frappe.translate.get_all_translations', return_value=catalog):
                load_translations(boot)
            self.assertEqual(boot['lang'], language)
            boot_catalogs[language] = boot['__messages']
        payload = {
            'languages': languages,
            'catalogs': boot_catalogs,
            'numberError': number_error,
            'tradeError': trade_error,
            'frappeTranslate': str(Path(frappe.__file__).parent / 'public' / 'js' / 'frappe' / 'translate.js'),
            'jsDir': str(APP / 'public' / 'js'),
        }
        result = subprocess.run(['node', str(APP / 'tests' / 'fixtures' / 'frappe_i18n_bridge.js')],
                                input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
