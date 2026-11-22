#!/bin/sh
# No insecure escape hatch, plus the POSIX-only static contract that the
# shell gates exist to protect.  Every check here is on the DEPLOYED file's
# text, because that is what gets piped into a root shell.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='static contract'

# --- no insecure escape hatch ---------------------------------------------

case_start 'exactly one verification-disabling flag appears in robot.sh'
hits=$(grep -cE -- '(-k|--insecure|--no-check-certificate|GIT_SSL_NO_VERIFY|CURL_CA_BUNDLE|SSL_CERT_FILE)' "$ROBOT_SH")
if [ "$hits" = '1' ]; then ok; else
    fail "expected exactly 1 hit, got $hits:
$(grep -nE -- '(-k|--insecure|--no-check-certificate|GIT_SSL_NO_VERIFY|CURL_CA_BUNDLE|SSL_CERT_FILE)' "$ROBOT_SH")"
fi

hit_line=$(grep -nE -- '(-k|--insecure|--no-check-certificate|GIT_SSL_NO_VERIFY|CURL_CA_BUNDLE|SSL_CERT_FILE)' "$ROBOT_SH")

case_start 'that one line is the body of fetch_public'
_fetch_public=$(extract_function fetch_public)
_hit_text=${hit_line#*:}
case $_fetch_public in
    *"$_hit_text"*) ok ;;
    *) fail "the single hit is not inside fetch_public: $hit_line" ;;
esac

case_start 'curl is invoked in exactly one place: fetch_public'
_curl_lines=$(grep -v '^[[:space:]]*#' "$ROBOT_SH" | grep -E '^[[:space:]]*curl[[:space:]]|[;|&(][[:space:]]*curl[[:space:]]' || true)
if [ "$(printf '%s\n' "$_curl_lines" | grep -c .)" = '1' ] && [ "$_curl_lines" = "$_hit_text" ]; then ok; else
    fail "curl invocations outside fetch_public:
$_curl_lines"
fi

case_start 'no fetch_public call site passes the code, the token or a header'
_calls=$(grep -n 'fetch_public "' "$ROBOT_SH")
if [ -z "$_calls" ]; then
    fail 'no fetch_public call sites found'
elif printf '%s\n' "$_calls" | grep -E 'CODE|TOKEN|-H|--header|--data|--cookie|-u ' >/dev/null; then
    fail "a public fetch carries a credential: $_calls"
else
    ok
fi

case_start 'fetch_public fetches only the CA and the agent tarball'
if [ "$(printf '%s\n' "$_calls" | grep -c .)" = '2' ] \
   && printf '%s\n' "$_calls" | grep -q '"$VAULT/ca.crt"' \
   && printf '%s\n' "$_calls" | grep -q '"$VAULT/robot/agent-linux-amd64.tar.gz"'; then
    ok
else
    fail "unexpected public fetches: $_calls"
fi

for forbidden in '--code' ' -H ' ' -d ' '--data' ' -b ' '--cookie'; do
    case_start "the unverified request carries no credential ($forbidden)"
    case $hit_line in
        *"$forbidden"*) fail "the unverified CA fetch carries $forbidden: $hit_line" ;;
        *) ok ;;
    esac
done

case_start 'no environment variable can disable verification'
# Any assignment or test of a well-known TLS-bypass variable would be a hatch.
if grep -nE '(SSL_NO_VERIFY|NODE_TLS_REJECT_UNAUTHORIZED|PYTHONHTTPSVERIFY|REQUESTS_CA_BUNDLE)' "$ROBOT_SH" >/dev/null; then
    fail "robot.sh references a TLS-bypass environment variable"
else
    ok
fi

case_start 'the enrollment exchange is not made by curl'
# The exchange must go through the agent's gRPC client, never HTTP.
if grep -n 'curl' "$ROBOT_SH" | grep -q -- '--code\|CODE'; then
    fail "a curl invocation references the enrollment code"
else
    ok
fi

case_start 'the code is handed to the agent on stdin, not on its argv'
if grep -q 'printf .%s. "\$CODE" | "\$AGENT_PREFIX/current/bin/ingotron-agent" enroll' "$ROBOT_SH"; then
    ok
else
    fail 'the enrollment invocation does not pipe $CODE on stdin'
fi

# --- POSIX-only (static half) ---------------------------------------------

