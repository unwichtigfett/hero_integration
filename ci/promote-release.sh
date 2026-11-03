#!/usr/bin/env bash
# Promote a fully tested hero_int_dev commit to the installation branch.
#
# Runs only after GitHub CI and the Woodpecker install test succeeded for
# COMMIT_SHA. It raises the patch version in a release commit on top of the
# tested commit (only the two version lines change), tags it v<version> and
# fast-forwards hero_int_dev and hero_int_1.0 to it on Forgejo and GitHub.
# Nothing is ever force-pushed: if a branch moved, the push is rejected and
# hero_int_1.0 keeps its state.
#
# Environment:
#   COMMIT_SHA       tested commit (required)
#   FORGEJO_REMOTE   e.g. https://hurensohn.tailbbed27.ts.net/unwichtigfett/hero_integration.git
#   GITHUB_REMOTE    e.g. https://github.com/unwichtigfett/hero_integration.git
#   FORGEJO_TOKEN, GITHUB_TOKEN   write tokens (optional for local paths)
#   FORGEJO_USER     user for the Forgejo token (default unwichtigfett)
set -Eeuo pipefail

DEV=hero_int_dev
REL=hero_int_1.0
RELEASE_PREFIX="Release "
SKIP_MARK="[skip ci]"
VERSION_FILES=(hero_integration/__init__.py pyproject.toml)

sha="${COMMIT_SHA:-${CI_COMMIT_SHA:-}}"
[ -n "$sha" ] || { echo "COMMIT_SHA or CI_COMMIT_SHA is required" >&2; exit 2; }
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit: $sha" >&2; exit 2; }

with_token() {  # url token user
    local url=$1 token=${2:-} user=${3:-x-access-token}
    if [[ -n $token && $url == https://* ]]; then
        printf 'https://%s:%s@%s' "$user" "$token" "${url#https://}"
    else
        printf '%s' "$url"
    fi
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git init -q "$work"
cd "$work"
git config user.name "${RELEASE_AUTHOR_NAME:-unwichtigfett}"
git config user.email "${RELEASE_AUTHOR_EMAIL:-95431041+unwichtigfett@users.noreply.github.com}"
git config advice.detachedHead false
git remote add forgejo "$(with_token "${FORGEJO_REMOTE:?FORGEJO_REMOTE is required}" "${FORGEJO_TOKEN:-}" "${FORGEJO_USER:-unwichtigfett}")"
git remote add github "$(with_token "${GITHUB_REMOTE:?GITHUB_REMOTE is required}" "${GITHUB_TOKEN:-}")"
REMOTES=(forgejo github)

for remote in "${REMOTES[@]}"; do
    git fetch -q --no-tags "$remote" "+refs/heads/$DEV:refs/r/$remote/dev" "+refs/heads/$REL:refs/r/$remote/rel"
    git fetch -q "$remote" "+refs/tags/*:refs/r/$remote/tags/*"
done

version_of() { git show "$1:hero_integration/__init__.py" | sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p'; }
newest() { printf '%s\n' "$@" | sort -V | tail -n 1; }
bump_patch() { local IFS=.; read -r major minor patch <<<"$1"; printf '%s.%s.%s' "$major" "$minor" "$((patch + 1))"; }
is_release_of() {  # commit tested-sha
    [[ $(git rev-parse "$1^" 2>/dev/null) == "$2" ]] && [[ $(git log -1 --format=%s "$1") == "$RELEASE_PREFIX"* ]]
}

git cat-file -e "$sha^{commit}" 2>/dev/null || { echo "Commit $sha is not on $DEV of either remote." >&2; exit 1; }

# A release commit already pushed to one remote (an earlier, interrupted run) is reused.
release=''
for remote in "${REMOTES[@]}"; do
    tip=$(git rev-parse "refs/r/$remote/dev")
    if [[ $tip == "$sha" ]]; then
        continue
    elif is_release_of "$tip" "$sha"; then
        release=$tip
    elif git merge-base --is-ancestor "$sha" "$tip"; then
        echo "$remote/$DEV already moved past $sha; the newer commit's pipeline promotes it. $REL is unchanged."
        exit 0
    else
        echo "$remote/$DEV does not contain $sha; $REL is unchanged." >&2
        exit 1
    fi
done

current=$(version_of "$sha")
[[ -n $current ]] || { echo "No __version__ in $sha." >&2; exit 1; }
released=$(newest "$(version_of refs/r/forgejo/rel)" "$(version_of refs/r/github/rel)")

if [[ -n $release ]]; then
    version=$(version_of "$release")
elif [[ $(newest "$current" "$released") == "$current" && $current != "$released" ]]; then
    version=$current   # a version raised by hand on hero_int_dev is released as it is
    release=$sha
else
    version=$(bump_patch "$(newest "$current" "$released")")
    git checkout -q --detach "$sha"
    sed -i -E "s/^__version__ = \"[0-9.]+\"$/__version__ = \"$version\"/" hero_integration/__init__.py
    sed -i -E "s/^version = \"[0-9.]+\"$/version = \"$version\"/" pyproject.toml
    git add "${VERSION_FILES[@]}"
    git commit -q -m "$RELEASE_PREFIX$version $SKIP_MARK" -m "Tested commit: $sha"
    release=$(git rev-parse HEAD)
fi

# The release commit may only change the version lines.
changed=$(git diff --name-only "$sha" "$release" | sort | tr '\n' ' ')
[[ -z $changed || $changed == "$(printf '%s\n' "${VERSION_FILES[@]}" | sort | tr '\n' ' ')" ]] || {
    echo "Release commit changes more than the version: $changed" >&2; exit 1; }
[[ $(version_of "$release") == "$version" ]] || { echo "Version bump failed." >&2; exit 1; }

tag="v$version"
reuse_tag=''
for remote in "${REMOTES[@]}"; do
    if ! git merge-base --is-ancestor "refs/r/$remote/rel" "$release"; then
        echo "$remote/$REL is not an ancestor of $release; it stays unchanged." >&2
        exit 1
    fi
    existing=$(git rev-parse -q --verify "refs/r/$remote/tags/$tag^{commit}" || true)
    if [[ -n $existing && $existing != "$release" ]]; then
        echo "Tag $tag already exists on $remote for another commit." >&2
        exit 1
    fi
    [[ -n $existing ]] && reuse_tag="refs/r/$remote/tags/$tag"
done
if [[ -n $reuse_tag ]]; then
    git update-ref "refs/tags/$tag" "$(git rev-parse "$reuse_tag")"   # same tag object everywhere
else
    git tag -a "$tag" -m "HERO Integration $version" "$release"
fi

for remote in "${REMOTES[@]}"; do
    # --atomic: both branches and the tag move together or not at all; no force.
    git push -q --atomic "$remote" "$release:refs/heads/$DEV" "$release:refs/heads/$REL" "refs/tags/$tag"
    echo "$remote: $DEV and $REL at ${release:0:10} ($tag)"
done
echo "Promoted $sha as $version."
