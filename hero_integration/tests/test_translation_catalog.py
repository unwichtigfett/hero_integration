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

    def test_package_setting_labels_match_exported_doctype_metadata(self):
        import json

        tree = ast.parse((APP / "services" / "config_exchange.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "SETTINGS_KEYS" for target in node.targets))
        keys = ast.literal_eval(assignment.value)
        doctype = json.loads((APP / "hero_integration" / "doctype" / "hero_integration_settings" / "hero_integration_settings.json").read_text(encoding="utf-8"))
        labels = {field.get("fieldname"): field.get("label") for field in doctype["fields"]}
        source = (APP / "public" / "js" / "hero_settings.js").read_text(encoding="utf-8")
        section = source.split("const packageSettingLabels = Object.freeze({", 1)[1].split("});", 1)[0]
        actual = dict(re.findall(r'^\s*(\w+): "([^"\n]+)",\s*$', section, re.MULTILINE))
        self.assertEqual(actual, {key: labels[key] for key in keys})

    def test_mapping_editor_literal_dialog_descriptions_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        source = (APP / "public" / "js" / "hero_mapping_editor.js").read_text(encoding="utf-8")
        literal = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
        pattern = re.compile(r"\bmodal\(\s*" + literal + r"\s*,\s*(?P<description>" + literal + r")")
        missing = [ast.literal_eval(match.group('description')) for match in pattern.finditer(source)
                   if ast.literal_eval(match.group('description')) not in messages]
        self.assertEqual(missing, [])

    def test_framework_extractable_messages_have_german_entries(self):
        try:
            from frappe.translate import extract_messages_from_python_code, extract_messages_from_javascript_code
            from frappe.gettext.extractors.utils import extract_messages_from_code
        except ModuleNotFoundError:
            self.skipTest("Frappe is not installed")
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        missing = []
        extracted_count = 0
        for path in APP.rglob("*"):
            if "tests" in path.relative_to(APP).parts or path.suffix not in {".py", ".js", ".html", ".vue"}:
                continue
            content = path.read_text(encoding="utf-8")
            extracted = extract_messages_from_python_code(content) if path.suffix == ".py" else extract_messages_from_code(content)
            if path.suffix == ".js":
                extracted += extract_messages_from_javascript_code(content)
            for line, source, context in extracted:
                if not isinstance(source, str) or not source:
                    continue
                extracted_count += 1
                if source not in messages and (not context or f"{source}:{context}" not in messages):
                    missing.append(f"{path.relative_to(APP)}:{line}: {source}")
        self.assertGreater(extracted_count, 0)
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

    def test_static_response_explanations_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        paths = [APP / "api.py"] + [APP / "services" / name for name in (
            "mapping_draft_exchange.py", "mapping_exchange.py", "snapshot_inheritance.py",
            "schema_operations.py", "personal_mapping_content.py", "node_bookmark.py")]
        missing = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value in {"message", "reason", "blocker", "detail", "description", "summary", "warning"}
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and len(value.value) > 20 and " " in value.value and value.value not in messages):
                        missing.append(f"{path.name}:{value.lineno}: {value.value}")
        self.assertEqual(missing, [])

    def test_native_link_diagnostics_hook_resolves_through_frappe(self):
        try:
            import frappe
            from frappe.desk.form.meta import get_code_files_via_hooks
        except ModuleNotFoundError:
            self.skipTest("Frappe is not installed")
        from hero_integration import hooks
        with patch.object(frappe, "get_installed_apps", return_value=["hero_integration"]), \
             patch.object(frappe, "get_hooks", return_value=hooks.doctype_js):
            paths = get_code_files_via_hooks("doctype_js", "HERO Integration Link")
        self.assertEqual([Path(path).resolve() for path in paths], [(APP / "public" / "js" / "hero_diagnostics.js").resolve()])
        self.assertIn("hero_integration_diagnostics", Path(paths[0]).read_text(encoding="utf-8"))

    def test_send_preview_labels_and_effect_summaries_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        labels = {"Notes", "Phase", "Project"}
        for filename, names in (("customer_send.py", {"CUSTOMER_FIELDS", "ADDRESS_FIELDS"}), ("task_send.py", {"FIELDS"})):
            tree = ast.parse((APP / "services" / filename).read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets):
                    labels.update(ast.literal_eval(entry.elts[0]) for entry in node.value.elts)
        tree = ast.parse((APP / "api.py").read_text(encoding="utf-8"))
        effects = {value.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
                   for key, value in zip(node.keys, node.values)
                   if isinstance(key, ast.Constant) and key.value == "effect_summary"
                   and isinstance(value, ast.Constant) and isinstance(value.value, str)}
        self.assertTrue(effects)
        self.assertEqual(sorted((labels | effects) - messages.keys()), [])

    def test_mutation_draft_errors_have_german_entries(self):
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            messages = dict(csv.reader(stream))
        path = APP / "services" / "mutation_draft.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        errors = [node.exc.args[0].value for node in ast.walk(tree)
                  if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                  and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ValueError"
                  and node.exc.args and isinstance(node.exc.args[0], ast.Constant)
                  and isinstance(node.exc.args[0].value, str)]
        self.assertTrue(errors)
        self.assertEqual([message for message in errors if message not in messages], [])

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
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Result" and node.args):
                for part in ast.walk(node.args[0]):
                    if (isinstance(part, ast.Constant) and isinstance(part.value, str)
                        and part.value.isupper() and part.value not in messages):
                        missing.append(f"{node.lineno}: {part.value}")
        self.assertEqual(missing, [])

    def test_frappe_catalog_loader_and_language_precedence(self):
        try:
            import frappe
            from frappe.translate import get_translation_dict_from_file, get_translations_from_apps, get_user_lang
        except ModuleNotFoundError:
            self.skipTest("Frappe is not installed")
        catalog = get_translation_dict_from_file(str(APP / "translations" / "de.csv"), "de", "hero_integration", throw=True)
        with (APP / "translations" / "de.csv").open(encoding="utf-8", newline="") as stream:
            expected = dict(csv.reader(stream))
        self.assertEqual(catalog, expected)
        self.assertEqual(catalog["HERO Settings"], "HERO-Einstellungen")
        # Frappe resolves a regional German variant through its parent language.
        for language in ("de-DE", "de-CH"):
            self.assertEqual(get_translations_from_apps(language, apps=["hero_integration"])["HERO Settings"], "HERO-Einstellungen")
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
