#!/bin/sh
# The vault bootstrap must serialize install, upgrade and re-enrollment.
# The lock is intentionally a kernel-backed flock rather than a PID file, so a
# killed bootstrap cannot strand the next recovery attempt.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='bootstrap lock'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

LOCK_FILE="$WORK/robot-bootstrap.lock"

run_lock_command() {
    ROBOT_LOCK_FILE="$LOCK_FILE"
    ROBOT_LOCK_HELD=0
    EX_PREFLIGHT=2
    die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
    eval "$(extract_function acquire_bootstrap_lock)"
    eval "$(extract_function release_bootstrap_lock)"
    acquire_bootstrap_lock
    if [ "${1:-}" = hold ]; then
        # Do not let the holder's sleep inherit fd 9. Otherwise a SIGKILL
        # test would quite correctly keep the lock until that child exits,
        # obscuring the kernel-release property being tested.
        sh -c 'exec 9>&-; sleep 2'
    fi
    release_bootstrap_lock
}

assert_status 'the first process acquires the bootstrap lock' 0 \
    run_lock_command

run_lock_command hold &
HOLDER_PID=$!
sleep 1
assert_status 'a concurrent bootstrap is refused while the lock is held' 2 \
    run_lock_command
wait "$HOLDER_PID"

assert_status 'the lock is reusable after a normal release' 0 \
    run_lock_command

run_lock_command hold &
HOLDER_PID=$!
sleep 1
kill -KILL "$HOLDER_PID" 2>/dev/null || true
wait "$HOLDER_PID" 2>/dev/null || true
assert_status 'SIGKILL releases the kernel-backed lock' 0 \
    run_lock_command

case_start 'preflight checks flock before any network or mutation'
_pf=$(extract_function preflight)
case $_pf in
    *'command -v flock'*'/run/lock'*) ok ;;
    *) fail 'preflight does not check flock and /run/lock' ;;
esac

case_start 'main acquires the lock before opening its temporary workdir'
_main=$(sed -n '/^main() {/,/^}$/p' "$ROBOT_SH")
_lock_line=$(printf '%s\n' "$_main" | grep -n '^    acquire_bootstrap_lock$' | cut -d: -f1)
_tmp_line=$(printf '%s\n' "$_main" | grep -n 'mktemp -d' | head -n 1 | cut -d: -f1)
if [ -n "$_lock_line" ] && [ -n "$_tmp_line" ] && [ "$_lock_line" -lt "$_tmp_line" ]; then
    ok
else
    fail "lock at ${_lock_line:-none}, mktemp at ${_tmp_line:-none}"
fi

summary
