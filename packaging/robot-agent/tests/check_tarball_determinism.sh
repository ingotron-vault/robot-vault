#!/usr/bin/env bash
# Full agent artifact gate: duplicate build + archive + clean-host runtime.
set -euo pipefail

TEST_ROOT=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$TEST_ROOT/../../.." && pwd)
BUILDER="$REPO_ROOT/packaging/robot-agent/build-agent-tarball.sh"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/agent-artifact-gate.XXXXXX")
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

VERSION=0.9.1
EPOCH=1700000000
TARBALL="$WORK/agent-linux-amd64.tar.gz"
LOG="$WORK/build.log"
BUILDER_IMAGE=$(sed -n 's/^FROM --platform=linux\/amd64 \([^[:space:]]*\) AS robot-agent-builder$/\1/p' \
  "$REPO_ROOT/phases/05-query-service/Dockerfile")

bash "$BUILDER" --out "$TARBALL" --version "$VERSION" \
  --source-epoch "$EPOCH" --verify-reproducible >"$LOG"

for key in path sha256 size root_dir platform glibc_min version builder_image; do
  grep -q "^$key=" "$LOG" || { echo "missing builder result field: $key" >&2; exit 1; }
done
grep -q '^platform=linux/amd64$' "$LOG"
grep -q '^glibc_min=2.31$' "$LOG"
grep -q '^version=0.9.1$' "$LOG"
[ "$(sed -n 's/^builder_image=//p' "$LOG")" = "$BUILDER_IMAGE" ]
[ "$(sed -n 's/^sha256=//p' "$LOG")" = "$(sha256sum "$TARBALL" | cut -d' ' -f1)" ]
[ "$(sed -n 's/^size=//p' "$LOG")" = "$(stat -c%s "$TARBALL")" ]

ROOT_DIR=$(sed -n 's/^root_dir=//p' "$LOG")
[ "$ROOT_DIR" = "ingotron-agent-$VERSION" ]
BAD=$(tar -tzf "$TARBALL" | grep -v "^$ROOT_DIR\(/\|$\)" || true)
[ -z "$BAD" ] || { echo "member outside root: $BAD" >&2; exit 1; }
tar -tvzf "$TARBALL" | awk '$1 ~ /^[lhbcp]/ {print; bad=1} END {exit bad ? 0 : 1}' >"$WORK/links" || true
[ ! -s "$WORK/links" ] || { echo "archive contains special/link members" >&2; cat "$WORK/links" >&2; exit 1; }

tar -xzf "$TARBALL" -C "$WORK"
ROOT="$WORK/$ROOT_DIR"
for member in bin/ingotron-agent bin/ingotron-agent-run \
              bin/ingotron-agent-retention systemd/ingotron-agent.service \
              VERSION LICENSE THIRD_PARTY_NOTICES.md \
              AGENT_DEPENDENCIES.lock.txt AGENT_DEPENDENCY_SBOM.cdx.json \
              THIRD_PARTY_LICENSES/Python-3.10.18-LICENSE.txt BUILD_INFO.json; do
  [ -f "$ROOT/$member" ] || { echo "missing agent member: $member" >&2; exit 1; }
done
[ -d "$ROOT/bin/_internal" ]
[ ! -e "$ROOT/bin/python3" ]
[ -z "$(find "$ROOT" -type f -name '*.py' -print -quit)" ]
[ -z "$(find "$ROOT" -type l -print -quit)" ]
[ -z "$(find "$ROOT" -type f \( -path '*/grpc_tools/*' \
    -o -name ar -o -name as -o -name ld -o -name nm -o -name objdump \
    -o -name ranlib -o -name readelf -o -name strip \) -print -quit)" ]
cmp "$ROOT/LICENSE" "$REPO_ROOT/LICENSE"
cmp "$ROOT/THIRD_PARTY_NOTICES.md" "$REPO_ROOT/THIRD_PARTY_NOTICES.md"
# THIRD_PARTY_INVENTORY.json is present only when freeze_agent.py's optional inventory hook ran.
INVENTORY_TOOL="$REPO_ROOT/deploy/release-images/generate_third_party_inventory.py"
if [ -f "$INVENTORY_TOOL" ]; then
  [ -f "$ROOT/THIRD_PARTY_INVENTORY.json" ] || { echo "missing agent member: THIRD_PARTY_INVENTORY.json" >&2; exit 1; }
else
  [ ! -e "$ROOT/THIRD_PARTY_INVENTORY.json" ]
fi
cmp "$ROOT/AGENT_DEPENDENCIES.lock.txt" \
    "$REPO_ROOT/packaging/robot-agent/requirements-build.lock.txt"

python3 - "$ROOT" "$BUILDER_IMAGE" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
expected_builder = sys.argv[2]
info = json.loads((root / "BUILD_INFO.json").read_text())
assert info["platform"] == "linux/amd64"
assert info["glibc_min"] == "2.31"
assert info["builder_image"] == expected_builder
bom = json.loads((root / "AGENT_DEPENDENCY_SBOM.cdx.json").read_text())
assert bom["bomFormat"] == "CycloneDX"
assert len(bom["components"]) == 11
if (root / "THIRD_PARTY_INVENTORY.json").is_file():
    inventory = json.loads((root / "THIRD_PARTY_INVENTORY.json").read_text())
    agent = [row for row in inventory["components"]["python_locked"]
             if row["authority"] == "packaging/robot-agent/requirements-build.lock.txt"]
    assert len(agent) == 11, len(agent)
    assert {row["name"].lower() for row in agent} >= {
        "grpcio", "grpcio-tools", "protobuf", "prometheus-client", "psutil", "pyinstaller"
    }
PY

bash "$TEST_ROOT/check_frozen_bundle.sh" "$TARBALL"
echo "frozen agent artifact gate: PASS (sha256=$(sha256sum "$TARBALL" | cut -d' ' -f1), size=$(stat -c%s "$TARBALL"))"
