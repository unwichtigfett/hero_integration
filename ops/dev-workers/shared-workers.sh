#!/usr/bin/env bash
# Every process consumes every queue. Rotate its first queue on pool startup.
set -euo pipefail
count=${1:?number of workers required}
offset=${2:-0}
[[ "$count" =~ ^[1-8]$ && "$offset" =~ ^[0-3]$ ]] || exit 2
env/bin/python apps/hero_integration/ops/dev-workers/configure.py

queues=(short default long hero_binding)
children=()
shutdown() {
    trap - TERM INT EXIT
    if (( ${#children[@]} )); then
        kill -TERM "${children[@]}" 2>/dev/null || true
        for child in "${children[@]}"; do wait "$child" || true; done
    fi
}
trap shutdown EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
for ((i=0; i<count; i++)); do
    order=()
    for ((q=0; q<4; q++)); do
        order+=("${queues[(offset+i+q)%4]}")
    done
    IFS=,; queue_list="${order[*]}"; unset IFS
    bench worker --queue "$queue_list" --strategy round_robin &
    children+=("$!")
done
wait -n || true
# Docker restarts every process if a queue consumer exits unexpectedly.
exit 1
