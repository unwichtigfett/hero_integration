"""Check that release test gates reject failures and incomplete discovery."""
from __future__ import annotations

import os
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TestReleaseCiGate(unittest.TestCase):
    def run_gate(self, *, offline_status=0, site_status=0, fixture_status=0, offline_count=2, site_count=2,
                 offline_outcome=None, site_outcome=None, stale_site=False):
        function = (ROOT / 'ci/test-gates.sh').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / 'apps/hero_integration'
            (app / 'ci').mkdir(parents=True)
            (app / 'ci/check-test-report.py').write_text((ROOT / 'ci/check-test-report.py').read_text())
            manifest = {phase: {'critical': [f'synthetic.{phase}.test_0']} for phase in ('offline', 'site')}
            (app / 'ci/required-tests.json').write_text(json.dumps(manifest))
            for phase, count, outcome in [('offline', offline_count, offline_outcome), ('site', site_count, site_outcome)]:
                suite = ET.Element('testsuite')
                for i in range(count):
                    case = ET.SubElement(suite, 'testcase', classname=f'synthetic.{phase}', name=f'test_{i}')
                    if i == 0 and outcome:
                        ET.SubElement(case, outcome)
                (root / f'{phase}-fixture.xml').write_bytes(ET.tostring(suite))
            if stale_site:
                (root / 'site.xml').write_text('<testsuite/>')
            (root / 'env/bin').mkdir(parents=True)
            python = root / 'env/bin/python'
            python.write_text('''#!/bin/bash
if [[ "$1" == ci/run-offline-tests.py ]]; then
  cp "$HERO_TEST_LOG_DIR/offline-fixture.xml" "$3"
  printf "Ran %s tests in 0s\\n" "$OFFLINE_COUNT"
  exit "$OFFLINE_STATUS"
fi
exec "$REAL_PYTHON" "$@"
''')
            python.chmod(0o700)
            script = function + '''
git() { printf 'synthetic-commit\\n'; }
bench() {
  printf '%s\\n' "$*" >> "$TRACE"
  if [[ "$*" == *"execute hero_integration.tests.ci_fixture_setup.prepare"* ]]; then
    [[ "$HERO_CI_TEST_SITE" == "$SITE" ]] || return 8
    return "${FIXTURE_STATUS:-0}"
  fi
  printf '%s %s %s\\n' "$HERO_RUN_RESERVATION_TEST_SITE" "$HERO_WRITEBACK_SOURCE_TEST_SITE" "$HERO_CHECKPOINT_TEST_SITE" >> "$TRACE"
  printf 'SYNTHETIC_SITE_DIAGNOSTIC\\n'
  for (( i=0; i<SITE_COUNT; i++ )); do printf ' ✔ synthetic_test\\n'; done
  if [[ "$STALE_SITE" != 1 ]]; then cp "$HERO_TEST_LOG_DIR/site-fixture.xml" "${@: -1}"; fi
  return "$SITE_STATUS"
}
run_hero_test_gates
printf 'GATE_PASSED\\n'
'''
            env = dict(os.environ, MIN_TESTS='1', HERO_TEST_LOG_DIR=tmp,
                       OFFLINE_STATUS=str(offline_status), SITE_STATUS=str(site_status), FIXTURE_STATUS=str(fixture_status),
                       OFFLINE_COUNT=str(offline_count), SITE_COUNT=str(site_count), TRACE=str(root / 'trace'),
                       REAL_PYTHON=sys.executable, STALE_SITE=str(int(stale_site)), SITE='hero-ci.localhost')
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

    def test_schema_fixture_failure_stops_before_site_tests(self):
        result, trace = self.run_gate(fixture_status=6)
        self.assertEqual(result.returncode, 6)
        self.assertIn('execute hero_integration.tests.ci_fixture_setup.prepare', trace)
        self.assertNotIn('run-tests --app hero_integration', trace)

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

    def test_required_skip_is_a_failure_despite_enough_other_successes(self):
        for changes in ({'offline_outcome': 'skipped'}, {'site_outcome': 'skipped'}):
            with self.subTest(changes=changes):
                result, _ = self.run_gate(**changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('is skipped', result.stderr)
                self.assertNotIn('GATE_PASSED', result.stdout)

    def test_xml_failure_cannot_pass_even_when_process_exit_is_zero(self):
        result, _ = self.run_gate(site_outcome='failure')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('GATE_PASSED', result.stdout)

    def test_stale_site_report_cannot_mask_missing_output(self):
        result, _ = self.run_gate(stale_site=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing site report', result.stderr)

    def test_database_regressions_are_enabled_for_the_exact_disposable_site(self):
        result, trace = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('hero-ci.localhost hero-ci.localhost hero-ci.localhost', trace)

    def test_both_providers_use_the_shared_gate(self):
        for path in ('ci/github-install-test.sh', 'ci/standard-install-test.sh'):
            source = (ROOT / path).read_text()
            self.assertIn('/ci/test-gates.sh"', source)
            self.assertIn('run_hero_test_gates', source)
            self.assertNotIn('TEST_FAILED', source)

    def test_manual_no_hero_selection_cannot_pass_a_maintained_branch_gate(self):
        source = (ROOT / 'ci/github-install-test.sh').read_text().split('cd /tmp', 1)[0]
        for branch in ('hero_int_dev', 'hero_int_1.0', 'diagnostic-feature'):
            with self.subTest(branch=branch):
                env = dict(os.environ, GITHUB_WORKSPACE=str(ROOT), DB_ROOT_PASSWORD='synthetic',
                           INSTALL_APPS='none', GITHUB_REF_NAME=branch, GITHUB_BASE_REF='')
                result = subprocess.run(['bash', '-c', source], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if branch == 'diagnostic-feature' else 2)

    def test_workflows_cover_both_maintained_branches(self):
        github = (ROOT / '.github/workflows/ci.yml').read_text()
        before_jobs = github.split('\njobs:', 1)[0]
        self.assertIn('branches: [hero_int_dev, hero_int_1.0]', before_jobs)
        self.assertIn('      - hero_int_1.0\n', before_jobs)
        woodpecker = (ROOT / '.woodpecker/standard.yml').read_text()
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


class TestArchitectureWorkflowIsolation(unittest.TestCase):
    def test_the_named_workflow_is_canonical_and_uses_its_own_installer(self):
        import yaml

        configs = list((ROOT / '.woodpecker').glob('*.yml'))
        self.assertEqual([p.name for p in configs], ['standard.yml'])
        self.assertFalse((ROOT / '.woodpecker.yml').exists())
        steps = {step['name']: step for step in yaml.safe_load(configs[0].read_text())['steps']}
        self.assertIn('ci/standard-install-test.sh', '\n'.join(steps['install-test']['commands']))
        self.assertTrue((ROOT / 'ci/standard-install-test.sh').is_file())
        self.assertFalse((ROOT / 'ci/bench-install-test.sh').exists())

    def test_required_architecture_groups_cannot_be_silently_dropped(self):
        manifest = json.loads((ROOT / 'ci/required-tests.json').read_text())
        expected = ['standard query architecture', 'standard mutation architecture']
        for group in expected:
            self.assertIn(group, manifest['offline'])
            self.assertTrue(manifest['offline'][group])
        other_prefix = 'opt '
        self.assertFalse([name for name in manifest['offline'] if name.startswith(other_prefix)])
