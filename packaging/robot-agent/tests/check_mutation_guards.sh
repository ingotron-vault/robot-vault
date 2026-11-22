#!/bin/sh
# MUTATION GUARDS -- a passing test proves nothing until it has been seen
# failing.
#
# Each case below removes or inverts ONE guard in a COPY of robot.sh, re-runs
# the suite that is supposed to cover it, and requires that suite to FAIL. If a
# suite still passes with its guard deleted, the suite is decoration and this
# script says so.
#
# Named check_* so run-all.sh does not recurse into it.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='mutation guards'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

RUNNER=${SHELL_UNDER_TEST:-/bin/sh}

# mutate <name> <suite> <sed-expression...>
# Applies the sed program to a copy of robot.sh, points the suite at the copy,
# and requires the suite to FAIL.
mutate() {
    _mu_name=$1; _mu_suite=$2; shift 2
    _mu_copy="$WORK/robot.mutated.sh"
    cp "$ROBOT_SH" "$_mu_copy"
    for _mu_expr in "$@"; do
        sed -i "$_mu_expr" "$_mu_copy"
    done

    case_start "$_mu_name"
    if cmp -s "$ROBOT_SH" "$_mu_copy"; then
        fail 'the mutation changed nothing -- the sed expression no longer matches, so this guard is UNTESTED'
        return
    fi
    _mu_out=$(ROBOT_SH_OVERRIDE="$_mu_copy" SHELL_UNDER_TEST="$RUNNER" \
              "$RUNNER" "$TEST_ROOT/$_mu_suite" 2>&1)
    if [ $? -ne 0 ]; then
        ok
    else
        fail "$_mu_suite still PASSED with the guard removed:
$(printf '%s\n' "$_mu_out" | tail -n 3)"
    fi
}

# --- the pin encoder -------------------------------------------------------

# Revert to the wrong "encoding of the first 16 bytes" definition by
# truncating the input digest.
mutate 'truncating the digest to 16 bytes is caught' \
    test_b32_prefix.sh \
    's/^    _hex=\$1; _want=\$2/    _hex=$(printf "%s" "$1" | cut -c1-32); _want=$2/'

# Emit a partial answer instead of refusing on short input: the failure mode
# that would compare unequal and look like a MITM.
mutate 'a partial answer on short input is caught' \
    test_b32_prefix.sh \
    's/^    \[ \${#_out} -eq "\$_want" \] || return 1$/    :/'

# Use a standard RFC 4648 alphabet instead of Crockford.
mutate 'a non-Crockford alphabet is caught' \
    test_b32_prefix.sh \
    "s|^C32='0123456789ABCDEFGHJKMNPQRSTVWXYZ'|C32='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'|"

# --- the CA pin comparison -------------------------------------------------

# Accept any CA: the single most consequential guard in the script.
mutate 'removing the pin comparison is caught' \
    test_pin_verification.sh \
    's/^    if \[ "\$observed" != "\$CODE_PIN_SEGMENT" \]; then$/    if false; then/'

# Hash the PEM encoding of the public key instead of its DER SubjectPublicKeyInfo.
# This still produces a 26-character pin that looks entirely normal -- it simply
# is not the pin the vault minted, so every enrollment on the fleet fails.
mutate 'hashing a different encoding of the public key is caught' \
    test_pin_verification.sh \
    's@openssl pkey -pubin -outform DER@openssl pkey -pubin -outform PEM@'

# Stop requiring CA:TRUE, so a leaf served at /ca.crt would be installed as a
# trust anchor.
mutate 'dropping the CA:TRUE check is caught' \
    test_pin_verification.sh \
    "s@grep -q 'CA:TRUE'@grep -q ''@"

# Install a --ca-file without checking its pin -- the "USB stick" convenience
# quietly becoming "trust whatever you were handed".
mutate 'skipping the pin check on the --ca-file path is caught' \
    test_pin_verification.sh \
    's@^        log "using the CA supplied by --ca-file (the pin is still verified)"@        log "using the CA supplied by --ca-file"@'

# --- the vault delivery ----------------------------------------------------

# Run an unrendered copy: it would download an agent it cannot check.
mutate 'dropping the rendered-value check in the preflight is caught' \
    test_bootstrap_refusals.sh \
    's/^    check_rendered$/    :/'

