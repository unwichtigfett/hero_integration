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
            'frappeTranslate': str(Path(frappe.__file__).parent / 'public' / 'js' / 'frappe' / 'translate.js'),
            'jsDir': str(APP / 'public' / 'js'),
        }
        result = subprocess.run(['node', str(APP / 'tests' / 'fixtures' / 'frappe_i18n_bridge.js')],
                                input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
