#!/bin/sh
# The upgrade path is a branch of the vault's /robot bootstrap: no code, no CA
# change, no enrollment; the vault serves exactly one agent version, rendered
# into the script. These tests exercise its local gates without downloading an
# artifact or requiring a running systemd instance. The full upgrade run (real
# download, flip, preserved credential, no-op, refused downgrade) is in
# test_vault_delivery.sh.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='vault agent upgrade mode'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

run_existing_guard() {
    ( INSTALL_MARKER="$1"
      AGENT_PREFIX="$2"
      TOKEN_FILE="$3"
      CONF_DIR="$4"
      EX_PREFLIGHT=2
      die() { _code=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_code"; }
      eval "$(extract_function is_digits)"
      eval "$(extract_function is_semver)"
      eval "$(extract_function file_size)"
      eval "$(extract_function install_marker_version)"
      eval "$(extract_function require_existing_install_for_upgrade)"
      require_existing_install_for_upgrade
      printf 'READY\n' )
}

MARKER="$WORK/install-complete"
PREFIX="$WORK/opt/ingotron-agent"
TOKEN="$WORK/robot.token"
CONF="$WORK/etc/ingotron"
mkdir -p "$PREFIX/current/bin" "$CONF" "$(dirname "$TOKEN")" "$(dirname "$MARKER")"
touch "$PREFIX/current/bin/ingotron-agent" "$TOKEN" "$CONF/vault.json"
printf '0.9.1\n' > "$MARKER"
chmod 0755 "$PREFIX/current/bin/ingotron-agent"

assert_status 'a completed installed robot can enter upgrade mode' 0 \
    run_existing_guard "$MARKER" "$PREFIX" "$TOKEN" "$CONF"
assert_output_contains 'READY'

rm -f "$MARKER"
assert_status 'upgrade refuses before a first install exists' 2 \
    run_existing_guard "$MARKER" "$PREFIX" "$TOKEN" "$CONF"
assert_output_contains 'requires a completed'
printf '0.9.1\n' > "$MARKER"

run_upgrade_args() {
    ( UPGRADE_MODE=0
      REENROLL_MODE=0
      VAULT=''
      CODE=''
      INGEST=''
      CA_FILE=''
      RECORDINGS_ROOT='/data/robot_data'
      ROOT_OVERRIDE=0
      EX_PREFLIGHT=2
      die() { _code=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_code"; }
      usage() { :; }
      eval "$(extract_function is_digits)"
      eval "$(extract_function collect_args)"
      eval "$(extract_function validate_args)"
      eval "$(extract_function validate_vault_url)"
      collect_args "$@"
      validate_args
      printf 'UPGRADE mode=%s vault=%s\n' "$UPGRADE_MODE" "$VAULT" )
}

assert_status 'upgrade accepts --vault and nothing else' 0 run_upgrade_args --upgrade --vault https://vault.example.com
assert_output_contains 'UPGRADE mode=1 vault=https://vault.example.com:443'
assert_status 'upgrade needs --vault' 2 run_upgrade_args --upgrade
assert_output_contains '--upgrade needs --vault'
assert_status 'upgrade refuses a plaintext --vault' 2 run_upgrade_args --upgrade --vault http://vault.example.com
for extra in "--code $VALID_CODE" '--ingest ingest.example.com:50051' '--root /srv/rec' \
             '--ca-file /etc/hostname' '--reenroll'; do
    # shellcheck disable=SC2086 -- the pair is split on purpose.
    assert_status "upgrade refuses an install-only option ($extra)" 2 \
        run_upgrade_args --upgrade --vault https://vault.example.com $extra
done
assert_status '--version is not an option any more' 2 run_upgrade_args --upgrade --vault https://v.example --version 1.2.3
assert_output_contains 'unknown argument: --version'

# version_lt is the whole downgrade rule; check it numerically, not as text.
eval "$(extract_function version_lt)"
for pair in '0.9.9 1.0.0' '1.2.3 1.2.10' '1.9.0 1.10.0' '0.0.1 0.1.0' '9.9.9 10.0.0'; do
    case_start "version_lt orders ${pair% *} below ${pair#* }"
    # shellcheck disable=SC2086
    if version_lt $pair; then ok; else fail "version_lt $pair returned false"; fi
done
for pair in '1.0.0 0.9.9' '1.2.10 1.2.3' '1.2.3 1.2.3' '10.0.0 9.9.9'; do
    case_start "version_lt does not order ${pair% *} below ${pair#* }"
    # shellcheck disable=SC2086
    if version_lt $pair; then fail "version_lt $pair returned true"; else ok; fi
done

run_decide() {
    ( INSTALL_MARKER="$1"
      AGENT_VERSION="$2"
      EX_PREFLIGHT=2
      die() { _code=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_code"; }
      log() { printf 'robot.sh: %s\n' "$*"; }
      eval "$(extract_function is_digits)"
      eval "$(extract_function is_semver)"
      eval "$(extract_function file_size)"
      eval "$(extract_function install_marker_version)"
      eval "$(extract_function version_lt)"
      eval "$(extract_function decide_upgrade)"
      decide_upgrade
      printf 'PROCEED\n' )
}
printf '0.9.10\n' > "$MARKER"
assert_status 'the same version is a successful no-op' 0 run_decide "$MARKER" 0.9.10
assert_output_contains 'nothing to do'
assert_output_lacks 'PROCEED'
assert_status 'a lower vault version is refused' 2 run_decide "$MARKER" 0.9.9
assert_output_contains 'Refusing to downgrade'
assert_status 'a higher vault version proceeds' 0 run_decide "$MARKER" 0.10.0
assert_output_contains 'PROCEED'
printf 'garbage\n' > "$MARKER"
assert_status 'an invalid marker refuses the upgrade' 2 run_decide "$MARKER" 1.0.0
printf '0.9.1\n' > "$MARKER"

case_start 'the upgrade branch decides before it downloads, and never touches CA or enrollment'
main_src=$(sed -n '/^main() {/,/^}$/p' "$ROBOT_SH")
upgrade_branch=$(printf '%s\n' "$main_src" | sed -n '/^    if \[ "\$UPGRADE_MODE" = '\''1'\'' \]; then$/,/^    fi$/p')
case $upgrade_branch in
    *fetch_ca*|*install_ca*|*verify_pin*|*run_enrollment*|*create_user_and_dirs*|*CODE*)
        fail "the upgrade branch reaches CA or enrollment steps: $upgrade_branch" ;;
    *'decide_upgrade'*'download_and_verify_agent'*'install_release'*'install_upgrade_units'*'record_install_complete'*'exit 0'*)
        ok ;;
    *) fail "unexpected upgrade branch: $upgrade_branch" ;;
