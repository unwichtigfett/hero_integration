#!/usr/bin/env bash
# Shared gates, sourced from the disposable bench by both CI providers.
run_hero_test_gates() {
  local minimum=${MIN_TESTS:-1000}
  local site=${SITE:-hero-ci.localhost}
  local app="$PWD/apps/hero_integration" python="$PWD/env/bin/python"
  local report_dir=${HERO_TEST_LOG_DIR:-$PWD/.ci-results}
  [[ "$minimum" =~ ^[1-9][0-9]*$ ]] || { echo 'MIN_TESTS must be positive.' >&2; return 2; }
  mkdir -p "$report_dir" || return "$?"
  report_dir=$(cd "$report_dir" && pwd) || return "$?"
  # Remove only this invocation's exact result files; a stale report cannot pass.
  rm -f "$report_dir/offline.xml" "$report_dir/site.xml" \
    "$report_dir/offline.log" "$report_dir/site.log" || return "$?"
  git -c safe.directory="$app" -C "$app" rev-parse HEAD > "$report_dir/source.txt" || return "$?"
  git -c safe.directory="$app" -C "$app" status --short >> "$report_dir/source.txt" || return "$?"
  printf 'Tested checkout (revision and any local changes):\n'
  cat "$report_dir/source.txt" || return "$?"
  printf 'Test site: %s; reports: %s\n' "$site" "$report_dir"

  if (cd "$app" && "$python" ci/run-offline-tests.py --junit-xml-output "$report_dir/offline.xml") \
      > "$report_dir/offline.log" 2>&1; then
    cat "$report_dir/offline.log" || return "$?"
  else
    local rc=$?
    cat "$report_dir/offline.log"
    return "$rc"
  fi
  "$python" "$app/ci/check-test-report.py" --phase offline \
    --report "$report_dir/offline.xml" --minimum "$minimum" || return "$?"

  # The deadline is mapping-owned, so a clean install does not create it.
  # Provision the checked-in schema only on this explicitly selected test site.
  HERO_CI_TEST_SITE="$site" bench --site "$site" execute hero_integration.tests.ci_fixture_setup.prepare || return "$?"

  # These tests commit through multiple real MariaDB connections. Opt in only on
  # the explicitly disposable site created by the caller, never the test server.
  if HERO_RUN_RESERVATION_TEST_SITE="$site" HERO_WRITEBACK_SOURCE_TEST_SITE="$site" \
      HERO_CHECKPOINT_TEST_SITE="$site" bench --site "$site" run-tests --app hero_integration \
      --junit-xml-output "$report_dir/site.xml" > "$report_dir/site.log" 2>&1; then
    cat "$report_dir/site.log" || return "$?"
  else
    local rc=$?
    cat "$report_dir/site.log"
    return "$rc"
  fi
  "$python" "$app/ci/check-test-report.py" --phase site \
    --report "$report_dir/site.xml" --minimum "$minimum" || return "$?"
  printf 'HERO offline and complete site test suites passed.\n'
}
