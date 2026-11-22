#!/bin/sh
# compose_up.sh <timeout-seconds> docker compose <global args...>
#
# The `up -d --remove-orphans` step of release-up, closed against data loss:
#  - stdin is /dev/null, so compose's "Volume ... doesn't match configuration
#    in compose file. Recreate (data will be lost)?" can never wait for an
#    answer, and the caller's stdin can never answer yes. Never add -y/--yes.
#  - compose 5.x then takes the default answer (no), keeps the volume and exits
#    0 (checked with a probe run). This script fails instead, so a
#    volume compose wants to recreate always stops the release.
#  - `timeout -k 30` ends any other hang (compose ignores SIGTERM at a prompt).
# volume_guard.py refuses the drift it can see before this runs; this catches
# what only compose sees (for example its own config hash after an upgrade).
set -u
limit=$1; shift
dir=$(mktemp -d) || exit 1
trap 'rm -rf "$dir"' EXIT
{ timeout -k 30 "$limit" "$@" up -d --remove-orphans </dev/null 2>&1; echo $? >"$dir/rc"; } | tee "$dir/log"
status=$(cat "$dir/rc")
case $status in 124|137) echo "release-up: compose up did not finish within ${limit}s" >&2;; esac
if grep -qi 'data will be lost' "$dir/log"; then
  echo "REFUSING: compose found a data volume that differs from its configuration and asked to" \
    "recreate it (its data would be lost). It kept the volume (the answer from /dev/null is no);" \
    "the release stops here. Compare 'docker volume inspect' with the compose files." >&2
  exit 1
fi
exit "$status"
