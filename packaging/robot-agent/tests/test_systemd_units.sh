#!/bin/sh
# The systemd surface of the agent units.
#
# Two layers, because `systemd-analyze verify` is not present everywhere and
# the structural properties are the ones that carry the security content:
#   * lint with systemd-analyze when it exists (skipped loudly otherwise);
#   * structural assertions that always run.
#
# The most important assertion here is the negative one: NO unit may carry the
# credential in `Environment=`, because that is readable through
# `systemctl show` and /proc/<pid>/environ.
set -u

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
. "$TEST_ROOT/lib/harness.sh"
SUITE_NAME='systemd units'

UNITS_DIR="$PKG_ROOT/agent/systemd"

SERVICE="$UNITS_DIR/ingotron-agent.service"
TIMER="$UNITS_DIR/ingotron-agent.timer"
RETENTION_SERVICE="$UNITS_DIR/ingotron-agent-retention.service"

for unit in ingotron-agent.service ingotron-agent.timer \
            ingotron-agent-rotate.service ingotron-agent-rotate.timer \
            ingotron-agent-retention.service ingotron-agent-retention.timer; do
    assert_file_present "$unit is shipped" "$UNITS_DIR/$unit"
done

# --- no credential may reach the environment -------------------------------

case_start 'no unit sets Environment= at all'
if grep -ln '^Environment=' "$UNITS_DIR"/* 2>/dev/null | grep -q .; then
    fail "a unit sets Environment=:
$(grep -n '^Environment=' "$UNITS_DIR"/*)"
else
    ok
fi

case_start 'no unit contains a token-shaped string'
if grep -nE '(ignr1\.|INGEST_AUTH_TOKEN|token=)' "$UNITS_DIR"/* 2>/dev/null | grep -q .; then
    fail "a unit references a credential:
$(grep -nE '(ignr1\.|INGEST_AUTH_TOKEN|token=)' "$UNITS_DIR"/*)"
else
    ok
fi

case_start 'the service reads its environment from a file, optionally'
if grep -q '^EnvironmentFile=-/etc/ingotron/agent.env$' "$SERVICE"; then ok; else
    fail 'EnvironmentFile=-/etc/ingotron/agent.env is missing (the leading - makes it optional)'
fi

# --- the required directives ----------------------------------------------

expect_line() {
    case_start "$1 contains '$2'"
    if grep -qxF "$2" "$1"; then ok; else
        fail "not found in $(basename "$1")"
    fi
}

expect_line "$SERVICE" 'Type=oneshot'
expect_line "$SERVICE" 'User=ingotron'
expect_line "$SERVICE" 'Group=ingotron'
expect_line "$SERVICE" 'ExecStart=/opt/ingotron-agent/current/bin/ingotron-agent-run'
expect_line "$SERVICE" 'ConditionPathExists=/etc/ingotron/credentials/robot.token'
expect_line "$SERVICE" 'WorkingDirectory=/var/lib/ingotron'
expect_line "$SERVICE" 'UMask=0077'
expect_line "$SERVICE" 'NoNewPrivileges=yes'
expect_line "$SERVICE" 'ProtectSystem=strict'
expect_line "$SERVICE" 'ProtectHome=yes'
expect_line "$SERVICE" 'ReadOnlyPaths=/etc/ingotron'
expect_line "$SERVICE" 'ReadWritePaths=/var/lib/ingotron'
expect_line "$SERVICE" 'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX'
expect_line "$SERVICE" 'MemoryDenyWriteExecute=yes'
expect_line "$SERVICE" 'SystemCallFilter=@system-service'
expect_line "$SERVICE" 'SystemCallFilter=~@privileged @resources'
expect_line "$SERVICE" 'CapabilityBoundingSet='
expect_line "$RETENTION_SERVICE" 'Type=oneshot'
expect_line "$RETENTION_SERVICE" 'TimeoutStartSec=3h'
expect_line "$RETENTION_SERVICE" 'ProtectSystem=strict'
expect_line "$RETENTION_SERVICE" 'ProtectHome=yes'
expect_line "$RETENTION_SERVICE" 'ReadWritePaths=/var/lib/ingotron'

expect_line "$TIMER" 'OnBootSec=2min'
expect_line "$TIMER" 'OnUnitInactiveSec=5min'
expect_line "$TIMER" 'AccuracySec=30s'
expect_line "$TIMER" 'Persistent=true'
expect_line "$TIMER" 'Unit=ingotron-agent.service'
expect_line "$TIMER" 'WantedBy=timers.target'

# The shipped unit stays pristine; ProtectHome is relaxed only in the
# installer-written drop-in, and only when the recordings tree needs it.
case_start 'the shipped upload and retention services do not relax ProtectHome'
if grep -q '^ProtectHome=no' "$SERVICE" "$RETENTION_SERVICE"; then
    fail 'a shipped unit sets ProtectHome=no; that belongs in the site drop-in only'
else
    ok
fi

# --- the two systemd pitfalls ----------------------------------------------
#
# Both were measured on systemd 249 and differ from the obvious way to write
# the unit.

case_start 'no directive carries a trailing inline comment'
# systemd does not support them: it parses "60s     # ..." as one malformed
# time-span token and DROPS THE WHOLE ASSIGNMENT, silently disabling the
# setting. RandomizedDelaySec is the easy one to write that way.
if grep -nE '^[A-Za-z][A-Za-z0-9]*=[^#]*[^ #] +#' "$UNITS_DIR"/* 2>/dev/null | grep -q .; then
    fail "a directive carries a trailing inline comment, which systemd silently drops:
$(grep -nE '^[A-Za-z][A-Za-z0-9]*=[^#]*[^ #] +#' "$UNITS_DIR"/*)"
else
    ok
fi

expect_line "$TIMER" 'RandomizedDelaySec=60s'

case_start 'the upload and retention oneshots have bounds that systemd enforces'
if grep -q '^TimeoutStartSec=3h$' "$SERVICE" \
   && grep -q '^TimeoutStartSec=3h$' "$RETENTION_SERVICE"; then
    ok
else
    fail 'an upload or retention service lacks TimeoutStartSec=3h, which is the directive that bounds a oneshot'
fi

case_start 'RuntimeMaxSec must not appear in any shipped unit'
# RuntimeMaxSec has no effect with Type=oneshot, and an inert directive is
# worse than an absent one because it reads as a guarantee. Keeping it beside
# a whitelisted warning would maintain the illusion.
if grep -l '^RuntimeMaxSec=' "$UNITS_DIR"/* 2>/dev/null | grep -q .; then
    fail "an inert RuntimeMaxSec survives:
$(grep -n '^RuntimeMaxSec=' "$UNITS_DIR"/*)"
else
    ok
fi

# --- the rotate unit may write credentials, the others may not -------------

ROTATE="$UNITS_DIR/ingotron-agent-rotate.service"
case_start 'only the rotate unit may write the credentials directory'
rotate_ok=0
grep -q 'ReadWritePaths=.*\/etc\/ingotron\/credentials' "$ROTATE" && rotate_ok=1
others=$(grep -l 'ReadWritePaths=.*\/etc\/ingotron\/credentials' \
            "$SERVICE" "$UNITS_DIR/ingotron-agent-retention.service" 2>/dev/null || true)
if [ "$rotate_ok" = '1' ] && [ -z "$others" ]; then
    ok
else
    fail "rotate_writes=$rotate_ok other_units_that_write=[$others]"
fi

# --- lint ------------------------------------------------------------------

if command -v systemd-analyze >/dev/null 2>&1; then
    LINT_DIR=$(mktemp -d)
    cp "$UNITS_DIR"/* "$LINT_DIR/"
    for unit in "$LINT_DIR"/*.service "$LINT_DIR"/*.timer; do
        base=$(basename "$unit")
        case_start "systemd-analyze verify $base"
        out=$(systemd-analyze verify "$unit" 2>&1 || true)
        # `systemd-analyze verify` pulls in the whole host unit graph, so a
        # developer box with any broken unit of its own (ordering cycles,
        # unreadable units under /run, unknown keys in third-party units)
        # drowns our three lines in noise. Keep only the lines that name the
        # unit under test -- anything else is the host's problem, not this
        # unit's, and treating it as ours makes the gate untrustworthy in
        # exactly the way it exists to prevent.
        mine=$(printf '%s\n' "$out" | grep -F "$base" || true)
        # EXACTLY ONE complaint is tolerated: the ExecStart binaries live under
        # /opt on a real robot and are absent on a build host. The gate
        # is "systemd-analyze verify runs clean on every shipped unit with NO
        # whitelisted complaint naming that unit" -- so this list must not grow.
        # A directive that only passes because its warning is filtered here is
        # an inert directive, which is why RuntimeMaxSec must not appear.
        filtered=$(printf '%s\n' "$mine" \
                   | grep -v 'is not executable: No such file or directory' \
                   | grep -v '^$' || true)
        if [ -z "$filtered" ]; then
            ok
        else
            fail "unexpected lint output for $base:
$filtered"
        fi
    done
    rm -rf "$LINT_DIR"
else
    skip 'systemd-analyze verify' 'systemd-analyze is not installed on this host; the structural assertions above still ran'
fi

summary
