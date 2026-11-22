#!/bin/sh
# Vault delivery: the install command, robot.sh and render-robot-bootstrap.sh
# against a REAL local HTTPS server (openssl s_server -WWW) that plays the
# vault's public routes /robot, /ca.crt and /robot/agent-linux-amd64.tar.gz.
#
# The trust chain under test:
#   install-command digest -> robot.sh bytes; robot.sh's rendered AGENT_* values
#   -> agent tarball; pin in the code -> CA.
#
# Every case asserts an exit status AND what the server was asked for (its
# FILE: transcript), because "refused" is only meaningful if nothing after the
# refusal happened. Cases that stop before any write run everywhere; the full
# install and upgrade flows write /opt, /etc and /var and run only as root
# inside a container (run-in-container.sh), never on a developer host.
#
# EVIDENCE_DIR, when set, receives the server transcripts and sentinels.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='vault delivery'
SHELL_UNDER_TEST=${SHELL_UNDER_TEST:-/bin/sh}

WORK=$(mktemp -d)
SERVER_PID=''
cleanup_suite() {
    [ -z "$SERVER_PID" ] || kill "$SERVER_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup_suite EXIT

for tool in openssl curl tar gzip sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { skip 'the whole suite' "$tool is not installed"; summary; exit $?; }
done

make_stub_path "$WORK"
# Real curl for this suite: the server is local. Keep the id stub.
rm -f "$WORK/bin/curl"

# --- the vault's material ----------------------------------------------------

grep '^C32=' "$ROBOT_SH" > "$WORK/consts.sh"
. "$WORK/consts.sh"
eval "$(extract_function b32_prefix)"

make_ca() {
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$2" \
        -addext 'basicConstraints=critical,CA:TRUE' \
        -keyout "$1.key" -out "$1.pem" >/dev/null 2>&1
}
pin_of() {
    b32_prefix "$(openssl x509 -in "$1" -noout -pubkey | openssl pkey -pubin -outform DER \
                  | openssl dgst -sha256 -r | cut -d' ' -f1)" 26
}
make_ca "$WORK/vault-ca" vault-ca
make_ca "$WORK/other-ca" other-ca
SECRET='4KQ29WMF7T3DZ8H1RN6VBX0S3EYA2GJT'  # gitleaks:allow (test value)
CODE="ign1.$(pin_of "$WORK/vault-ca.pem").$SECRET"

# The fake agent records what robot.sh asked of it. `enroll` reads the code on
# stdin and writes what the real agent writes for robot.sh.
AGENT_LOG="$WORK/agent.log"
write_fake_agent() {
    cat > "$1" <<STUB
#!/bin/sh
printf 'argv=%s\n' "\$*" >> '$AGENT_LOG'
case \$1 in
    --version) echo "$2" ;;
    enroll)
        printf 'stdin=%s\n' "\$(cat)" >> '$AGENT_LOG'
        result=''; token=''; config=''
        while [ \$# -gt 0 ]; do
            case \$1 in
                --result-file) result=\$2; shift 2 ;;
                --token-file) token=\$2; shift 2 ;;
                --config) config=\$2; shift 2 ;;
                *) shift ;;
            esac
        done
        printf 'ignr1.fake\n' > "\$token"
        printf '{}\n' > "\$config"
        printf 'robot_id=r-1\ningest_endpoint=ingest.test:50052\nself_rotation_enabled=false\n' > "\$result"
        ;;
    status) echo 'status: ok' ;;
esac
STUB
    chmod 0755 "$1"
}

