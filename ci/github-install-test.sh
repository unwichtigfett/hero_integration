#!/usr/bin/env bash
# Runs only in an isolated CI container with disposable MariaDB and Redis services.
set -Eeuo pipefail
: "${GITHUB_WORKSPACE:?}" "${DB_ROOT_PASSWORD:?}"
export HERO_TEST_LOG_DIR="${HERO_TEST_LOG_DIR:-$GITHUB_WORKSPACE/.ci-results}"
source "$GITHUB_WORKSPACE/ci/test-gates.sh"
selection=${INSTALL_APPS:-erpnext,helpdesk,hero_integration}
case "$selection" in
  none|erpnext|helpdesk|erpnext,helpdesk|erpnext,hero_integration|erpnext,helpdesk,hero_integration) ;;
  *) echo 'Unsupported INSTALL_APPS; HERO requires ERPNext and selections must use the documented order.' >&2; exit 2 ;;
esac
# A manual diagnostic selection must not turn the maintained branch's required
# install-test check green without running HERO's complete test gate.
case "${GITHUB_BASE_REF:-${GITHUB_REF_NAME:-}}" in
  hero_int_dev|hero_int_1.0)
    [[ ",$selection," == *,hero_integration,* ]] || {
      echo 'Maintained branches require a selection containing HERO Integration.' >&2; exit 2;
    }
    ;;
esac
cd /tmp
bench init ci-bench --frappe-branch version-16 --skip-redis-config-generation --skip-assets --python "$(command -v python3.14)"
cd ci-bench
bench set-config -g db_host mariadb
bench set-config -g redis_cache redis://redis:6379
bench set-config -g redis_queue redis://redis:6379
bench set-config -g socketio_port 9000
if [[ ",$selection," == *,erpnext,* ]]; then
  bench get-app erpnext --branch version-16 --skip-assets
fi
if [[ ",$selection," == *,helpdesk,* ]]; then
  bench get-app https://github.com/frappe/telephony.git --branch develop --skip-assets
  bench get-app https://github.com/frappe/helpdesk.git --branch main --skip-assets
fi
if [[ ",$selection," == *,hero_integration,* ]]; then
  bench get-app "$GITHUB_WORKSPACE" --skip-assets
  test "$(git -C apps/hero_integration rev-parse HEAD)" = "$(git -C "$GITHUB_WORKSPACE" rev-parse HEAD)"
fi
for i in {1..60}; do
  if MYSQL_PWD="$DB_ROOT_PASSWORD" mariadb-admin ping -h mariadb -u root --silent >/dev/null 2>&1; then break; fi
  if [[ $i == 60 ]]; then echo 'MariaDB did not become ready.' >&2; exit 1; fi
  sleep 2
done
MYSQL_PWD="$DB_ROOT_PASSWORD" mariadb -h mariadb -u root -e "SET GLOBAL innodb_read_only_compressed=OFF"
bench new-site hero-ci.localhost --db-root-username root --db-root-password "$DB_ROOT_PASSWORD" --mariadb-user-host-login-scope='%' --admin-password ci-only --set-default
if [[ ",$selection," == *,erpnext,* ]]; then
  bench --site hero-ci.localhost install-app erpnext
fi
if [[ ",$selection," == *,helpdesk,* ]]; then
  bench --site hero-ci.localhost install-app telephony
  bench --site hero-ci.localhost install-app helpdesk
fi
if [[ ",$selection," == *,hero_integration,* ]]; then
  bench --site hero-ci.localhost install-app hero_integration
  result=$(bench --site hero-ci.localhost execute hero_integration.app_setup.verify_install)
  printf "%s\n" "$result" | python3 -c 'import json,sys; r=json.load(sys.stdin); assert not r["missing"] and not r["unsafe_defaults"], "HERO installation verification failed"'
  bench --site hero-ci.localhost set-config allow_tests true
  ./env/bin/python -m pip install -q 'pytest>=8' 'unittest-xml-reporting>=3.2,<4'
  bench build --app hero_integration
  # The setup wizard is deliberately absent on this disposable site.
  if [ -z "$(bench --site hero-ci.localhost execute frappe.db.exists --kwargs '{"dt":"Warehouse Type","dn":"Transit"}')" ]; then
    bench --site hero-ci.localhost execute frappe.client.insert --kwargs '{"doc":{"doctype":"Warehouse Type","name":"Transit"}}' >/dev/null
  fi
  company_count=$(bench --site hero-ci.localhost execute frappe.db.count --args '["Company"]')
  if [ -z "$company_count" ] || [ "$company_count" = 0 ]; then
    bench --site hero-ci.localhost execute frappe.client.insert --kwargs '{"doc":{"doctype":"Company","company_name":"HERO CI Company","abbr":"HCI","country":"Germany","default_currency":"EUR"}}' >/dev/null
  fi
  run_hero_test_gates
fi
bench --site hero-ci.localhost list-apps
