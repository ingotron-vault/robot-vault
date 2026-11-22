#!/bin/sh
# The refusal matrix: malformed codes and the OpenSSL floor.
#
# Every case here runs the REAL robot.sh to completion of its refusal, with a
# PATH sandbox whose `curl` RECORDS an invocation instead of making one.  That
# recorder is how "zero network activity" is asserted without strace or root:
# if the script reaches any network step, the marker file exists.  Two things
# are asserted for every case, never one:
#
#   1. the EXIT STATUS (2 for preflight/argument refusals, robot.sh
#      steps 0-1) -- because a gate that checks only end state passes when the
#      command never ran; and
#   2. that no network was attempted and nothing was written.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='bootstrap refusals'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

make_stub_path "$WORK"
render_robot_sh "$WORK"

SHELL_UNDER_TEST=${SHELL_UNDER_TEST:-/bin/sh}

# run_bootstrap <extra-env-assignments-or-empty> <args...>
# Always runs the RENDERED copy (a vault serves a rendered /robot), with the
# stub PATH, and clears the network marker first.
run_bootstrap() {
    rm -f "$WORK/network-was-attempted"
    env PATH="$STUB_PATH" $SHELL_UNDER_TEST "$RENDERED_ROBOT_SH" "$@"
}

assert_no_network() {
    case_start "$CURRENT_CASE (made zero network calls)"
    if [ -e "$WORK/network-was-attempted" ]; then
        fail "the script attempted network I/O:
$(cat "$WORK/network-was-attempted")"
    else
        ok
    fi
}

# ---------------------------------------------------------------------------
# A malformed code aborts before any network activity.
# ---------------------------------------------------------------------------

assert_status 'code with a v2 tag is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "ign2.$VALID_PIN.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"
assert_output_contains "must begin with 'ign1.'"
assert_no_network

assert_status 'code with a 25-character pin segment is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"
assert_no_network

# 27-character pin, 31-character secret: the TOTAL is still 64, so the overall
# length check cannot fire and the per-segment checks are the only thing
# standing between this and a lookup. Without this case, deleting the segment
# length check goes unnoticed (found by check_mutation_guards.sh).
assert_status 'a code with 27/31 segments but the right total length is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2X.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJ"
assert_output_contains 'pin segment must be 26 characters'
assert_no_network

assert_status 'code with a non-Crockford character (I) is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KI2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"
assert_output_contains 'Crockford'
assert_no_network

assert_status 'code with a non-Crockford character (U) in the secret is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "ign1.$VALID_PIN.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJU"
assert_no_network

assert_status 'a lowercase code is refused rather than silently normalised' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "ign1.7q2m4xkb9wmf7t3dz8h1rn4kq2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT"
assert_no_network

assert_status 'a missing --code is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003
assert_output_contains '--code is required'
assert_no_network

assert_status 'a missing --vault is refused' 2 \
    run_bootstrap --code "$VALID_CODE"
assert_output_contains '--vault is required'
assert_no_network

assert_status 'a plaintext --vault is refused' 2 \
    run_bootstrap --vault http://10.0.0.5:8003 --code "$VALID_CODE"
assert_output_contains 'must be https'
assert_no_network

assert_status 'a --vault with a path is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003/admin --code "$VALID_CODE"
assert_no_network

assert_status 'an unknown flag is refused, never ignored' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --insecure-please
assert_output_contains 'unknown argument'
assert_no_network

assert_status '--version is not an option: the vault serves one agent version' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --version 1.2.3
assert_output_contains 'unknown argument: --version'
assert_no_network

# --ingest reaches the agent's argv; it must be one boring host:port token.
for bad_ingest in 'ingest.example.com' 'ingest.example.com:' ':50051' \
                  'ingest.example.com:0' 'ingest.example.com:65536' \
                  'ingest.example.com:5005a' 'ingest.example.com:000050051' \
                  "ingest'.example.com:50051" 'in gest.example.com:50051' \
                  'ingest.example.com:50051;id' '$(id):50051'; do
    assert_status "a malformed --ingest is refused ($bad_ingest)" 2 \
        run_bootstrap --vault https://10.0.0.5 --ingest "$bad_ingest" --code "$VALID_CODE"
    assert_output_contains '--ingest'
    assert_no_network
done


assert_status 'a relative --root is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root data
assert_no_network

# The recordings root is interpolated into `su -s /bin/sh -c "..."` in step 7
# and into a systemd drop-in in step 8. Both parse what they are given.
assert_status "a --root containing a single quote is refused" 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root "/data/rob'ot"
assert_output_contains 'may not contain quotes'
assert_no_network

assert_status 'a --root containing a command substitution is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/data/$(id)'
assert_no_network

assert_status 'a --root containing a backtick is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/data/`id`'
assert_no_network

assert_status 'a --root containing a space cannot become multiple ReadWritePaths' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/srv/recordings /etc'
assert_output_contains 'ASCII whitespace'
assert_no_network

assert_status 'a --root containing a tab cannot become multiple ReadWritePaths' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/srv/recordings	/etc'
assert_output_contains 'ASCII whitespace'
assert_no_network

