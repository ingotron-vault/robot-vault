# POSIX sh test harness for the /robot bootstrap gates.
#
# Deliberately tiny and dependency-free: these tests have to run under dash and
# under busybox ash, on a host with no bash, which is the whole point of
# these gates.  A framework that needed bash would defeat the thing it tests.
#
# Every assertion is on an EXIT STATUS, not on end state.  A gate that only
# checks the end state passes when the command never ran (this project has been
# bitten by exactly that), so `assert_status` is the only
# way a test here is allowed to conclude anything.

TESTS_RUN=0
TESTS_FAILED=0
CURRENT_CASE=''

# Absolute paths, computed once. $HARNESS_DIR is set by the caller before
# sourcing, because POSIX sh has no reliable $BASH_SOURCE equivalent.
: "${TEST_ROOT:?TEST_ROOT must be set before sourcing harness.sh}"
PKG_ROOT=$(dirname "$TEST_ROOT")
REPO_ROOT=$(dirname "$(dirname "$PKG_ROOT")")
# ROBOT_SH_OVERRIDE points a suite at a MUTATED copy of the bootstrap. It exists
# for check_mutation_guards.sh, which proves each gate fails when the guard it
# covers is removed -- a passing test proves nothing until it has been seen
# failing. Nothing else sets it.
ROBOT_SH="${ROBOT_SH_OVERRIDE:-$PKG_ROOT/robot.sh}"
FIXTURES="$TEST_ROOT/fixtures"

case_start() {
    CURRENT_CASE=$1
    TESTS_RUN=$(( TESTS_RUN + 1 ))
}

ok() {
    printf '  ok   %s\n' "$CURRENT_CASE"
}

fail() {
    TESTS_FAILED=$(( TESTS_FAILED + 1 ))
    printf '  FAIL %s\n' "$CURRENT_CASE" >&2
    printf '       %s\n' "$*" >&2
}

# assert_status <name> <expected-status> <command...>
# Runs the command, captures combined output in $LAST_OUTPUT, and asserts the
# exit status.  Never uses `set -e` semantics: the point is to observe failure.
assert_status() {
    case_start "$1"
    _expected=$2
    shift 2
    LAST_OUTPUT=$( "$@" 2>&1 )
    _actual=$?
    if [ "$_actual" -eq "$_expected" ]; then
        ok
        return 0
    fi
    fail "expected exit $_expected, got $_actual; output:
$LAST_OUTPUT"
    return 1
}

# assert_output_contains <needle>  -- inspects $LAST_OUTPUT from the last
# assert_status.  Used to prove WHICH refusal fired, not merely that one did.
assert_output_contains() {
    case_start "$CURRENT_CASE (message names '$1')"
    case $LAST_OUTPUT in
        *"$1"*) ok ;;
        *) fail "output did not contain '$1'; output:
$LAST_OUTPUT" ;;
    esac
}

assert_output_lacks() {
    case_start "$CURRENT_CASE (message must not contain '$1')"
    case $LAST_OUTPUT in
        *"$1"*) fail "output unexpectedly contained '$1'; output:
$LAST_OUTPUT" ;;
        *) ok ;;
    esac
}

assert_equals() {
    case_start "$1"
    if [ "$2" = "$3" ]; then
        ok
    else
        fail "expected '$3', got '$2'"
    fi
}

assert_file_absent() {
    case_start "$1"
    if [ -e "$2" ]; then
        fail "$2 exists and must not"
    else
        ok
    fi
}

assert_file_present() {
    case_start "$1"
    if [ -e "$2" ]; then
        ok
    else
        fail "$2 does not exist and must"
    fi
}

TESTS_SKIPPED=0

skip() {
    TESTS_SKIPPED=$(( TESTS_SKIPPED + 1 ))
    printf '  SKIP %s\n       %s\n' "$1" "$2"
}

# busybox ash resolves many external commands as built-in applets before it
# consults PATH, so a PATH stub for `id` or `uname` can be silently ignored.
# A case that depends on such a stub must know whether the stub is live rather
# than quietly assert nothing.
stub_is_effective() {
    _si_expected=$1
    shift
    _si_got=$(env PATH="$STUB_PATH" $SHELL_UNDER_TEST -c "$*" 2>/dev/null || true)
    [ "$_si_got" = "$_si_expected" ]
}

