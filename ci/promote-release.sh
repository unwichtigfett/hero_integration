#!/usr/bin/env bash
# Promote a tested development lane to its matching installation branch.
# Standard: hero_int_dev -> hero_int_1.0, v<version> tags.
# Opt: hero_int_dev_opt -> hero_int_opt, opt-v<version> tags.
# The opt release branch may be created by its first successful promotion.
# Only version lines may differ from the tested tree. Publication is atomic
# and never forced; a diverged release branch is rejected.
# COMMIT_SHA/CI_COMMIT_SHA and GITHUB_REMOTE are required. GITHUB_TOKEN is
# optional for local test remotes. CI_COMMIT_BRANCH selects the CI lane;
# RELEASE_CHANNEL selects it outside CI (default hero_int_dev).
set -Eeuo pipefail

case "${CI_COMMIT_BRANCH:-${RELEASE_CHANNEL:-hero_int_dev}}" in
    hero_int_dev) DEV=hero_int_dev; REL=hero_int_1.0; TAG_PREFIX=v ;;
    hero_int_dev_opt) DEV=hero_int_dev_opt; REL=hero_int_opt; TAG_PREFIX=opt-v ;;
    *) echo "Unsupported release source branch" >&2; exit 2 ;;
esac
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
git remote add github "$(with_token "${GITHUB_REMOTE:?GITHUB_REMOTE is required}" "${GITHUB_TOKEN:-}")"
REMOTES=(github)

for remote in "${REMOTES[@]}"; do
    git fetch -q --no-tags "$remote" "+refs/heads/$DEV:refs/r/$remote/dev"
    if git ls-remote --exit-code "$remote" "refs/heads/$REL" > /dev/null; then
        git fetch -q --no-tags "$remote" "+refs/heads/$REL:refs/r/$remote/rel"
    else
        status=$?
        [[ $status == 2 && $DEV == hero_int_dev_opt ]] || exit "$status"
    fi
    git fetch -q "$remote" "+refs/tags/*:refs/r/$remote/tags/*"
done

version_of() { git show "$1:hero_integration/__init__.py" | sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p'; }
newest() { printf '%s\n' "$@" | sort -V | tail -n 1; }
bump_patch() { local IFS=.; read -r major minor patch <<<"$1"; printf '%s.%s.%s' "$major" "$minor" "$((patch + 1))"; }
is_release_of() {  # commit tested-sha
    [[ $(git rev-parse "$1^" 2>/dev/null) == "$2" ]] && [[ $(git log -1 --format=%s "$1") == "$RELEASE_PREFIX"* ]]
}

git cat-file -e "$sha^{commit}" 2>/dev/null || { echo "Commit $sha is not on GitHub $DEV." >&2; exit 1; }

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
released=$current
if git show-ref --verify --quiet refs/r/github/rel; then
    released=$(version_of refs/r/github/rel)
fi

# The only allowed release change: the two version lines of the tested commit.
version_commit() {  # tested-sha version -> commit with exactly that change
    git checkout -q --detach "$1"
    sed -i -E "s/^__version__ = \"[0-9.]+\"$/__version__ = \"$2\"/" hero_integration/__init__.py
    sed -i -E "s/^version = \"[0-9.]+\"$/version = \"$2\"/" pyproject.toml
    git add "${VERSION_FILES[@]}"
    git commit -q --allow-empty -m "$RELEASE_PREFIX$2 $SKIP_MARK" -m "Tested commit: $1"
    git rev-parse HEAD
}
pyproject_version() { git show "$1:pyproject.toml" | sed -nE 's/^version = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p'; }

if [[ -n $release ]]; then
    version=$(version_of "$release")
    [[ -n $version ]] || { echo "The existing release commit has no version." >&2; exit 1; }
    # Reuse only a commit whose whole tree equals the tested tree plus the version lines.
    expected=$(version_commit "$sha" "$version")
    if [[ $(git rev-parse "$expected^{tree}") != $(git rev-parse "$release^{tree}") ]]; then
        echo "The existing release commit ${release:0:10} changes more than the version; nothing is pushed." >&2
        git diff --stat "$expected" "$release" >&2
        exit 1
    fi
elif [[ $(newest "$current" "$released") == "$current" && $current != "$released" ]]; then
    version=$current   # a version raised by hand on hero_int_dev is released as it is
    release=$sha
else
    version=$(bump_patch "$(newest "$current" "$released")")
    release=$(version_commit "$sha" "$version")
fi

[[ $(version_of "$release") == "$version" && $(pyproject_version "$release") == "$version" ]] || {
    echo "hero_integration/__init__.py and pyproject.toml must both carry version $version." >&2; exit 1; }

tag="$TAG_PREFIX$version"
reuse_tag=''
for remote in "${REMOTES[@]}"; do
    if git show-ref --verify --quiet "refs/r/$remote/rel" &&
            ! git merge-base --is-ancestor "refs/r/$remote/rel" "$release"; then
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
