#!/bin/sh
# The promise robot.sh step 7 makes.
#
#   "Verify the `ingotron` user can write the recordings tree; if not, print
#    the exact setfacl/chgrp command and exit non-zero AFTER the credential is
#    safely stored, so a re-run does not need a new code."
#
# The credential is written in step 6; steps 7 and 8 run after it. So every
# failure past the exchange leaves a token with no units -- and if the
# "already installed" refusal keys on the TOKEN, that state is
# indistinguishable from a completed install and every recovery path is shut:
# the one-liner refuses, `enroll --force` needs a code that was spent by the
# successful exchange. A chmod on the
# recordings tree would brick the robot while this very script printed
# "re-running this command does NOT need a new enrollment code".
#
# The fix is that "already installed" means the COMPLETION MARKER exists, not
# the token. This suite asserts the distinction directly against the shipped
# script, because the difference between the two is invisible in any test that
# only ever runs a clean install.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='resume after a partial install'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

make_stub_path "$WORK"
render_robot_sh "$WORK"
SHELL_UNDER_TEST=${SHELL_UNDER_TEST:-/bin/sh}

# refuse_existing_install is driven directly with the paths relocated into the
# sandbox, so the decision is exercised without needing root or a real /etc.
run_refusal() {
	( TOKEN_FILE="$1"
	  INSTALL_MARKER="$2"
	  ENROLLMENT_PENDING="$WORK/enrollment-pending"
	  AGENT_PREFIX="$WORK/opt-absent"
      RESUMING=0
      EX_PREFLIGHT=2
      warn() { printf 'robot.sh: %s\n' "$*" >&2; }
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      eval "$(extract_function is_digits)"
      eval "$(extract_function is_semver)"
      eval "$(extract_function file_size)"
      eval "$(extract_function install_marker_present)"
      eval "$(extract_function install_marker_version)"
      eval "$(extract_function refuse_existing_install)"
      refuse_existing_install
      printf 'PROCEED resuming=%s\n' "$RESUMING" )
}

TOKEN="$WORK/robot.token"
MARKER="$WORK/install-complete"

# 1. A clean host proceeds, not resuming.
assert_status 'a clean host proceeds' 0 run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'PROCEED resuming=0'

# 2. Token present, NO marker -- the state a step-7 refusal leaves. This is the
#    case that used to be a dead end.
printf 'ignr1.x' > "$TOKEN"
assert_status 'a token with no completion marker RESUMES rather than refusing' 0 \
    run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'PROCEED resuming=1'
assert_output_contains 'none is needed'

# A crash may leave the private pending record before the token rename. That is
# also a local resume, never permission to spend the supplied code again.
rm -f "$TOKEN"
touch "$WORK/enrollment-pending"
assert_status 'a recovery record with no token resumes locally' 0 \
    run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'PROCEED resuming=1'
assert_output_contains 'recovery record exists'
rm -f "$WORK/enrollment-pending"

# A marker path that exists but is not an exact version line is not completion
# evidence. Preflight only warns; removal is deferred until the lock is held.
: > "$MARKER"
assert_status 'an empty completion marker is recoverable' 0 \
    run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'empty, partial or corrupt'
assert_file_present 'preflight leaves the invalid marker for the locked recheck' "$MARKER"

printf '0.9.1\nJUNK' > "$MARKER"
assert_status 'a completion marker with an extra unterminated tail is recoverable' 0 \
    run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'empty, partial or corrupt'

printf '0.9' > "$MARKER"
assert_status 'a partial version marker is recoverable' 0 \
    run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'empty, partial or corrupt'

rm -f "$MARKER"
printf '0.9.1\n' > "$WORK/valid-marker-target"
ln -s "$WORK/valid-marker-target" "$MARKER"
assert_status 'a symlink at the completion marker path is recoverable' 0 \
    run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'empty, partial or corrupt'
rm -f "$MARKER" "$WORK/valid-marker-target"

