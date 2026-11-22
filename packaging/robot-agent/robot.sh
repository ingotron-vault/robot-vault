#!/bin/sh
# robot.sh -- install, re-enroll or upgrade the Ingotron robot agent from a vault.
#
# Each vault serves this script at /robot and the agent at
# /robot/agent-linux-amd64.tar.gz. The Robots page renders the command:
#
#   f=$(mktemp) && curl -fsSk --proto =https https://<vault-host>/robot -o "$f" \
#     && echo "<sha256 of this file>  $f" | sha256sum -c - \
#     && sudo sh "$f" --vault https://<vault-host> --ingest <ingest-host>:50051 \
#          --code ign1.<26-char-ca-pin>.<32-char-secret>
#
# TRUST CHAIN (no signing keys, no outside host, works air-gapped):
#   * the admin copies the command from an authenticated HTTPS page, so the
#     SHA-256 in it authenticates THIS FILE;
#   * this file carries the agent's version, size and SHA-256 (rendered at
#     build time by render-robot-bootstrap.sh), so it authenticates the agent;
#   * the code carries the pin of the vault's ingest CA, so it authenticates the
#     CA; and the pinned CA protects the code and every upload.
#
# THE THREE FLOWS (state machine; every refusal before "lock" writes nothing):
#
#   install   (--vault --ingest --code [--root] [--ca-file])
#     preflight -> args -> lock -> fetch CA, check pin -> fetch agent, check
#     size+sha256+members -> user/dirs -> install release, flip `current` ->
#     install CA -> enroll at --ingest over TLS pinned to that CA (the code is
#     spent only now, after the executable needed for recovery exists) ->
#     recordings writable -> units -> completion marker -> first run.
#     A failure after the exchange leaves a private recovery record or a token;
#     re-running the same command resumes locally and sends no code.
#   re-enroll (--reenroll --vault --ingest --code [--root] [--ca-file])
#     preflight (completed install required) -> args -> lock -> fetch CA,
#     check pin -> stage CA -> installed agent exchanges the code -> new
#     credential. Keeps the agent release, recordings and queue.
#   upgrade   (--upgrade --vault)
#     preflight (completed install required) -> args -> lock -> compare the
#     installed version with the one rendered here: same = no-op, lower =
#     refused -> fetch agent, check it -> install release, flip `current` ->
#     refresh units -> completion marker. No code, no CA change, no enrollment;
#     the CA, vault.json and the credential stay.
#
# STRICTLY POSIX sh. On Debian/Ubuntu /bin/sh is dash; the shell test gate also
# runs it under busybox ash on a host with no bash installed at all. So: no
# arrays, no `local`, no ${var:0:n}, no `read`, no `pipefail`, no [[ ]], no
# process substitution, no `echo -e`.
#
# The one property this script exists to guarantee:
#
#   THE VAULT CA IS PINNED AND VERIFIED BEFORE ANY SOCKET CARRIES THE
#   ENROLLMENT SECRET.
#
# Public material (the CA and the agent) is fetched by ONE helper,
# fetch_public, without certificate verification: nothing it returns is
# trusted before its own check, and it never carries a credential. A static test
# greps this file for verification-disabling flags and requires exactly one
# hit, in that helper, with none of --code, -H, -d, --data, -b or --cookie on
# it. So: do not name that flag anywhere else in this file, not even in a
# comment, or the gate fails on prose.

set -eu

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_NAME='robot.sh'
BOOTSTRAP_VERSION='1'

# The agent the vault serves. render-robot-bootstrap.sh replaces these four
# lines when the query image is built; a copy that still has the markers
# refuses in the preflight (check_rendered).
AGENT_VERSION='@@AGENT_VERSION@@'
AGENT_SHA256='@@AGENT_SHA256@@'
AGENT_SIZE='@@AGENT_SIZE@@'
AGENT_ROOT_DIR='@@AGENT_ROOT_DIR@@'

# The bundle is built on pinned Debian 11 (glibc 2.31), so 2.31 is the floor.
GLIBC_FLOOR_MAJOR=2
GLIBC_FLOOR_MINOR=31

# Host preflight: >= 200 MB free on /opt.
MIN_FREE_KB=204800

# The CA response is bounded.
CA_MAX_BYTES=8192

# The CA pin alphabet.  Crockford base32: no I, L, O, U.
C32='0123456789ABCDEFGHJKMNPQRSTVWXYZ'
CA_PIN_CHARS=26
CODE_SECRET_CHARS=32

# Default recordings tree.  The advertised one-liner carries two flags and no
# third, so the recordings tree has to have a default; this is a common path.
# --root overrides it for a site that keeps recordings elsewhere.
DEFAULT_RECORDINGS_ROOT='/data/robot_data'

# Install locations.
AGENT_PREFIX='/opt/ingotron-agent'
CONF_DIR='/etc/ingotron'
CRED_DIR='/etc/ingotron/credentials'
TOKEN_FILE='/etc/ingotron/credentials/robot.token'
STATE_DIR='/var/lib/ingotron'
# Written only after step 8 succeeds. A valid marker -- not the token's presence
# -- is what "already installed" means; see refuse_existing_install.
INSTALL_MARKER='/var/lib/ingotron/state/install-complete'
# A post-exchange recovery record includes the replacement credential. It is
# root-only, unlike /var/lib/ingotron/state which the service user can write.
ENROLLMENT_PENDING='/etc/ingotron/recovery/enrollment-pending.json'
REENROLL_PENDING="$ENROLLMENT_PENDING"
UNIT_DIR='/etc/systemd/system'
SERVICE_USER='ingotron'
SERVICE_GROUP='ingotron'

# The bootstrap mutates the shared installation tree, credential and systemd
# units.  A lock in /run/lock is deliberate: util-linux flock holds the lock
# in the kernel, so SIGKILL and power loss cannot leave a stale PID file that
# blocks the next repair attempt.  The runtime agent has its own upload lock;
# this one covers only bootstrap install/upgrade/re-enroll transactions.
ROBOT_LOCK_FILE='/run/lock/ingotron-robot-bootstrap.lock'
ROBOT_LOCK_HELD=0

# Exit codes.  Distinct so a gate can assert *which* refusal fired rather than
# merely that something failed.
EX_FAIL=1        # generic
EX_PREFLIGHT=2   # host preflight, argument validation, refused upgrade (downgrade)
EX_PIN=3         # CA pin mismatch
EX_ARTIFACT=4    # agent download / size / hash / extraction
EX_ENROLL=5      # the enrollment exchange itself
EX_PERMS=6       # the ingotron user cannot write the recordings tree

TMPDIR_SELF=''

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

log() { printf '%s: %s\n' "$SCRIPT_NAME" "$*"; }
warn() { printf '%s: %s\n' "$SCRIPT_NAME" "$*" >&2; }

die() {
    _die_code=$1
    shift
    printf '%s: error: %s\n' "$SCRIPT_NAME" "$*" >&2
    exit "$_die_code"
}

cleanup() {
    release_bootstrap_lock
    if [ -n "$TMPDIR_SELF" ] && [ -d "$TMPDIR_SELF" ]; then
        rm -rf "$TMPDIR_SELF"
    fi
}

# Acquire a non-blocking process-wide lock for the state-changing bootstrap
# modes.  This function is called only after preflight and argument validation,
# so the dependency and lock-directory checks remain side-effect free.  A
# non-blocking refusal is intentional: a second pasted one-liner should tell
# the operator to retry instead of waiting behind an enrollment exchange with
# no visible progress.
acquire_bootstrap_lock() {
    [ "$ROBOT_LOCK_HELD" = '0' ] || return 0
    exec 9>"$ROBOT_LOCK_FILE" || die "$EX_PREFLIGHT" \
        "could not open the bootstrap lock at $ROBOT_LOCK_FILE"
    if ! flock -n 9; then
        exec 9>&-
        die "$EX_PREFLIGHT" \
            "another robot install, upgrade or re-enrollment is already in progress; retry after it finishes"
    fi
    ROBOT_LOCK_HELD=1
}

release_bootstrap_lock() {
    if [ "$ROBOT_LOCK_HELD" = '1' ]; then
        flock -u 9 2>/dev/null || true
        # No redirection on this exec: `exec 9>&- 2>/dev/null` would make the
        # stderr redirection permanent and hide the error that follows.
        exec 9>&-
        ROBOT_LOCK_HELD=0
    fi
}

