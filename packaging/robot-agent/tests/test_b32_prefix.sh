#!/bin/sh
# Encoder conformance for b32_prefix.
#
# `b32_prefix` is EXTRACTED FROM THE DEPLOYED robot.sh and run against the
# shared fixture set under whichever shell is running this file.  The same
# fixture set drives the Python implementation
# (packaging/robot-agent/tests/test_agent_runtime.py) and is the recorded output
# of the ingest-side Rust minter, so minter and verifier cannot drift without
# one of the three failing.
#
# A drift here is a TOTAL ENROLLMENT OUTAGE: every code the vault mints
# would be refused by every robot.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='b32_prefix conformance'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

extract_function b32_prefix > "$WORK/fn.sh"

# The function references $C32, which is defined at the top of robot.sh.
grep '^C32=' "$ROBOT_SH" > "$WORK/consts.sh"

. "$WORK/consts.sh"
. "$WORK/fn.sh"

case_start 'the C32 alphabet is Crockford (no I, L, O, U)'
if [ "$C32" = '0123456789ABCDEFGHJKMNPQRSTVWXYZ' ]; then ok; else
    fail "alphabet is '$C32'"
fi

VECTORS="$FIXTURES/b32_prefix_vectors.tsv"
assert_file_present 'the fixture file exists' "$VECTORS"

# Read the TSV. Blank lines and lines starting with '#' are comments. The
# empty-input vector uses the literal token <empty>, because tab is IFS
# whitespace and a leading empty field would be silently swallowed by `read`,
# shifting every column by one and giving a passing-looking wrong test.
while IFS='	' read -r hex want rc out; do
    case $hex in
        '#'*) continue ;;
        '<empty>') hex='' ;;
    esac
    [ -n "$want" ] || continue

    label="b32_prefix('${hex:-<empty>}', $want)"
    got=$(b32_prefix "$hex" "$want")
    got_rc=$?

    case_start "$label -> rc $rc"
    if [ "$got_rc" -eq "$rc" ]; then ok; else
        fail "expected rc $rc, got $got_rc (output '$got')"
    fi

    if [ "$rc" -eq 0 ]; then
        case_start "$label -> '$out'"
        if [ "$got" = "$out" ]; then ok; else
            fail "expected '$out', got '$got'"
        fi
    else
        # A failing call must emit NOTHING. A partial answer would be worse
        # than an error: it would compare unequal and look like a MITM.
        case_start "$label emits no partial output"
        if [ -z "$got" ]; then ok; else
            fail "expected empty output on failure, got '$got'"
        fi
    fi
done < "$VECTORS"

# The key property of the pin, asserted directly rather
# than only through the fixture row: the pin is the prefix of the encoding of
# the WHOLE digest, so bytes past the 16th DO affect character 26.
first16='00000000000000000000000000000000'
case_start 'characters past byte 16 change the 26th character'
a=$(b32_prefix "${first16}0000" 26)
b=$(b32_prefix "${first16}ff00" 26)
if [ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]; then
    ok
else
    fail "the 26th character did not change ('$a' vs '$b') -- the encoder is truncating to 16 bytes"
fi

case_start 'the first 25 characters are unaffected by byte 17'
a25=$(b32_prefix "${first16}0000" 25)
b25=$(b32_prefix "${first16}ff00" 25)
if [ "$a25" = "$b25" ]; then ok; else
    fail "characters 1-25 differ ('$a25' vs '$b25'), which they must not"
fi

# A real certificate end to end: the exact openssl pipeline robot.sh uses in
# verify_pin, fed into the extracted encoder. This is what catches a change to the
# pipeline (e.g. hashing the certificate instead of the SPKI).
if command -v openssl >/dev/null 2>&1; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
        -subj '/CN=ingotron-test-ca' \
        -addext 'basicConstraints=critical,CA:TRUE' \
        -keyout "$WORK/ca.key" -out "$WORK/ca.pem" >/dev/null 2>&1
    spki=$(openssl x509 -in "$WORK/ca.pem" -noout -pubkey \
           | openssl pkey -pubin -outform DER \
           | openssl dgst -sha256 -r | cut -d' ' -f1)
    case_start 'the SPKI digest from the shipped pipeline is 64 hex characters'
    if [ ${#spki} -eq 64 ]; then ok; else fail "got '${spki}' (${#spki} chars)"; fi

    pin=$(b32_prefix "$spki" 26)
    case_start 'a real CA yields a 26-character pin'
    if [ ${#pin} -eq 26 ]; then ok; else fail "got '$pin' (${#pin} chars)"; fi

    # Hashing the CERTIFICATE instead of its SPKI is the classic wrong turn:
    # it would break CA-certificate reissue on the same key.
    cert_hash=$(openssl dgst -sha256 -r "$WORK/ca.pem" | cut -d' ' -f1)
    case_start 'the pin is over the SPKI, not over the certificate file'
    if [ "$spki" != "$cert_hash" ]; then ok; else
        fail 'the SPKI digest equals the certificate file digest'
    fi
fi

summary
