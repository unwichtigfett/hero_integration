#!/usr/bin/env bash
# Fresh, bench-native install test for HERO Integration (Woodpecker CI).
#
# Builds a throw-away bench with Frappe and ERPNext v16, installs this app from
# the checkout, checks the install, migrates twice (idempotency), builds the
# assets, runs the tests and - when files are provided - imports a HERO schema
# and the complete configuration.  Nothing contacts HERO.  Everything is
# created below $WORKDIR and can be deleted afterwards.
#
# Required:  DB_HOST, DB_ROOT_PASSWORD
# Optional:  WORKDIR (default ./.ci-bench), APP_PATH (default: repository root),
#            FRAPPE_BRANCH / ERPNEXT_BRANCH (default version-16), PYTHON (default python3.14),
#            HERO_SCHEMA_FILE (introspection JSON), HERO_CONFIG_FILE (configuration export),
#            REDIS_CACHE / REDIS_QUEUE (default redis://127.0.0.1:6379)
set -euo pipefail

APP_PATH="${APP_PATH:-$(cd "$(dirname "$0")/.." && pwd)}"
WORKDIR="${WORKDIR:-$PWD/.ci-bench}"
FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-16}"
ERPNEXT_BRANCH="${ERPNEXT_BRANCH:-version-16}"
PYTHON="${PYTHON:-python3.14}"
SITE="${SITE:-hero-ci.localhost}"
REDIS="${REDIS_CACHE:-redis://127.0.0.1:6379}"
: "${DB_HOST:?DB_HOST is required}" "${DB_ROOT_PASSWORD:?DB_ROOT_PASSWORD is required}"

step() { printf '\n==== %s\n' "$*"; }
run_py() { bench --site "$SITE" execute "$1" ${2:+--kwargs "$2"}; }

step "bench init ($FRAPPE_BRANCH)"
mkdir -p "$WORKDIR"; cd "$WORKDIR"
[ -d bench ] || bench init bench --frappe-branch "$FRAPPE_BRANCH" --python "$PYTHON" --skip-redis-config-generation --no-backups
cd bench
bench set-config -g redis_cache "$REDIS"; bench set-config -g redis_queue "${REDIS_QUEUE:-$REDIS}"; bench set-config -g redis_socketio "$REDIS"

step "get apps"
[ -d apps/erpnext ] || bench get-app erpnext --branch "$ERPNEXT_BRANCH"
rm -rf apps/hero_integration
bench get-app "$APP_PATH"

step "new site + ERPNext"
bench drop-site "$SITE" --db-root-password "$DB_ROOT_PASSWORD" --force --no-backup >/dev/null 2>&1 || true
bench new-site "$SITE" --db-host "$DB_HOST" --db-root-password "$DB_ROOT_PASSWORD" --admin-password admin --no-mariadb-socket
bench --site "$SITE" install-app erpnext

step "install HERO Integration"
bench --site "$SITE" install-app hero_integration
run_py hero_integration.app_setup.verify_install

step "migrate twice (idempotent)"
bench --site "$SITE" migrate
before=$(run_py hero_integration.app_setup.verify_install)
bench --site "$SITE" migrate
after=$(run_py hero_integration.app_setup.verify_install)
[ "$before" = "$after" ] || { echo "Second migrate changed the install state: $before -> $after"; exit 1; }

step "build assets"
bench build --app hero_integration

step "tests"
bench --site "$SITE" set-config allow_tests true
./env/bin/python -m pip install -q "pytest>=8"   # test dependency (see pyproject optional "test")
KNOWN=apps/hero_integration/ci
# Offline suite: tests that need a real site or pytest are known failures; only new ones fail the build.
(cd apps/hero_integration && ../../env/bin/python -m unittest discover -s hero_integration/tests -t . 2>&1 >/dev/null \
  | grep -E "^(ERROR|FAIL):" | sort > /tmp/hero-offline-now.txt) || true
new_failures=$(comm -23 /tmp/hero-offline-now.txt <(sort $KNOWN/known-offline-failures.txt))
if [ -n "$new_failures" ]; then echo "New offline test failures:"; echo "$new_failures"; TEST_FAILED=1; fi
# Site tests: failures listed in known-site-failures.txt predate this pipeline; only new ones fail the build.
bench --site "$SITE" run-tests --app hero_integration > /tmp/hero-site-tests.log 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' /tmp/hero-site-tests.log | grep -E "^\s*✖ " | awk '{print $2}' | sort -u > /tmp/hero-site-failed.txt
grep -q "Failed to discover tests" /tmp/hero-site-tests.log && { echo "Test discovery failed"; TEST_FAILED=1; }
new_site=$(comm -23 /tmp/hero-site-failed.txt <(sort -u $KNOWN/known-site-failures.txt))
if [ -n "$new_site" ]; then echo "New site test failures:"; echo "$new_site"; TEST_FAILED=1; fi
echo "site tests: $(sed 's/\x1b\[[0-9;]*m//g' /tmp/hero-site-tests.log | grep -cE '^\s*✔ ') passed, $(wc -l < /tmp/hero-site-failed.txt) failed (known: $(wc -l < $KNOWN/known-site-failures.txt))"

if [ -n "${HERO_SCHEMA_FILE:-}" ]; then
  step "offline HERO schema snapshot"
  run_py hero_integration.api.snapshot_import_file "{\"path\": \"$HERO_SCHEMA_FILE\"}"
  if [ -n "${HERO_CONFIG_FILE:-}" ]; then
    step "configuration import (preview, import, idempotency)"
    run_py hero_integration.api.config_import_file "{\"path\": \"$HERO_CONFIG_FILE\"}"
    run_py hero_integration.api.config_import_file "{\"path\": \"$HERO_CONFIG_FILE\", \"dry_run\": 0}"
    second=$(run_py hero_integration.api.config_import_file "{\"path\": \"$HERO_CONFIG_FILE\"}")
    echo "$second"
    echo "$second" | grep -q '"New": 0' && ! echo "$second" | grep -q '"state": "New"' \
      || { echo "Configuration import is not idempotent"; exit 1; }
    run_py hero_integration.app_setup.verify_install
  fi
fi

step "uninstall"
bench --site "$SITE" uninstall-app hero_integration --yes --no-backup

[ -z "${TEST_FAILED:-}" ] || { echo "Tests failed (see above)."; exit 1; }
echo "INSTALL TEST PASSED"
