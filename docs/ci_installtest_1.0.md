# CI: bench-native install test

> CI checks source and disposable Frappe-site behavior. Passing CI is not proof of live HERO synchronization, target deployment or recovery.

`ci/bench-install-test.sh` checks that the app installs on a fresh Frappe/ERPNext
v16 bench without manual steps. `.woodpecker.yml` runs it on a Woodpecker CI
server for pushes to `hero_int_dev` and `hero_int_1.0`, and on manual runs.

The test never contacts HERO. It runs these steps:

1. `bench init` (Frappe `version-16`), `get-app erpnext`, `get-app` this checkout.
2. New site, `install-app erpnext`, `install-app hero_integration`.
3. `hero_integration.app_setup.verify_install`: HERO fields, Phase/Volume/Assignee
   fields, role, Project Types, salutations, DocTypes, mutation gate off.
4. `migrate` twice; the install state must not change (create-only setup).
5. `bench build --app hero_integration`, offline unit tests, `bench run-tests`.
6. On a fresh separate CI site, with `HERO_CONFIG_FILE` (complete package export,
   see below): package preview, import, and a second preview that must report
   nothing new. This avoids state left by the app test suite. `HERO_SCHEMA_FILE`
   (introspection JSON) is optional; a v2 package brings its own snapshot.
7. `uninstall-app hero_integration`.

The disposable test site now seeds a minimal ERPNext Company when the setup wizard has not run. Optional schema and configuration imports use a **separate new site** so the app test suite cannot affect the import checks. This does not install or verify the separately distributed assignment-intent framework patch.

Image requirements: Python 3.14 (`requires-python >=3.14`), Node 20+, yarn,
git, MariaDB client, `frappe-bench` (pip). Services: MariaDB 11, Redis 7.
The first `bench init` takes long on a small CI server. Use a prebuilt image
with an initialised bench (Frappe + ERPNext); `WORKDIR` reuses `bench/`.

The package file is not part of the repository. Put it on the CI server as
`/srv/hero-ci/hero-integration-config.json` (Mapping Editor → "Complete
configuration" → Export on a configured site). It contains the HERO schema
snapshot, so no separate schema file is needed.

The pipeline mounts `/srv/hero-ci` read-only at `/hero-ci`. Woodpecker only allows
volume mounts for repositories marked as *trusted*. The workflow supplies each
file only when it is present and nonempty. If either is absent, the configuration
import is skipped. A directly configured path that is missing still fails.

The test also fails when fewer than `MIN_TESTS` (default 1000) offline or site tests
run. A crash during test discovery therefore never counts as a pass.

## GitHub Actions

Pushes and pull requests targeting `hero_int_dev` or `hero_int_1.0` run package checks and an isolated install test on a
GitHub-hosted runner. It uses fresh MariaDB and Redis services, not the VPS test
site or production secrets. The push test installs ERPNext, Telephony, Helpdesk
and HERO, builds HERO assets, and runs both the offline suite and the complete
HERO Frappe-site test suite. Both must exit successfully and discover at least
`MIN_TESTS` tests (default 1000). Minimal selections without HERO remain available
through manual dispatch on other branches; maintained branches reject them so
a required install-test check cannot pass without exercising HERO.

To run a different selection, open **Actions → CI → Run workflow**, choose
`hero_int_dev`, and select `install_apps`. The workflow must also be present on
the default branch for GitHub to offer manual dispatch. HERO requires ERPNext.
`erpnext,helpdesk`
also installs Telephony. The automatic GitHub run does not import schema or
configuration files. Woodpecker and GitHub receive push events independently.

On both maintained branches, Woodpecker uses strict exit-code checks: offline tests run through `ci/run-offline-tests.py`, while site-dependent tests run in Frappe. The former known-failure allowlists no longer authorize a passing result. `MIN_TESTS` is a coverage floor, not an alternative to a successful test command. GitHub Actions also propagates the actual failure exit codes from the complete
offline and site suites. No test-failure allowlist can make either gate pass.
Repository administrators must require the successful package and install-test
checks in branch protection for both branches. Workflow files request the
checks but cannot enforce GitHub or Forgejo branch protection; that setting is
outside this repository and must be verified separately.

## Test-site update on Forgejo push

A push to Forgejo `hero_int_dev` starts the test-site updater through a dedicated,
restricted Woodpecker SSH secret. The step waits up to two minutes for GitHub
`hero_int_dev` to reach the exact same commit before requesting an update. If the
branches do not match, it fails safely; the twice-daily VPS check remains a
fallback. The updater creates a backup and runs its own checks and migration.
This step depends on the successful isolated install test; a failed release test gate prevents the update.