# 3. Marker present -- a genuinely completed install still refuses, which is
#    what the install contract requires.
printf '0.9.1\n' > "$MARKER"
printf 'ignr1.x' > "$TOKEN"
assert_status 'a completed install is still refused' 2 run_refusal "$TOKEN" "$MARKER"
assert_output_contains 'already enrolled and installed'

# 4. Marker but no token: a purge that removed credentials and left the marker.
#    Treated as complete; the operator is pointed at the documented commands
#    rather than silently re-enrolling over a half-purged tree.
rm -f "$TOKEN"
assert_status 'a marker without a token is still refused' 2 run_refusal "$TOKEN" "$MARKER"

# The invalid marker is removed only after the bootstrap lock is acquired.
run_locked_recheck() {
    (
        INSTALL_MARKER="$1"
        ENROLLMENT_PENDING="$WORK/enrollment-pending"
        TOKEN_FILE="$WORK/robot.token"
        UPGRADE_MODE=0
        REENROLL_MODE=0
        RESUMING=0
        ROBOT_LOCK_HELD=0
        ROBOT_LOCK_FILE="$WORK/bootstrap.lock"
        EX_PREFLIGHT=2
        warn() { printf 'robot.sh: %s\n' "$*" >&2; }
        die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
        eval "$(extract_function is_digits)"
        eval "$(extract_function is_semver)"
        eval "$(extract_function file_size)"
        eval "$(extract_function install_marker_present)"
        eval "$(extract_function install_marker_version)"
        eval "$(extract_function acquire_bootstrap_lock)"
        eval "$(extract_function release_bootstrap_lock)"
        eval "$(extract_function recheck_fresh_install_under_lock)"
        acquire_bootstrap_lock
        recheck_fresh_install_under_lock
        release_bootstrap_lock
        if install_marker_present; then
            printf 'MARKER_PRESENT\n'
        else
            printf 'MARKER_REMOVED\n'
        fi
    )
}

printf '0.9.1\nJUNK' > "$MARKER"
assert_status 'the locked recheck removes an invalid marker' 0 \
    run_locked_recheck "$MARKER"
assert_output_contains 'MARKER_REMOVED'

printf '0.9.1\n' > "$MARKER"
assert_status 'the locked recheck still refuses an exact marker' 2 \
    run_locked_recheck "$MARKER"
assert_output_contains 'already enrolled and installed'

# The writer publishes an exact marker through a same-directory temporary and
# leaves no interrupted temporary behind. The real sync command is used here;
# no container or service is involved.
run_record_marker() {
    (
        INSTALL_MARKER="$1"
        AGENT_VERSION='0.9.4'
        EX_FAIL=1
        die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
        eval "$(extract_function is_digits)"
        eval "$(extract_function is_semver)"
        eval "$(extract_function sync_marker_path)"
        eval "$(extract_function record_install_complete)"
        record_install_complete
        printf 'CONTENT='
        cat "$INSTALL_MARKER"
        printf 'MODE='
        stat -c '%a' "$INSTALL_MARKER"
        if [ -e "$(dirname "$INSTALL_MARKER")/.install-complete.tmp.$$" ]; then
            printf 'TEMP_PRESENT\n'
        else
            printf 'TEMP_ABSENT\n'
        fi
    )
}

rm -f "$MARKER"
printf 'partial\n' > "$(dirname "$MARKER")/.install-complete.tmp.$$"
assert_status 'completion marker publication succeeds' 0 \
    run_record_marker "$MARKER"
assert_output_contains 'CONTENT=0.9.4'
assert_output_contains 'MODE=644'
assert_output_contains 'TEMP_ABSENT'

# --- the refusal runs in the PREFLIGHT -------------------------------------

case_start 'refuse_existing_install is called from preflight, not from step 1'
# The ordering rule: every side-effect-free refusal runs in the
# preflight. This one is decided from two local stats, so refusing it late
# protects nothing while costing a spent enrollment code.
_pf=$(extract_function preflight)
case $_pf in
    *refuse_existing_install*) ok ;;
    *) fail 'preflight() does not call refuse_existing_install' ;;