# hex -> first $2 Crockford-base32 characters of the decoded bytes.
#
# NORMATIVE.  This is the reference pin function.  The ingest-side
# minter (phases/03-ingest-server-rs/src/crockford.rs, b32_prefix_hex) is a
# second implementation of the same function; they are held together by one
# fixture set -- packaging/robot-agent/tests/fixtures/b32_prefix_vectors.tsv --
# which both sides must pass.  A drift between minter and verifier is
# a total enrollment outage, so do not "clean this up" without re-running that
# fixture under dash AND busybox ash.
#
# The comparison this feeds is plain string equality, and that is correct: the
# pin is a truncated hash of a PUBLIC key just downloaded over an
# unauthenticated channel, so there is nothing for a timing oracle to leak.
b32_prefix() {
    _hex=$1; _want=$2; _acc=0; _bits=0; _out=''
    while [ -n "$_hex" ] && [ ${#_out} -lt "$_want" ]; do
        _c=${_hex%"${_hex#?}"}          # first char
        _hex=${_hex#?}                  # rest
        case $_c in
            [0-9]) _v=$_c ;;
            a|A) _v=10 ;; b|B) _v=11 ;; c|C) _v=12 ;;
            d|D) _v=13 ;; e|E) _v=14 ;; f|F) _v=15 ;;
            *) return 1 ;;
        esac
        _acc=$(( (_acc << 4) | _v )); _bits=$(( _bits + 4 ))
        while [ "$_bits" -ge 5 ] && [ ${#_out} -lt "$_want" ]; do
            _i=$(( (_acc >> (_bits - 5)) & 31 ))
            _bits=$(( _bits - 5 ))
            _acc=$(( _acc & ((1 << _bits) - 1) ))
            _out=$_out$( printf '%s' "$C32" | cut -c $(( _i + 1 )) )
        done
    done
    [ ${#_out} -eq "$_want" ] || return 1
    printf '%s' "$_out"
}

# sha256 of a file, lowercase hex, no trailing fields.
#
# `openssl dgst -sha256 -r` emits "<hex> *<name>" (on stdin the suffix is
# " *stdin", not "  -").  Either way the first
# space-delimited field is the digest, which is all this reads.  OpenSSL is
# used rather than sha256sum because the preflight already guarantees OpenSSL
# >= 1.1.1 and depending on coreutils as well would be a second unpinned host
# assumption.
sha256_file() {
    openssl dgst -sha256 -r "$1" | cut -d' ' -f1
}

file_size() {
    # POSIX: wc -c on a redirect gives a bare number with no filename.
    wc -c < "$1" | tr -d ' \n\t'
}

# Use the portable no-operand form so an unsupported path operand cannot be
# confused with a successful recovery from a real file or directory I/O error.
# This global flush is intentionally conservative and runs only while publishing
# the completion marker. The preflight requires `sync` before any state change.
sync_marker_path() {
    sync 2>/dev/null
}

# Strict pattern assertions, used on everything parsed out of a downloaded
# document before it is allowed to influence a path, a size or a comparison.
is_hex64() {
    case $1 in
        *[!0-9a-f]*) return 1 ;;
        '') return 1 ;;
    esac
    [ ${#1} -eq 64 ]
}

is_digits() {
    case $1 in
        *[!0-9]*) return 1 ;;
        '') return 1 ;;
    esac
    return 0
}

is_semver() {
    _sv_major=${1%%.*}
    _sv_rest=${1#*.}
    case $1 in *.*.*) ;; *) return 1 ;; esac
    case $_sv_rest in *.*) ;; *) return 1 ;; esac
    _sv_minor=${_sv_rest%%.*}
    _sv_patch=${_sv_rest#*.}
    case $_sv_patch in *.*) return 1 ;; esac
    is_digits "$_sv_major" && is_digits "$_sv_minor" && is_digits "$_sv_patch"
}

# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

usage() {
    cat <<'USAGE'
robot.sh -- install, re-enroll or upgrade the Ingotron robot agent from a vault.

Copy the command from the Robots page of your Robot Vault. It downloads this
script, checks its SHA-256, and runs it:

  sudo sh robot.sh --vault https://<vault-host> --ingest <ingest-host>:50051 \
    --code ign1.<26-char-ca-pin>.<32-char-secret>
  sudo sh robot.sh --reenroll --vault https://<vault-host> --ingest <ingest-host>:50051 \
    --code ign1.<26-char-ca-pin>.<32-char-secret>
  sudo sh robot.sh --upgrade --vault https://<vault-host>

Options:
  --vault URL       the vault's HTTPS origin, https://host[:port] (default port 443)
  --ingest HOST:PORT
                    the vault's ingest endpoint for the enrollment exchange
                    (default: <vault host>:50051)
  --code CODE       the one-use enrollment code from the Robots page
  --root PATH       recordings tree the agent uploads from
                    (default: /data/robot_data)
  --ca-file PATH    a CA certificate transferred out of band; the pin from the
                    code is still verified against it, only the fetch is skipped
  --reenroll        replace an installed robot's CA and credential; keeps the
                    installed agent, recordings and queue
  --upgrade         install the agent this vault serves; keeps the CA, the
                    configuration and the credential. A lower version than the
                    installed one is refused; the same version does nothing.
  --help            this text

If a failure happens after the code was accepted, the script prints that a
private recovery record exists. Re-run the same command; it resumes locally
without sending the code again. Otherwise mint a fresh code before retrying.
USAGE
}

# ---------------------------------------------------------------------------
# Argument collection.  Pure string handling: no network, no filesystem writes,
# and no validation yet -- the host preflight is step 0 and argument
# validation is step 1, in that order, so that a host which cannot
# possibly complete an enrollment says so first.
# ---------------------------------------------------------------------------

VAULT=''
CODE=''
INGEST=''
CA_FILE=''
RESUMING=0
UPGRADE_MODE=0
REENROLL_MODE=0
RECORDINGS_ROOT="$DEFAULT_RECORDINGS_ROOT"
ROOT_OVERRIDE=0

collect_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --vault)   [ $# -ge 2 ] || die "$EX_PREFLIGHT" "--vault requires a value"
                       VAULT=$2; shift 2 ;;
            --vault=*) VAULT=${1#--vault=}; shift ;;
            --code)    [ $# -ge 2 ] || die "$EX_PREFLIGHT" "--code requires a value"
                       CODE=$2; shift 2 ;;
            --code=*)  CODE=${1#--code=}; shift ;;
            --root)    [ $# -ge 2 ] || die "$EX_PREFLIGHT" "--root requires a value"
                       RECORDINGS_ROOT=$2; ROOT_OVERRIDE=1; shift 2 ;;
            --root=*)  RECORDINGS_ROOT=${1#--root=}; ROOT_OVERRIDE=1; shift ;;
            --ca-file) [ $# -ge 2 ] || die "$EX_PREFLIGHT" "--ca-file requires a value"
                       CA_FILE=$2; shift 2 ;;
            --ca-file=*) CA_FILE=${1#--ca-file=}; shift ;;
            --ingest)  [ $# -ge 2 ] || die "$EX_PREFLIGHT" "--ingest requires a value"
                       INGEST=$2; shift 2 ;;
            --ingest=*) INGEST=${1#--ingest=}; shift ;;
            --upgrade) UPGRADE_MODE=1; shift ;;
            --reenroll) REENROLL_MODE=1; shift ;;
            --help|-h) usage; exit 0 ;;
            *)
                # Named explicitly so the refusal reads as a refusal rather
                # than as a parse accident.  There is no
                # verification-disabling flag and no environment variable that
                # turns verification off, so an unknown flag is never ignored.
                die "$EX_PREFLIGHT" "unknown argument: $1"
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# STEP 0 -- host preflight.  Before any download and before any network call.
# Failure exits 2 with NOTHING downloaded and NOTHING written.
# ---------------------------------------------------------------------------

preflight() {
    _uname_s=$(uname -s)
    _uname_m=$(uname -m)
    [ "$_uname_s" = 'Linux' ] || \
        die "$EX_PREFLIGHT" "this agent supports Linux only (found: $_uname_s)"
    [ "$_uname_m" = 'x86_64' ] || \
        die "$EX_PREFLIGHT" "this agent ships linux/amd64 only (found: $_uname_m)"

    [ "$(id -u)" = '0' ] || \
        die "$EX_PREFLIGHT" "must run as root; use: sudo sh robot.sh ..."

    [ -d /run/systemd/system ] || \
        die "$EX_PREFLIGHT" "systemd is required (no /run/systemd/system); this agent installs a systemd timer"

    # flock is the only lock primitive accepted here.  Check both the command
    # and its dedicated runtime directory before any lock file is opened, so a
    # missing host dependency is reported as a side-effect-free preflight
    # refusal rather than after a partial installation has started.
    command -v flock >/dev/null 2>&1 || \
        die "$EX_PREFLIGHT" "flock is required to serialize robot install, upgrade and re-enrollment (install util-linux)"
    [ -d /run/lock ] || \
        die "$EX_PREFLIGHT" "the /run/lock directory is required for the robot bootstrap lock"
    [ -w /run/lock ] || \
        die "$EX_PREFLIGHT" "the /run/lock directory is not writable; cannot acquire the robot bootstrap lock"

    command -v curl >/dev/null 2>&1 || \
        die "$EX_PREFLIGHT" "curl is required. Install it: apt-get install -y curl   (or: dnf install -y curl)"

    command -v sync >/dev/null 2>&1 || \
        die "$EX_PREFLIGHT" "sync is required to make the completion marker durable"

    if [ "$REENROLL_MODE" != '1' ]; then
        command -v tar >/dev/null 2>&1 || \
            die "$EX_PREFLIGHT" "tar is required. Install it: apt-get install -y tar"
    fi

    command -v openssl >/dev/null 2>&1 || \
        die "$EX_PREFLIGHT" "openssl >= 1.1.1 is required. Install it: apt-get install -y openssl"

    # A SHA-256 tool.  openssl satisfies this and is required anyway; the check
    # is kept distinct so the error names the missing tool.
    if ! command -v sha256sum >/dev/null 2>&1; then
        openssl dgst -sha256 </dev/null >/dev/null 2>&1 || \
            die "$EX_PREFLIGHT" "no SHA-256 tool: neither sha256sum nor a working 'openssl dgst -sha256'"
    fi

    check_openssl
    check_rendered
    if [ "$REENROLL_MODE" != '1' ]; then
        check_glibc_floor "$GLIBC_FLOOR_MAJOR" "$GLIBC_FLOOR_MINOR" 'this bootstrap'
        check_disk_space
    fi
    # ORDERING RULE: every side-effect-free refusal runs in the PREFLIGHT.
    # This one is decided from two local stats and nothing else, so it belongs beside the
    # rest of step 0 rather than after argument validation -- refusing late
    # protects nothing, while refusing early costs one stat.
    if [ "$UPGRADE_MODE" = '1' ]; then
        require_existing_install_for_upgrade
    elif [ "$REENROLL_MODE" = '1' ]; then
        require_existing_install_for_reenroll
    else
        refuse_existing_install
    fi
}

