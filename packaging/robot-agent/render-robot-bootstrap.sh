#!/bin/sh
# Render the /robot bootstrap that a vault serves.
#
#   sh packaging/robot-agent/render-robot-bootstrap.sh \
#       --tarball agent-linux-amd64.tar.gz --version X.Y.Z --out robot.sh \
#       [--template packaging/robot-agent/robot.sh]
#
# It replaces the four AGENT_* marker lines in robot.sh with the version, the
# SHA-256, the size and the root directory of the tarball the same build
# produced. robot.sh refuses a tarball that does not match them, so these values
# are the agent's whole trust anchor. The tool refuses when a marker line is
# missing or repeated, when the tarball does not have exactly one root
# `ingotron-agent-<version>/`, and when any `@@` marker survives.
#
# POSIX sh, so the packaging tests can run it under dash and busybox ash on a
# host with no bash.
set -eu

TEMPLATE="$(dirname "$0")/robot.sh"
TARBALL=''
VERSION=''
OUT=''

die() {
	printf 'render-robot-bootstrap: %s\n' "$*" >&2
	exit 2
}
usage() { die "usage: $0 --tarball FILE --version X.Y.Z --out FILE [--template FILE]"; }

while [ $# -gt 0 ]; do
	[ $# -ge 2 ] || usage
	case $1 in
	--tarball) TARBALL=$2 ;;
	--version) VERSION=$2 ;;
	--out) OUT=$2 ;;
	--template) TEMPLATE=$2 ;;
	*) usage ;;
	esac
	shift 2
done
[ -n "$TARBALL" ] && [ -n "$VERSION" ] && [ -n "$OUT" ] || usage
[ -r "$TEMPLATE" ] || die "cannot read template $TEMPLATE"
[ -f "$TARBALL" ] || die "no such tarball: $TARBALL"
printf '%s\n' "$VERSION" | grep -Eqx '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)' \
	|| die "version is not strict X.Y.Z: $VERSION"

ROOT_DIR="ingotron-agent-$VERSION"
ROOTS=$(tar -tzf "$TARBALL" | cut -d/ -f1 | LC_ALL=C sort -u) \
	|| die "cannot list $TARBALL as a gzip tar archive"
[ "$ROOTS" = "$ROOT_DIR" ] || die "the tarball's root is '$ROOTS', expected exactly '$ROOT_DIR'"
SHA256=$(sha256sum "$TARBALL" | cut -d' ' -f1)
SIZE=$(wc -c < "$TARBALL" | tr -d ' \t\n')

for marker in AGENT_VERSION AGENT_SHA256 AGENT_SIZE AGENT_ROOT_DIR; do
	[ "$(grep -cx "$marker='@@$marker@@'" "$TEMPLATE")" = 1 ] \
		|| die "$TEMPLATE must have exactly one line $marker='@@$marker@@'"
done

TMPOUT=$(mktemp)
trap 'rm -f "$TMPOUT"' EXIT
# Whole-line replacement; the values are hex, digits and [a-z0-9.-] only.
sed -e "s/^AGENT_VERSION='@@AGENT_VERSION@@'\$/AGENT_VERSION='$VERSION'/" \
	-e "s/^AGENT_SHA256='@@AGENT_SHA256@@'\$/AGENT_SHA256='$SHA256'/" \
	-e "s/^AGENT_SIZE='@@AGENT_SIZE@@'\$/AGENT_SIZE='$SIZE'/" \
	-e "s/^AGENT_ROOT_DIR='@@AGENT_ROOT_DIR@@'\$/AGENT_ROOT_DIR='$ROOT_DIR'/" \
	"$TEMPLATE" > "$TMPOUT"

if grep -n '@@' "$TMPOUT" >&2; then
	die "a marker survived rendering (lines above); refusing to write $OUT"
fi
# The output runs under dash or busybox ash on the robot.
if command -v dash >/dev/null 2>&1; then
	dash -n "$TMPOUT" || die "the rendered script is not valid POSIX sh (dash -n failed)"
fi

mkdir -p "$(dirname "$OUT")"
cp "$TMPOUT" "$OUT"
chmod 0644 "$OUT"
printf 'wrote %s (agent %s, sha256 %s, %s bytes)\n' "$OUT" "$VERSION" "$SHA256" "$SIZE" >&2
