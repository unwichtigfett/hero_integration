# Installation

- Prerequisites: Frappe v16, ERPNext v16, a site and administrative permissions.
- Installation branch: `hero_int_1.0`; development branch: `hero_int_dev`.
- Install ERPNext first: `bench get-app erpnext --branch version-16`; `bench --site <site> install-app erpnext`.
- Install HERO Integration: `bench get-app https://github.com/unwichtigfett/hero_integration --branch hero_int_1.0`; `bench --site <site> install-app hero_integration`.
- Upgrade: `bench --site <site> migrate`; `bench build --app hero_integration`.
- Verify: `bench --site <site> execute hero_integration.app_setup.verify_install`.
- Installation creates app-owned schema and records; it does not activate mappings, approve read services or contact HERO.
- Configuration packages exclude credentials, runtime switches and business records; import requires System Manager, preview and confirmation.
- A partial configuration preview is insufficient for approval; validate the target snapshot and inspect the full preview first.
- Runtime requires an approved source binding, schema, mapping, read service and permissions; external mutations require separate authorization.
- Fresh-install tests: `ci/bench-install-test.sh` and `.github/workflows/ci.yml`.
