#!/usr/bin/env bash
# Keep existing installations' entry point during replacement of the queue units.
exec bash apps/hero_integration/ops/dev-workers/shared-workers.sh 4 0
