# CI: bench-native install test

`ci/bench-install-test.sh` checks that the app installs on a fresh Frappe/ERPNext
v16 bench without manual steps. `.woodpecker.yml` runs it on a Woodpecker CI
server for pushes to `hero_int_1.0` and `hero_int_dev` and on manual runs.

The test never contacts HERO. It runs these steps:

1. `bench init` (Frappe `version-16`), `get-app erpnext`, `get-app` this checkout.
2. New site, `install-app erpnext`, `install-app hero_integration`.
3. `hero_integration.app_setup.verify_install`: HERO fields, Phase/Volume/Assignee
   fields, role, Project Types, salutations, DocTypes, mutation gate off.
4. `migrate` twice; the install state must not change (create-only setup).
5. `bench build --app hero_integration`, offline unit tests, `bench run-tests`.
6. With `HERO_SCHEMA_FILE` (introspection JSON): offline snapshot. With
   `HERO_CONFIG_FILE` (a configuration export, see below):
   configuration preview, import, and a second preview that must report nothing new.
7. `uninstall-app hero_integration`.

Image requirements: Python 3.14 (`requires-python >=3.14`), Node 20+, yarn,
git, MariaDB client, `frappe-bench` (pip). Services: MariaDB 11, Redis 7.
The first `bench init` takes long on a small CI server. Use a prebuilt image
with an initialised bench (Frappe + ERPNext); `WORKDIR` reuses `bench/`.

Neither input file is part of the repository. Put both on the CI server in
`/srv/hero-ci/`:

- `hero-schema.json`: HERO's API description (a GraphQL introspection result).
- `hero-integration-config.json`: a site's own configuration export (Mapping
  Editor → "Complete configuration" → Export).

The pipeline mounts `/srv/hero-ci` read-only at `/hero-ci`. Woodpecker only allows
volume mounts for repositories marked as *trusted*. If a configured file is missing,
the test fails. To run without the configuration import, remove `HERO_SCHEMA_FILE`
and `HERO_CONFIG_FILE` from `.woodpecker.yml`; the log then says the import was
skipped.

The test also fails when fewer than `MIN_TESTS` (default 1000) offline or site tests
run. A crash during test discovery therefore never counts as a pass.