# Pin check after the download instead of before: a wrong vault then serves
# the agent before the refusal. test_vault_delivery asserts the request order.
mutate 'downloading the agent before the pin check is caught' \
    test_vault_delivery.sh \
    '/^    # INSTALL. The pin is verified first/,/^    download_and_verify_agent$/{/^    download_and_verify_agent$/d}' \
    's/^    fetch_ca$/    download_and_verify_agent\n    fetch_ca/'

# Refuse nothing on a downgrade: an old vault script rolls a robot back.
mutate 'allowing a downgrade is caught' \
    test_upgrade_mode.sh \
    's/^    if version_lt "\$AGENT_VERSION" "\$_du_installed"; then$/    if false; then/'

# Pass the code to the agent without --ingest: it would enroll at the query host.
mutate 'dropping --ingest from the enrollment is caught' \
    test_reenroll_mode.sh \
    's/ --ingest "\$INGEST"//'

# --- the extraction guard ------------------------------

# Truncate the member name to its first whitespace-delimited word. Nothing
# looks broken -- until a member called "a file with spaces" is read as "a",
# which is outside the root, or a hostile "evil thing" is read as "evil".
mutate 'truncating the member name at the first space is caught' \
    test_tar_member_parse.sh \
    "s@cut -d' ' -f6-)@cut -d' ' -f6)@"

# Accept symlinks, which in a root-owned extraction is a write primitive
# anywhere on the filesystem.
# Symlinks are refused twice over: by the arrow rendered in the listing and by
# the `l` type character. Neutering EITHER alone changes no outcome -- which is
# what defence in depth means and is why a single-guard mutation cannot fire
# here. Neutering BOTH must be caught.
mutate 'accepting symlink members is caught' \
    test_tar_member_parse.sh \
    "s@^            \*' -> '\*)@            *'NEVERMATCHES1'*)@" \
    's/^            l) die /            l) : die /'

# Accept setuid members: a local root in the shipped bundle.
mutate 'accepting setuid members is caught' \
    test_tar_member_parse.sh \
    's/^            \*s\*|\*S\*|\*t\*) die /            *NEVERMATCHES*) die /'

# Stop confining members to the rendered root directory.
mutate 'dropping the root_dir containment check is caught' \
    test_tar_member_parse.sh \
    's/^            \*) die "\$EX_ARTIFACT" "the agent tarball contains a member outside/            *) : "the agent tarball contains a member outside/'

# Check the hash but not the size, so an oversized body is downloaded whole.
mutate 'dropping the size check is caught' \
    test_tar_member_parse.sh \
    's/^    \[ "\$_dv_size" = "\$AGENT_SIZE" \] || die "\$EX_ARTIFACT" \\$/    [ 1 -eq 1 ] || die "$EX_ARTIFACT" \\/'

# Check the size but not the hash.
mutate 'dropping the sha256 check is caught' \
    test_tar_member_parse.sh \
    's/^    \[ "\$_dv_sha" = "\$AGENT_SHA256" \] || die "\$EX_ARTIFACT" \\$/    [ 1 -eq 1 ] || die "$EX_ARTIFACT" \\/'

# Refuse hardlinks by the type character only -- inert under busybox tar, which
# types a hardlink '-' and renders " -> target".
mutate 'removing the arrow-based link refusal is caught' \
    test_tar_member_parse.sh \
    "s@^            \*' -> '\*)@            *'NEVERMATCHES'*)@"

# Strip the arrow BEFORE validating the name, so an absolute or '..' target
# hides in the part that was cut off.
# Without the check: no arrow refusal, and the target stripped off the
# name before it is validated. That combination lets a busybox-typed
# hardlink to an absolute path outside the archive be accepted and materialised.
mutate 'stripping the link target before validation is caught' \
    test_tar_member_parse.sh \
    "s@^            \*' -> '\*)@            *'NEVERMATCHES2'*)@" \
    "s@^            \*' link to '\*)@            *'NEVERMATCHES3'*)@" \
    's@^        _dv_entry=\$(printf .%s..n. "\$_dv_line" | tr -s . . | cut -d. . -f6-)@&\n        _dv_entry=${_dv_entry%% -> *}@'

# --- the resume path -------------------------------------------------------