case_start 'the shebang is /bin/sh'
head_line=$(head -n 1 "$ROBOT_SH")
if [ "$head_line" = '#!/bin/sh' ]; then ok; else fail "shebang is '$head_line'"; fi

# Bashism checks run against the CODE, with whole-line comments removed. The
# header comment deliberately spells out the forbidden constructs by name, so
# checking the raw file would fail on its own documentation -- and "delete the
# documentation to make the test pass" is the wrong direction.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
grep -v '^[[:space:]]*#' "$ROBOT_SH" > "$WORK/code.sh"

check_absent() {
    case_start "no $1"
    if grep -nE "$2" "$WORK/code.sh" >/dev/null; then
        fail "found:
$(grep -nE "$2" "$WORK/code.sh" | head -n 5)"
    else
        ok
    fi
}

# The bashism set the contract names explicitly: "no arrays, no local, no
# ${var:0:n}, no read, no pipefail".
#
# `local` is matched at a line start OR after a ';' or '{', because a bashism
# smuggled onto the same line as a function opener is still a bashism -- and
# a start-of-line-only pattern missed exactly that (found by
# check_mutation_guards.sh).
check_absent 'local'            '(^|[;{])[[:space:]]*local[[:space:]]'
check_absent 'substring expansion ${var:0:n}' '\$\{[A-Za-z_][A-Za-z0-9_]*:[0-9]'
check_absent 'pipefail'         'pipefail'
check_absent '[[ ]] test'       '\[\['
check_absent 'function keyword' '^[[:space:]]*function[[:space:]]'
check_absent 'array assignment' '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=\('
check_absent 'echo -e / echo -n' 'echo[[:space:]]+-[en]'
check_absent 'interactive read'  '^[[:space:]]*read[[:space:]]'
check_absent 'process substitution' '<\('
check_absent 'here-string'      '<<<'
check_absent '+= append'        '[A-Za-z_][A-Za-z0-9_]*\+='

# --- the build-time render markers ----------------------------------------

for marker in AGENT_VERSION AGENT_SHA256 AGENT_SIZE AGENT_ROOT_DIR; do
    case_start "the repo copy carries the unrendered $marker marker line"
    if [ "$(grep -cx "$marker='@@$marker@@'" "$ROBOT_SH")" = '1' ]; then ok; else
        fail "robot.sh must have exactly one line $marker='@@$marker@@' for render-robot-bootstrap.sh"
    fi
done

case_start 'the preflight refuses an unrendered copy'
case $(extract_function preflight) in
    *'    check_rendered'*) ok ;;
    *) fail 'preflight does not call check_rendered, so an unrendered copy would download' ;;
esac

case_start 'no outside-origin or signing code'
# The host name is built from parts so this file does not carry it as a literal.
_no_origin_re="$(printf '%s\\.%s\\.%s' get ingotron com)"'|latest\.json|INGOTRON_ORIGIN|RELEASE_PUBKEY|pkeyutl|manifest\.json"|yanked'
if grep -nE "$_no_origin_re" "$ROBOT_SH" >/dev/null; then
    fail "found:
$(grep -nE "$_no_origin_re" "$ROBOT_SH")"
else
    ok
fi

case_start 'no generic JSON parser is invoked'
if grep -nE '\b(jq|python3? -c|python -m json)' "$ROBOT_SH" >/dev/null; then
    fail "robot.sh shells out to a JSON parser:
$(grep -nE '\b(jq|python3? -c|python -m json)' "$ROBOT_SH")"
else
    ok
fi

# --- completion marker durability ------------------------------------------

_marker_writer=$(extract_function record_install_complete)
case_start 'completion marker is staged in its containing directory and flushed before publish'
case $_marker_writer in
    *'.install-complete.tmp.$$'*'set -C'*'sync_marker_path "$_mic_tmp"'*'mv -f "$_mic_tmp" "$INSTALL_MARKER"'*'sync_marker_path "$_mic_dir"'*)
        ok ;;
    *)
        fail 'completion marker is missing same-directory temp, pre-rename flush, atomic publish, or directory flush' ;;
esac

_marker_validator=$(extract_function install_marker_version)
case_start 'completion marker validation requires exact semver bytes in a regular file'
case $_marker_validator in
    *'[ -L "$INSTALL_MARKER" ]'*'wc -l'*'is_semver'*'file_size'*)
        ok ;;
    *)
        fail 'completion marker validation does not require exact bytes, semver content, and a non-symlink regular file' ;;
esac

summary