assert_status 'a --root with leading whitespace is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root ' /srv/recordings'
assert_no_network

assert_status 'a --root with a parent-directory component is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/data/../etc'
assert_output_contains 'parent-directory component'
assert_no_network

assert_status 'a --root with a trailing slash is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/data/robot_data/'
assert_no_network

# '%' is the character systemd actually interprets. ReadWritePaths= goes through
# specifier expansion, so `--root /data/%T/rec` would grant the service write
# access to /var/tmp/rec instead of the operator's tree, and `--root /data/50%`
# emits a drop-in that fails to parse. The shell-metacharacter list missed it
# because that list was written for `su -c`, not for systemd.
assert_status 'a --root containing a systemd specifier is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/data/%T/rec'
assert_output_contains 'systemd expands it as a specifier'
assert_no_network

assert_status 'a --root ending in a bare percent is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root '/data/50%'
assert_no_network

# The drop-in grants the service WRITE access to whatever --root names, and the
# step-7 probe creates a file there, so a system-owned prefix is out.
for bad_root in /etc /etc/ingotron /usr/local/share/rec /bin /boot/rec \
                /var/lib/ingotron/rec /opt/ingotron-agent/rec /; do
    assert_status "a --root inside a system-owned prefix is refused ($bad_root)" 2 \
        run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root "$bad_root"
    assert_no_network
done

case_start 'a sibling path that merely shares a prefix STRING is still accepted'
# /etcetera must not be refused because it starts with the characters of /etc:
# a prefix test written on strings rather than on path components is the classic
# way this check becomes a support ticket.
rm -f "$WORK/network-was-attempted"
LAST_OUTPUT=$(run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root /etcetera/rec 2>&1)
TESTS_RUN=$(( TESTS_RUN + 1 ))
case $LAST_OUTPUT in
    *'--root'*) fail "a sibling path was rejected: $LAST_OUTPUT" ;;
    *) ok ;;
esac

# ...and a perfectly ordinary path must still be accepted, or the validation
# above is just a way to break every install.
case_start 'an ordinary --root is accepted'
rm -f "$WORK/network-was-attempted"
LAST_OUTPUT=$(run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" --root /srv/recordings 2>&1)
TESTS_RUN=$(( TESTS_RUN + 1 ))
case $LAST_OUTPUT in
    *'--root'*) fail "an ordinary path was rejected: $LAST_OUTPUT" ;;
    *) ok ;;
esac

# ---------------------------------------------------------------------------
# A valid install command: the FIRST request is the public CA at the --vault
# origin, made by fetch_public, and it carries no credential.
# ---------------------------------------------------------------------------

assert_status 'a valid install command stops at the (stubbed) CA fetch' 1 \
    run_bootstrap --vault https://vault.example.com --ingest ingest.example.com:50051 --code "$VALID_CODE"
assert_output_contains 'could not fetch https://vault.example.com:443/ca.crt'
case_start 'the first request is the CA at the --vault origin'
_first=$(head -n 1 "$WORK/network-was-attempted" 2>/dev/null)
case $_first in
    *'https://vault.example.com:443/ca.crt'*) ok ;;
    *) fail "first request: $_first" ;;
esac
case_start 'the CA request carries no part of the code'
case $(cat "$WORK/network-was-attempted") in
    *"$VALID_PIN"*|*4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT*) fail "the code reached curl: $(cat "$WORK/network-was-attempted")" ;;
    *) ok ;;
esac

# ---------------------------------------------------------------------------
# The unrendered-template refusal: a copy of robot.sh whose AGENT_* values were
# never rendered cannot check the agent it would download, so it must not
# download anything.
# ---------------------------------------------------------------------------

case_start 'an unrendered robot.sh refuses before any network call'
rm -f "$WORK/network-was-attempted"
LAST_OUTPUT=$(env PATH="$STUB_PATH" $SHELL_UNDER_TEST "$ROBOT_SH" \
                  --vault https://10.0.0.5:8003 --code "$VALID_CODE" 2>&1)
rc=$?
TESTS_RUN=$(( TESTS_RUN + 1 ))
if [ "$rc" -eq 2 ]; then ok; else fail "expected exit 2, got $rc: $LAST_OUTPUT"; fi
assert_output_contains 'was not rendered by a vault build'
assert_no_network

for mode in --upgrade --reenroll; do
    case_start "an unrendered robot.sh refuses $mode too"
    rm -f "$WORK/network-was-attempted"
    LAST_OUTPUT=$(env PATH="$STUB_PATH" $SHELL_UNDER_TEST "$ROBOT_SH" $mode \
                      --vault https://10.0.0.5 2>&1)
    rc=$?
    TESTS_RUN=$(( TESTS_RUN + 1 ))
    case $rc:$LAST_OUTPUT in
        2:*'was not rendered by a vault build'*) ok ;;
        *) fail "expected the render refusal (exit 2), got $rc: $LAST_OUTPUT" ;;
    esac
done