# Key "already installed" on the token instead of the install marker.
mutate 'keying the install refusal on the token again is caught' \
    test_resume_after_partial_install.sh \
    's@^    if install_marker_present; then$@    if [ -e "$TOKEN_FILE" ]; then@'

# Never write the marker, so a completed install is treated as interrupted and
# a re-run silently resumes over a live robot.
mutate 'never writing the completion marker is caught' \
    test_resume_after_partial_install.sh \
    's@^    record_install_complete$@    :@'

# Accept an extra unterminated tail after the version line. The marker suite
# must reject it instead of treating a torn write as a completed install.
mutate 'accepting an extra completion-marker tail is caught' \
    test_resume_after_partial_install.sh \
    's@^    \[ "\$_im_bytes" = "\$_im_expected_bytes" \] || return 1$@    :@'

# Follow a symlink at the marker path. The marker lives below a service-owned
# state directory, so treating a symlink target as completion evidence would
# let an untrusted path decide whether the root bootstrap may proceed.
mutate 'accepting a completion-marker symlink is caught' \
    test_resume_after_partial_install.sh \
    's@^    if \[ -L "\$INSTALL_MARKER" \]; then$@    if false; then@'

# Remove the durable atomic-publication contract. Runtime content tests cannot
# observe torn-write windows, so this is intentionally guarded by the static
# contract suite.
mutate 'writing the completion marker without a same-directory temp is caught' \
    test_static_contract.sh \
    's@^    _mic_tmp="\$_mic_dir/.install-complete.tmp\.\$\$"$@    _mic_tmp="$INSTALL_MARKER"@'

mutate 'omitting the completion-marker directory flush is caught' \
    test_static_contract.sh \
    's@^    sync_marker_path "\$_mic_dir" \|\| \\$@    :@'

mutate 'dropping exclusive completion-marker temp creation is caught' \
    test_static_contract.sh \
    's@^        set -C$@        :@'

# --- preflight and arguments ----------------------------------------------

# Allow systemd specifiers in --root.
mutate 'allowing a systemd specifier in --root is caught' \
    test_bootstrap_refusals.sh \
    "s@\*'%'\*|@@"

# --- install-root and preflight refusals -----------------------------------

# Drop the system-owned-prefix rejection, so --root /etc gets ReadWritePaths=.
mutate 'allowing --root inside a system-owned prefix is caught' \
    test_bootstrap_refusals.sh \
    's@^    for _va_deny in /etc /usr /bin /sbin /lib /lib64 /boot /proc /sys /dev /run \\$@    for _va_deny in /nonexistent-prefix \\@'

# Move the already-installed refusal back out of the preflight, where it
# protects nothing because the code is spent by the time it fires.
mutate 'moving the install refusal out of the preflight is caught' \
    test_resume_after_partial_install.sh \
    's@^        refuse_existing_install$@        :@'


# Accept any openssl 1.x -- the gate that must precede every download.
mutate 'weakening the OpenSSL floor is caught' \
    test_bootstrap_refusals.sh \
    's/^        1.1.1\*) return 0 ;;$/        1.*) return 0 ;;/'

# Accept a code whose pin segment is the wrong length.
mutate 'dropping the pin-segment length check is caught' \
    test_bootstrap_refusals.sh \
    's/^    \[ \${#CODE_PIN_SEGMENT} -eq "\$CA_PIN_CHARS" \] || die "\$EX_PREFLIGHT" \\$/    [ 1 -eq 1 ] || die "$EX_PREFLIGHT" \\/'

# Accept characters outside the Crockford alphabet in the code, by turning the
# refusal into a no-op (`: die ...` runs the null utility with those arguments).
mutate 'dropping the Crockford charset check is caught' \
    test_bootstrap_refusals.sh \
    '/segment contains a character outside/s/die /: die /'

# --- the static contract ---------------------------------------------------

# Add a second verification-disabling flag (no insecure escape hatch).
mutate 'a second verification-disabling flag is caught' \
    test_static_contract.sh \
    's|^SCRIPT_NAME=.robot.sh.$|SCRIPT_NAME=@robot.sh@\ncurl_insecure_helper() { curl --insecure "$@"; }|'

# Introduce a bashism.
mutate 'a bashism is caught' \
    test_static_contract.sh \
    's/^log() {/log() { local _unused=1;/'

summary
