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

Both providers source `ci/test-gates.sh`. The gate preserves each test process's
exit code and validates its JUnit report with `ci/check-test-report.py`. It fails
on any reported error/failure, missing or malformed reports, fewer than `MIN_TESTS` (default 1000) **successful** tests, or a missing,
skipped or failed required regression. Skips do not count toward the minimum.
Console checkmarks and aggregate counts alone cannot pass the gate. Rediscovered
identities count only once and retain their worst result; a repeated success
cannot hide a failure or skip.

`ci/required-tests.json` names the exact existing regression methods for N-01
through N-06 (source isolation, file rollback/commit failure, price dimensions,
Customer lifecycle, optional related identities and late file attachment), plus
the verified writeback and binding-resume contracts. Renaming or deleting one
requires an explicit manifest update with equivalent behavioral coverage.
The manifest selects evidence; it does not introduce duplicate copies of tests.

The site suite explicitly opts in to the disposable site's source-snapshot,
site-reservation and checkpoint tests. These are real MariaDB checks, including
concurrent connections, rather than mocks of transaction visibility. Their nine
named tests must pass; the remaining site suite still runs in full. The offline
suite reserves integration classes for the site runner.

All skipped tests are listed by identity. Other opt-in tests requiring dedicated
socket databases, a separately distributed framework patch or a saved reference
schema are **not** implicitly enabled. A green gate does not claim those were
executed. Add new mandatory contracts and their isolated prerequisites together.

Full logs and `offline.xml`/`site.xml` are written to `HERO_TEST_LOG_DIR` (default
`$WORKDIR/results` on Woodpecker, `.ci-results/` in the GitHub checkout); both
success and failure logs are printed in CI. Woodpecker's report directory is
owned by the bench user even when the source checkout is read-only or owned by root.
Each invocation removes only its two previous XML files before starting, so an
interrupted run cannot reuse stale evidence. GitHub retains this directory as
an artifact for 14 days, including on failed runs. Woodpecker prints the full
logs and per-contract results; its container-local files are ephemeral unless
the operator supplies a dedicated persistent results directory. No report path
is mounted to the running test site or production.

## GitHub Actions

Pushes and pull requests targeting `hero_int_dev` or `hero_int_1.0` run package checks and an isolated install test on a
GitHub-hosted runner. It uses fresh MariaDB and Redis services, not the VPS test
site or production secrets. The push test installs ERPNext, Telephony, Helpdesk
and HERO, builds HERO assets, and runs both the offline suite and the complete
HERO Frappe-site test suite. Both must exit successfully, pass the shared JUnit regression gate and execute
at least `MIN_TESTS` successful tests (default 1000). Minimal selections without HERO remain available
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
checks but cannot enforce GitHub branch protection; that setting is
outside this repository and must be verified separately.

## Test-site update on GitHub push

A GitHub-backed Woodpecker repository is required for a GitHub `hero_int_dev`
push to start the test-site updater through its dedicated, restricted SSH secret.
The step verifies GitHub `hero_int_dev` is still at the exact pipeline commit
before requesting an update. If it has moved, it fails safely. The former
twice-daily VPS update timer is disabled. Each pipeline update reinstalls the dedicated test database, including
repeated runs of the same commit. It retains users, login password hashes,
role assignments and role/module profiles. User API keys and API secrets are
cleared; encrypted API credentials are not restored. Business records, HERO
configuration, custom schema, user defaults, record permissions and uploaded
files are discarded. The dedicated test queue is cleared while workers are
stopped, including deferred jobs, so old work cannot refill the database.
Installed apps and their standard installation data remain;
the setup wizard starts fresh. The CI configuration package is never restored
onto this site.

Pipeline resets create **no database dumps, site archives, Docker backup images
or backup volumes**. The identity snapshot lives only in container-local RAM
(`/dev/shm/frappe-test-users.json`) and is removed on success. A failed reset
retains that small identity snapshot and leaves maintenance enabled and workers
stopped for manual recovery. Preserve the failed backend container until its
identities have been recovered; replacing it loses the RAM snapshot.

