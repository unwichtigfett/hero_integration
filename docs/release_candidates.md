# Manual release candidates

Start a manual Woodpecker pipeline on `hero_int_dev` or `hero_int_dev_opt`.
Begin its **message** with the exact marker `-canidate`, for example:

```text
-canidate Validate the next release
```

After the GitHub `package` and `install-test` checks and the complete Woodpecker
installation/import test pass for the same commit, `promote-candidate` advances
`hero_int_release` to that exact tested commit. It creates no version commit or
tag and does not change the source branch or reset the persistent test site.
Existing push promotions remain unchanged and retain their separate approval
requirements. An ordinary manual run without the leading marker only tests.

The release branch must already exist. Publication fails if the source has
moved since the manual pipeline started or if advancing `hero_int_release`
would not be a fast-forward. In particular, switching between development
branches with divergent histories requires a separately reviewed reconciliation;
the candidate script never overwrites the release history. Publishing the same
commit again succeeds without a push.

The repository needs a separate `github_candidate_token` secret, available only
for **manual** events. The GitHub credential needs repository contents write
access. Woodpecker v3.20 image-filtered secrets are restricted to plugins and
cannot be used by this commands step, so this secret has no image filter.
Repository access and the manual event restrict its availability; the pinned
image and marker condition are not credential authorization boundaries.
These restrictions do not limit the underlying GitHub credential to one Git
branch; GitHub rules must provide any additional branch-level restrictions.
Do not expand the event scopes of `github_release_token` or
`hero_site_update_key`. Only trusted repository administrators may start these
manual pipelines; the message marker expresses intent, not authentication.

Woodpecker v3.20 stores the UI message as `MANUAL: <message> @ <branch>`.
Both the workflow condition and script recognize that prefix and require
`-canidate` as a complete leading word, followed by whitespace or end of text.
The script also validates the manual event, successful preceding CI status,
allowed source branch and full tested commit SHA before contacting GitHub.

## Standard workflow

This branch uses `.woodpecker/standard.yml` and `ci/standard-install-test.sh`. Its workflow admits only `hero_int_dev` and `hero_int_1.0`. The required architecture groups in `ci/required-tests.json` belong to this branch and remain in addition to the full offline/site test suites and package-import gate. The manual candidate step admits only `hero_int_dev`. Woodpecker discovers the named workflow automatically; do not set a repository-wide path that breaks older branches.
## Opt workflow

This branch uses `.woodpecker/opt.yml` and `ci/opt-install-test.sh`. Its workflow admits only `hero_int_dev_opt` and `hero_int_opt`. The required architecture groups in `ci/required-tests.json` belong to this branch and remain in addition to the full offline/site test suites and package-import gate. The manual candidate step admits only `hero_int_dev_opt`. Woodpecker discovers the named workflow automatically; do not set a repository-wide path that breaks older branches.

## Consolidated candidate

The integrated runtime shares the Opt compiler and safety gates across both lanes. The standard and Opt Woodpecker workflows retain independent branch filters, promotion namespaces and credentials. Both lane install entry points delegate to ci/bench-install-test.sh; GitHub and that installer reuse ci/test-gates.sh. The integration branch does not invoke either live updater or promotion.

## Unified release target and explicit operations

The consolidated candidate uses version 1.1.0 (numeric release schema), with active development on hero_int_dev and stable release on hero_int_release. GitHub Actions also accepts integration/** candidates. The standard Woodpecker lane accepts integration/* and runs only wait-github/install-test for ordinary integration pushes. Historical Opt lane compatibility remains available without becoming a runtime prerequisite of the two target branches.

Automatic test-site updates and release promotion on push are disabled in the consolidated workflows. Those existing capabilities require a manual pipeline with DEPLOY_APPROVED or RELEASE_APPROVED respectively; the manual -canidate stable-promotion path retains its existing explicit gate. Merely pushing code to CI does not authorize a deployment or release. No remote protection/default branch setting is changed by this repository configuration.
