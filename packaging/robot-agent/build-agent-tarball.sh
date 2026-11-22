#!/usr/bin/env bash
# Deterministic, self-contained agent-linux-amd64.tar.gz producer.
#
# PyInstaller runs only in the pinned Python 3.10 / Debian 11 builder: stage
# `robot-agent-builder` of phases/05-query-service/Dockerfile, the same stage
# the query image uses to build the agent it serves. pack_agent.sh writes the
# canonical archive for both. The shipped tree contains its own CPython runtime,
# grpcio/protobuf closure, and first-party packages. No host Python, source
# tree, or dependency-resolution fallback exists in the release artifact.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$HERE/../.." && pwd)
BUILDER_DOCKERFILE="$REPO_ROOT/phases/05-query-service/Dockerfile"
BUILDER_TARGET=robot-agent-builder
BUILDER_LOCK="$HERE/requirements-build.lock.txt"

usage() {
  cat >&2 <<'EOF'
Usage: build-agent-tarball.sh --out PATH [--version X.Y.Z] [--source-epoch N]
                               [--force] [--verify-reproducible]
EOF
}

OUT=""
VERSION=""
SOURCE_EPOCH=""
FORCE=0
VERIFY_REPRO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || { usage; exit 2; }; OUT=$2; shift 2 ;;
    --version) [ $# -ge 2 ] || { usage; exit 2; }; VERSION=$2; shift 2 ;;
    --source-epoch) [ $# -ge 2 ] || { usage; exit 2; }; SOURCE_EPOCH=$2; shift 2 ;;
    --force) FORCE=1; shift ;;
    --verify-reproducible) VERIFY_REPRO=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

[ -n "$OUT" ] || { echo "ERROR: --out is required" >&2; usage; exit 2; }
if [ -z "$VERSION" ]; then VERSION=$(tr -d '[:space:]' < "$REPO_ROOT/VERSION"); fi
if [ -z "$SOURCE_EPOCH" ]; then
  SOURCE_EPOCH=$(git -C "$REPO_ROOT" log -1 --format=%ct HEAD)
fi
printf '%s' "$VERSION" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
  || { echo "ERROR: agent version is not strict semver: $VERSION" >&2; exit 2; }
printf '%s' "$SOURCE_EPOCH" | grep -Eq '^(0|[1-9][0-9]*)$' \
  || { echo "ERROR: --source-epoch must be a non-negative integer" >&2; exit 2; }
[ ! -e "$OUT" ] || [ "$FORCE" = 1 ] \
  || { echo "ERROR: $OUT exists; use --force to replace it" >&2; exit 1; }

for required in "$BUILDER_DOCKERFILE" "$BUILDER_LOCK" "$HERE/freeze_agent.py" \
                "$HERE/pyinstaller_entry.py" "$HERE/pack_agent.sh" \
                "$REPO_ROOT/LICENSE"; do
  [ -f "$required" ] && [ ! -L "$required" ] \
    || { echo "ERROR: required agent build authority is missing: $required" >&2; exit 1; }
done
BUILDER_BASE_IMAGE=$(sed -n "s/^FROM --platform=linux\/amd64 \([^[:space:]]*\) AS $BUILDER_TARGET\$/\1/p" "$BUILDER_DOCKERFILE")
[ "$(printf '%s\n' "$BUILDER_BASE_IMAGE" | grep -c .)" -eq 1 ] \
  && printf '%s' "$BUILDER_BASE_IMAGE" \
    | grep -Eq '^python:3\.10\.18-slim-bullseye@sha256:[0-9a-f]{64}$' \
  || { echo "ERROR: $BUILDER_DOCKERFILE must have one pinned Python 3.10.18 FROM ... AS $BUILDER_TARGET" >&2; exit 1; }
command -v docker >/dev/null 2>&1 \
  || { echo "ERROR: Docker is required for the pinned agent build" >&2; exit 1; }
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) echo "ERROR: agent-linux-amd64 requires an amd64 Docker builder" >&2; exit 1 ;;
esac

ROOT_DIR="ingotron-agent-$VERSION"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/build-agent-tarball.XXXXXX")
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT

# The local tag is derived from the builder stage's inputs. The release source
# is mounted read-only at run time and cannot be baked into or recovered from
# this builder image: the stage copies only the lock and freeze_agent.py.
BUILDER_KEY=$(
  { sed -n "/ AS $BUILDER_TARGET\$/,/^ENTRYPOINT /p" "$BUILDER_DOCKERFILE"
    cat "$BUILDER_LOCK" "$HERE/freeze_agent.py"; } | sha256sum | cut -d' ' -f1
)
BUILDER_IMAGE="ingotron-agent-builder:${BUILDER_KEY:0:24}"
echo "== Preparing pinned Debian 11 agent builder $BUILDER_IMAGE"
docker build --platform linux/amd64 --target "$BUILDER_TARGET" \
  -f "$BUILDER_DOCKERFILE" -t "$BUILDER_IMAGE" "$REPO_ROOT" </dev/null

build_once() {
  local stage_parent=$1 out_path=$2
  mkdir -p "$stage_parent"
  docker run --rm --network none --platform linux/amd64 \
    --user "$(id -u):$(id -g)" \
    -e HOME=/output/.home \
    -e PYTHONHASHSEED=0 \
    -e SOURCE_DATE_EPOCH="$SOURCE_EPOCH" \
    -v "$REPO_ROOT:/source:ro" \
    -v "$stage_parent:/output" \
    "$BUILDER_IMAGE" \
      --source /source \
      --output-root "/output/$ROOT_DIR" \
      --version "$VERSION" \
      --source-epoch "$SOURCE_EPOCH" </dev/null

  bash "$HERE/pack_agent.sh" "$stage_parent" "$ROOT_DIR" "$SOURCE_EPOCH" "$out_path"
}

if [ "$VERIFY_REPRO" = 1 ]; then
  mkdir -p "$WORK/a" "$WORK/b"
  build_once "$WORK/a" "$WORK/a.tar.gz"
  build_once "$WORK/b" "$WORK/b.tar.gz"
  cmp -s "$WORK/a.tar.gz" "$WORK/b.tar.gz" || {
    echo "ERROR: identical pinned agent builds produced different tar bytes" >&2
    sha256sum "$WORK/a.tar.gz" "$WORK/b.tar.gz" >&2
    exit 1
  }
  mkdir -p "$(dirname "$OUT")"
  mv "$WORK/a.tar.gz" "$OUT"
else
  mkdir -p "$WORK/one"
  build_once "$WORK/one" "$OUT"
fi

SHA256=$(sha256sum "$OUT" | cut -d' ' -f1)
SIZE=$(stat -c%s "$OUT")
cat <<EOF
path=$OUT
sha256=$SHA256
size=$SIZE
root_dir=$ROOT_DIR
platform=linux/amd64
glibc_min=2.31
version=$VERSION
builder_image=$BUILDER_BASE_IMAGE
EOF