The source of the host updater is `ci/frappe-test-update.py`; it invokes
`ci/reset-test-site.py` inside the backend. On the test server, install both into
`/srv/frappe-test-ctl/` with the same filenames, owned by root, mode `0600`.
The `update` action of `/usr/local/sbin/frappe-test` must execute
`python3 /srv/frappe-test-ctl/frappe-test-update.py` instead of its old embedded
Python updater. Keep the existing shared lock and forced SSH command.
The workflow sends `update:blank:<commit>` through the restricted SSH key; an
outdated pipeline cannot reset a later development head. The legacy SSH request
`update` also resets, so existing deployed workflows inherit the policy.
The twice-daily local timer is disabled; updates run from the GitHub pipeline.
This step depends on the successful isolated install test; a failed release test gate prevents the update.

## Release chain: hero_int_dev → hero_int_1.0

A push to GitHub `hero_int_dev` runs these Woodpecker steps once the GitHub
repository has been connected and its secrets configured in Woodpecker. Until
that external setup is complete, GitHub Actions runs but the Woodpecker test-site
update and release promotion do not run. Each
step starts only when the previous one succeeded:

1. `wait-github` waits up to 50 minutes for the GitHub Actions checks
   `package` and `install-test` of the same commit (`ci/wait-github-checks.sh`).
   A failed or missing check stops the chain.
2. `install-test` runs the bench install test.
3. `update-test-site` updates the code and resets the test site, retaining users.
4. `promote` (`ci/promote-release.sh`) releases the tested commit:
   - it adds a release commit `Release <version> [skip ci]` on top of it that
     changes only the version in `hero_integration/__init__.py` and
     `pyproject.toml` (next patch version; a version raised by hand on
     `hero_int_dev` is released as it is, without an extra commit);
   - it tags `v<version>`;
   - it fast-forwards `hero_int_dev` and `hero_int_1.0` on GitHub to the
     release commit, both branches and the tag in one atomic push, never forced.

If any step fails, `hero_int_1.0` keeps its state. If `hero_int_dev` moved on
in the meantime, the older pipeline promotes nothing; the newer commit's
pipeline does. An interrupted promotion is completed by re-running the
pipeline; it reuses the release commit and tag already pushed to GitHub.
`[skip ci]` keeps the promotion push from starting GitHub Actions or
Woodpecker again.

Because of the release commit, local clones must fetch `hero_int_dev` before
the next commit.

Required Woodpecker secrets (repository settings, push events only):

| Secret | Content |
|---|---|
| `github_release_token` | GitHub fine-grained token for `unwichtigfett/hero_integration`, permission *Contents: Read and write* |

The pipeline timeout of the Woodpecker repository must cover the GitHub wait
plus the install test (at least 120 minutes). If branch protection on
`hero_int_1.0` requires status checks, the token owner must be allowed to
bypass it, because the release commit itself is not built again.

## Codex code review before the push

The review runs before anything is pushed, on vps1, where the Codex CLI is
signed in with the owner's ChatGPT subscription (no API key). The Mac push
script calls

```bash
python3 /home/dev/frappe-bench/apps/hero_integration/ci/codex_review.py --head <commit>
```

on vps1. It reviews `origin/hero_int_1.0..<commit>` in a temporary detached
worktree with `codex exec` (read-only sandbox, ephemeral session, strict JSON
schema) and asks for evidence-backed findings with severity P1–P3, answered in
German. The report is stored in `~/hero-reviews/<commit>.md` (and `.json`); a
second run for the same commit and release reuses it (`--force` reviews again).

A P1 finding (exit 1) or a review that cannot run (exit 2) stops the push
script: nothing is pushed, the pipeline does not start and `hero_int_1.0`
keeps its state. `--block P2` is stricter, `--block none` reports only. The
review uses the Codex allowance of the subscription.
