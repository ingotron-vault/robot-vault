#!/bin/sh
# Run every shell gate in this directory under one shell, and fail on the first
# suite that fails.  The shell under test is $1 (default: the shell running
# this file), and it is exported so each suite can invoke robot.sh with it.
#
#   ./run-all.sh dash
#   ./run-all.sh 'busybox ash'
#
# The busybox half must run AS ROOT, because busybox resolves `id` as a
# built-in applet before it consults PATH -- see tests/run-in-container.sh,
# which runs this gate as root.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
SHELL_UNDER_TEST=${1:-/bin/sh}
export SHELL_UNDER_TEST

# The suite files themselves are run by the same shell, so a bashism in a TEST
# is caught too.
RUNNER=$SHELL_UNDER_TEST

failed=0
for suite in "$TEST_ROOT"/test_*.sh; do
    name=$(basename "$suite")
    printf '\n=== %s  [%s] ===\n' "$name" "$SHELL_UNDER_TEST"
    if $RUNNER "$suite"; then
        :
    else
        failed=$(( failed + 1 ))
        printf '*** %s FAILED under %s\n' "$name" "$SHELL_UNDER_TEST" >&2
    fi
done

printf '\n===============================\n'
if [ "$failed" -eq 0 ]; then
    printf 'ALL SUITES PASSED under %s\n' "$SHELL_UNDER_TEST"
    exit 0
fi
printf '%d SUITE(S) FAILED under %s\n' "$failed" "$SHELL_UNDER_TEST" >&2
exit 1
