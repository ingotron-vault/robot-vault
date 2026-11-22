#!/bin/sh
# Enrollment refuses on CA pin mismatch, and the enrollment secret is
# never transmitted.
#
# The functions under test are EXTRACTED FROM THE DEPLOYED robot.sh and driven
# against real OpenSSL-generated CAs, so this exercises the shipped bytes rather
# than a paraphrase.  A full-script variant (tcpdump, a live vault, the code
# still redeemable afterwards) is not automated.
# What is proven here is the part that must hold on every host with no
# vault at all:
#
#   * a matching pin is accepted and a one-character change is refused;
#   * the refusal exits 3, prints BOTH values, and prints the full SPKI hash;
#   * the pin travels with the code, so a code minted by vault A cannot be
#     redeemed against vault B (the "wrong vault" case);
#   * the refusal happens with the secret still in a shell variable and no
#     socket opened, which is asserted structurally: verify_pin's body contains
#     no network command at all.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='CA pin verification'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if ! command -v openssl >/dev/null 2>&1; then
    skip 'the whole suite' 'openssl is not installed'
    exit 0
fi

# --- structural: the pin check opens no socket -----------------------------

extract_function verify_pin > "$WORK/verify_pin.src"

case_start 'verify_pin exists in the deployed script'
if [ -s "$WORK/verify_pin.src" ]; then ok; else fail 'verify_pin() was not found in robot.sh'; fi

case_start 'verify_pin invokes no network command'
if grep -nE '\b(curl|wget|nc|openssl s_client)\b' "$WORK/verify_pin.src" >/dev/null; then
    fail "verify_pin reaches the network:
$(cat "$WORK/verify_pin.src")"
else
    ok
fi

case_start 'verify_pin never references the enrollment secret'
# It may reference CODE_PIN_SEGMENT (the pin) but must not touch $CODE itself.
if grep -nE '\$CODE([^_]|$)|\$\{CODE\}' "$WORK/verify_pin.src" >/dev/null; then
    fail 'verify_pin references the full code'
else
    ok
fi

case_start 'the pin comparison happens before run_enrollment in main'
verify_line=$(grep -n '^    verify_pin$' "$ROBOT_SH" | head -n 1 | cut -d: -f1)
enroll_line=$(grep -n '^    run_enrollment$' "$ROBOT_SH" | head -n 1 | cut -d: -f1)
if [ -n "$verify_line" ] && [ -n "$enroll_line" ] && [ "$verify_line" -lt "$enroll_line" ]; then
    ok
else
    fail "verify_pin at line ${verify_line:-none}, run_enrollment at line ${enroll_line:-none}"
fi

# --- behavioural: real CAs -------------------------------------------------

grep '^C32=' "$ROBOT_SH" > "$WORK/consts.sh"
. "$WORK/consts.sh"
eval "$(extract_function b32_prefix)"
eval "$(extract_function is_hex64)"

make_ca() {
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
        -subj "/CN=$2" -addext 'basicConstraints=critical,CA:TRUE' \
        -keyout "$1.key" -out "$1.pem" >/dev/null 2>&1
}

pin_of() {
    _po_hex=$(openssl x509 -in "$1" -noout -pubkey \
              | openssl pkey -pubin -outform DER \
              | openssl dgst -sha256 -r | cut -d' ' -f1)
    b32_prefix "$_po_hex" 26
}

make_ca "$WORK/vault-a" 'vault-a'
make_ca "$WORK/vault-b" 'vault-b'

PIN_A=$(pin_of "$WORK/vault-a.pem")
PIN_B=$(pin_of "$WORK/vault-b.pem")

case_start 'two different vaults produce different pins'
if [ -n "$PIN_A" ] && [ -n "$PIN_B" ] && [ "$PIN_A" != "$PIN_B" ]; then ok; else
    fail "PIN_A='$PIN_A' PIN_B='$PIN_B'"
fi

case_start 'the pin is stable across repeated computation of the same CA'
if [ "$(pin_of "$WORK/vault-a.pem")" = "$PIN_A" ]; then ok; else
    fail 'the same certificate produced two different pins'
fi

# Re-issuing the CA CERTIFICATE on the SAME KEY must
# not invalidate a pending enrollment, because the pin is over the SPKI.
openssl req -x509 -new -key "$WORK/vault-a.key" -days 2 \
    -subj '/CN=vault-a-reissued' -addext 'basicConstraints=critical,CA:TRUE' \
    -out "$WORK/vault-a-reissued.pem" >/dev/null 2>&1
case_start 'a CA certificate re-issued on the same key keeps the same pin'
if [ "$(pin_of "$WORK/vault-a-reissued.pem")" = "$PIN_A" ]; then ok; else
    fail 'the pin changed when only the certificate was re-issued -- it is not over the SPKI'
fi

# The refusal itself. verify_pin is driven directly with the variables robot.sh
# would have set, in a subshell so its `exit` does not take the suite with it.
run_verify_pin() {
    ( TMPDIR_SELF="$WORK"
      CODE_PIN_SEGMENT=$2
      EX_FAIL=1; EX_PIN=3
      CA_PIN_CHARS=26
      cleanup() { :; }
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      cp "$1" "$WORK/ca.pem"
      eval "$(extract_function verify_pin)"
      verify_pin
      printf 'ACCEPTED\n' )
}

assert_status 'the matching CA is accepted' 0 run_verify_pin "$WORK/vault-a.pem" "$PIN_A"
assert_output_contains 'ACCEPTED'

assert_status 'a different vault is refused with exit 3' 3 \
    run_verify_pin "$WORK/vault-b.pem" "$PIN_A"