# build_release <version> <dir>: a tarball with the real units and a fake agent,
# then robot.sh rendered for it. Sets TARBALL and RENDERED.
build_release() {
    _br_v=$1; _br_d=$2
    rm -rf "$_br_d"; mkdir -p "$_br_d/src/ingotron-agent-$_br_v/bin"
    cp -R "$PKG_ROOT/agent/systemd" "$_br_d/src/ingotron-agent-$_br_v/systemd"
    write_fake_agent "$_br_d/src/ingotron-agent-$_br_v/bin/ingotron-agent" "$_br_v"
    cp "$_br_d/src/ingotron-agent-$_br_v/bin/ingotron-agent" "$_br_d/src/ingotron-agent-$_br_v/bin/ingotron-agent-run"
    TARBALL="$_br_d/agent-linux-amd64.tar.gz"
    ( cd "$_br_d/src" && tar -cf - "ingotron-agent-$_br_v" | gzip -n ) > "$TARBALL"
    RENDERED="$_br_d/robot.sh"
    $SHELL_UNDER_TEST "$PKG_ROOT/render-robot-bootstrap.sh" --template "$ROBOT_SH" \
        --tarball "$TARBALL" --version "$_br_v" --out "$RENDERED" 2>/dev/null
}

# serve <release dir> [<ca.pem>]: put the routes in place for the next request.
WWW="$WORK/www"
serve() {
    # Overwrite in place: the server's working directory must stay the same.
    mkdir -p "$WWW/robot"
    cp "$1/robot.sh" "$WWW/robot-script"
    cp "$1/agent-linux-amd64.tar.gz" "$WWW/robot/agent-linux-amd64.tar.gz"
    cp "${2:-$WORK/vault-ca.pem}" "$WWW/ca.crt"
}

# --- the local vault ---------------------------------------------------------

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=127.0.0.1' \
    -keyout "$WORK/https.key" -out "$WORK/https.pem" >/dev/null 2>&1
PORT=$(( 20000 + $$ % 20000 ))
TRANSCRIPT="$WORK/server.log"
build_release 0.9.9 "$WORK/r099"
serve "$WORK/r099"
# Line-buffered, so the FILE: transcript is complete when a case ends.
LINEBUF=''
command -v stdbuf >/dev/null 2>&1 && LINEBUF='stdbuf -oL'
( cd "$WWW" && exec $LINEBUF openssl s_server -WWW -accept "127.0.0.1:$PORT" \
      -cert "$WORK/https.pem" -key "$WORK/https.key" ) >> "$TRANSCRIPT" 2>&1 &
SERVER_PID=$!
VAULT="https://127.0.0.1:$PORT"
_tries=0
until curl -fsS -k -o /dev/null "$VAULT/ca.crt" 2>/dev/null; do
    _tries=$(( _tries + 1 ))
    [ "$_tries" -lt 50 ] || { skip 'the whole suite' "openssl s_server did not start on $PORT"; summary; exit $?; }
    sleep 0.1
done

# s_server -WWW maps a URL path to a file path, so /robot must be a file and
# /robot/agent-linux-amd64.tar.gz must be a file under a directory of the same
# name. The script route therefore lives at /robot-script here; the install
# command below names it explicitly.
SCRIPT_URL="$VAULT/robot-script"

requests() { sed -n 's/^FILE://p' "$TRANSCRIPT" | tr '\n' ' '; }
reset_transcript() { : > "$TRANSCRIPT"; }
keep_evidence() {
    [ -n "${EVIDENCE_DIR:-}" ] || return 0
    mkdir -p "$EVIDENCE_DIR"
    cp "$TRANSCRIPT" "$EVIDENCE_DIR/$1.transcript.txt"
}

# --- the install command never runs bytes it did not authenticate ---------

# The command shape the Robots page renders. sudo is a PATH stub
# that records its argv and runs nothing, so a pass proves the script was
# reached and a refusal proves it was not.
cat > "$WORK/bin/sudo" <<STUB
#!/bin/sh
printf '%s\n' "\$@" > '$WORK/sudo-argv'
STUB
chmod 0755 "$WORK/bin/sudo"
SENTINEL="$WORK/executed-sentinel"

sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
install_command() {
    printf 'f=$(mktemp) && curl -fsSk --proto =https %s -o "$f" && echo "%s  $f" | sha256sum -c - && sudo sh "$f" --vault %s --ingest %s --code %s%s' \
        "$(sq "$SCRIPT_URL")" "$1" "$(sq "$VAULT")" "$(sq 'ingest.test:50052')" "$(sq "$CODE")" "${2:-}"
}
run_command() {
    rm -f "$WORK/sudo-argv" "$SENTINEL"
    reset_transcript
    env PATH="$STUB_PATH" $SHELL_UNDER_TEST -c "$1"
}

GENUINE_SHA=$(sha256sum "$WORK/r099/robot.sh" | cut -d' ' -f1)
assert_status 'the genuine /robot with its digest reaches sudo sh' 0 run_command "$(install_command "$GENUINE_SHA")"
case_start 'sudo received the exact vault, ingest and code arguments'
if [ -f "$WORK/sudo-argv" ] && grep -qx -- '--ingest' "$WORK/sudo-argv" && grep -qx 'ingest.test:50052' "$WORK/sudo-argv" \
   && grep -qx "$VAULT" "$WORK/sudo-argv" && grep -qx "$CODE" "$WORK/sudo-argv"; then ok; else
    fail "sudo argv: $(cat "$WORK/sudo-argv" 2>/dev/null)"
fi

# A substituted /robot: different bytes that would touch a sentinel.
printf '#!/bin/sh\ntouch %s\n' "$SENTINEL" > "$WWW/robot-script"
assert_status 'a substituted /robot fails the digest check' 1 run_command "$(install_command "$GENUINE_SHA")"
assert_file_absent 'the substituted script never executed' "$SENTINEL"
assert_file_absent 'sudo was never invoked for the substituted script' "$WORK/sudo-argv"
assert_equals 'the only request was the script itself' "$(requests)" 'robot-script '
keep_evidence substituted-robot
sh "$SENTINEL" 2>/dev/null; rm -f "$SENTINEL"

cp "$WORK/r099/robot.sh" "$WWW/robot-script"
for bad_digest in '' 'abc' "$(printf '%064d' 0)" "$GENUINE_SHA$GENUINE_SHA"; do
    assert_status "no executable fallback for a missing or malformed digest ('$bad_digest')" 1 \
        run_command "$(install_command "$bad_digest")"
    assert_file_absent "sudo was never invoked ('$bad_digest')" "$WORK/sudo-argv"
done

# Every substituted argument arrives as one literal argv element, even a root
# with a space and a quote (robot.sh then refuses that root itself).
ODD_ROOT="/data/rob ot's"
assert_status 'a quoted --root with a space and a quote reaches sudo intact' 0 \
    run_command "$(install_command "$GENUINE_SHA" " --root $(sq "$ODD_ROOT")")"
case_start 'the odd root is one argv element, byte for byte'
if [ -f "$WORK/sudo-argv" ] && grep -qxF -- "$ODD_ROOT" "$WORK/sudo-argv"; then ok; else
    fail "sudo argv: $(cat "$WORK/sudo-argv" 2>/dev/null)"
fi

# --- robot.sh against the vault: refusals before any write -------------------

run_robot() {
    reset_transcript
    env PATH="$STUB_PATH" $SHELL_UNDER_TEST "$1" --vault "$VAULT" --ingest ingest.test:50052 --code "$CODE"
}

# Wrong CA: the pin refusal fires before the agent is requested.
serve "$WORK/r099" "$WORK/other-ca.pem"
assert_status 'a CA that does not match the pin is refused with EX_PIN' 3 run_robot "$WORK/r099/robot.sh"
assert_output_contains 'NOT been transmitted'
assert_equals 'only the CA was requested; the agent was never fetched' "$(requests)" 'ca.crt '
keep_evidence wrong-ca

