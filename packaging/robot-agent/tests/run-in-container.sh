#!/usr/bin/env bash
# Run every shell gate as ROOT, under dash AND under
# busybox ash, on a host where `bash` is shadowed by a trap.
#
# Why a container rather than the host:
#   * `id -u` must be 0 for the preflight to be reached at all, and busybox ash
#     resolves `id` as a built-in applet BEFORE consulting PATH, so a PATH stub
#     is silently ignored. Being genuinely root removes the stub entirely.
#   * The goal is a host with no bash installed. Debian cannot have bash
#     removed without removing half the system, so bash is replaced here by a
#     trap that fails loudly and records the fact: if robot.sh ever reaches for
#     bash, the marker file exists and this gate fails. That is the property that
#     matters -- "does not depend on bash" -- proven positively rather than
#     by the absence of a package.
#
# Everything runs in Docker per the project convention (no host toolchains).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PKG_ROOT=$(dirname "$HERE")
REPO_ROOT=$(cd "$PKG_ROOT/../.." && pwd)
IMAGE=ingotron-robot-shell-gate:latest

DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
    DOCKER=(sg docker -c)
fi

run_docker() {
    if [ "${DOCKER[0]}" = "sg" ]; then
        # `sg` takes ONE shell string, so the arguments have to be re-quoted or
        # the embedded `dash -c '...'` payload is word-split apart.
        local quoted=""
        local arg
        for arg in "$@"; do
            quoted+=" $(printf '%q' "$arg")"
        done
        sg docker -c "$quoted"
    else
        "$@"
    fi
}

build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT

cat > "$build_dir/Dockerfile" <<'DOCKERFILE'
FROM debian:12-slim
# The exact host tool contract /robot is allowed to assume:
# tar, a SHA-256 tool, curl, and openssl >= 1.1.1. Plus dash and busybox as the
# two shells under test.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      dash busybox openssl curl tar ca-certificates coreutils sed grep \
 && rm -rf /var/lib/apt/lists/*

# systemd is not installed here, but the preflight only checks for the
# directory's existence, and creating it is what lets the argument and
# artifact gates run. A container is not a robot; the E2E install path is
# the end-to-end test's job, not this gate's.
RUN mkdir -p /run/systemd/system

# `bash` becomes a trap. If robot.sh (or any suite) invokes it, the marker
# appears and the gate fails.
RUN printf '#!/bin/dash\nprintf "BASH WAS INVOKED: %%s\\n" "$*" >> /tmp/bash-was-invoked\nexit 127\n' > /usr/local/bin/bash \
 && chmod 0755 /usr/local/bin/bash \
 && printf '#!/bin/dash\nprintf "BASH WAS INVOKED: %%s\\n" "$*" >> /tmp/bash-was-invoked\nexit 127\n' > /bin/bash \
 && chmod 0755 /bin/bash
DOCKERFILE

echo "=== building the shell-gate image ==="
run_docker docker build -q -t "$IMAGE" "$build_dir"

status=0
for shell in "dash" "busybox ash"; do
    echo
    echo "############################################################"
    echo "### shell gates under '$shell', as root, no bash ###"
    echo "############################################################"
    if run_docker docker run --rm \
        -v "$REPO_ROOT:/repo:ro" \
        -w /repo \
        "$IMAGE" \
        dash -c "rm -f /tmp/bash-was-invoked; \
                 dash /repo/packaging/robot-agent/tests/run-all.sh '$shell'; \
                 rc=\$?; \
                 if [ -e /tmp/bash-was-invoked ]; then \
                   echo 'FAIL: bash was invoked'; cat /tmp/bash-was-invoked; exit 1; \
                 fi; \
                 echo 'bash was never invoked'; exit \$rc"; then
        echo "PASS: $shell"
    else
        echo "FAIL: $shell" >&2
        status=1
    fi
done

exit "$status"
