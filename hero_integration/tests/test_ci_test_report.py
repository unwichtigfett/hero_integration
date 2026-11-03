"""CI must prove critical regressions executed, not merely count green output."""

import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hero_ci_test_report", ROOT / "ci/check-test-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class TestCiTestReport(unittest.TestCase):
    def check(self, xml, *, minimum=1):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "report.xml").write_text(xml)
            (path / "required.json").write_text(json.dumps({"offline": {"safety": ["critical.Guard.test_safe"]}}))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return report.check_report(path / "report.xml", path / "required.json", "offline", minimum)

    @staticmethod
    def xml(outcome="", *, classname="critical.Guard", name="test_safe"):
        return f'<testsuite><testcase classname="{classname}" name="{name}">{outcome}</testcase></testsuite>'

    def test_success_requires_the_actual_test_identity(self):
        self.assertTrue(self.check(self.xml()))
        self.assertFalse(self.check(self.xml(name="different_test")))

    def test_required_skip_failure_and_error_all_block(self):
        for status in ("skipped", "failure", "error"):
            with self.subTest(status=status):
                self.assertFalse(self.check(self.xml(f"<{status}/>")))

    def test_noncritical_failure_also_blocks(self):
        self.assertFalse(self.check('<?xml version="1.0"?>' + self.xml()
                                    + '<?xml version="1.0"?>' + self.xml('<failure/>', name='other')))

    def test_optional_skip_is_visible_but_does_not_count_as_success(self):
        xml = '<?xml version="1.0"?>' + self.xml() + '<?xml version="1.0"?>' + self.xml('<skipped/>', name='optional')
        self.assertTrue(self.check(xml))
        self.assertFalse(self.check(xml, minimum=2))

    def test_frappe_reports_with_multiple_category_documents_are_supported(self):
        xml = '<?xml version="1.0" encoding="utf-8"?>' + self.xml()
        xml += '<?xml version="1.0" encoding="utf-8"?>' + self.xml(name='another')
        self.assertTrue(self.check(xml, minimum=2))

    def test_nested_testsuites_are_supported(self):
        self.assertTrue(self.check('<testsuites>' + self.xml() + '</testsuites>'))

    def test_declared_counts_cannot_replace_missing_testcases(self):
        with self.assertRaisesRegex(ValueError, 'no test cases'):
            self.check('<testsuite tests="5000" failures="0"/>')

    def test_malformed_or_truncated_reports_are_rejected(self):
        for xml in ('', self.xml()[:-5], '<?xml version="1.0"?><testsuite/>garbage', '<unexpected/>'):
            with self.subTest(xml=xml), self.assertRaises((ValueError, ET.ParseError)):
                self.check(xml)

    def test_duplicate_results_cannot_inflate_success_count(self):
        self.assertFalse(self.check('<testsuites>' + self.xml() * 2 + '</testsuites>', minimum=2))

    def test_duplicate_success_cannot_hide_a_skip_or_failure(self):
        for state in ('skipped', 'failure', 'error'):
            for cases in (self.xml() + self.xml(f'<{state}/>'), self.xml(f'<{state}/>') + self.xml()):
                with self.subTest(state=state, cases=cases):
                    self.assertFalse(self.check('<testsuites>' + cases + '</testsuites>'))

    def test_dtds_and_entities_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DTD/entity'):
            self.check('<!DOCTYPE testsuite [<!ENTITY example "injected">]>' + self.xml())

    def test_nonpositive_minimum_is_not_an_escape_hatch(self):
        for minimum in (0, -1):
            with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                self.check(self.xml(), minimum=minimum)

    def test_required_manifest_resolves_to_real_test_methods(self):
        manifest = json.loads((ROOT / 'ci/required-tests.json').read_text())
        self.assertEqual(set(manifest), {'offline', 'site'})
        for phase, groups in manifest.items():
            self.assertTrue(groups)
            for group, identities in groups.items():
                self.assertTrue(identities, group)
                for identity in identities:
                    module, class_name, method = identity.rsplit('.', 2)
                    path = ROOT / (module.replace('.', '/') + '.py')
                    tree = ast.parse(path.read_text())
                    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
                    methods = {node.name for node in classes[class_name].body if isinstance(node, ast.FunctionDef)}
                    self.assertIn(method, methods, f'{phase}/{group}: {identity}')
