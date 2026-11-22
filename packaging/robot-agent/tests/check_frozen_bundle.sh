#!/usr/bin/env bash
# Run an already-built agent tarball on genuinely clean Debian/Ubuntu roots.
set -euo pipefail

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$TEST_ROOT/../../.." && pwd)
TARBALL=${1:-}
[ -n "$TARBALL" ] && [ -f "$TARBALL" ] || {
  echo "usage: check_frozen_bundle.sh agent-linux-amd64.tar.gz" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || { echo "Docker is required" >&2; exit 2; }

WORK=$(mktemp -d "${TMPDIR:-/tmp}/agent-clean-host.XXXXXX")
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

# This helper is also useful standalone, so validate names/types before the
# first extraction instead of relying only on its usual caller's archive gate.
tar -tzf "$TARBALL" > "$WORK/members"
ROOT_NAME=$(cut -d/ -f1 "$WORK/members" | LC_ALL=C sort -u)
printf '%s' "$ROOT_NAME" | grep -Eq '^ingotron-agent-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
  || { echo "clean-host smoke: archive root is invalid" >&2; exit 1; }
while IFS= read -r member; do
  case "$member" in
    "$ROOT_NAME"|"$ROOT_NAME"/*) ;;
    *) echo "clean-host smoke: member outside the agent root: $member" >&2; exit 1 ;;
  esac
  case "/$member/" in
    */../*) echo "clean-host smoke: parent-directory member: $member" >&2; exit 1 ;;
  esac
done < "$WORK/members"
tar -tvzf "$TARBALL" > "$WORK/member-details"
if awk '$1 ~ /^[lhbcp]/ {print; bad=1} END {exit bad ? 0 : 1}' \
    "$WORK/member-details" > "$WORK/special-members"; then
  echo "clean-host smoke: archive contains a link or special member" >&2
  cat "$WORK/special-members" >&2
  exit 1
fi
tar -xzf "$TARBALL" -C "$WORK"
ROOT=$(find "$WORK" -mindepth 1 -maxdepth 1 -type d -name 'ingotron-agent-*' -print)
[ -n "$ROOT" ] && [ "$(printf '%s\n' "$ROOT" | wc -l)" -eq 1 ] || {
  echo "clean-host smoke: archive does not have one agent root" >&2
  exit 1
}
VERSION=$(cat "$ROOT/VERSION")
EXPECTED_LICENSE=$(sha256sum "$REPO_ROOT/LICENSE" | cut -d' ' -f1)
ACTUAL_LICENSE=$(sha256sum "$ROOT/LICENSE" | cut -d' ' -f1)
[ "$ACTUAL_LICENSE" = "$EXPECTED_LICENSE" ] || {
  echo "clean-host smoke: agent LICENSE differs from source authority" >&2
  exit 1
}

# Platform manifests are pinned: Debian 11 and Ubuntu 20.04 both provide glibc
# 2.31, while neither image contains Python, grpc, protobuf, or prometheus.
HOSTS=(
  "debian:bullseye-slim@sha256:4a2e40d0d34f8f86f60ef0d79c14d3b6b3d2620825dcdf93152535b5efbaf490"
  "ubuntu:20.04@sha256:c664f8f86ed5a386b0a340d981b8f81714e21a8b9c73f658c4bea56aa179d54a"
)
for host in "${HOSTS[@]}"; do
  echo "== clean-host frozen-agent smoke: $host"
  docker run --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --platform linux/amd64 --user 65534:65534 \
    -e PATH=/usr/bin:/bin \
    -v "$ROOT:/agent:ro" "$host" sh -eu -c '
      if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
        echo "clean host unexpectedly contains Python" >&2
        exit 1
      fi
      test ! -e /agent/bin/python3
      test "$(/agent/bin/ingotron-agent --version)" = "$1"
      /agent/bin/ingotron-agent __bundle-self-test > /tmp/self-test.json
      grep -F "\"frozen\": true" /tmp/self-test.json >/dev/null
      grep -F "\"imports\": \"ok\"" /tmp/self-test.json >/dev/null
      /agent/bin/ingotron-agent status --config /does-not-exist > /tmp/status.json
      grep -F "\"agent_version\":" /tmp/status.json >/dev/null
      set +e
      /agent/bin/ingotron-agent-run --config /does-not-exist > /tmp/run.json 2>&1
      run_status=$?
      /agent/bin/ingotron-agent-retention --config /does-not-exist > /tmp/retention.json 2>&1
      retention_status=$?
      set -e
      test "$run_status" -eq 2
      test "$retention_status" -eq 2
      grep -F "\"stage\": \"preflight\"" /tmp/run.json >/dev/null
      grep -F "\"stage\": \"retention\"" /tmp/retention.json >/dev/null
    ' sh "$VERSION" </dev/null
done

echo "clean-host frozen-agent smoke: PASS ($VERSION; Debian 11 + Ubuntu 20.04)"
