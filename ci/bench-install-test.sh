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
#            HERO_CONFIG_FILE (complete package export), HERO_SCHEMA_FILE (optional introspection JSON);
#            if a variable is set, its file must exist, otherwise the test fails,
#            MIN_TESTS (default 1000: fewer offline or site tests counts as a broken run),
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
# Git's local clone runs under the bench user, while CI may own the checkout as root.
# Scope the ownership exception to this checkout and this command, not the user's Git config.
git_config=$(mktemp "$WORKDIR/git-safe.XXXXXXXX")
git config --file "$git_config" --add safe.directory "$APP_PATH/.git"
GIT_CONFIG_GLOBAL="$git_config" bench get-app "$APP_PATH"
rm -f "$git_config"

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
MIN_TESTS="${MIN_TESTS:-1000}"
if ! (cd apps/hero_integration && ../../env/bin/python ci/run-offline-tests.py > /tmp/hero-offline.log 2>&1); then
  echo "Offline tests failed:"; tail -80 /tmp/hero-offline.log; TEST_FAILED=1
fi
offline_ran=$(grep -oE "^Ran [0-9]+ tests" /tmp/hero-offline.log | grep -oE "[0-9]+" || echo 0)
grep -F "Site-dependent tests reserved for the full site suite:" /tmp/hero-offline.log || true
echo "offline tests: $offline_ran run"
if [ "$offline_ran" -lt "$MIN_TESTS" ]; then echo "Offline suite did not run (only $offline_ran tests)"; tail -20 /tmp/hero-offline.log; TEST_FAILED=1; fi
if ! bench --site "$SITE" run-tests --app hero_integration > /tmp/hero-site-tests.log 2>&1; then
  echo "Site tests failed:"; tail -100 /tmp/hero-site-tests.log; TEST_FAILED=1
fi
site_passed=$(sed 's/\x1b\[[0-9;]*m//g' /tmp/hero-site-tests.log | grep -cE '^\s*✔ ' || true)
echo "site tests: $site_passed passed"
if [ "$site_passed" -lt "$MIN_TESTS" ]; then echo "Site tests did not run (only $site_passed passed)"; tail -30 /tmp/hero-site-tests.log; TEST_FAILED=1; fi

for var in HERO_SCHEMA_FILE HERO_CONFIG_FILE; do
  file="${!var:-}"
  if [ -n "$file" ] && [ ! -s "$file" ]; then echo "$var is set but $file is missing or empty"; exit 1; fi
done
# The application test suite can leave a different source instance or schema
# snapshot behind. Import the user's package on a fresh, separate CI site so
# its safety checks see the same clean installation as a new deployment.
if [ -n "${HERO_SCHEMA_FILE:-}" ] || [ -n "${HERO_CONFIG_FILE:-}" ]; then
  MAIN_TEST_SITE="$SITE"
  SITE=hero-config-ci.localhost
  if [ "$SITE" = "$MAIN_TEST_SITE" ] || [ -e "sites/$SITE" ]; then
    echo "Configuration test site is already present; refusing to reuse it" >&2
    exit 1
  fi
  step "fresh site for offline configuration import"
  bench new-site "$SITE" --db-host "$DB_HOST" --db-root-password "$DB_ROOT_PASSWORD" --admin-password admin --no-mariadb-socket
  bench --site "$SITE" install-app erpnext
  bench --site "$SITE" install-app hero_integration
fi
if [ -n "${HERO_SCHEMA_FILE:-}" ]; then
  step "offline HERO schema snapshot (optional; a v2 configuration package brings its own)"
  run_py hero_integration.api.snapshot_import_file "{\"path\": \"$HERO_SCHEMA_FILE\"}"
fi
if [ -n "${HERO_CONFIG_FILE:-}" ]; then
  step "complete package import (preview, import, idempotency)"
  run_py hero_integration.api.config_import_file "{\"path\": \"$HERO_CONFIG_FILE\"}"
  run_py hero_integration.api.config_import_file "{\"path\": \"$HERO_CONFIG_FILE\", \"dry_run\": 0}"
  second=$(run_py hero_integration.api.config_import_file "{\"path\": \"$HERO_CONFIG_FILE\"}")
  echo "$second"
  echo "$second" | grep -q '"New": 0' && ! echo "$second" | grep -q '"state": "New"' \
    || { echo "Configuration import is not idempotent"; exit 1; }
  run_py hero_integration.app_setup.verify_install
else
  echo "NOTE: HERO_CONFIG_FILE not set - package import skipped."
fi

if [ -n "${MAIN_TEST_SITE:-}" ]; then
  SITE="$MAIN_TEST_SITE"
fi
step "uninstall"
bench --site "$SITE" uninstall-app hero_integration --yes --no-backup

[ -z "${TEST_FAILED:-}" ] || { echo "Tests failed (see above)."; exit 1; }
echo "INSTALL TEST PASSED"
