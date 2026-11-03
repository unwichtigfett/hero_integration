#!/bin/sh
# Wait until GitHub Actions finished the required checks for one commit.
# Exit 0 only when every required check succeeded; any other result fails.
#
# Environment: COMMIT_SHA (required), GITHUB_REPO, REQUIRED_CHECKS,
# WAIT_SECONDS, POLL_SECONDS, GITHUB_TOKEN (optional, raises the API limit),
# GITHUB_API (tests only).
set -eu

sha="${COMMIT_SHA:-${CI_COMMIT_SHA:-}}"
[ -n "$sha" ] || { echo "COMMIT_SHA or CI_COMMIT_SHA is required" >&2; exit 2; }
repo="${GITHUB_REPO:-unwichtigfett/hero_integration}"
required="${REQUIRED_CHECKS:-package install-test}"
wait_seconds="${WAIT_SECONDS:-3000}"
poll="${POLL_SECONDS:-60}"
api="${GITHUB_API:-https://api.github.com}"

printf '%s\n' "$sha" | grep -Eq '^[0-9a-f]{40}$' || { echo "Invalid commit: $sha" >&2; exit 2; }

fetch() {
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsS -H "Authorization: Bearer $GITHUB_TOKEN" -H 'Accept: application/vnd.github+json' "$1"
    else
        curl -fsS -H 'Accept: application/vnd.github+json' "$1"
    fi
}

waited=0
while :; do
    if runs="$(fetch "$api/repos/$repo/commits/$sha/check-runs?per_page=100")"; then
        pending=''
        for name in $required; do
            # The newest GitHub Actions run of this name decides (a re-run replaces an older one).
            line="$(printf '%s' "$runs" | jq -r --arg n "$name" '
                [.check_runs[] | select(.name == $n and .app.slug == "github-actions")]
                | sort_by(.started_at) | last | if . == null then "missing" else "\(.status) \(.conclusion)" end')"
            case "$line" in
                "completed success") ;;
                completed*) echo "GitHub check $name finished as ${line#completed }; Woodpecker stops." >&2; exit 1 ;;
                *) pending="$pending $name(${line%% *})" ;;
            esac
        done
        if [ -z "$pending" ]; then
            echo "GitHub checks succeeded for $sha: $required"
            exit 0
        fi
        echo "Waiting for GitHub:$pending"
    else
        echo 'GitHub API not reachable; retrying.'
    fi
    if [ "$waited" -ge "$wait_seconds" ]; then
        echo "GitHub checks did not finish within $wait_seconds s." >&2
        exit 1
    fi
    sleep "$poll"
    waited=$((waited + poll))
done
