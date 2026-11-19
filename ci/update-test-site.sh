#!/bin/sh
# Operator entry point for the separately authorized, forced-command SSH key.
set -eu
[ "${1:-}" = DEPLOY_APPROVED ] && [ "$#" -eq 2 ] || exit 2
: "${HERO_UPDATE_IDENTITY:?}"
known_hosts=$(mktemp)
trap 'rm -f "$known_hosts"' EXIT
printf '%s\n' '100.90.177.57 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICoj1r6d6+za2IdYIKkBGDddbAEGCvZPF2gl0MpfplRj' > "$known_hosts"
expected="$2"
printf '%s\n' "$expected" | grep -Eq '^[0-9a-f]{40}$' || { echo 'Invalid commit'; exit 1; }
found=''
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  found="$(git ls-remote https://github.com/unwichtigfett/hero_integration.git refs/heads/hero_int_dev | cut -f1 || true)"
  [ "$found" = "$expected" ] && break
  sleep 10
done
[ "$found" = "$expected" ] || { echo 'GitHub branch did not reach this pipeline commit'; exit 1; }
ssh -T -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$known_hosts" -i "$HERO_UPDATE_IDENTITY" -o BatchMode=yes -o ServerAliveInterval=30 root@100.90.177.57 "update:blank:$expected"
