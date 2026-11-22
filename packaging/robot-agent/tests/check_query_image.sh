#!/usr/bin/env bash
# Smoke the robot files inside a built query image.
#
#   packaging/robot-agent/tests/check_query_image.sh [<query-image>] [<outdir>]
#
# Default image: robot-vault-core-query:latest. It copies the files out with
# release-assets.sh (into <outdir>, default a temp dir) and checks that:
#   * robot.sh is rendered (no @@ marker), is valid POSIX sh, and names the
#     tarball's exact size, SHA-256, version and root directory;
#   * the tarball has one root ingotron-agent-<VERSION>/, no links;
#   * bin/ingotron-agent --version prints VERSION, run inside the query image.
set -euo pipefail

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$TEST_ROOT/../../.." && pwd)
IMAGE=${1:-robot-vault-core-query:latest}
OUT=${2:-}
if [ -z "$OUT" ]; then
  OUT=$(mktemp -d "${TMPDIR:-/tmp}/query-image-robot.XXXXXX")
  trap 'rm -rf -- "$OUT"' EXIT
fi
VERSION=$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")
fail() { echo "check_query_image: FAIL: $*" >&2; exit 1; }

bash "$REPO_ROOT/packaging/robot-agent/release-assets.sh" "$IMAGE" "$OUT"
SCRIPT="$OUT/robot.sh"
TARBALL="$OUT/agent-linux-amd64.tar.gz"

! grep -n '@@' "$SCRIPT" || fail "robot.sh still has a marker"
if command -v dash >/dev/null 2>&1; then dash -n "$SCRIPT" || fail "robot.sh is not valid POSIX sh"; fi
field() { sed -n "s/^$1='\\(.*\\)'\$/\\1/p" "$SCRIPT"; }
[ "$(field AGENT_VERSION)" = "$VERSION" ] || fail "AGENT_VERSION=$(field AGENT_VERSION), VERSION=$VERSION"
[ "$(field AGENT_ROOT_DIR)" = "ingotron-agent-$VERSION" ] || fail "AGENT_ROOT_DIR=$(field AGENT_ROOT_DIR)"
[ "$(field AGENT_SIZE)" = "$(stat -c%s "$TARBALL")" ] || fail "AGENT_SIZE does not match the tarball"
[ "$(field AGENT_SHA256)" = "$(sha256sum "$TARBALL" | cut -d' ' -f1)" ] || fail "AGENT_SHA256 does not match the tarball"

[ "$(tar -tzf "$TARBALL" | cut -d/ -f1 | LC_ALL=C sort -u)" = "ingotron-agent-$VERSION" ] \
  || fail "the tarball root is not ingotron-agent-$VERSION"
! tar -tvzf "$TARBALL" | awk '$1 ~ /^[lhbcp]/' | grep -q . || fail "the tarball has link or special members"

got=$(docker run --rm --network none --entrypoint sh "$IMAGE" -c \
  "set -e; d=\$(mktemp -d); tar -xzf /opt/robot-vault/robot/agent-linux-amd64.tar.gz -C \"\$d\"; \"\$d/ingotron-agent-$VERSION/bin/ingotron-agent\" --version")
[ "$got" = "$VERSION" ] || fail "bin/ingotron-agent --version printed '$got', expected $VERSION"

echo "query image robot files: PASS (agent $VERSION, sha256 $(field AGENT_SHA256), $(field AGENT_SIZE) bytes)"