esac

case_start 'upgrade refreshes units without rewriting the local site drop-in or starting uploads'
upgrade_units=$(extract_function install_upgrade_units)
case $upgrade_units in
    *write_site_dropin*|*'enable --now'*|*first_run_and_status*)
        fail 'upgrade unit refresh changes site policy or starts an upload' ;;
    *) ok ;;
esac

# A SIGKILL or power loss can land after install_release flips `current` but
# before main() records install-complete. The next upgrade invocation must
# reuse that active release. If it removes the target first, `current` dangles
# and the only runnable agent is lost during the recovery that should repair
# it. Exercise the exact boundary with a real symlink and a sentinel file.
run_active_target_recovery() {
    (
        AGENT_PREFIX="$1"
        AGENT_VERSION='1.2.3'
        STAGED_ROOT="$WORK/staged"
        EX_ARTIFACT=4
        die() { _c=$1; shift; printf 'robot.sh: error: %s\n' "$*" >&2; exit "$_c"; }
        log() { :; }
        # The test must not rewrite the host's /usr/local/bin symlink. The two
        # release-tree ln calls still use the real utility.
        ln() {
            if [ "$3" = '/usr/local/bin/ingotron-agent' ]; then
                return 0
            fi
            /bin/ln "$@"
        }
        eval "$(extract_function install_release)"
        install_release
        printf '%s\n' "$(readlink "$AGENT_PREFIX/current")"
        cat "$AGENT_PREFIX/releases/1.2.3/sentinel"
    )
}

ACTIVE_PREFIX="$WORK/active-prefix"
ACTIVE_RELEASE="$ACTIVE_PREFIX/releases/1.2.3"
mkdir -p "$ACTIVE_RELEASE/bin" "$WORK/staged"
printf 'still-live\n' > "$ACTIVE_RELEASE/sentinel"
touch "$ACTIVE_RELEASE/bin/ingotron-agent" "$ACTIVE_RELEASE/bin/ingotron-agent-run"
chmod 0755 "$ACTIVE_RELEASE/bin/ingotron-agent" "$ACTIVE_RELEASE/bin/ingotron-agent-run"
/bin/ln -s "$ACTIVE_RELEASE" "$ACTIVE_PREFIX/current"
assert_status 'retry after current flips reuses the active release' 0 \
    run_active_target_recovery "$ACTIVE_PREFIX"
assert_output_contains "$ACTIVE_RELEASE"
assert_output_contains 'still-live'
assert_file_present 'the active release remains on disk after retry' "$ACTIVE_RELEASE/sentinel"
assert_equals 'current still resolves to the target release' \
    "$(readlink "$ACTIVE_PREFIX/current")" "$ACTIVE_RELEASE"

summary