# Genuine CA, one byte of the agent changed (same size).
serve "$WORK/r099"
_size=$(wc -c < "$WWW/robot/agent-linux-amd64.tar.gz" | tr -d ' ')
{ head -c $(( _size - 1 )) "$WORK/r099/agent-linux-amd64.tar.gz"; printf 'X'; } > "$WWW/robot/agent-linux-amd64.tar.gz"
assert_status 'a tarball with one changed byte is refused with EX_ARTIFACT' 4 run_robot "$WORK/r099/robot.sh"
assert_output_contains 'sha256 mismatch'
assert_output_contains 'Nothing was extracted'
assert_equals 'the CA and then the agent were requested' "$(requests)" 'ca.crt robot/agent-linux-amd64.tar.gz '
assert_file_absent 'nothing was installed under /opt by the refused tarball' /opt/ingotron-agent/releases/0.9.9
keep_evidence tampered-tarball

# A tarball one byte longer than the rendered size.
{ cat "$WORK/r099/agent-linux-amd64.tar.gz"; printf 'X'; } > "$WWW/robot/agent-linux-amd64.tar.gz"
assert_status 'a longer tarball is refused with EX_ARTIFACT' 4 run_robot "$WORK/r099/robot.sh"
assert_file_absent 'nothing was installed under /opt by the longer tarball' /opt/ingotron-agent/releases/0.9.9

# A genuine tarball for a DIFFERENT version than the rendered script expects.
build_release 0.9.8 "$WORK/r098"
cp "$WORK/r098/agent-linux-amd64.tar.gz" "$WWW/robot/agent-linux-amd64.tar.gz"
assert_status 'a genuine tarball of another version is refused with EX_ARTIFACT' 4 run_robot "$WORK/r099/robot.sh"
serve "$WORK/r099"

# --- render-robot-bootstrap.sh refuses what it cannot render -----------------

run_render() {
    $SHELL_UNDER_TEST "$PKG_ROOT/render-robot-bootstrap.sh" --template "$1" --tarball "$2" --version "$3" --out "$WORK/render-out.sh"
}
grep -v "^AGENT_SIZE='@@AGENT_SIZE@@'\$" "$ROBOT_SH" > "$WORK/template-missing.sh"
assert_status 'render refuses a template with a missing marker line' 2 \
    run_render "$WORK/template-missing.sh" "$WORK/r099/agent-linux-amd64.tar.gz" 0.9.9
{ cat "$ROBOT_SH"; printf "AGENT_SIZE='@@AGENT_SIZE@@'\n"; } > "$WORK/template-twice.sh"
assert_status 'render refuses a template with a repeated marker line' 2 \
    run_render "$WORK/template-twice.sh" "$WORK/r099/agent-linux-amd64.tar.gz" 0.9.9
{ cat "$ROBOT_SH"; printf "EXTRA='@@SOMETHING_NEW@@'\n"; } > "$WORK/template-extra.sh"
assert_status 'render refuses when any other @@ marker survives' 2 \
    run_render "$WORK/template-extra.sh" "$WORK/r099/agent-linux-amd64.tar.gz" 0.9.9
assert_output_contains 'marker survived'
assert_status 'render refuses a tarball whose root is not ingotron-agent-<version>' 2 \
    run_render "$ROBOT_SH" "$WORK/r098/agent-linux-amd64.tar.gz" 0.9.9
assert_status 'render refuses a version that is not X.Y.Z' 2 \
    run_render "$ROBOT_SH" "$WORK/r099/agent-linux-amd64.tar.gz" 0.9
assert_file_absent 'no rendered output was written by any refusal' "$WORK/render-out.sh"

# --- full flows: root inside a container only -------------------------------

if [ "$(id -u)" != '0' ] || [ ! -f /.dockerenv ]; then
    skip 'full install, re-run and upgrade flows' 'they write /opt, /etc and /var; run-in-container.sh runs them as root'
    summary
    exit $?
fi

# The container has no systemd; record systemctl calls instead.
cat > "$WORK/bin/systemctl" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> '$WORK/systemctl.log'
STUB
chmod 0755 "$WORK/bin/systemctl"
wipe_robot() {
    rm -rf /opt/ingotron-agent /etc/ingotron /var/lib/ingotron /data/robot_data \
           /etc/systemd/system/ingotron-agent* /usr/local/bin/ingotron-agent \
           /run/lock/ingotron-robot-bootstrap.lock
}
digest() { sha256sum "$1" | cut -d' ' -f1; }
wipe_robot
: > "$AGENT_LOG"

