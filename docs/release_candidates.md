# Test and publish a HERO release

| UI event | GitHub Actions | Woodpecker standard | Publication |
| --- | --- | --- | --- |
| Pull request into `hero_int_dev` | Package and full install test | — | — |
| Push to `hero_int_dev` | Package and full install test | Wait for both SHA checks, then isolated integration tests | — |
| GitHub Actions → CI → Run workflow on Dev | Package and full install test | — | — |
| New manual Woodpecker pipeline on Dev, ordinary message | Use existing checks | Same tests as a push | — |
| New manual Woodpecker pipeline on Dev, exact message `RELEASE_APPROVED` | Use existing checks | Same tests, then promote after success | Both branches and version tag |
| Push to `hero_int_release` or a version tag | — | — | — |

Open the GitHub Actions run to inspect the job logs, wheel, source distribution
and test reports. Woodpecker records the tested checkout, suite counts,
configuration-import result and per-step logs on the pipeline page. Its
install job uses a disposable Bench and the existing read-only fixture mount.
Compiler, cache, binding and native relationship tests use the shared
`ci/required-tests.json` manifest.

## Release approval

1. Confirm the desired Dev SHA in GitHub and the successful push run in Woodpecker.
2. As an authorized repository operator, start a **new manual pipeline** in
   Woodpecker. Select branch `hero_int_dev` and enter exactly `RELEASE_APPROVED`
   as its message. The repository discovers the `standard` workflow automatically.
   Approve execution in the UI if the repository approval policy requests it.
3. Open the promotion step to verify the tested SHA, publication SHA and tag.
   GitHub shows the stable tree and downloadable source archives at that tag.

The release step alone requests the repository secret `github_release_token`.
Configure its Woodpecker event scope to **manual only** and restrict manual
pipeline execution/approval to trusted repository operators. Scope the GitHub
credential to contents writes on this repository. Ordinary CI uses public
GitHub check results. The marker expresses release intent within a manual run.

Reusing a manually increased version publishes the exact tested SHA. Otherwise
the gate creates a direct child with a patch-version bump in the two package
version fields and a `[skip ci]` subject. It fast-forwards both branches and
publishes `v<version>` in one atomic push. Repeating an already published
transaction verifies the branches and tag. A moved source, divergent release
or conflicting tag requires inspection. Start a fresh test on the intended
Dev HEAD after resolving the cause.

## Separate test-site maintenance

The existing restricted updater accepts `update:blank:<dev-sha>`. A separately
authorized operator with the dedicated SSH identity may use
`sh ci/update-test-site.sh DEPLOY_APPROVED <full-dev-sha>` with
`HERO_UPDATE_IDENTITY` pointing to the identity file. The command checks the
GitHub Dev HEAD and pins the existing host key. The checked-in server-side
updater and reset tools retain their existing backup and restore behavior.
Neither CI provider invokes this operation. Production deployment remains a
separate operations approval.