# The first state check above happens before locking so malformed/ineligible
# invocations still refuse without opening the lock file.  Repeat the one check
# whose answer can change while another bootstrap is running after the lock is
# held. Without this linearization point two clean first-install invocations
# could both pass preflight and the second could overwrite the first robot's
# credential after the first transaction completed. Existing-install modes also
# revalidate their marker here so a malformed marker cannot enter upgrade or
# re-enrollment after the side-effect-free preflight.
recheck_fresh_install_under_lock() {
    if [ "$UPGRADE_MODE" = '1' ] || [ "$REENROLL_MODE" = '1' ]; then
        install_marker_version >/dev/null 2>&1 || die "$EX_PREFLIGHT" \
            "the completion marker at $INSTALL_MARKER is missing or invalid; refusing --upgrade/--reenroll until the completed install state is repaired"
        return 0
    fi
    if install_marker_present; then
        if install_marker_version >/dev/null 2>&1; then
            die "$EX_PREFLIGHT" \
                "this host is already enrolled and installed. To move it to the vault's agent version, copy the upgrade command from the Robots page (robot.sh --upgrade --vault <url>). To re-enroll it, mint a re-enrollment code on the Robots page and run the command shown there (robot.sh --reenroll ...)."
        fi
        # This is the only place an invalid marker is removed. It runs after
        # acquire_bootstrap_lock, so a concurrent bootstrap cannot race the
        # decision or observe a half-repaired completion state.
        rm -f "$INSTALL_MARKER" || die "$EX_PREFLIGHT" \
            "could not remove the invalid completion marker at $INSTALL_MARKER while holding the bootstrap lock"
        warn "removed the invalid completion marker; the interrupted install will be resumed and republished after it succeeds"
    fi
    if [ -e "$ENROLLMENT_PENDING" ]; then
        RESUMING=1
        warn "a durable enrollment recovery record exists; resuming locally without sending the code again."
    elif [ -e "$TOKEN_FILE" ]; then
        RESUMING=1
        warn "a credential is already stored at $TOKEN_FILE but the install did not finish; resuming it. The enrollment code is NOT re-used and none is needed."
    fi
}

# OpenSSL 1.1.1 or newer, parsed from `openssl version`. The script uses it
# for the CA checks and the SPKI pin (x509, pkey, dgst -sha256 -r), which
# OpenSSL 1.1.1 has, so Ubuntu 20.04 and Debian 11 qualify. The vendor must be
# OpenSSL: the pin pipeline is tested only against OpenSSL's output formats.
check_openssl() {
    _ossl_line=$(openssl version 2>/dev/null || true)
    case $_ossl_line in
        'OpenSSL '*) ;;
        '') die "$EX_PREFLIGHT" "'openssl version' produced no output; OpenSSL >= 1.1.1 is required" ;;
        *) die "$EX_PREFLIGHT" "OpenSSL >= 1.1.1 is required; found: $_ossl_line" ;;
    esac
    _ossl_ver=${_ossl_line#OpenSSL }
    _ossl_ver=${_ossl_ver%% *}
    _ossl_major=${_ossl_ver%%.*}
    case $_ossl_major in
        *[!0-9]*|'') die "$EX_PREFLIGHT" "could not parse an openssl major version from: $_ossl_line" ;;
    esac
    [ "$_ossl_major" -ge 3 ] && return 0
    case $_ossl_ver in
        1.1.1*) return 0 ;;
    esac
    die "$EX_PREFLIGHT" \
        "OpenSSL >= 1.1.1 is required, found $_ossl_ver. Install it: apt-get install -y openssl"
}

# The four AGENT_* values must have been rendered by render-robot-bootstrap.sh.
# A copy with markers, or with values of the wrong shape, can verify nothing it
# downloads, so it refuses before any network call.
check_rendered() {
    is_semver "$AGENT_VERSION" && is_hex64 "$AGENT_SHA256" && is_digits "$AGENT_SIZE" \
        && [ "$AGENT_ROOT_DIR" = "ingotron-agent-$AGENT_VERSION" ] || die "$EX_PREFLIGHT" \
        "this copy of $SCRIPT_NAME was not rendered by a vault build, so it cannot check the agent it would download. Copy the command from the Robots page of your vault."
}

# $1 major, $2 minor, $3 what needs it
check_glibc_floor() {
    _need_major=$1; _need_minor=$2; _need_who=$3
    _glibc_raw=''
    if command -v getconf >/dev/null 2>&1; then
        _glibc_raw=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
        _glibc_raw=${_glibc_raw#glibc }
    fi
    if [ -z "$_glibc_raw" ] && command -v ldd >/dev/null 2>&1; then
        _glibc_raw=$(ldd --version 2>/dev/null | head -n 1 | tr ' ' '\n' | tail -n 1 || true)
    fi
    if [ -z "$_glibc_raw" ]; then
        die "$EX_PREFLIGHT" "could not determine the glibc version; $_need_who needs glibc >= $_need_major.$_need_minor"
    fi
    _have_major=${_glibc_raw%%.*}
    _have_minor=${_glibc_raw#*.}
    _have_minor=${_have_minor%%.*}
    case $_have_major$_have_minor in
        *[!0-9]*|'') die "$EX_PREFLIGHT" "could not parse a glibc version from: $_glibc_raw" ;;
    esac
    if [ "$_have_major" -gt "$_need_major" ]; then
        return 0
    fi
    if [ "$_have_major" -eq "$_need_major" ] && [ "$_have_minor" -ge "$_need_minor" ]; then
        return 0
    fi
    die "$EX_PREFLIGHT" \
        "glibc $_need_major.$_need_minor or newer is required by $_need_who; this host has $_glibc_raw. Ubuntu 20.04+ / Debian 11+ satisfy it."
}

check_disk_space() {
    _df_target=/opt
    [ -d "$_df_target" ] || _df_target=/
    # POSIX output format; the 4th field of the last line is available 1K blocks.
    _free_kb=$(df -Pk "$_df_target" 2>/dev/null | tail -n 1 | tr -s ' ' | cut -d' ' -f4)
    case $_free_kb in
        *[!0-9]*|'') die "$EX_PREFLIGHT" "could not read free space on $_df_target" ;;
    esac
    [ "$_free_kb" -ge "$MIN_FREE_KB" ] || die "$EX_PREFLIGHT" \
        "at least 200 MB free is required on $_df_target; found $(( _free_kb / 1024 )) MB. Free space and re-run."
}

# ---------------------------------------------------------------------------
# STEP 1 -- parse and validate arguments before touching the network.
# A bad argument exits 2 with zero connection attempts.
# ---------------------------------------------------------------------------

_VA_NL='
'

validate_args() {
    if [ "$UPGRADE_MODE" = '1' ]; then
        [ "$REENROLL_MODE" = '0' ] || die "$EX_PREFLIGHT" "--upgrade cannot be combined with --reenroll"
        [ -n "$VAULT" ] || die "$EX_PREFLIGHT" "--upgrade needs --vault https://<vault-host> (the vault that serves the agent)"
        [ -z "$CODE" ] || die "$EX_PREFLIGHT" "--upgrade cannot be combined with --code"
        [ -z "$INGEST" ] || die "$EX_PREFLIGHT" "--upgrade cannot be combined with --ingest"
        [ -z "$CA_FILE" ] || die "$EX_PREFLIGHT" "--upgrade cannot be combined with --ca-file"
        [ "$ROOT_OVERRIDE" = '0' ] || \
            die "$EX_PREFLIGHT" "--upgrade cannot be combined with --root"
        validate_vault_url "$VAULT"
        return 0
    fi

    [ -n "$VAULT" ] || die "$EX_PREFLIGHT" "--vault is required (https://<vault-host>)"
    [ -n "$CODE" ]  || die "$EX_PREFLIGHT" "--code is required (ign1.<pin>.<secret>, copied from the Robots page)"

    validate_vault_url "$VAULT"
    [ -n "$INGEST" ] || INGEST="$VAULT_HOST:50051"
    validate_ingest "$INGEST"
    validate_code "$CODE"

    if [ -n "$CA_FILE" ]; then
        [ -f "$CA_FILE" ] || die "$EX_PREFLIGHT" "--ca-file $CA_FILE does not exist or is not a regular file"
        [ -r "$CA_FILE" ] || die "$EX_PREFLIGHT" "--ca-file $CA_FILE is not readable"
    fi

    # In re-enrollment's default case the installed vault.json keeps the root.
    if [ "$REENROLL_MODE" = '0' ] || [ "$ROOT_OVERRIDE" = '1' ]; then
        validate_recordings_root
    fi
}