# INSTALL: pin -> agent -> install -> enroll at --ingest -> units -> marker.
assert_status 'a full install from the vault succeeds' 0 run_robot "$WORK/r099/robot.sh"
assert_equals 'install requested the CA first, then the agent' "$(requests)" 'ca.crt robot/agent-linux-amd64.tar.gz '
assert_equals 'the completion marker names the vault agent version' "$(cat /var/lib/ingotron/state/install-complete)" '0.9.9'
assert_equals 'current points at the installed release' "$(readlink /opt/ingotron-agent/current)" /opt/ingotron-agent/releases/0.9.9
case_start 'the agent enrolled at --ingest with the code on stdin, not argv'
if grep -q -- '^argv=enroll .*--ingest ingest.test:50052' "$AGENT_LOG" \
   && grep -qx "stdin=$CODE" "$AGENT_LOG" && ! grep -q "^argv=.*$SECRET" "$AGENT_LOG"; then ok; else
    fail "agent log: $(cat "$AGENT_LOG")"
fi
assert_file_present 'the pinned CA is installed' /etc/ingotron/ca.crt
assert_file_present 'the upload timer unit is installed' /etc/systemd/system/ingotron-agent.timer
keep_evidence full-install

assert_status 'a second install on a completed robot is refused' 2 run_robot "$WORK/r099/robot.sh"
assert_output_contains 'already enrolled and installed'

# UPGRADE: no code, keeps CA, config and credential.
TOKEN_SHA=$(digest /etc/ingotron/credentials/robot.token)
CONFIG_SHA=$(digest /etc/ingotron/vault.json)
CA_SHA=$(digest /etc/ingotron/ca.crt)
run_upgrade() {
    reset_transcript
    : > "$AGENT_LOG"
    env PATH="$STUB_PATH" $SHELL_UNDER_TEST "$1" --upgrade --vault "$VAULT"
}
build_release 1.0.0 "$WORK/r100"
serve "$WORK/r100"
assert_status 'an upgrade to the newer vault agent succeeds' 0 run_upgrade "$WORK/r100/robot.sh"
assert_equals 'upgrade requested only the agent (no CA, no code)' "$(requests)" 'robot/agent-linux-amd64.tar.gz '
assert_equals 'the marker names the new version' "$(cat /var/lib/ingotron/state/install-complete)" '1.0.0'
assert_equals 'current points at the new release' "$(readlink /opt/ingotron-agent/current)" /opt/ingotron-agent/releases/1.0.0
assert_file_present 'the previous release stays on disk' /opt/ingotron-agent/releases/0.9.9/bin/ingotron-agent
assert_equals 'the credential is unchanged' "$(digest /etc/ingotron/credentials/robot.token)" "$TOKEN_SHA"
assert_equals 'vault.json is unchanged' "$(digest /etc/ingotron/vault.json)" "$CONFIG_SHA"
assert_equals 'the CA is unchanged' "$(digest /etc/ingotron/ca.crt)" "$CA_SHA"
case_start 'the upgrade never ran an enrollment'
if grep -q '^argv=enroll' "$AGENT_LOG"; then fail "$(cat "$AGENT_LOG")"; else ok; fi
keep_evidence upgrade

assert_status 'the same version again is a no-op' 0 run_upgrade "$WORK/r100/robot.sh"
assert_output_contains 'nothing to do'
assert_equals 'the no-op made no request' "$(requests)" ''

serve "$WORK/r099"
assert_status 'an older vault agent is refused (no downgrade)' 2 run_upgrade "$WORK/r099/robot.sh"
assert_output_contains 'Refusing to downgrade'
assert_equals 'the refused downgrade made no request' "$(requests)" ''
assert_equals 'the refused downgrade kept the new release current' \
    "$(readlink /opt/ingotron-agent/current)" /opt/ingotron-agent/releases/1.0.0

wipe_robot
summary