assert_output_contains 'not the one that issued this code'
assert_output_contains "expected: $PIN_A"
assert_output_contains 'full observed CA SPKI sha256:'

# One character changed in the pin segment: the single most likely real-world
# corruption of a copy-paste.
first=$(printf '%s' "$PIN_A" | cut -c1)
if [ "$first" = '0' ]; then altered="1$(printf '%s' "$PIN_A" | cut -c2-)"; else
    altered="0$(printf '%s' "$PIN_A" | cut -c2-)"
fi
assert_status 'a one-character pin corruption is refused with exit 3' 3 \
    run_verify_pin "$WORK/vault-a.pem" "$altered"
assert_output_contains "expected: $altered"

# --- validate_ca_pem: what the unverified response is allowed to be --------

eval "$(extract_function file_size)"

run_validate_ca() {
    ( TMPDIR_SELF="$WORK/vc"
      mkdir -p "$TMPDIR_SELF"
      CA_MAX_BYTES=8192
      EX_FAIL=1
      VAULT='https://vault.test'
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      eval "$(extract_function file_size)"
      eval "$(extract_function validate_ca_pem)"
      cp "$1" "$TMPDIR_SELF/ca.pem"
      validate_ca_pem
      printf 'ACCEPTED\n' )
}

assert_status 'a real CA certificate is accepted' 0 run_validate_ca "$WORK/vault-a.pem"

# A leaf certificate (CA:FALSE) served at /ca.crt is a misconfiguration or an
# attack; either way it is not a trust anchor.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=leaf' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -keyout "$WORK/leaf.key" -out "$WORK/leaf.pem" >/dev/null 2>&1
assert_status 'a non-CA certificate is refused' 1 run_validate_ca "$WORK/leaf.pem"
assert_output_contains 'CA:TRUE'

cat "$WORK/vault-a.pem" "$WORK/vault-b.pem" > "$WORK/chain.pem"
assert_status 'a bundle of two certificates is refused' 1 run_validate_ca "$WORK/chain.pem"
assert_output_contains 'exactly one PEM CERTIFICATE'

cat "$WORK/vault-a.pem" "$WORK/vault-a.key" > "$WORK/withkey.pem"
assert_status 'a response containing a private key is refused' 1 run_validate_ca "$WORK/withkey.pem"

printf 'this is not a certificate\n' > "$WORK/garbage.pem"
assert_status 'a non-PEM response is refused' 1 run_validate_ca "$WORK/garbage.pem"

# 8 KiB bound: anything larger is not a CA certificate, and an unbounded read of
# an unauthenticated response is how a bootstrap gets memory-exhausted.
{ cat "$WORK/vault-a.pem"; dd if=/dev/zero bs=1024 count=16 2>/dev/null | tr '\0' 'A'; } > "$WORK/huge.pem"
assert_status 'an oversized response is refused' 1 run_validate_ca "$WORK/huge.pem"
assert_output_contains '8192'

# --- the --ca-file offline variant -----------------------------------------
#
# An offline variant, --ca-file /path/ca.pem, accepts a CA transferred out of
# band (USB stick). It STILL VERIFIES THE PIN from the code against that file
# and skips only the CA fetch.
#
# Both halves are asserted, because the dangerous failure mode is the one that
# looks like it works: a --ca-file path that installs the operator's file
# without checking it would turn the USB-stick convenience into "trust whatever
# was handed to you", which is precisely what the pin exists to prevent.

CURL_MARKER="$WORK/curl-was-called"
mkdir -p "$WORK/stub"
cat > "$WORK/stub/curl" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$CURL_MARKER"
exit 7
STUB
chmod 0755 "$WORK/stub/curl"

run_fetch_ca() {
    ( PATH="$WORK/stub:$PATH"
      TMPDIR_SELF="$WORK/fc"
      rm -rf "$TMPDIR_SELF"; mkdir -p "$TMPDIR_SELF"
      CA_FILE=$1
      VAULT='https://vault.test'
      EX_FAIL=1
      log() { printf '%s\n' "$*"; }
      die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
      eval "$(extract_function fetch_ca)"
      fetch_ca
      cmp -s "$TMPDIR_SELF/ca.pem" "$CA_FILE" || { printf 'ca.pem differs from --ca-file\n' >&2; exit 9; }
      printf 'STAGED\n' )
}

rm -f "$CURL_MARKER"
assert_status '--ca-file stages the supplied CA' 0 run_fetch_ca "$WORK/vault-a.pem"
assert_output_contains 'STAGED'

assert_file_absent '--ca-file makes ZERO network calls' "$CURL_MARKER"

case_start '--ca-file still announces that the pin is verified'
case $LAST_OUTPUT in
    *'pin is still verified'*) ok ;;
    *) fail "the operator is not told the pin is still checked; output: $LAST_OUTPUT" ;;
esac

# The half that matters: an out-of-band CA from the WRONG vault is still
# refused by the pin check, exactly as a fetched one would be.
assert_status '--ca-file from the wrong vault is refused with exit 3' 3 \
    run_verify_pin "$WORK/vault-b.pem" "$PIN_A"
assert_output_contains 'not the one that issued this code'

case_start 'fetch_ca invokes curl ONLY when --ca-file is absent'
# Structural: the function must return before the fetch when CA_FILE is set.
_fc_body=$(extract_function fetch_ca)
_fc_before_curl=$(printf '%s\n' "$_fc_body" | sed -n '1,/curl/p')
case $_fc_before_curl in
    *'return 0'*) ok ;;
    *) fail "fetch_ca reaches curl without an early return for --ca-file:
$_fc_body" ;;
esac

summary
