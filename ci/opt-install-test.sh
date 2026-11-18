#!/usr/bin/env bash
# Lane-compatible entry point; one canonical installation implementation.
set -euo pipefail
exec bash "$(dirname -- "${BASH_SOURCE[0]}")/bench-install-test.sh" "$@"
