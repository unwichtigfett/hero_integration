"""Checks for the app's Frappe translation catalog."""

import ast
import csv
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1]


class TestGermanCatalog(unittest.TestCase):
    def test_parameterized_errors_translate_only_at_the_display_boundary(self):
        from hero_integration.services.localized_error import LocalizedError, LocalizedRuntimeError, translate_error
        from hero_integration.services.node_bookmark import NodeBookmarkError, normalize_alias

        with self.assertRaises(NodeBookmarkError) as caught:
            normalize_alias("x" * 81)
        error = caught.exception
        self.assertEqual(str(error), "Alias must not exceed 80 characters.")
        catalog = {"Alias must not exceed {0} characters.": "Der Alias darf höchstens {0} Zeichen lang sein."}
        translate = lambda message: catalog.get(message, message)
        self.assertEqual(translate_error(error, translate), "Der Alias darf höchstens 80 Zeichen lang sein.")
        wrapped = LocalizedError.from_error(error)
        self.assertEqual(str(wrapped), str(error))
        self.assertEqual(translate_error(wrapped, translate), "Der Alias darf höchstens 80 Zeichen lang sein.")
        self.assertEqual(translate_error(ValueError("Unknown error"), translate), "Unknown error")
        self.assertEqual(translate_error(error, lambda _message: "Fehler ohne Platzhalter"), str(error))
        self.assertEqual(translate_error(error, lambda _message: "Fehler {"), str(error))
        runtime = LocalizedRuntimeError("Alias must not exceed {0} characters.", 80)
        self.assertIsInstance(runtime, RuntimeError)
        self.assertEqual(translate_error(runtime, translate), "Der Alias darf höchstens 80 Zeichen lang sein.")

    def test_graphql_error_preserves_code_detail_in_both_languages(self):
        from hero_integration.services.localized_error import translate_error
        from hero_integration.services.query_draft import QueryDraftError, _tokens

        with self.assertRaises(QueryDraftError) as caught:
            _tokens("'")
        error = caught.exception
        self.assertEqual(str(error), 'Unsupported GraphQL character "\'".')
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        self.assertEqual(translate_error(error, lambda message: messages.get(message, message)),
                         'Nicht unterstütztes GraphQL-Zeichen "\'".')

    def test_catalog_is_valid_and_preserves_placeholders(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        seen = set()
        for row in rows:
            self.assertEqual(len(row), 2)
            source, translation = row
            self.assertTrue(source and translation)
            self.assertNotIn(source, seen)
            seen.add(source)
            placeholder = r"\{(\w+)(![rsa])?(?::([^{}]+))?\}"
            self.assertEqual(sorted(re.findall(placeholder, source)),
                             sorted(re.findall(placeholder, translation)))
            for term in ("mapping", "polling"):
                if term in source.lower():
                    self.assertIn(term, translation.lower(), source)
        self.assertIn("Polling Settings", seen)
        self.assertIn("Configuration package", seen)

    def test_frappe_settings_and_domain_terms(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        self.assertEqual(messages["HERO Settings"], "HERO-Einstellungen")
        self.assertIn("Polling", messages["Polling Settings"])
        self.assertIn("Mapping", messages["Active Mappings"])
        self.assertIn("{0}", messages["Source binding confirmed (revision {0})."])

    def test_python_frappe_translation_calls_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        missing = []
        for path in APP.rglob("*.py"):
            if "tests" in path.relative_to(APP).parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"frappe", "frappe_api"} and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                    and node.args[0].value not in messages):
                    missing.append(f"{path.relative_to(APP)}:{node.lineno}: {node.args[0].value}")
        self.assertEqual(missing, [])

    def test_static_javascript_translation_calls_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        # These literal calls are a useful minimum; dynamic labels and raw DOM
        # text still need their own review and runtime tests.
        pattern = re.compile(
            r"""(?<![\w.$])(?:tr|text|uiText|__|format)\(\s*(?P<literal>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
        )
        missing = []
        for path in (APP / "public" / "js").glob("*.js"):
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                source = ast.literal_eval(match.group("literal"))
                if source not in messages:
                    missing.append(f"{path.name}: {source}")
        self.assertEqual(missing, [])

    def test_static_browser_errors_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        pattern = re.compile(r"""\bnew\s+Error\(\s*(?P<literal>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
        missing = []
        for path in (APP / "public" / "js").glob("*.js"):
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                source = ast.literal_eval(match.group('literal'))
                if source and source not in messages:
                    missing.append(f"{path.name}: {source}")
        self.assertEqual(missing, [])

    def test_mapping_editor_direct_labels_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        source = (APP / "public" / "js" / "hero_mapping_editor.js").read_text(encoding="utf-8")
        pattern = re.compile(r"""(?<![\w.$])(?:button|field|modal|say)\(\s*(?P<literal>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
        missing = [ast.literal_eval(match.group('literal')) for match in pattern.finditer(source)
                   if ast.literal_eval(match.group('literal')) and ast.literal_eval(match.group('literal')) not in messages]
        self.assertEqual(missing, [])

    def test_api_exposed_service_error_literals_have_german_entries(self):
        # These exception classes are passed to Frappe's translator at the API
        # boundary. Dynamic messages and other exception types need separate review.
        exposed = {"RuntimeBridgeError", "SyncError", "RuntimePlanConflict", "RuntimePlanError",
                   "GraphQLContractError", "NodeBookmarkError", "QueryDraftError", "MappingExchangeError",
                   "SchemaProvisioningError", "ProjectSendError", "TaskSendError", "CustomerSendError",
                   "SchemaOperationError", "SchemaRefreshError", "ReadContractError", "MappingPlanError",
                   "WritebackContractError", "MappingDraftExchangeError", "ConfigExchangeError"}
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        missing = []
        for path in APP.rglob("*.py"):
            if "tests" in path.relative_to(APP).parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name) and node.exc.func.id in exposed
                    and node.exc.args and isinstance(node.exc.args[0], ast.Constant)
                    and isinstance(node.exc.args[0].value, str) and node.exc.args[0].value not in messages):
                    missing.append(f"{path.relative_to(APP)}:{node.lineno}: {node.exc.args[0].value}")
        self.assertEqual(missing, [])

    def test_doctype_labels_descriptions_and_select_options_have_german_entries(self):
        import json

        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        missing = []
        for path in (APP / "hero_integration" / "doctype").glob("*/*.json"):
            doctype = json.loads(path.read_text(encoding="utf-8"))
            for field in doctype.get("fields", []):
                texts = [field.get("label", ""), field.get("description", "")]
                if field.get("fieldtype") == "Select":
                    texts.extend((field.get("options") or "").split("\n"))
                for text in texts:
                    if text and text.strip() and text not in messages:
                        missing.append(f"{doctype['name']}.{field.get('fieldname')}: {text}")
        self.assertEqual(missing, [])

    def test_static_migration_results_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        path = APP / "services" / "migration.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        missing = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Result" and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                and node.args[1].value not in messages):
                missing.append(f"{node.lineno}: {node.args[1].value}")
        self.assertEqual(missing, [])

    def test_frappe_catalog_loader_and_language_precedence(self):
        try:
            import frappe
            from frappe.translate import get_translation_dict_from_file, get_user_lang
        except ModuleNotFoundError:
            self.skipTest("Frappe is not installed")
        catalog = get_translation_dict_from_file(str(APP / "translations" / "de.csv"), "de", "hero_integration", throw=True)
        self.assertEqual(catalog["HERO Settings"], "HERO-Einstellungen")
        with patch.object(frappe, "local", SimpleNamespace(lang="en")), \
             patch.object(frappe, "get_cached_value", return_value="de"), \
             patch.object(frappe, "get_system_settings", return_value="en"):
            self.assertEqual(get_user_lang("synthetic@example.invalid"), "de")
        with patch.object(frappe, "local", SimpleNamespace(lang="en")), \
             patch.object(frappe, "get_cached_value", return_value=None), \
             patch.object(frappe, "get_system_settings", return_value="de"):
            self.assertEqual(get_user_lang("synthetic@example.invalid"), "de")
        # Exercise the installed framework translator, not just the CSV parser.
        with patch.object(frappe, "local", SimpleNamespace(lang="de")), \
             patch("frappe.translate.get_all_translations", side_effect=lambda lang: catalog if lang == "de" else {}):
            self.assertEqual(frappe._("HERO Settings"), "HERO-Einstellungen")
            self.assertEqual(frappe._("HERO Settings", lang="en"), "HERO Settings")
            self.assertEqual(frappe._("A missing translation"), "A missing translation")


if __name__ == "__main__":
    unittest.main()
