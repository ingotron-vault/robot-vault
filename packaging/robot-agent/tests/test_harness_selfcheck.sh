#!/bin/sh
# The harness is the thing every other gate depends on, so it gets a gate too.
#
# The specific worry: `assert_status` captures the exit status with
#
#     LAST_OUTPUT=$( "$@" 2>&1 )
#     _actual=$?
#
# and that is only correct because a simple command consisting of ONE
# assignment whose value is a command substitution takes the substitution's
# exit status. Add a prefix word (`local`, `export`, `readonly`) and $? becomes
# the status of THAT builtin, which is almost always 0 -- at which point every
# assert_status in the suite silently passes forever. That failure mode is
# invisible, so it is asserted here rather than assumed.
#
# Also asserted: extract_function really extracts whole functions, and a
# deliberately failing assertion really registers as a failure.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='harness self-check'

# --- $? after an assignment-with-command-substitution ----------------------

_probe() { return "$1"; }

for want in 0 1 2 3 7 42; do
    case_start "\$? after an assignment captures exit status $want"
    _out=$( _probe "$want" 2>&1 )
    _got=$?
    if [ "$_got" -eq "$want" ]; then ok; else
        fail "assignment swallowed the status: expected $want, got $_got"
    fi
done

# The same shape assert_status uses, through assert_status itself.
_exit_with() { exit "$1"; }

assert_status 'assert_status observes a non-zero exit' 5 _exit_with 5
assert_status 'assert_status observes a zero exit' 0 _exit_with 0

# --- a failing assertion must actually register ----------------------------
#
# Run a throwaway harness instance in a subshell so the deliberate failure does
# not pollute this suite's counters, and check that it reports the failure and
# returns non-zero. Without this, "0 failed" could mean "nothing can fail".
case_start 'a deliberately wrong assertion is reported as a failure'
_sub=$( ( TESTS_RUN=0; TESTS_FAILED=0; TESTS_SKIPPED=0; SUITE_NAME='inner'
          assert_status 'this must fail' 0 _exit_with 3
          summary ) 2>&1 )
_sub_rc=$?
case $_sub in
    *'FAIL this must fail'*)
        if [ "$_sub_rc" -ne 0 ]; then ok; else
            fail "the inner suite reported the failure but exited 0:
$_sub"
        fi ;;
    *) fail "a wrong assertion was not reported:
$_sub" ;;
esac

# --- a suite that asserts nothing must not pass ----------------------------

case_start 'a suite with zero checks is a failure, not a pass'
_empty=$( ( TESTS_RUN=0; TESTS_FAILED=0; TESTS_SKIPPED=0; SUITE_NAME='empty'
            summary ) 2>&1 )
_empty_rc=$?
if [ "$_empty_rc" -ne 0 ]; then ok; else
    fail "an empty suite passed, so a missing tool or an early exit would look green:
$_empty"
fi

# --- extract_function ------------------------------------------------------

case_start 'extract_function returns a complete, sourceable function'
_ef=$(extract_function is_hex64)
case $_ef in
    'is_hex64() {'*'}') ok ;;
    *) fail "extraction is not a whole function:
$_ef" ;;
esac

case_start 'an extracted function actually works when sourced'
eval "$_ef"
if is_hex64 "$(printf '%064d' 0)" && ! is_hex64 'zz'; then ok; else
    fail 'the extracted is_hex64 does not behave'
fi

# The extraction terminates at the first `}` in column 0. Any shipped function
# containing such a line mid-body would be silently truncated -- and a
# truncated function usually still parses, so nothing would look wrong.
case_start 'no shipped function body contains a column-0 closing brace'
_bad=''
for _fn in $(sed -n 's/^\([a-z_][a-z0-9_]*\)() {$/\1/p' "$ROBOT_SH"); do
    _body=$(extract_function "$_fn")
    # Count column-0 '}' lines: exactly one (the terminator) is correct.
    _braces=$(printf '%s\n' "$_body" | grep -c '^}$' || true)
    [ "$_braces" -eq 1 ] || _bad="$_bad $_fn($_braces)"
done
if [ -z "$_bad" ]; then ok; else
    fail "functions whose extraction is ambiguous:$_bad"
fi

case_start 'every shipped function extracts to something non-empty'
_missing=''
for _fn in $(sed -n 's/^\([a-z_][a-z0-9_]*\)() {$/\1/p' "$ROBOT_SH"); do
    [ -n "$(extract_function "$_fn")" ] || _missing="$_missing $_fn"
done
if [ -z "$_missing" ]; then ok; else fail "no extraction for:$_missing"; fi

summary
