#!/usr/bin/env bash
# Opt architecture: fresh Frappe/ERPNext installation and required branch tests.
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
APP_PATH="$(cd "$APP_PATH" && pwd)"
source "$APP_PATH/ci/test-gates.sh"
WORKDIR="${WORKDIR:-$PWD/.ci-bench}"
export HERO_TEST_LOG_DIR="${HERO_TEST_LOG_DIR:-$WORKDIR/results}"
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
test "$(git -C apps/hero_integration rev-parse HEAD)" = "$(git -c safe.directory="$APP_PATH" -C "$APP_PATH" rev-parse HEAD)"

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
./env/bin/python -m pip install -q 'pytest>=8' 'unittest-xml-reporting>=3.2,<4'
# The disposable site does not run the ERPNext setup wizard. Provide its
# minimal Company dependency without creating the unrelated ERPNext fixtures.
company_count=$(bench --site "$SITE" execute frappe.db.count --args '["Company"]')
if [ -z "$company_count" ] || [ "$company_count" = 0 ]; then
  if [ -z "$(bench --site "$SITE" execute frappe.db.exists --kwargs '{"dt":"Warehouse Type","dn":"Transit"}')" ]; then
    bench --site "$SITE" execute frappe.client.insert --kwargs '{"doc":{"doctype":"Warehouse Type","name":"Transit"}}' >/dev/null
  fi
  bench --site "$SITE" execute frappe.client.insert --kwargs '{"doc":{"doctype":"Company","company_name":"HERO CI Company","abbr":"HCI","country":"Germany","default_currency":"EUR"}}' >/dev/null
fi
run_hero_test_gates

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

echo "INSTALL TEST PASSED"