summary() {
    printf '\n%s: %d checks, %d failed' "${SUITE_NAME:-suite}" "$TESTS_RUN" "$TESTS_FAILED"
    if [ "$TESTS_SKIPPED" -gt 0 ]; then
        printf ', %d skipped' "$TESTS_SKIPPED"
    fi
    printf '\n'
    # A suite that asserted NOTHING is not a passing suite. Without this, a
    # missing tool or an early `exit 0` turns a gate into a green line that
    # proves nothing -- which is exactly how a gate hides.
    if [ "$TESTS_RUN" -eq 0 ]; then
        printf '*** %s ran ZERO checks -- treating that as a failure, not a pass\n' \
            "${SUITE_NAME:-suite}" >&2
        return 1
    fi
    [ "$TESTS_FAILED" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Extracting a function from robot.sh by its literal text.
#
# The alternative -- adding a "don't run main" environment hook to robot.sh --
# would put a behaviour-changing environment variable into a script that is
# piped into a root shell, which must not change behaviour based on its
# environment.  Extracting by text also means the test runs the DEPLOYED
# bytes: if someone edits the shipped function, the extract changes with it.
# ---------------------------------------------------------------------------
extract_function() {
    _ef_name=$1
    _ef_src=${2:-$ROBOT_SH}
    sed -n "/^$_ef_name() {\$/,/^}\$/p" "$_ef_src"
}

# A PATH sandbox: stubs that make the preflight satisfiable off a real robot,
# plus a curl that RECORDS ANY INVOCATION instead of making one.  The recorder
# is how "zero network activity" is asserted without strace or privileges: if
# the script reaches any network step, the marker file exists.
make_stub_path() {
    _sp_dir=$1
    mkdir -p "$_sp_dir/bin"
    NETWORK_MARKER="$_sp_dir/network-was-attempted"

    # `id -u` must report 0 for the preflight to be reached at all. When the
    # suite already runs as root -- which is how the container gate runs it --
    # no stub is installed, because busybox ash resolves `id` as a built-in
    # applet before consulting PATH and a stub would be silently ignored. That
    # difference is exactly why the busybox half of the shell gate runs in a container
    # as root rather than under a PATH sandbox.
    if [ "$(/usr/bin/id -u 2>/dev/null || id -u)" != '0' ]; then
        cat > "$_sp_dir/bin/id" <<'STUB'
#!/bin/sh
[ "$1" = "-u" ] && { echo 0; exit 0; }
exec /usr/bin/id "$@"
STUB
        chmod 0755 "$_sp_dir/bin/id"
    fi

    cat > "$_sp_dir/bin/curl" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$_sp_dir/network-was-attempted"
exit 7
STUB

    chmod 0755 "$_sp_dir/bin/curl"
    STUB_PATH="$_sp_dir/bin:$PATH"
}

# Render robot.sh the way the query image build does: a small agent tarball
# with the right root, then render-robot-bootstrap.sh. Sets $RENDERED_ROBOT_SH,
# $RENDERED_TARBALL and $RENDERED_VERSION. $2 overrides the version (0.9.9).
render_robot_sh() {
    _rr_dir=$1
    RENDERED_VERSION=${2:-0.9.9}
    rm -rf "$_rr_dir/agent-src"
    mkdir -p "$_rr_dir/agent-src/ingotron-agent-$RENDERED_VERSION/bin"
    printf '#!/bin/sh\necho %s\n' "$RENDERED_VERSION" \
        > "$_rr_dir/agent-src/ingotron-agent-$RENDERED_VERSION/bin/ingotron-agent"
    chmod 0755 "$_rr_dir/agent-src/ingotron-agent-$RENDERED_VERSION/bin/ingotron-agent"
    RENDERED_TARBALL="$_rr_dir/agent-linux-amd64.tar.gz"
    ( cd "$_rr_dir/agent-src" && tar -cf - "ingotron-agent-$RENDERED_VERSION" | gzip -n ) > "$RENDERED_TARBALL"
    RENDERED_ROBOT_SH="$_rr_dir/robot.sh"
    ${SHELL_UNDER_TEST:-/bin/sh} "$PKG_ROOT/render-robot-bootstrap.sh" --template "$ROBOT_SH" \
        --tarball "$RENDERED_TARBALL" --version "$RENDERED_VERSION" --out "$RENDERED_ROBOT_SH" 2>/dev/null \
        || { printf 'render-robot-bootstrap.sh failed for %s\n' "$ROBOT_SH" >&2; return 1; }
}

# A syntactically valid code whose pin segment is $1 (26 Crockford chars).
make_code() {
    printf 'ign1.%s.%s' "$1" "4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"
}

VALID_PIN='7Q2M4XKB9WMF7T3DZ8H1RN4KQ2'
VALID_CODE="ign1.$VALID_PIN.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"
