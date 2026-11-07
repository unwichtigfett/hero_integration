#!/usr/bin/env bash
# Publish the exact tested commit to hero_int_release after an opted-in manual CI.
# No version changes, tags, test-site updates or source-branch writes are made.
set -Eeuo pipefail

[[ ${CI_PIPELINE_EVENT:-} == manual ]] || {
    echo 'Candidate publication requires a manual pipeline.' >&2; exit 2;
}
message=${CI_COMMIT_MESSAGE:-}
# Woodpecker v3.20 wraps the UI message as "MANUAL: <message> @ <branch>".
message=${message#MANUAL: }
if [[ ! $message =~ ^-canidate([[:space:]]|$) ]]; then
    echo 'No leading -canidate marker; no release branch is changed.'
    exit 0
fi
[[ ${CI_PIPELINE_STATUS:-} == success ]] || {
    echo 'Candidate publication requires successful preceding CI steps.' >&2; exit 1;
}
source_branch=${CI_COMMIT_BRANCH:-}
case "$source_branch" in
    hero_int_dev|hero_int_dev_opt) ;;
    *) echo 'Unsupported candidate source branch.' >&2; exit 2 ;;
esac
sha=${CI_COMMIT_SHA:-}
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid tested commit.' >&2; exit 2; }
: "${GITHUB_REMOTE:?GITHUB_REMOTE is required}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git init -q "$work"
cd "$work"
# Keep credentials out of URLs, Git config and Git error output.
cat > "$work/askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
    *Username*) printf '%s\n' x-access-token ;;
    *Password*) printf '%s\n' "${GITHUB_TOKEN:-}" ;;
    *) exit 1 ;;
esac
ASKPASS
chmod 700 "$work/askpass"
export GIT_ASKPASS="$work/askpass" GIT_TERMINAL_PROMPT=0
git remote add github "$GITHUB_REMOTE"
git fetch -q --no-tags github \
    "+refs/heads/$source_branch:refs/remotes/github/source" \
    '+refs/heads/hero_int_release:refs/remotes/github/release'
[[ $(git rev-parse refs/remotes/github/source) == "$sha" ]] || {
    echo 'Source branch moved since this pipeline started; release is unchanged.' >&2; exit 1;
}
release=$(git rev-parse refs/remotes/github/release)
if [[ $release == "$sha" ]]; then
    echo "hero_int_release already contains the tested commit $sha."
    exit 0
fi
git merge-base --is-ancestor "$release" "$sha" || {
    echo 'hero_int_release has diverged; refusing to overwrite its history.' >&2; exit 1;
}
# A single, ordinary fast-forward push: concurrent incompatible updates fail.
git push -q github "$sha:refs/heads/hero_int_release"
published=$(git ls-remote --exit-code github refs/heads/hero_int_release | cut -f1)
[[ $published == "$sha" ]] || {
    echo 'Release branch changed during verification; inspect the remote before retrying.' >&2; exit 1;
}
echo "Published tested commit $sha to hero_int_release."
