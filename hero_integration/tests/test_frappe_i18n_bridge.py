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
        from hero_integration.services.conflict_notifications import recipient_diagnostic, recipient_text

        catalog = get_translation_dict_from_file(str(APP / 'translations' / 'de.csv'), 'de', 'hero_integration', throw=True)
        with patch.object(frappe, 'local', SimpleNamespace(lang='en')), \
             patch.object(frappe, 'get_system_settings', return_value='de'), \
             patch.object(frappe, 'get_cached_value', side_effect=lambda _doctype, user, _field: 'en' if user == 'en@example.invalid' else None), \
             patch('frappe.translate.get_all_translations', side_effect=lambda lang: catalog if lang == 'de' else {}):
            self.assertEqual(recipient_text('de@example.invalid', 'HERO polling status'), 'HERO-Polling-Status')
            self.assertEqual(recipient_text('en@example.invalid', 'HERO polling status'), 'HERO polling status')
            polling = 'Materialized monitoring observed one bounded read page; full coverage is not claimed and no business records were written.'
            self.assertEqual(recipient_diagnostic('en@example.invalid', polling), polling)
            self.assertEqual(recipient_diagnostic('de@example.invalid', polling),
                             'Das materialisierte Monitoring hat eine begrenzte Leseseite beobachtet; vollständige Abdeckung wird nicht behauptet und es wurden keine Geschäftsdaten geschrieben.')
            blocked = 'Materialized monitoring is blocked: HERO pagination repeated an offset; monitoring stopped safely.'
            self.assertEqual(recipient_diagnostic('de@example.invalid', blocked),
                             'Materialisiertes Monitoring ist blockiert: Die HERO-Seitennavigation hat einen Offset wiederholt; das Monitoring wurde sicher angehalten.')
            detail = 'Field conflict requires review: customer_name, custom_id.'
            self.assertEqual(recipient_diagnostic('de@example.invalid', detail), 'Feldkonflikt erfordert Prüfung: customer_name, custom_id.')
            self.assertEqual(recipient_diagnostic('en@example.invalid', detail), detail)
            nested = 'Recheck requested: Awaits administrator approval: ' + detail
            self.assertEqual(recipient_diagnostic('de@example.invalid', nested),
                             'Erneute Prüfung angefordert: Wartet auf Freigabe durch einen Administrator: Feldkonflikt erfordert Prüfung: customer_name, custom_id.')
            self.assertEqual(recipient_diagnostic('de@example.invalid', 'Unknown external detail'), 'Unknown external detail')
            missing = 'HERO response omits the source of: customer_type, phone.'
            self.assertEqual(recipient_diagnostic('en@example.invalid', missing), missing)
            self.assertEqual(recipient_diagnostic('de@example.invalid', missing),
                             'Die HERO-Antwort enthält keine Quelle für: customer_type, phone.')
            address = ('The project address is missing or incomplete '
                       '(ProjectMatch.address.city is required for the ERPNext project title.); the title omits it.')
            review = 'The HERO customer is deleted in HERO; the Project has no customer. ' + address
            expected = ('Der HERO-Kunde ist in HERO gelöscht; dem Projekt ist kein Kunde zugeordnet. '
                        'Die Projektadresse fehlt oder ist unvollständig '
                        '(ProjectMatch.address.city ist für den ERPNext-Projekttitel erforderlich.); sie wird im Titel ausgelassen.')
            self.assertEqual(recipient_diagnostic('de@example.invalid', review), expected)
            self.assertEqual(recipient_diagnostic('en@example.invalid', review), review)
            collision = ("Project 'P-42' already exists without this HERO record's link. The HERO number is not "
                         "reassigned, suffixed or merged; check whether 'P-42' is the same project and link or rename it.")
            self.assertEqual(recipient_diagnostic('en@example.invalid', collision), collision)
            self.assertEqual(recipient_diagnostic('de@example.invalid', collision),
                             "Projekt 'P-42' existiert bereits ohne Link zu diesem HERO-Datensatz. Die HERO-Nummer wird weder neu vergeben noch mit einem Zusatz versehen oder zusammengeführt; prüfen Sie, ob 'P-42' dasselbe Objekt (Projekt) ist, und verknüpfen oder benennen Sie es um.")
            drift = ("HERO customer number changed from 'old' to 'new'. Customer CUST-42 keeps its name and number "
                     "until reviewed; set custom_hero_customer_number to 'new' to accept the new number.")
            self.assertEqual(recipient_diagnostic('en@example.invalid', drift), drift)
            self.assertEqual(recipient_diagnostic('de@example.invalid', drift),
                             "HERO-Kundennummer wurde von 'old' auf 'new' geändert. Kunde CUST-42 behält seinen Namen und seine Nummer bis zur Prüfung; setzen Sie custom_hero_customer_number auf 'new', um die neue Nummer zu übernehmen.")

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
            'frappeFormScripts': str(Path(frappe.__file__).parent / 'public' / 'js' / 'frappe' / 'form' / 'script_manager.js'),
            'frappeFormat': str(Path(frappe.__file__).parent / 'public' / 'js' / 'frappe' / 'format.js'),
            'recordForm': str(APP / 'hero_integration' / 'doctype' / 'hero_integration_record' / 'hero_integration_record.js'),
            'jsDir': str(APP / 'public' / 'js'),
        }
        result = subprocess.run(['node', str(APP / 'tests' / 'fixtures' / 'frappe_i18n_bridge.js')],
                                input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
