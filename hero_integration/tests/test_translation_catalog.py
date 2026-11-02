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
            self.assertEqual(sorted(re.findall(r"\{\w+\}", source)),
                             sorted(re.findall(r"\{\w+\}", translation)))
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


if __name__ == "__main__":
    unittest.main()
