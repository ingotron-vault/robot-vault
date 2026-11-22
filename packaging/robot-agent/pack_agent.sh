#!/usr/bin/env bash
# Pack a frozen agent tree into the canonical agent-linux-amd64.tar.gz.
#
#   pack_agent.sh <stage-parent> <root-dir> <source-epoch> <out.tar.gz>
#
# <stage-parent>/<root-dir> is the tree freeze_agent.py wrote. The modes, mtimes,
# owner and member order are fixed, so the same tree always gives the same
# bytes. Used by the query image build (stage robot-agent) and by
# build-agent-tarball.sh: one recipe, two users.
set -euo pipefail

[ $# -eq 4 ] || { echo "usage: pack_agent.sh <stage-parent> <root-dir> <source-epoch> <out.tar.gz>" >&2; exit 2; }
STAGE_PARENT=$1
ROOT_DIR=$2
SOURCE_EPOCH=$3
OUT=$4
ROOT="$STAGE_PARENT/$ROOT_DIR"

[[ "$ROOT_DIR" =~ ^ingotron-agent-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
  || { echo "ERROR: root directory is not ingotron-agent-X.Y.Z: $ROOT_DIR" >&2; exit 2; }
[[ "$SOURCE_EPOCH" =~ ^(0|[1-9][0-9]*)$ ]] \
  || { echo "ERROR: source epoch must be a non-negative integer" >&2; exit 2; }
[ -x "$ROOT/bin/ingotron-agent" ] \
  || { echo "ERROR: PyInstaller produced no frozen executable" >&2; exit 1; }
[ ! -e "$ROOT/bin/python3" ] \
  || { echo "ERROR: bundle unexpectedly exposes a python3 launcher" >&2; exit 1; }
[ -z "$(find "$ROOT" -type l -print -quit)" ] \
  || { echo "ERROR: bundle contains a link member" >&2; exit 1; }
[ -z "$(find "$ROOT" -type f -name '*.py' -print -quit)" ] \
  || { echo "ERROR: bundle contains Python source" >&2; exit 1; }

VERSION=${ROOT_DIR#ingotron-agent-}
"$ROOT/bin/ingotron-agent" --version | grep -Fx "$VERSION" >/dev/null
"$ROOT/bin/ingotron-agent" __bundle-self-test | grep -F '"frozen": true' >/dev/null

# Canonical archive modes and mtimes. Shared objects/data are not marked
# executable; only the three operator entries are 0755.
find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT" -type f -exec chmod 0644 {} +
chmod 0755 "$ROOT/bin/ingotron-agent" \
           "$ROOT/bin/ingotron-agent-run" \
           "$ROOT/bin/ingotron-agent-retention"
find "$ROOT" -exec touch -h -d "@$SOURCE_EPOCH" {} +

mkdir -p "$(dirname "$OUT")"
tar --sort=name --format=pax \
  --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
  --mtime="@$SOURCE_EPOCH" --owner=0 --group=0 --numeric-owner \
  -cf - -C "$STAGE_PARENT" "$ROOT_DIR" \
  | gzip -n > "$OUT"