# Shared path validation for first install and the optional re-enrollment root
# override.
validate_recordings_root() {
    case $RECORDINGS_ROOT in
        /*) ;;
        *) die "$EX_PREFLIGHT" "--root must be an absolute path, got: $RECORDINGS_ROOT" ;;
    esac
    # Keep the path a single boring token in every consumer. In particular,
    # systemd parses ReadWritePaths= as a whitespace-separated list, so
    # `/srv/recordings /etc` would otherwise grant a second path. C locale
    # makes [:space:]/[:cntrl:] mean the portable ASCII set on every host.
    _va_no_ascii_ws=$(printf '%s' "$RECORDINGS_ROOT" | LC_ALL=C tr -d '[:space:][:cntrl:]')
    [ "$_va_no_ascii_ws" = "$RECORDINGS_ROOT" ] || \
        die "$EX_PREFLIGHT" "--root may not contain ASCII whitespace or control characters (it must remain one systemd path)"
    # The recordings root reaches `su -s /bin/sh -c` in step 7 and a systemd
    # drop-in in step 8, both of which interpolate it into a string that is
    # then parsed. A path with a quote, a dollar sign, a backtick, a newline or
    # a backslash would escape one of those contexts. The operator typed this
    # on the same command line as `sudo sh`, so it is not an untrusted input --
    # but "the caller is already root" is a reason to be careful about
    # accidents, not a reason to interpolate unvalidated text into a shell
    # string. A recordings directory does not need these characters.
    # Each forbidden character is its own QUOTED pattern rather than a bracket
    # expression: backslash handling inside a case bracket expression differs
    # between shells, and a bracket that silently matches everything would
    # refuse every legitimate path.
    #
    # '%' is in this list for the systemd drop-in, not for `su -c`: systemd runs ReadWritePaths=
    # through specifier expansion, so `--root /data/%T/rec` would grant the
    # service write access to /var/tmp/rec instead of the operator's tree, and
    # `--root /data/50%` would emit a drop-in that fails to parse. The shell
    # metacharacters below matter to the `su -s /bin/sh -c "..."` probe in
    # step 7 instead. Two different consumers, one list.
    case $RECORDINGS_ROOT in
        *"'"*|*'"'*|*'$'*|*'`'*|*'\'*|*'%'*|*"$_VA_NL"*)
            die "$EX_PREFLIGHT" "--root may not contain quotes, \$, backticks, backslashes, '%' (systemd expands it as a specifier) or newlines" ;;
    esac
    case $RECORDINGS_ROOT in
        *//*|*/) die "$EX_PREFLIGHT" "--root must be a normalised absolute path with no trailing or doubled slash, got: $RECORDINGS_ROOT" ;;
        */../*|*/..) die "$EX_PREFLIGHT" "--root may not contain a parent-directory component" ;;
    esac

    # Rule: validate that the path is absolute, contains no `..` component,
    # and resolves inside no system-owned prefix before it is written.
    #
    # The drop-in this value lands in grants the service WRITE access to it,
    # and the step-7 probe creates a file there. Pointing that at /etc or /usr
    # is not an attack so much as an accident with the blast radius of one --
    # and the agent's own trees are on the list because widening them from a
    # site drop-in is how the credential directory stops being 0700.
    for _va_deny in /etc /usr /bin /sbin /lib /lib64 /boot /proc /sys /dev /run \
                    /var/lib/ingotron /opt/ingotron-agent; do
        case "$RECORDINGS_ROOT/" in
            "$_va_deny"/*)
                die "$EX_PREFLIGHT" "--root may not be inside the system-owned prefix $_va_deny (it would be granted write access by the unit drop-in), got: $RECORDINGS_ROOT" ;;
        esac
    done
    [ "$RECORDINGS_ROOT" != '/' ] || \
        die "$EX_PREFLIGHT" "--root may not be the filesystem root"
}

# --vault must parse as https://host[:port].
validate_vault_url() {
    _vu=$1
    case $_vu in
        https://*) ;;
        http://*) die "$EX_PREFLIGHT" "--vault must be https://; the vault has no plaintext listener by design" ;;
        *) die "$EX_PREFLIGHT" "--vault must be an https:// URL, got: $_vu" ;;
    esac
    _vu_hostport=${_vu#https://}
    _vu_hostport=${_vu_hostport%/}
    case $_vu_hostport in
        ''|*/*|*@*|*'?'*|*'#'*)
            die "$EX_PREFLIGHT" "--vault must be https://host[:port] with no path, credentials or query, got: $_vu" ;;
    esac
    case $_vu_hostport in
        *:*)
            VAULT_HOST=${_vu_hostport%:*}
            VAULT_PORT=${_vu_hostport##*:}
            case $VAULT_HOST in *:*) die "$EX_PREFLIGHT" "--vault host is malformed: $_vu_hostport" ;; esac
            is_digits "$VAULT_PORT" || die "$EX_PREFLIGHT" "--vault port must be numeric, got: $VAULT_PORT"
            [ "$VAULT_PORT" -ge 1 ] && [ "$VAULT_PORT" -le 65535 ] || \
                die "$EX_PREFLIGHT" "--vault port out of range: $VAULT_PORT"
            ;;
        *)
            VAULT_HOST=$_vu_hostport
            VAULT_PORT=443
            ;;
    esac
    case $VAULT_HOST in
        ''|*[!A-Za-z0-9.-]*)
            die "$EX_PREFLIGHT" "--vault host may contain only letters, digits, '.' and '-', got: $VAULT_HOST" ;;
    esac
    VAULT="https://$VAULT_HOST:$VAULT_PORT"
}

