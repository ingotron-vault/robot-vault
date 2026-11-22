#!/bin/sh
# The vault bootstrap's --reenroll mode must refresh a rotated vault CA
# without replacing the installed release, and a failed exchange must leave a
# working robot on its previous trust anchor.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='public re-enrollment bootstrap'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

EX_PREFLIGHT=2
EX_ENROLL=5
EX_FAIL=1
warn() { :; }
die() { _code=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_code"; }

eval "$(extract_function require_existing_install_for_reenroll)"
eval "$(extract_function run_reenrollment_bootstrap)"
eval "$(extract_function reenroll_backup_ca)"
eval "$(extract_function reenroll_restore_old_ca)"
eval "$(extract_function sha256_file)"
eval "$(extract_function is_digits)"
eval "$(extract_function is_semver)"
eval "$(extract_function file_size)"
eval "$(extract_function install_marker_version)"

INSTALL_MARKER="$WORK/state/install-complete"
AGENT_PREFIX="$WORK/opt/ingotron-agent"
TOKEN_FILE="$WORK/etc/ingotron/credentials/robot.token"
CONF_DIR="$WORK/etc/ingotron"
REENROLL_PENDING="$WORK/state/reenroll-pending"
TMPDIR_SELF="$WORK/tmp"
# Distinct query and ingest hosts and a non-default ingest port.
VAULT='https://vault.example:443'
INGEST='ingest.example:50052'
RECORDINGS_ROOT='/mnt/robot_recordings'
ROOT_OVERRIDE=1
CODE='ign1.7Q2M4XKB9WMF7T3DZ8H1RN4KQ2.4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT'
mkdir -p "$TMPDIR_SELF" "$CONF_DIR/credentials" "$AGENT_PREFIX/current/bin" "$(dirname "$INSTALL_MARKER")"
printf '0.9.1\n' > "$INSTALL_MARKER"
touch "$TOKEN_FILE" "$CONF_DIR/vault.json" "$CONF_DIR/ca.crt" "$AGENT_PREFIX/current/bin/ingotron-agent"
chmod 0755 "$AGENT_PREFIX/current/bin/ingotron-agent"

assert_status 'a completed install satisfies re-enrollment preflight' 0 \
    require_existing_install_for_reenroll

rm -f "$INSTALL_MARKER"
assert_status 're-enrollment refuses an incomplete install' 2 \
    require_existing_install_for_reenroll
assert_output_contains 'completed'
printf '0.9.1\n' > "$INSTALL_MARKER"

# The fake installed agent records its argv and stdin, then fails before it can
# create the recovery marker. The bootstrap must restore the old CA.
AGENT_LOG="$WORK/agent.log"
cat > "$AGENT_PREFIX/current/bin/ingotron-agent" <<'STUB'
#!/bin/sh
printf 'argv=%s\n' "$*" | tee -a "$AGENT_LOG"
cat >> "$AGENT_LOG"
exit 7
STUB
chmod 0755 "$AGENT_PREFIX/current/bin/ingotron-agent"
export AGENT_LOG
printf 'old-ca\n' > "$CONF_DIR/ca.crt"
printf 'old-pin\n' > "$CONF_DIR/ca.spki.sha256"
printf 'new-ca\n' > "$TMPDIR_SELF/ca.pem"
SPKI_HEX='aabb'
reenroll_backup_ca
install() {
    # The extracted production function uses install; this shim models the
    # successful replacement while keeping the test independent of ownership.
    cp "$7" "$8"
}
reenroll_install_ca() {
    cp "$TMPDIR_SELF/ca.pem" "$CONF_DIR/ca.crt"
    printf '%s\n' "$SPKI_HEX" > "$CONF_DIR/ca.spki.sha256"
}
reenroll_install_ca

assert_status 'a pre-exchange failure returns enrollment error' 5 \
    run_reenrollment_bootstrap
assert_output_contains 'Mint a fresh re-enrollment code'
assert_equals 'failed exchange restores the previous CA' "$(cat "$CONF_DIR/ca.crt")" 'old-ca'
assert_equals 'failed exchange restores the previous pin' "$(cat "$CONF_DIR/ca.spki.sha256")" 'old-pin'
assert_output_contains '--ca '
assert_output_contains '--root /mnt/robot_recordings'
assert_output_contains '--vault https://vault.example:443 --ingest ingest.example:50052 --code-stdin'
assert_output_lacks "$CODE"

# The agent writes its durable, root-only recovery record before it replaces the
# credential. A token change without that record is therefore corruption or an
# old agent, not a recoverable condition for the shell to guess at.
cat > "$AGENT_PREFIX/current/bin/ingotron-agent" <<STUB
#!/bin/sh
printf 'replacement-token' > "$TOKEN_FILE"
printf 'argv=%s\n' "\$*" | tee -a "$AGENT_LOG"
exit 7
STUB
chmod 0755 "$AGENT_PREFIX/current/bin/ingotron-agent"
rm -f "$REENROLL_PENDING"
printf 'old-token' > "$TOKEN_FILE"
REENROLL_OLD_TOKEN_SHA=$(sha256_file "$TOKEN_FILE")
printf 'new-ca\n' > "$CONF_DIR/ca.crt"
assert_status 'a token change without a marker is treated as committed' 5 \
    run_reenrollment_bootstrap
assert_output_contains 'recovery record is missing'
assert_file_absent 'the shell never fabricates an unvalidated recovery marker' "$REENROLL_PENDING"
assert_equals 'the replacement CA is retained after a token change' "$(cat "$CONF_DIR/ca.crt")" 'new-ca'

# A marker means the code was accepted and local repair is now the only safe
# operation. The bootstrap must retain the new CA and call the agent without
# sending another secret.
cat > "$AGENT_PREFIX/current/bin/ingotron-agent" <<'STUB'
#!/bin/sh
printf 'argv=%s\n' "$*" | tee -a "$AGENT_LOG"
cat >> "$AGENT_LOG"
exit 0
STUB
chmod 0755 "$AGENT_PREFIX/current/bin/ingotron-agent"
touch "$REENROLL_PENDING"
printf 'new-ca\n' > "$CONF_DIR/ca.crt"
assert_status 'a committed exchange retries locally without a new code' 0 \
    run_reenrollment_bootstrap
assert_equals 'successful recovery retains the replacement CA' "$(cat "$CONF_DIR/ca.crt")" 'new-ca'
assert_output_lacks "$CODE"

# Main ordering is the security contract: pin verification precedes CA
# replacement, and CA replacement precedes the installed agent exchange.
case_start 'pin verification precedes CA replacement and credential exchange'
main_src=$(sed -n '/^main() {/,/^}$/p' "$ROBOT_SH")
pin_line=$(printf '%s\n' "$main_src" | grep -n '^        verify_pin$' | head -n 1 | cut -d: -f1)
ca_line=$(printf '%s\n' "$main_src" | grep -n '^        install_ca$' | head -n 1 | cut -d: -f1)
exchange_line=$(printf '%s\n' "$main_src" | grep -n '^        run_reenrollment_bootstrap$' | head -n 1 | cut -d: -f1)
if [ -n "$pin_line" ] && [ -n "$ca_line" ] && [ -n "$exchange_line" ] \
   && [ "$pin_line" -lt "$ca_line" ] && [ "$ca_line" -lt "$exchange_line" ]; then
    ok
else
    fail "verify_pin=$pin_line install_ca=$ca_line exchange=$exchange_line"
fi

summary
