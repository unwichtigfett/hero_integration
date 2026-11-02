"""Check that release test gates reject failures and incomplete discovery."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TestReleaseCiGate(unittest.TestCase):
    def run_gate(self, *, offline_status=0, site_status=0, offline_count=2, site_count=2):
        source = (ROOT / 'ci/github-install-test.sh').read_text()
        function = source.split('# BEGIN HERO TEST GATES\n', 1)[1].split('# END HERO TEST GATES', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'apps/hero_integration').mkdir(parents=True)
            (root / 'env/bin').mkdir(parents=True)
            python = root / 'env/bin/python'
            python.write_text('#!/bin/bash\nprintf "Ran %s tests in 0s\\n" "$OFFLINE_COUNT"\nexit "$OFFLINE_STATUS"\n')
            python.chmod(0o700)
            script = function + '''
bench() {
  printf '%s\\n' "$*" >> "$TRACE"
  printf 'SYNTHETIC_SITE_DIAGNOSTIC\\n'
  for (( i=0; i<SITE_COUNT; i++ )); do printf ' ✔ synthetic_test\\n'; done
  return "$SITE_STATUS"
}
run_hero_test_gates
printf 'GATE_PASSED\\n'
'''
            env = dict(os.environ, MIN_TESTS='1', HERO_TEST_LOG_DIR=tmp,
                       OFFLINE_STATUS=str(offline_status), SITE_STATUS=str(site_status),
                       OFFLINE_COUNT=str(offline_count), SITE_COUNT=str(site_count), TRACE=str(root / 'trace'))
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=root, env=env,
                                    text=True, capture_output=True)
            trace = (root / 'trace').read_text() if (root / 'trace').exists() else ''
            return result, trace

    def test_offline_failure_is_propagated_before_site_tests(self):
        result, trace = self.run_gate(offline_status=7)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(trace, '')
        self.assertNotIn('GATE_PASSED', result.stdout)

    def test_site_failure_exit_code_cannot_be_allowed(self):
        result, trace = self.run_gate(site_status=9)
        self.assertEqual(result.returncode, 9)
        self.assertIn('run-tests --app hero_integration', trace)
        self.assertNotIn('--module', trace)
        self.assertNotIn('GATE_PASSED', result.stdout)

    def test_incomplete_discovery_is_not_a_pass(self):
        for changes in ({'offline_count': 0}, {'site_count': 0}):
            with self.subTest(changes=changes):
                result, _ = self.run_gate(**changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('GATE_PASSED', result.stdout)

    def test_site_failure_keeps_diagnostics_before_the_last_hundred_lines(self):
        result, _ = self.run_gate(site_status=9, site_count=110)
        self.assertEqual(result.returncode, 9)
        self.assertIn('SYNTHETIC_SITE_DIAGNOSTIC', result.stdout)
        self.assertNotIn('GATE_PASSED', result.stdout)

    def test_both_full_suites_must_succeed(self):
        result, trace = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GATE_PASSED', result.stdout)
        self.assertIn('--site hero-ci.localhost run-tests --app hero_integration', trace)

    def test_manual_no_hero_selection_cannot_pass_a_maintained_branch_gate(self):
        source = (ROOT / 'ci/github-install-test.sh').read_text().split('cd /tmp', 1)[0]
        for branch in ('hero_int_dev', 'hero_int_1.0', 'diagnostic-feature'):
            with self.subTest(branch=branch):
                env = dict(os.environ, GITHUB_WORKSPACE='/synthetic', DB_ROOT_PASSWORD='synthetic',
                           INSTALL_APPS='none', GITHUB_REF_NAME=branch, GITHUB_BASE_REF='')
                result = subprocess.run(['bash', '-c', source], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if branch == 'diagnostic-feature' else 2)

    def test_workflows_cover_both_maintained_branches(self):
        github = (ROOT / '.github/workflows/ci.yml').read_text()
        before_jobs = github.split('\njobs:', 1)[0]
        self.assertIn('branches: [hero_int_dev, hero_int_1.0]', before_jobs)
        self.assertIn('      - hero_int_1.0\n', before_jobs)
        woodpecker = (ROOT / '.woodpecker.yml').read_text()
        self.assertIn('branch: [hero_int_dev, hero_int_1.0]', woodpecker)
        update = woodpecker.split('  - name: update-test-site\n', 1)[1].split('  - name: install-test', 1)[0]
        self.assertIn('depends_on: [install-test]', update)

    def test_company_setup_handles_bench_zero_output_and_query_failures(self):
        source = (ROOT / 'ci/github-install-test.sh').read_text()
        setup = source.split('  # The setup wizard is deliberately absent on this disposable site.\n', 1)[1]
        setup = setup.split('  run_hero_test_gates\n', 1)[0]
        for count, status, should_insert in (('', 0, True), ('0', 0, True), ('1', 0, False), ('', 7, False)):
            with self.subTest(count=count, status=status), tempfile.TemporaryDirectory() as tmp:
                trace = Path(tmp) / 'trace'
                env = dict(os.environ, COMPANY_COUNT=count, COUNT_STATUS=str(status), TRACE=str(trace))
                script = '''
bench() {
  case "$*" in
    *"execute frappe.db.count"*) printf '%s' "$COMPANY_COUNT"; return "$COUNT_STATUS" ;;
    *"execute frappe.db.exists"*) printf 'Transit' ;;
    *"execute frappe.client.insert"*) printf '%s\\n' "$*" >> "$TRACE" ;;
    *) return 99 ;;
  esac
}
''' + setup
                result = subprocess.run(['bash', '-euo', 'pipefail', '-c', script], env=env,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, status, result.stderr)
                inserted = trace.read_text() if trace.exists() else ''
                self.assertEqual('"doctype":"Company"' in inserted, should_insert)