# --ingest must be host:port with the same host alphabet as --vault, so the
# value reaches the agent's argv as one boring token. validate_args fills the
# default, <vault host>:50051, before this runs.
validate_ingest() {
    case $1 in
        *:*) ;;
        *) die "$EX_PREFLIGHT" "--ingest must be host:port, got: $1" ;;
    esac
    _vi_host=${1%:*}
    _vi_port=${1##*:}
    case $_vi_host in
        ''|*[!A-Za-z0-9.-]*)
            die "$EX_PREFLIGHT" "--ingest host may contain only letters, digits, '.' and '-', got: $_vi_host" ;;
    esac
    is_digits "$_vi_port" || die "$EX_PREFLIGHT" "--ingest port must be numeric, got: $_vi_port"
    [ ${#_vi_port} -le 5 ] && [ "$_vi_port" -ge 1 ] && [ "$_vi_port" -le 65535 ] || \
        die "$EX_PREFLIGHT" "--ingest port out of range: $_vi_port"
}

# Enrollment code grammar: the literal prefix `ign1.`, a 26-character Crockford
# pin segment, a `.`, and a 32-character Crockford secret segment.  Anything
# else exits 2 with no network activity and with the secret never leaving argv.
#
# Crockford's alphabet excludes I, L, O and U; the bracket expression below is
# exactly 0-9 A-H J K M N P-T V-Z.  Uppercase only: the minter emits uppercase
# and the pin comparison in step 5 is a byte-for-byte comparison of encoded
# strings, so accepting a case-folded or I/L/O-substituted variant would mean
# normalising input that is copy-pasted, never typed.
validate_code() {
    _cd=$1
    [ ${#_cd} -eq 64 ] || die "$EX_PREFLIGHT" \
        "--code must be 64 characters (ign1.<26>.<32>), got ${#_cd}. Copy the whole command from the Robots page."
    case $_cd in
        ign1.*) ;;
        *) die "$EX_PREFLIGHT" "--code must begin with 'ign1.'; a code with any other version tag is refused before any lookup" ;;
    esac
    _cd_rest=${_cd#ign1.}
    case $_cd_rest in
        *.*.*) die "$EX_PREFLIGHT" "--code has too many '.'-separated segments" ;;
        *.*) ;;
        *) die "$EX_PREFLIGHT" "--code must be ign1.<pin>.<secret>" ;;
    esac
    CODE_PIN_SEGMENT=${_cd_rest%%.*}
    _cd_secret=${_cd_rest#*.}
    [ ${#CODE_PIN_SEGMENT} -eq "$CA_PIN_CHARS" ] || die "$EX_PREFLIGHT" \
        "--code pin segment must be $CA_PIN_CHARS characters, got ${#CODE_PIN_SEGMENT}"
    [ ${#_cd_secret} -eq "$CODE_SECRET_CHARS" ] || die "$EX_PREFLIGHT" \
        "--code secret segment must be $CODE_SECRET_CHARS characters, got ${#_cd_secret}"
    case $CODE_PIN_SEGMENT in
        *[!0-9A-HJKMNP-TV-Z]*)
            die "$EX_PREFLIGHT" "--code pin segment contains a character outside the Crockford base32 alphabet" ;;
    esac
    case $_cd_secret in
        *[!0-9A-HJKMNP-TV-Z]*)
            die "$EX_PREFLIGHT" "--code secret segment contains a character outside the Crockford base32 alphabet" ;;
    esac
    _cd_secret=''
}

# ORDERING RULE: every side-effect-free refusal runs in the PREFLIGHT, and
# 'already installed' is a COMPLETION MARKER, never a credential.
#
# Refusing after steps 3-8 would protect nothing: by then the service user is
# created, the agent is installed to /opt, the CA is pinned, the enrollment code
# is spent and the timers are started.  Refusing in the preflight costs one
# local stat, so preflight() calls this function.
#
# The test is the CREDENTIAL, not the presence of /opt, and that distinction is
# load-bearing.  The agent is installed BEFORE the exchange, because the
# exchange is run by the installed agent.  So the state left behind by a failed
# TLS handshake or a refused code is "bundle installed, nothing enrolled".
# Keying the refusal on `/opt/ingotron-agent/current` would make "the code is
# still unconsumed afterwards and is still redeemable by a correct invocation"
# unsatisfiable, because the correct invocation would refuse to start.  An
# unenrolled tree is a failed
# attempt, and re-running over it is the documented remedy; a token means a
# real enrollment, and that is what must never be silently replaced.
install_marker_present() {
    [ -e "$INSTALL_MARKER" ] || [ -L "$INSTALL_MARKER" ]
}

# Return the exact one-line X.Y.Z version only for a regular, non-symlink
# marker. `wc -l` and the byte-size check intentionally require exactly the
# final newline written by record_install_complete, so an interrupted write or
# an extra tail cannot be mistaken for a complete install. The caller uses this
# both before and after the bootstrap lock; the second check is the
# state-changing linearization point.
install_marker_version() {
    [ -f "$INSTALL_MARKER" ] || return 1
    if [ -L "$INSTALL_MARKER" ]; then
        return 1
    fi
    _im_lines=$(wc -l < "$INSTALL_MARKER" 2>/dev/null | tr -d ' \t\n') || return 1
    [ "$_im_lines" = '1' ] || return 1
    _im_version=$(sed -n '1p' "$INSTALL_MARKER" 2>/dev/null) || return 1
    is_semver "$_im_version" || return 1
    _im_bytes=$(file_size "$INSTALL_MARKER") || return 1
    _im_expected_bytes=$(( ${#_im_version} + 1 ))
    [ "$_im_bytes" = "$_im_expected_bytes" ] || return 1
    printf '%s' "$_im_version"
}

refuse_existing_install() {
    # "Already installed" means the install COMPLETED, and the marker written
    # at the end of main() is the only thing that says so.
    #
    # Keying this on the TOKEN would brick the robot: the token is written
    # in step 6, but steps 7 and 8 run after it, so a step-7 permissions
    # refusal (for example a chmod on the recordings tree) would leave a token
    # with no units and no way forward. The one-liner would refuse over it, and
    # `enroll --force` would need a fresh code, because the successful exchange
    # spent the old one, while this script printed "re-running this command
    # does NOT need a new enrollment code".
    #
    # So: valid marker present -> genuinely enrolled, refuse. Token but no valid
    # marker -> an interrupted install, and re-running is the documented remedy,
    # which means resuming WITHOUT an exchange.
    if install_marker_present; then
        if install_marker_version >/dev/null 2>&1; then
            die "$EX_PREFLIGHT" \
                "this host is already enrolled and installed. To move it to the vault's agent version, copy the upgrade command from the Robots page (robot.sh --upgrade --vault <url>). To re-enroll it, mint a re-enrollment code on the Robots page and run the command shown there (robot.sh --reenroll ...)."
        fi
        # Do not remove anything before the bootstrap lock. An empty, partial
        # or corrupt marker is recoverable state, not proof of completion.
        warn "the completion marker at $INSTALL_MARKER is empty, partial or corrupt; it will be repaired under the bootstrap lock"
    fi
    if [ -e "$ENROLLMENT_PENDING" ]; then
        RESUMING=1
        warn "a durable enrollment recovery record exists; resuming locally without sending the code again."
    elif [ -e "$TOKEN_FILE" ]; then
        RESUMING=1
        warn "a credential is already stored at $TOKEN_FILE but the install did not finish; resuming it. The enrollment code is NOT re-used and none is needed."
    fi
    if [ -e "$AGENT_PREFIX/current" ]; then
        warn "an agent is installed at $AGENT_PREFIX but the install did not finish; replacing it"
    fi
}

# The upgrade path has a distinct preflight from first installation. It only
# needs enough local state to prove that there is an installed robot whose
# data and enrollment files must be retained. The agent tarball is still
# verified later, before `current` changes.
require_existing_install_for_upgrade() {
    install_marker_version >/dev/null 2>&1 || die "$EX_PREFLIGHT" \
        "--upgrade requires a completed Ingotron robot install with a valid completion marker; repair the marker or use the enrollment command from the Robots page first"
    [ -x "$AGENT_PREFIX/current/bin/ingotron-agent" ] || die "$EX_PREFLIGHT" \
        "--upgrade found no current agent executable at $AGENT_PREFIX/current"
    [ -f "$TOKEN_FILE" ] || die "$EX_PREFLIGHT" \
        "--upgrade found no robot credential at $TOKEN_FILE; repair or re-enroll this robot before upgrading"
    [ -f "$CONF_DIR/vault.json" ] || die "$EX_PREFLIGHT" \
        "--upgrade found no vault configuration at $CONF_DIR/vault.json; repair or re-enroll this robot before upgrading"
}

# Re-enrollment is deliberately a mode of the PUBLIC bootstrap, not a direct
# invocation of the installed agent from the UI. That lets a rebuilt vault
# replace its CA before the credentialed exchange while retaining every local
# release, recording and queue file. The completion marker is required so an
# interrupted first install cannot accidentally enter this path.
require_existing_install_for_reenroll() {
    install_marker_version >/dev/null 2>&1 || die "$EX_PREFLIGHT" \
        "--reenroll requires a completed Ingotron robot install with a valid completion marker; repair the marker or use the enrollment command from the Robots page first"
    [ -x "$AGENT_PREFIX/current/bin/ingotron-agent" ] || die "$EX_PREFLIGHT" \
        "--reenroll found no current agent executable at $AGENT_PREFIX/current"
    [ -f "$TOKEN_FILE" ] || die "$EX_PREFLIGHT" \
        "--reenroll found no robot credential at $TOKEN_FILE; use the enrollment command from the Robots page first"
    [ -f "$CONF_DIR/vault.json" ] || die "$EX_PREFLIGHT" \
        "--reenroll found no vault configuration at $CONF_DIR/vault.json"
    [ -f "$CONF_DIR/ca.crt" ] || die "$EX_PREFLIGHT" \
        "--reenroll found no installed vault CA at $CONF_DIR/ca.crt"
}

record_install_complete() {
    is_semver "$AGENT_VERSION" || die "$EX_FAIL" \
        "cannot publish an invalid agent version in the completion marker"
    _mic_dir=${INSTALL_MARKER%/*}
    _mic_tmp="$_mic_dir/.install-complete.tmp.$$"
    rm -f "$_mic_tmp" || die "$EX_FAIL" \
        "could not clear the completion-marker temporary file"
    (
        umask 077
        # O_EXCL via noclobber keeps a stale/raced symlink from turning the
        # root shell's temporary write into an arbitrary-file overwrite.
        set -C
        printf '%s\n' "$AGENT_VERSION" > "$_mic_tmp"
    ) || {
        rm -f "$_mic_tmp"
        die "$EX_FAIL" "could not write the completion marker temporary file"
    }
    chmod 0644 "$_mic_tmp" || {
        rm -f "$_mic_tmp"
        die "$EX_FAIL" "could not set completion marker permissions"
    }
    sync_marker_path "$_mic_tmp" || {
        rm -f "$_mic_tmp"
        die "$EX_FAIL" "could not durably flush the completion marker"
    }
    # The temporary file is deliberately in the marker's directory: rename is
    # then atomic on the same filesystem and cannot expose a partial marker.
    mv -f "$_mic_tmp" "$INSTALL_MARKER" || {
        rm -f "$_mic_tmp"
        die "$EX_FAIL" "could not atomically publish the completion marker"
    }
    sync_marker_path "$_mic_dir" || \
        die "$EX_FAIL" "completion marker was published but its directory could not be durably flushed"
}

# ---------------------------------------------------------------------------
# Public downloads.  THIS IS THE ONLY FUNCTION THAT SKIPS CERTIFICATE
# VERIFICATION, AND NO CALLER PASSES IT A CREDENTIAL.  It fetches only public
# material from the vault -- the CA certificate and the agent tarball -- and
# each caller checks what it returns before trusting it: the CA against the
# pin in the code, the tarball against the size and SHA-256 rendered into this
# file.  The vault's HTTPS certificate may come from any CA, including a private
# one this host does not trust, so verification here would add no security and
# would stop air-gapped sites.
#   $1 = URL, $2 = output file, $3 = maximum bytes, $4 = maximum seconds
# ---------------------------------------------------------------------------
fetch_public() {
    curl -fsS --proto '=https' --tlsv1.2 --max-time "$4" --max-filesize "$3" --insecure -o "$2" "$1"
}

# ---------------------------------------------------------------------------
# Fetch and check the agent tarball against the values rendered into this file.
# Size BEFORE hash -- the size bounds disk use, so curl enforces it during the
# transfer and it is re-checked on the file; only then is the hash computed.
# Extraction refuses absolute paths, '..' components, symlinks, hardlinks,
# device nodes and setuid/setgid entries, and requires exactly one top-level
# directory equal to AGENT_ROOT_DIR.
# ---------------------------------------------------------------------------
download_and_verify_agent() {
    fetch_public "$VAULT/robot/agent-linux-amd64.tar.gz" "$TMPDIR_SELF/agent.tar.gz" "$AGENT_SIZE" 600 \
      || die "$EX_ARTIFACT" "could not download $VAULT/robot/agent-linux-amd64.tar.gz (or it was larger than the $AGENT_SIZE bytes this script expects)"

    _dv_size=$(file_size "$TMPDIR_SELF/agent.tar.gz")
    [ "$_dv_size" = "$AGENT_SIZE" ] || die "$EX_ARTIFACT" \
        "agent tarball size mismatch: this script expects $AGENT_SIZE bytes, downloaded $_dv_size. Nothing was extracted."

    _dv_sha=$(sha256_file "$TMPDIR_SELF/agent.tar.gz")
    [ "$_dv_sha" = "$AGENT_SHA256" ] || die "$EX_ARTIFACT" \
        "agent tarball sha256 mismatch: this script expects $AGENT_SHA256, downloaded $_dv_sha. Nothing was extracted."

    # `tar -tvzf` so the type character and the mode string are visible: a
    # name-only listing cannot tell a regular file from a symlink or a device
    # node, and a setuid member in a root-extracted archive is a local root.
    tar -tvzf "$TMPDIR_SELF/agent.tar.gz" > "$TMPDIR_SELF/agent.list" \
      || die "$EX_ARTIFACT" "the agent tarball is not readable as a gzip tar archive"
    while IFS= read -r _dv_line || [ -n "$_dv_line" ]; do
        [ -n "$_dv_line" ] || continue
        # LINK MEMBERS ARE REFUSED BY THE RENDERED ARROW, NOT BY THE TYPE
        # CHARACTER, and that is the whole point of this block.
        #
        # GNU tar types a hardlink `h` and renders " link to <target>".
        # busybox tar types a hardlink `-` -- indistinguishable from a regular
        # file -- and renders " -> <target>", the same syntax it uses for a
        # symlink. So a type-character check alone lets a hardlink through on
        # busybox, which is precisely a platform the shell test gate requires
        # the script to run on. Neither link kind is permitted in this archive,
        # so refusing the ARROW catches both on both tars, and refusing it
        # BEFORE the name is extracted is what stops the arrow-stripping below
        # from hiding an absolute or `..` target from the checks that follow.
        case $_dv_line in
            *' -> '*)
                die "$EX_ARTIFACT" "the agent tarball contains a link member (symlink or hardlink): $_dv_line" ;;
            *' link to '*)
                die "$EX_ARTIFACT" "the agent tarball contains a hardlink: $_dv_line" ;;
        esac

        _dv_modes=${_dv_line%% *}
        _dv_type=${_dv_modes%"${_dv_modes#?}"}
        case $_dv_type in
            -|d) ;;
            l) die "$EX_ARTIFACT" "the agent tarball contains a symlink: $_dv_line" ;;
            h) die "$EX_ARTIFACT" "the agent tarball contains a hardlink: $_dv_line" ;;
            b|c|p|s) die "$EX_ARTIFACT" "the agent tarball contains a device, fifo or socket: $_dv_line" ;;
            *) die "$EX_ARTIFACT" "the agent tarball contains a member of an unexpected type: $_dv_line" ;;
        esac
        case $_dv_modes in
            *s*|*S*|*t*) die "$EX_ARTIFACT" "the agent tarball contains a setuid/setgid/sticky member: $_dv_line" ;;
        esac
        # The member name is everything after the last field-separating space
        # run that precedes the path; `tar -tvzf`'s layout is
        # <modes> <owner/group> <size> <date> <time> <name>. No arrow can be
        # present here -- it was refused above -- so the whole tail is the name
        # and nothing is stripped off it before it is validated.
        _dv_entry=$(printf '%s\n' "$_dv_line" | tr -s ' ' | cut -d' ' -f6-)
        [ -n "$_dv_entry" ] || die "$EX_ARTIFACT" "could not read a member name out of: $_dv_line"
        case $_dv_entry in
            /*) die "$EX_ARTIFACT" "the agent tarball contains an absolute path: $_dv_entry" ;;
            *..*) die "$EX_ARTIFACT" "the agent tarball contains a parent-directory reference: $_dv_entry" ;;
            "$AGENT_ROOT_DIR"|"$AGENT_ROOT_DIR"/*) ;;
            *) die "$EX_ARTIFACT" "the agent tarball contains a member outside $AGENT_ROOT_DIR/: $_dv_entry" ;;
        esac
    done < "$TMPDIR_SELF/agent.list"

    mkdir -p "$TMPDIR_SELF/unpack"
    tar -xzf "$TMPDIR_SELF/agent.tar.gz" -C "$TMPDIR_SELF/unpack" \
      || die "$EX_ARTIFACT" "could not extract the agent tarball"
    [ -d "$TMPDIR_SELF/unpack/$AGENT_ROOT_DIR" ] || \
        die "$EX_ARTIFACT" "the agent tarball did not contain $AGENT_ROOT_DIR/"
    # Exactly ONE top-level entry, and it is that directory.
    _dv_tops=$(ls -A "$TMPDIR_SELF/unpack" | wc -l | tr -d ' ')
    [ "$_dv_tops" = '1' ] || \
        die "$EX_ARTIFACT" "the agent tarball unpacked to $_dv_tops top-level entries; exactly one ($AGENT_ROOT_DIR) is permitted"
    STAGED_ROOT="$TMPDIR_SELF/unpack/$AGENT_ROOT_DIR"
}

# ---------------------------------------------------------------------------
# STEP 3 -- the service user and the directory tree.
# ---------------------------------------------------------------------------

create_user_and_dirs() {
    if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        groupadd --system "$SERVICE_GROUP" \
          || die "$EX_FAIL" "could not create the '$SERVICE_GROUP' system group"
    fi
    if ! getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --gid "$SERVICE_GROUP" --home-dir "$STATE_DIR" \
                --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER" \
          || die "$EX_FAIL" "could not create the '$SERVICE_USER' system user"
    fi

    install -d -m 0755 -o root -g root "$CONF_DIR"
    install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CRED_DIR"
    install -d -m 0755 -o root -g root "$AGENT_PREFIX"
    install -d -m 0755 -o root -g root "$AGENT_PREFIX/releases"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR/state"
}

# ---------------------------------------------------------------------------
# STEP 4 -- install the release and flip `current`.
# ---------------------------------------------------------------------------

install_release() {
    RELEASE_DIR="$AGENT_PREFIX/releases/$AGENT_VERSION"
    rm -rf "$RELEASE_DIR.incoming"

    # A power loss can arrive after the atomic `current` flip but before the
    # completion marker is recorded. On the next invocation the marker still
    # names the previous release, so this function is entered again with the
    # same target version. Never remove a release while `current` points at it:
    # doing so briefly makes the live symlink dangling and a retry can destroy
    # the only runnable agent. The release was fully validated before the flip;
    # verify its two entry points and converge by reusing it.
    if [ -L "$AGENT_PREFIX/current" ] \
       && [ "$(readlink "$AGENT_PREFIX/current" 2>/dev/null)" = "$RELEASE_DIR" ]; then
        [ -x "$RELEASE_DIR/bin/ingotron-agent" ] || \
            die "$EX_ARTIFACT" "the active agent release at $RELEASE_DIR is incomplete; refusing to remove it during recovery"
        [ -x "$RELEASE_DIR/bin/ingotron-agent-run" ] || \
            die "$EX_ARTIFACT" "the active agent release at $RELEASE_DIR is incomplete; refusing to remove it during recovery"
        ln -sfn "$RELEASE_DIR/bin/ingotron-agent" /usr/local/bin/ingotron-agent
        log "agent release $AGENT_VERSION is already active; completing the interrupted upgrade"
        return 0
    fi

    cp -a "$STAGED_ROOT" "$RELEASE_DIR.incoming" \
      || die "$EX_ARTIFACT" "could not stage the agent release into $RELEASE_DIR.incoming"
    rm -rf "$RELEASE_DIR"
    mv "$RELEASE_DIR.incoming" "$RELEASE_DIR"
    chown -R root:root "$RELEASE_DIR"
    chmod -R go-w "$RELEASE_DIR"

    [ -x "$RELEASE_DIR/bin/ingotron-agent" ] || \
        die "$EX_ARTIFACT" "the agent release has no executable bin/ingotron-agent"
    [ -x "$RELEASE_DIR/bin/ingotron-agent-run" ] || \
        die "$EX_ARTIFACT" "the agent release has no executable bin/ingotron-agent-run"

    ln -sfn "$RELEASE_DIR" "$AGENT_PREFIX/current.new"
    mv -T "$AGENT_PREFIX/current.new" "$AGENT_PREFIX/current" 2>/dev/null \
      || { rm -f "$AGENT_PREFIX/current.new"; ln -sfn "$RELEASE_DIR" "$AGENT_PREFIX/current"; }
    ln -sfn "$AGENT_PREFIX/current/bin/ingotron-agent" /usr/local/bin/ingotron-agent
}

# ---------------------------------------------------------------------------
# Fetch the CA, verify the pin, install the CA.  The fetch goes through
# fetch_public and carries no credential; the pin check below is what makes
# the CA trusted.
# ---------------------------------------------------------------------------

fetch_ca() {
    if [ -n "$CA_FILE" ]; then
        # The offline variant (--ca-file): the pin from the code is still
        # verified against this file; only the fetch is skipped.
        cp "$CA_FILE" "$TMPDIR_SELF/ca.pem" || die "$EX_FAIL" "could not read $CA_FILE"
        chmod 0600 "$TMPDIR_SELF/ca.pem"
        log "using the CA supplied by --ca-file (the pin is still verified)"
        return 0
    fi
    fetch_public "$VAULT/ca.crt" "$TMPDIR_SELF/ca.pem" "$CA_MAX_BYTES" 15 \
      || die "$EX_FAIL" "could not fetch $VAULT/ca.crt. Check that the vault is reachable and that --vault names its HTTPS origin."
    chmod 0600 "$TMPDIR_SELF/ca.pem"
}

validate_ca_pem() {
    _vc_size=$(file_size "$TMPDIR_SELF/ca.pem")
    [ "$_vc_size" -le "$CA_MAX_BYTES" ] || \
        die "$EX_FAIL" "the CA response is $_vc_size bytes; more than $CA_MAX_BYTES is not a CA certificate"
    _vc_begins=$(grep -c '^-----BEGIN CERTIFICATE-----$' "$TMPDIR_SELF/ca.pem" || true)
    [ "$_vc_begins" = '1' ] || \
        die "$EX_FAIL" "the CA response must contain exactly one PEM CERTIFICATE block (found $_vc_begins)"
    if grep -q 'PRIVATE KEY' "$TMPDIR_SELF/ca.pem"; then
        die "$EX_FAIL" "the CA response contains a private key; refusing"
    fi
    openssl x509 -in "$TMPDIR_SELF/ca.pem" -noout >/dev/null 2>&1 \
      || die "$EX_FAIL" "the CA response does not parse as an X.509 certificate"
    openssl x509 -in "$TMPDIR_SELF/ca.pem" -noout -text 2>/dev/null | grep -q 'CA:TRUE' \
      || die "$EX_FAIL" "the certificate served at $VAULT/ca.crt is not a CA (basicConstraints CA:TRUE absent)"
}

verify_pin() {
    SPKI_HEX=$(openssl x509 -in "$TMPDIR_SELF/ca.pem" -noout -pubkey \
               | openssl pkey -pubin -outform DER \
               | openssl dgst -sha256 -r | cut -d' ' -f1)
    is_hex64 "$SPKI_HEX" || die "$EX_FAIL" "could not compute the CA SPKI digest"

    observed=$(b32_prefix "$SPKI_HEX" "$CA_PIN_CHARS") || die "$EX_FAIL" "could not compute the CA pin"

    if [ "$observed" != "$CODE_PIN_SEGMENT" ]; then
        printf 'expected: %s\nobserved: %s\n' "$CODE_PIN_SEGMENT" "$observed" >&2
        printf 'full observed CA SPKI sha256: %s\n' "$SPKI_HEX" >&2
        cleanup
        die "$EX_PIN" "The vault you reached is not the one that issued this code. Nothing has been enrolled and the enrollment code has NOT been transmitted -- it is still valid."
    fi
}

install_ca() {
    install -m 0644 -o root -g root "$TMPDIR_SELF/ca.pem" "$CONF_DIR/ca.crt"
    printf '%s\n' "$SPKI_HEX" > "$TMPDIR_SELF/ca.spki.sha256"
    install -m 0644 -o root -g root "$TMPDIR_SELF/ca.spki.sha256" "$CONF_DIR/ca.spki.sha256"
}

# Re-enrollment CA replacement is transactional from the bootstrap's point of
# view. The installed agent always writes vault.json with the stable
# /etc/ingotron/ca.crt path, so the new public CA can be staged there before the
# TLS exchange. If the exchange fails before the agent records its recovery
# marker, restore the old trust anchor. Once that marker exists, the one-use
# code was accepted and the new CA must remain so the same command can resume.
reenroll_backup_ca() {
    REENROLL_OLD_CA="$TMPDIR_SELF/old-ca.crt"
    REENROLL_OLD_SPKI="$TMPDIR_SELF/old-ca.spki.sha256"
    cp "$CONF_DIR/ca.crt" "$REENROLL_OLD_CA" || \
        die "$EX_FAIL" "could not snapshot the installed vault CA"
    if [ -f "$CONF_DIR/ca.spki.sha256" ]; then
        cp "$CONF_DIR/ca.spki.sha256" "$REENROLL_OLD_SPKI" || \
            die "$EX_FAIL" "could not snapshot the installed vault CA pin"
    else
        REENROLL_OLD_SPKI=''
    fi
    # Snapshot a credential digest too. The agent writes its private recovery
    # record before replacing the token, and this digest lets the bootstrap
    # detect an unexpected token change if that invariant is ever violated.
    # The token itself never enters logs or command arguments.
    REENROLL_OLD_TOKEN_SHA=$(sha256_file "$TOKEN_FILE")
}

reenroll_restore_old_ca() {
    [ -f "$REENROLL_OLD_CA" ] || return 0
    install -m 0644 -o root -g root "$REENROLL_OLD_CA" "$CONF_DIR/ca.crt" || \
        warn "could not restore the previous vault CA; keep this terminal open and re-run --reenroll"
    if [ -n "$REENROLL_OLD_SPKI" ] && [ -f "$REENROLL_OLD_SPKI" ]; then
        install -m 0644 -o root -g root "$REENROLL_OLD_SPKI" "$CONF_DIR/ca.spki.sha256" || \
            warn "could not restore the previous vault CA pin; keep this terminal open and re-run --reenroll"
    else
        rm -f "$CONF_DIR/ca.spki.sha256"
    fi
}

run_reenrollment_bootstrap() {
    _rb_agent="$AGENT_PREFIX/current/bin/ingotron-agent"

    # A pending marker means the exchange already succeeded and a previous
    # attempt failed after spending the code. The installed agent ignores the
    # supplied code on this branch and reconstructs vault.json locally.
    if [ -f "$REENROLL_PENDING" ]; then
        if [ "$ROOT_OVERRIDE" = '1' ]; then
            "$_rb_agent" reenroll --vault "$VAULT" --ca "$CONF_DIR/ca.crt" \
                --config "$CONF_DIR/vault.json" --root "$RECORDINGS_ROOT" || \
                die "$EX_ENROLL" "could not resume re-enrollment locally. The replacement credential is still stored; fix the reported cause and re-run this same command. No new code is needed."
        else
            "$_rb_agent" reenroll --vault "$VAULT" --ca "$CONF_DIR/ca.crt" \
                --config "$CONF_DIR/vault.json" || \
                die "$EX_ENROLL" "could not resume re-enrollment locally. The replacement credential is still stored; fix the reported cause and re-run this same command. No new code is needed."
        fi
        return 0
    fi

    if [ "$ROOT_OVERRIDE" = '1' ]; then
        if printf '%s' "$CODE" | "$_rb_agent" reenroll \
                --vault "$VAULT" --ingest "$INGEST" --code-stdin --ca "$CONF_DIR/ca.crt" \
                --config "$CONF_DIR/vault.json" --root "$RECORDINGS_ROOT"; then
            return 0
        fi
    elif printf '%s' "$CODE" | "$_rb_agent" reenroll \
            --vault "$VAULT" --ingest "$INGEST" --code-stdin --ca "$CONF_DIR/ca.crt" \
            --config "$CONF_DIR/vault.json"; then
        return 0
    fi
    if [ -f "$REENROLL_PENDING" ]; then
        die "$EX_ENROLL" "the re-enrollment exchange succeeded but local configuration needs recovery. Re-run this same command; it will resume without spending another code."
    fi
    if [ ! -f "$TOKEN_FILE" ] || [ "$(sha256_file "$TOKEN_FILE")" != "$REENROLL_OLD_TOKEN_SHA" ]; then
        # New agents durably write the root-only recovery record before changing
        # the token. If it is absent here, do not manufacture an unvalidated
        # marker: it would make the next run discard its one-use code without
        # enough data to recover safely.
        die "$EX_ENROLL" "the credential changed but its durable recovery record is missing. The new CA was retained. Do not reuse this code; mint a fresh re-enrollment code and run it after checking local storage."
    fi
    reenroll_restore_old_ca
    die "$EX_ENROLL" "re-enrollment did not leave a durable recovery record. The previous CA and local credential were restored, but the one-use code may have been consumed. Mint a fresh re-enrollment code on the Robots page before retrying."
}

# ---------------------------------------------------------------------------
# STEP 6/7/8 -- the enrollment exchange.
#
# The exchange is gRPC over a channel built with ssl_channel_credentials
# against the CA just installed, so it is done by the agent that was installed
# in step 4 -- not by curl, and with no verification-disabling flag of any
# kind anywhere on the path.  The code is
# handed over on stdin rather than on the child's argv so that it does not
# appear a second time in /proc.
# ---------------------------------------------------------------------------

run_enrollment() {
    ENROLL_RESULT="$TMPDIR_SELF/enroll.result"

    # Resuming an interrupted install: the exchange already succeeded and the
    # code is spent, so this rebuilds vault.json and the result file from the
    # facts in the private recovery record (or legacy public state). No code is
    # sent and none is needed
    # -- which is the promise step 7 makes and a regression test asserts.
    if [ "$RESUMING" = '1' ]; then
        if "$AGENT_PREFIX/current/bin/ingotron-agent" enroll \
                --vault "$VAULT" \
                --resume \
                --ca "$CONF_DIR/ca.crt" \
                --config "$CONF_DIR/vault.json" \
                --token-file "$TOKEN_FILE" \
                --root "$RECORDINGS_ROOT" \
                --result-file "$ENROLL_RESULT"; then
            log "resumed the existing enrollment; no new code was required"
        else
            die "$EX_ENROLL" "could not resume the existing enrollment. The credential at $TOKEN_FILE is still valid; mint a re-enrollment code on the Robots page and run the command shown there."
        fi
        read_enrollment_result
        return 0
    fi

    if printf '%s' "$CODE" | "$AGENT_PREFIX/current/bin/ingotron-agent" enroll \
            --vault "$VAULT" \
            --ingest "$INGEST" \
            --code-stdin \
            --ca "$CONF_DIR/ca.crt" \
            --config "$CONF_DIR/vault.json" \
            --token-file "$TOKEN_FILE" \
            --root "$RECORDINGS_ROOT" \
            --agent-version "$AGENT_VERSION" \
            --result-file "$ENROLL_RESULT"; then
        :
    else
        if [ -f "$ENROLLMENT_PENDING" ]; then
            die "$EX_ENROLL" "the enrollment exchange succeeded and local recovery was recorded. Re-run this same command; it will resume without spending another code."
        fi
        die "$EX_ENROLL" "enrollment did not leave a durable recovery record, so the exchange outcome is unknown. Do not reuse the supplied one-use code; mint a fresh enrollment code on the Robots page before retrying."
    fi

    read_enrollment_result
}

read_enrollment_result() {
    [ -f "$ENROLL_RESULT" ] || die "$EX_ENROLL" "the agent reported success but wrote no enrollment result"
    ROBOT_ID=$(result_field "$ENROLL_RESULT" robot_id)
    INGEST_ENDPOINT=$(result_field "$ENROLL_RESULT" ingest_endpoint)
    SELF_ROTATION=$(result_field "$ENROLL_RESULT" self_rotation_enabled)
    [ -n "$ROBOT_ID" ] || die "$EX_ENROLL" "the enrollment result carries no robot_id"
    case $SELF_ROTATION in
        true|false) ;;
        *) die "$EX_ENROLL" "the enrollment result carries no self_rotation_enabled flag" ;;
    esac
}

# The result file is a flat KEY=VALUE file written by the agent for exactly
# this consumer, so that this script never has to parse JSON.
result_field() {
    sed -n "s/^$2=\(.*\)\$/\1/p" "$1" | head -n 1
}

# ---------------------------------------------------------------------------
# STEP 7 -- the ingotron user must be able to WRITE the recordings
# tree: rsm writes a <file>.manifest.json sidecar and a .lock next to every
# recording.  Checked AFTER the credential is safely stored, so a re-run does
# not need a new code.  This never silently widens permissions on customer data.
# ---------------------------------------------------------------------------

check_recordings_writable() {
    if [ ! -d "$RECORDINGS_ROOT" ]; then
        warn "the recordings tree $RECORDINGS_ROOT does not exist yet; creating it owned by $SERVICE_USER"
        install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$RECORDINGS_ROOT" \
          || die "$EX_PERMS" "could not create $RECORDINGS_ROOT"
        return 0
    fi
    _cw_probe="$RECORDINGS_ROOT/.ingotron-write-probe.$$"
    if su -s /bin/sh -c "touch '$_cw_probe' && rm -f '$_cw_probe'" "$SERVICE_USER" >/dev/null 2>&1; then
        return 0
    fi
    printf '\n' >&2
    printf 'The credential is stored at %s -- re-running this command after fixing\n' "$TOKEN_FILE" >&2
    printf 'permissions does NOT need a new enrollment code.\n\n' >&2
    printf 'Grant the %s user write access to the recordings tree with EITHER:\n\n' "$SERVICE_USER" >&2
    printf '  setfacl -R -m u:%s:rwX -m d:u:%s:rwX %s\n\n' "$SERVICE_USER" "$SERVICE_USER" "$RECORDINGS_ROOT" >&2
    printf 'or, if the filesystem has no ACL support:\n\n' >&2
    printf '  chgrp -R %s %s && chmod -R g+rwX %s\n\n' "$SERVICE_GROUP" "$RECORDINGS_ROOT" "$RECORDINGS_ROOT" >&2
    printf 'then re-run this command.\n' >&2
    die "$EX_PERMS" "the '$SERVICE_USER' user cannot write $RECORDINGS_ROOT (rsm writes a manifest sidecar next to every recording)"
}

# ---------------------------------------------------------------------------
# STEP 8 -- units, drop-in, enable, one run, status.
# ---------------------------------------------------------------------------

install_units() {
    _iu_src="$AGENT_PREFIX/current/systemd"
    [ -d "$_iu_src" ] || die "$EX_FAIL" "the agent release ships no systemd units"

    for _iu_unit in ingotron-agent.service ingotron-agent.timer \
                    ingotron-agent-retention.service ingotron-agent-retention.timer; do
        [ -f "$_iu_src/$_iu_unit" ] || die "$EX_FAIL" "the agent release is missing $_iu_unit"
        install -m 0644 -o root -g root "$_iu_src/$_iu_unit" "$UNIT_DIR/$_iu_unit"
    done

    # When the vault has INGEST_ROBOT_SELF_ROTATION=disabled
    # the enrollment response carries self_rotation_enabled=false and the agent
    # installs NO rotate unit, so a disabled fleet does not accumulate robots
    # retrying a refused RPC every week.
    if [ "$SELF_ROTATION" = 'true' ]; then
        for _iu_unit in ingotron-agent-rotate.service ingotron-agent-rotate.timer; do
            [ -f "$_iu_src/$_iu_unit" ] || die "$EX_FAIL" "the agent release is missing $_iu_unit"
            install -m 0644 -o root -g root "$_iu_src/$_iu_unit" "$UNIT_DIR/$_iu_unit"
        done
    else
        rm -f "$UNIT_DIR/ingotron-agent-rotate.service" "$UNIT_DIR/ingotron-agent-rotate.timer"
        log "self-rotation is disabled by the vault; no rotate timer installed"
    fi

    write_site_dropin

    systemctl daemon-reload || die "$EX_FAIL" "systemctl daemon-reload failed"
    systemctl enable --now ingotron-agent.timer || die "$EX_FAIL" "could not enable ingotron-agent.timer"
    systemctl enable --now ingotron-agent-retention.timer || warn "could not enable ingotron-agent-retention.timer"
    if [ "$SELF_ROTATION" = 'true' ]; then
        systemctl enable --now ingotron-agent-rotate.timer || warn "could not enable ingotron-agent-rotate.timer"
    fi
}

# An upgrade keeps enrollment.json, vault.json, credentials and the existing
# site drop-ins unchanged. Unit ExecStart paths always point through `current`,
# but refresh the shipped unit files so a new release can carry a unit fix. A
# rotate timer is refreshed only when the old install already had one. The
# enrollment response is the authority for creating or removing that timer.
install_upgrade_units() {
    _iu_src="$AGENT_PREFIX/current/systemd"
    [ -d "$_iu_src" ] || die "$EX_FAIL" "the agent release ships no systemd units"

    for _iu_unit in ingotron-agent.service ingotron-agent.timer \
                    ingotron-agent-retention.service ingotron-agent-retention.timer; do
        [ -f "$_iu_src/$_iu_unit" ] || die "$EX_FAIL" "the agent release is missing $_iu_unit"
        install -m 0644 -o root -g root "$_iu_src/$_iu_unit" "$UNIT_DIR/$_iu_unit"
    done

    if [ -f "$UNIT_DIR/ingotron-agent-rotate.timer" ]; then
        for _iu_unit in ingotron-agent-rotate.service ingotron-agent-rotate.timer; do
            [ -f "$_iu_src/$_iu_unit" ] || die "$EX_FAIL" "the agent release is missing $_iu_unit"
            install -m 0644 -o root -g root "$_iu_src/$_iu_unit" "$UNIT_DIR/$_iu_unit"
        done
    fi

    systemctl daemon-reload || die "$EX_FAIL" "systemctl daemon-reload failed"
}

# The shipped unit stays pristine and upgradeable; everything site-specific
# lives in the drop-in.
write_site_dropin() {
    {
        printf '# Written by robot.sh at enrollment. Site-specific; the shipped unit is not edited.\n'
        printf '[Service]\n'
        printf 'ReadWritePaths=%s\n' "$RECORDINGS_ROOT"
    } > "$TMPDIR_SELF/10-site.conf"

    # ProtectHome=yes in the shipped unit would make a recordings tree under
    # /home or /root invisible to the service. Relaxing it is announced on
    # stdout rather than done quietly.
    case $RECORDINGS_ROOT in
        /home/*|/root/*|/home|/root)
            printf 'ProtectHome=no\n' >> "$TMPDIR_SELF/10-site.conf"
            log "NOTE: the recordings tree is under $RECORDINGS_ROOT, so the upload and retention units use ProtectHome=no on this host."
            ;;
    esac

    for _wd_unit in ingotron-agent.service ingotron-agent-retention.service; do
        _wd_dir="$UNIT_DIR/$_wd_unit.d"
        install -d -m 0755 -o root -g root "$_wd_dir"
        install -m 0644 -o root -g root "$TMPDIR_SELF/10-site.conf" "$_wd_dir/10-site.conf"
    done
}

first_run_and_status() {
    # --no-block deliberately. `systemctl start` on a Type=oneshot BLOCKS until
    # the unit finishes, and this unit's bound is three hours: a robot docked
    # with a week of recordings would leave the operator's `curl | sudo sh`
    # apparently hung, with no output, for as long as the backlog takes. The
    # run is triggered either way, and `status` below prints the queue depth
    # that tells the operator it is working.
    systemctl start --no-block ingotron-agent.service || \
        warn "the first upload run did not start cleanly; check: journalctl -u ingotron-agent.service"
    "$AGENT_PREFIX/current/bin/ingotron-agent" status || true
}

# 0 if semver $1 is lower than semver $2. Both were validated by is_semver.
version_lt() {
    _vl_a=$1; _vl_b=$2
    for _vl_i in 1 2 3; do
        _vl_x=${_vl_a%%.*}; _vl_y=${_vl_b%%.*}
        [ "$_vl_x" -lt "$_vl_y" ] && return 0
        [ "$_vl_x" -gt "$_vl_y" ] && return 1
        _vl_a=${_vl_a#*.}; _vl_b=${_vl_b#*.}
    done
    return 1
}

# Decided under the lock from the marker and the rendered version only, before
# any network call. The same version exits 0: reinstalling it would mean
# removing the directory `current` points at. A lower version is refused: an
# old vault script must not roll a robot back.
decide_upgrade() {
    _du_installed=$(install_marker_version) || die "$EX_PREFLIGHT" \
        "the completion marker at $INSTALL_MARKER became invalid; refusing to continue the upgrade"
    if [ "$_du_installed" = "$AGENT_VERSION" ]; then
        log "agent $AGENT_VERSION is already installed; nothing to do. The CA, configuration and credential were retained."
        exit 0
    fi
    if version_lt "$AGENT_VERSION" "$_du_installed"; then
        die "$EX_PREFLIGHT" \
            "this vault serves agent $AGENT_VERSION, which is lower than the installed $_du_installed. Refusing to downgrade; nothing was changed."
    fi
    log "upgrading agent $_du_installed -> $AGENT_VERSION"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

main() {
    collect_args "$@"

    # STEP 0 -- nothing above this line touches the network or the filesystem.
    preflight

    # STEP 1
    validate_args

    # Serialize every state-changing mode before opening a work directory or
    # making a network request.  The second fresh-install check is the
    # linearization point for two invocations that both passed preflight.
    acquire_bootstrap_lock
    recheck_fresh_install_under_lock

    TMPDIR_SELF=$(mktemp -d "${TMPDIR:-/tmp}/robot-bootstrap.XXXXXX")
    trap 'cleanup' EXIT
    trap 'cleanup; exit 130' INT
    trap 'cleanup; exit 129' HUP
    trap 'cleanup; exit 143' TERM
    chmod 0700 "$TMPDIR_SELF"

    # RE-ENROLL. Uses the installed agent; never downloads a release, changes
    # `current`, or rewrites systemd units.
    if [ "$REENROLL_MODE" = '1' ]; then
        fetch_ca
        validate_ca_pem
        verify_pin
        reenroll_backup_ca
        install_ca
        log "CA pin verified: $CODE_PIN_SEGMENT"
        run_reenrollment_bootstrap
        log "re-enrollment completed; the installed release, recordings and queue were retained"
        exit 0
    fi

    # UPGRADE. The vault serves exactly one agent version, rendered above. No
    # code, no CA change, no enrollment.
    if [ "$UPGRADE_MODE" = '1' ]; then
        decide_upgrade
        download_and_verify_agent
        install_release
        install_upgrade_units
        record_install_complete
        log "upgraded agent to $AGENT_VERSION; the CA, configuration and credential were retained"
        exit 0
    fi

    # INSTALL. The pin is verified first, so a wrong vault costs no download.
    fetch_ca
    validate_ca_pem
    verify_pin
    log "CA pin verified: $CODE_PIN_SEGMENT"
    download_and_verify_agent
    create_user_and_dirs
    install_release
    install_ca

    # The first and only credentialed exchange, over TLS pinned to that CA.
    # The agent that can resume a half-finished enrollment is installed first.
    run_enrollment
    log "enrolled as $ROBOT_ID against $INGEST_ENDPOINT"

    check_recordings_writable
    install_units
    # The marker goes down only once the units are installed and enabled --
    # everything before this point is resumable, and refuse_existing_install
    # reads this file to tell "enrolled" from "interrupted".
    record_install_complete
    first_run_and_status

    log "done. Manage this agent with: ingotron-agent status | logs | uninstall"
}

main "$@"
