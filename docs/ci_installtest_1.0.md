# CI: bench-native install test

`ci/bench-install-test.sh` checks that the app installs on a fresh Frappe/ERPNext
v16 bench without manual steps. `.woodpecker.yml` runs it on a Woodpecker CI
server (draft).

The test never contacts HERO. It runs these steps:

1. `bench init` (Frappe `version-16`), `get-app erpnext`, `get-app` this checkout.
2. New site, `install-app erpnext`, `install-app hero_integration`.
3. `hero_integration.app_setup.verify_install`: HERO fields, Phase/Volume/Assignee
   fields, role, Project Types, salutations, DocTypes, mutation gate off.
4. `migrate` twice; the install state must not change (create-only setup).
5. `bench build --app hero_integration`, offline unit tests, `bench run-tests`.
6. With `HERO_SCHEMA_FILE` (introspection JSON): offline snapshot. With
   `HERO_CONFIG_FILE` (default fixture `ci/fixtures/hero-integration-config.json`):
   configuration preview, import, and a second preview that must report nothing new.
7. `uninstall-app hero_integration`.

Image requirements: Python 3.14 (`requires-python >=3.14`), Node 20+, yarn,
git, MariaDB client, `frappe-bench` (pip). Services: MariaDB 11, Redis 7.
The first `bench init` takes long on a small CI server. Use a prebuilt image
with an initialised bench (Frappe + ERPNext); `WORKDIR` reuses `bench/`.

The schema file is HERO's API description and is not part of the repository.
Provide its path through the `hero_schema_file_path` secret/volume.
