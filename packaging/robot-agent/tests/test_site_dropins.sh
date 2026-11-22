#!/bin/sh
# The configured recordings tree must be writable by both jobs that use it.
# The upload service reads recordings, and retention may delete them when
# retention.auto_apply is true.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='recordings-root systemd drop-ins'

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

run_write_site_dropin() {
    _root=$1
    rm -rf "$WORK/units" "$WORK/tmp"
    mkdir -p "$WORK/units" "$WORK/tmp"
    : >"$WORK/log"

    (
        UNIT_DIR="$WORK/units"
        TMPDIR_SELF="$WORK/tmp"
        RECORDINGS_ROOT=$_root

        log() { printf '%s\n' "$*" >>"$WORK/log"; }

        # Preserve the production call shape while keeping this test unprivileged.
        install() {
            if [ "$1" = '-d' ]; then
                _dest=''
                for _arg in "$@"; do _dest=$_arg; done
                mkdir -p "$_dest"
                chmod 0755 "$_dest"
                return
            fi
            _src=$7
            _dest=$8
            cp "$_src" "$_dest"
            chmod 0644 "$_dest"
        }

        eval "$(extract_function write_site_dropin)"
        write_site_dropin
    )
}

run_write_site_dropin /srv/recordings

UPLOAD_DROPIN="$WORK/units/ingotron-agent.service.d/10-site.conf"
RETENTION_DROPIN="$WORK/units/ingotron-agent-retention.service.d/10-site.conf"

assert_file_present 'the upload service receives a site drop-in' "$UPLOAD_DROPIN"
assert_file_present 'the retention service receives a site drop-in' "$RETENTION_DROPIN"

case_start 'both services receive identical recording-root permissions'
if cmp -s "$UPLOAD_DROPIN" "$RETENTION_DROPIN"; then ok; else
    fail 'the upload and retention drop-ins differ'
fi

case_start 'the ordinary recordings root is writable in both services'
if grep -qxF 'ReadWritePaths=/srv/recordings' "$UPLOAD_DROPIN" \
   && grep -qxF 'ReadWritePaths=/srv/recordings' "$RETENTION_DROPIN"; then
    ok
else
    fail 'ReadWritePaths=/srv/recordings is missing from a drop-in'
fi

case_start 'an ordinary root keeps ProtectHome hardening enabled'
if grep -q '^ProtectHome=no$' "$UPLOAD_DROPIN" "$RETENTION_DROPIN"; then
    fail 'ProtectHome was relaxed for a path outside /home and /root'
else
    ok
fi

run_write_site_dropin /home/robot/recordings

case_start 'a home recordings root relaxes ProtectHome for both services'
if grep -qxF 'ProtectHome=no' "$UPLOAD_DROPIN" \
   && grep -qxF 'ProtectHome=no' "$RETENTION_DROPIN"; then
    ok
else
    fail 'a service cannot see the configured recordings tree under /home'
fi

case_start 'the ProtectHome relaxation is announced'
if grep -q 'upload and retention units use ProtectHome=no' "$WORK/log"; then
    ok
else
    fail 'the installer did not announce the reduced home-directory isolation'
fi

summary
