#!/usr/bin/env bash
# Woodpecker invokes this step after both CI gates. Only Git refs are published.
set -Eeuo pipefail
die() { echo "$*" >&2; exit 1; }

dev=hero_int_dev
rel=hero_int_release
sha=${CI_COMMIT_SHA:-}
[[ ${CI_PIPELINE_EVENT:-} == manual && ${CI_COMMIT_BRANCH:-} == "$dev" &&
   ${CI_COMMIT_MESSAGE:-} == "MANUAL: RELEASE_APPROVED @ $dev" ]] || die "Manual release approval required."
[[ ${CI_PIPELINE_STATUS:-} == success ]] || die "Preceding CI steps must succeed."
[[ $sha =~ ^[0-9a-f]{40}$ ]] || die "Invalid tested commit."
remote=${GITHUB_REMOTE:?}
case "$remote" in
    https://github.com/unwichtigfett/hero_integration.git) : "${GITHUB_TOKEN:?Release credential required}" ;;
    /*) [[ -z ${GITHUB_TOKEN:-} ]] || die "Local test remotes cannot receive credentials." ;;
    *) die "Unexpected release remote." ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git init -q "$work"
cd "$work"
cat > .git/askpass <<'ASKPASS'
#!/bin/sh
case "$1" in
    *Username*) printf '%s\n' x-access-token ;;
    *Password*) printf '%s\n' "$GITHUB_TOKEN" ;;
    *) exit 1 ;;
esac
ASKPASS
chmod 700 .git/askpass
export GIT_ASKPASS="$work/.git/askpass" GIT_TERMINAL_PROMPT=0
git config credential.helper ''
git config user.name 'HERO release CI'
git config user.email '95431041+unwichtigfett@users.noreply.github.com'
git remote add github "$remote"
git fetch -q --no-tags github "refs/heads/$dev:refs/r/dev" "refs/heads/$rel:refs/r/rel" 'refs/tags/*:refs/tags/*'
tip=$(git rev-parse refs/r/dev)
stable=$(git rev-parse refs/r/rel)
version_of() {
    local app package
    app=$(git show "$1:hero_integration/__init__.py" | sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p')
    package=$(git show "$1:pyproject.toml" | sed -nE 's/^version = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p')
    [[ $app =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ && $app == "$package" ]] || die "Inconsistent version at $1."
    printf '%s\n' "$app"
}
stage_version() {
    git checkout -q --detach "$sha"
    sed -i -E "s/^__version__ = \"[0-9.]+\"$/__version__ = \"$1\"/" hero_integration/__init__.py
    sed -i -E "s/^version = \"[0-9.]+\"$/version = \"$1\"/" pyproject.toml
    git add -- hero_integration/__init__.py pyproject.toml
}

current=$(version_of "$sha")
released=$(version_of "$stable")
tag_ref="refs/tags/v$released"
if [[ $tip == "$stable" && $(git rev-parse -q --verify "$tag_ref^{commit}" || true) == "$stable" ]]; then
    if [[ $tip == "$sha" ]]; then
        echo "Already published $tip as v$released."
        exit 0
    fi
    if [[ $(git show -s --format=%P "$tip") == "$sha" &&
          $(git show -s --format=%s "$tip") == "Release $released [skip ci]" ]]; then
        stage_version "$released"
        [[ $(git write-tree) == "$(git rev-parse "$tip^{tree}")" ]] || die "Unexpected release tree."
        echo "Already published $sha as v$released ($tip)."
        exit 0
    fi
fi
[[ $tip == "$sha" ]] || die "Development HEAD moved since testing."
git merge-base --is-ancestor "$stable" "$sha" || die "Release history diverged."
version=$current
release=$sha
latest=$(printf '%s\n' "$current" "$released" | sort -V | tail -n 1)
if [[ $current == "$released" || $latest != "$current" ]]; then
    version="${latest%.*}.$(( ${latest##*.} + 1 ))"
    stage_version "$version"
    git commit -qm "Release $version [skip ci]" -m "Tested commit: $sha"
    release=$(git rev-parse HEAD)
fi
[[ $(version_of "$release") == "$version" ]] || die "Version verification failed."
tag="refs/tags/v$version"
if git show-ref --verify --quiet "$tag"; then
    [[ $(git rev-parse "$tag^{commit}") == "$release" ]] || die "Version tag points to another commit."
else
    git tag -a "v$version" -m "HERO Integration $version" "$release"
fi
git push -q --atomic github "$release:refs/heads/$dev" "$release:refs/heads/$rel" "$tag"
actual=$(git ls-remote --refs github "refs/heads/$dev" "refs/heads/$rel" "$tag")
expected=$(printf '%s\t%s\n' "$release" "refs/heads/$dev" "$release" "refs/heads/$rel" "$(git rev-parse "$tag")" "$tag")
[[ $(sort <<< "$actual") == "$(sort <<< "$expected")" ]] || die "Published refs changed during verification."
echo "Published $sha as v$version ($release) on $dev and $rel."
