#!/usr/bin/env bash
# Copy the robot bootstrap and agent out of a built query image as release assets.
#
#   packaging/robot-agent/release-assets.sh <query-image> <outdir>
#
# Writes <outdir>/robot.sh, <outdir>/agent-linux-amd64.tar.gz and
# <outdir>/SHA256SUMS. The files are the same bytes the vaults serve at /robot,
# because they come from the image itself and are not rebuilt. Publishing them
# on the GitHub release is a maintainer step.
set -euo pipefail

[ $# -eq 2 ] || { echo "usage: release-assets.sh <query-image> <outdir>" >&2; exit 2; }
IMAGE=$1
OUTDIR=$2
SRC=/opt/robot-vault/robot

mkdir -p "$OUTDIR"
[ -z "$(ls -A "$OUTDIR")" ] || { echo "ERROR: $OUTDIR is not empty" >&2; exit 1; }

CID=$(docker create "$IMAGE")
trap 'docker rm -f "$CID" >/dev/null' EXIT
for name in robot.sh agent-linux-amd64.tar.gz; do
  docker cp "$CID:$SRC/$name" "$OUTDIR/$name"
done

# The rendered script names the tarball it accepts; refuse a mismatched pair.
want=$(sed -n "s/^AGENT_SHA256='\([0-9a-f]\{64\}\)'$/\1/p" "$OUTDIR/robot.sh")
have=$(sha256sum "$OUTDIR/agent-linux-amd64.tar.gz" | cut -d' ' -f1)
[ -n "$want" ] && [ "$want" = "$have" ] \
  || { echo "ERROR: robot.sh expects agent sha256 '${want:-<none>}', the image carries $have" >&2; exit 1; }

(cd "$OUTDIR" && sha256sum agent-linux-amd64.tar.gz robot.sh > SHA256SUMS)
cat "$OUTDIR/SHA256SUMS"