# On a host with no completed install, --upgrade and --reenroll refuse in the
# preflight: there is nothing to keep.
assert_status '--upgrade refuses on a host with no completed install' 2 \
    run_bootstrap --upgrade --vault https://10.0.0.5
assert_output_contains 'requires a completed'
assert_no_network

assert_status '--reenroll refuses on a host with no completed install' 2 \
    run_bootstrap --reenroll --vault https://10.0.0.5 --code "$VALID_CODE"
assert_output_contains 'requires a completed'
assert_no_network

# ---------------------------------------------------------------------------
# The OpenSSL >= 1.1.1 gate really does
# precede the download.  On a host with openssl 1.1.0 the script exits 2 having
# made zero network calls and written nothing.
# ---------------------------------------------------------------------------

make_openssl_stub() {
    cat > "$WORK/bin/openssl" <<STUB
#!/bin/sh
if [ "\$1" = "version" ]; then printf '%s\n' "$1"; exit 0; fi
exec /usr/bin/openssl "\$@"
STUB
    chmod 0755 "$WORK/bin/openssl"
}

make_openssl_stub 'OpenSSL 1.1.0l  10 Sep 2019'
assert_status 'openssl 1.1.0 refuses before any download' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE"
assert_output_contains 'OpenSSL >= 1.1.1 is required'
assert_no_network

make_openssl_stub 'LibreSSL 3.3.6'
assert_status 'LibreSSL is refused' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE"
assert_output_contains 'OpenSSL >= 1.1.1 is required; found: LibreSSL'
assert_no_network

for good in 'OpenSSL 1.1.1f  31 Mar 2020' 'OpenSSL 3.0.2 15 Mar 2022'; do
    make_openssl_stub "$good"
    case_start "openssl passes the version gate ($good)"
    rm -f "$WORK/network-was-attempted"
    LAST_OUTPUT=$(run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE" 2>&1)
    TESTS_RUN=$(( TESTS_RUN + 1 ))
    case $LAST_OUTPUT in
        *'OpenSSL >= 1.1.1 is required'*) fail "$good was rejected: $LAST_OUTPUT" ;;
        *) ok ;;
    esac
done
rm -f "$WORK/bin/openssl"

# The ordering claim itself: with BOTH a bad openssl AND a malformed code, the
# openssl refusal is the one that fires, because step 0 precedes step 1.
make_openssl_stub 'OpenSSL 1.0.2u  20 Dec 2019'
assert_status 'the step-0 preflight precedes step-1 argument validation' 2 \
    run_bootstrap --vault https://10.0.0.5:8003 --code 'ign2.nope'
assert_output_contains 'OpenSSL >= 1.1.1 is required'
assert_output_lacks "must begin with 'ign1.'"
rm -f "$WORK/bin/openssl"

# ---------------------------------------------------------------------------
# Non-root and wrong-architecture refusals.
# ---------------------------------------------------------------------------

HAD_ID_STUB=0
[ -f "$WORK/bin/id" ] && HAD_ID_STUB=1

cat > "$WORK/bin/id" <<'STUB'
#!/bin/sh
[ "$1" = "-u" ] && { echo 1000; exit 0; }
exec /usr/bin/id "$@"
STUB
chmod 0755 "$WORK/bin/id"
if stub_is_effective '1000' 'id -u'; then
    assert_status 'a non-root invocation is refused' 2 \
        run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE"
    assert_output_contains 'must run as root'
    assert_no_network
else
    skip 'a non-root invocation is refused' \
         "$SHELL_UNDER_TEST resolves 'id' as a built-in applet, so the PATH stub cannot take effect; covered by the dash run"
fi
rm -f "$WORK/bin/id"
if [ "$HAD_ID_STUB" = '1' ]; then
    cat > "$WORK/bin/id" <<'STUB'
#!/bin/sh
[ "$1" = "-u" ] && { echo 0; exit 0; }
exec /usr/bin/id "$@"
STUB
    chmod 0755 "$WORK/bin/id"
fi

cat > "$WORK/bin/uname" <<'STUB'
#!/bin/sh
[ "$1" = "-s" ] && { echo Linux; exit 0; }
[ "$1" = "-m" ] && { echo aarch64; exit 0; }
exec /usr/bin/uname "$@"
STUB
chmod 0755 "$WORK/bin/uname"
if stub_is_effective 'aarch64' 'uname -m'; then
    assert_status 'a non-amd64 host is refused' 2 \
        run_bootstrap --vault https://10.0.0.5:8003 --code "$VALID_CODE"
    assert_output_contains 'linux/amd64 only'
    assert_no_network
else
    skip 'a non-amd64 host is refused' \
         "$SHELL_UNDER_TEST resolves 'uname' as a built-in applet; covered by the dash run"
fi
rm -f "$WORK/bin/uname"

# ---------------------------------------------------------------------------
# Nothing was written anywhere, in ANY of the cases above.
# ---------------------------------------------------------------------------

assert_file_absent 'no /etc/ingotron was created by any refusal' /etc/ingotron
assert_file_absent 'no /opt/ingotron-agent was created by any refusal' /opt/ingotron-agent

summary