esac

case_start 'and it is not ALSO called later, where it would be redundant'
_calls=$(grep -c '^ *refuse_existing_install$' "$ROBOT_SH" || true)
if [ "$_calls" -eq 1 ]; then ok; else
    fail "refuse_existing_install is invoked $_calls times; exactly one call, in preflight, is correct"
fi

case_start 'the refusal precedes every filesystem-creating step in main'
_main=$(sed -n '/^main() {/,/^}$/p' "$ROBOT_SH")
_pre_line=$(printf '%s\n' "$_main" | grep -n '^    preflight$' | head -n 1 | cut -d: -f1)
_mk_line=$(printf '%s\n' "$_main" | grep -n 'mktemp -d' | head -n 1 | cut -d: -f1)
_dirs_line=$(printf '%s\n' "$_main" | grep -n '^    create_user_and_dirs$' | head -n 1 | cut -d: -f1)
if [ -n "$_pre_line" ] && [ -n "$_mk_line" ] && [ -n "$_dirs_line" ] \
   && [ "$_pre_line" -lt "$_mk_line" ] && [ "$_pre_line" -lt "$_dirs_line" ]; then
    ok
else
    fail "preflight at ${_pre_line:-none}, mktemp at ${_mk_line:-none}, create_user_and_dirs at ${_dirs_line:-none}"
fi

# --- the marker is written LAST --------------------------------------------

case_start 'record_install_complete is called after install_units'
_units_line=$(grep -n '^    install_units$' "$ROBOT_SH" | head -n 1 | cut -d: -f1)
_marker_line=$(grep -n '^    record_install_complete$' "$ROBOT_SH" | head -n 1 | cut -d: -f1)
if [ -n "$_units_line" ] && [ -n "$_marker_line" ] && [ "$_marker_line" -gt "$_units_line" ]; then
    ok
else
    fail "install_units at ${_units_line:-none}, record_install_complete at ${_marker_line:-none}"
fi

case_start 'the marker is not written anywhere before step 8'
_enroll_line=$(grep -n '^    run_enrollment$' "$ROBOT_SH" | head -n 1 | cut -d: -f1)
if [ "$_marker_line" -gt "$_enroll_line" ]; then ok; else
    fail 'the completion marker is written before the enrollment exchange'
fi

# --- the resume path sends no code -----------------------------------------

case_start 'the resume invocation passes --resume and no code'
_re=$(extract_function run_enrollment)
_resume_block=$(printf '%s\n' "$_re" | sed -n '/RESUMING.*=.*1/,/^    fi$/p')
case $_resume_block in
    *'--resume'*) ;;
    *) fail 'the resume branch does not pass --resume'; _resume_block='' ;;
esac
if [ -n "$_resume_block" ]; then
    case $_resume_block in
        *'--code-stdin'*|*'$CODE'*)
            fail 'the resume branch still sends the enrollment code, which is spent' ;;
        *) ok ;;
    esac
fi

case_start 'the resume failure message names the recovery that exists'
case $_re in
    *"re-enrollment code on the Robots page"*) ok ;;
    *) fail 'the resume failure does not tell the operator how to recover' ;;
esac

case_start 'an enrollment failure without recovery state never advises code reuse'
case $_re in
    *'Do not reuse the supplied one-use code'*'mint a fresh enrollment code'*) ok ;;
    *) fail 'the uncertain exchange path does not require a fresh enrollment code' ;;
esac

# --- the printed promise is now true ---------------------------------------

case_start 'step 7 still promises a re-run needs no new code'
_cw=$(extract_function check_recordings_writable)
case $_cw in
    *'does NOT need a new enrollment code'*) ok ;;
    *) fail 'the step-7 remediation no longer makes the promise this test asserts' ;;
esac

summary
