#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

MODE=execute
case "${1:-}" in
--check) MODE=check; shift ;;
"") ;;
*) printf 'usage: %s [--check]\n' "$0" >&2; exit 2 ;;
esac
[ $# -eq 0 ] || { printf 'usage: %s [--check]\n' "$0" >&2; exit 2; }

PROJECT="robot-vault-gc-test-$$"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/robot-vault-gc-test.XXXXXXXX")
chmod 0700 "$WORK"
mkdir -p "$WORK/robot-data"
# Throwaway root password for this run only (a Compose secret file).
ROOT_PASSWORD=gc-test-password-0000000000000001
printf '%s\n' "$ROOT_PASSWORD" >"$WORK/objectstore-root-password"
# Throwaway secret of the robot-vault-gc key; objectstore-access-ready creates
# the key with it and the GC under test signs with it.
GC_SECRET=gc-test-s3-secret-00000000000000001
STARTED=0
COMPOSE=(
    env
    COMPOSE_DISABLE_ENV_FILE=1
    OBJECTSTORE_ROOT_USER=gc-test-root
    OBJECTSTORE_ROOT_PASSWORD_FILE="$WORK/objectstore-root-password"
    S3_BUCKET=gc-test-bucket
    S3_ENDPOINT=http://objectstore:9000
    S3_REGION=us-east-1
    S3_SECURE=false
    GC_S3_SECRET_KEY="$GC_SECRET"
    DATABASE_URL=postgres://robotvault:robotvault@test-postgres:5432/robotvault
    ROBOT_DATA_HOST_DIR="$WORK/robot-data"
    DEBUG_BIND_HOST=127.0.0.1
    docker compose --project-name "$PROJECT"
    --env-file /dev/null
    -f docker-compose.yml
    -f development/release/docker-compose.gc-test.yml
    --profile test
)

cleanup() {
    local rc=$?
    if [ "$STARTED" = 1 ]; then
        if [ "$rc" -ne 0 ]; then
            "${COMPOSE[@]}" ps >&2 || true
            "${COMPOSE[@]}" logs --tail=300 test-postgres objectstore objectstore-access-ready >&2 || true
        fi
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    rm -rf -- "$WORK"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"${COMPOSE[@]}" config --format json >"$WORK/config.json"
python3 - "$WORK/config.json" "$WORK/objectstore-root-password" "$GC_SECRET" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
services = config["services"]
started = {
    "dev-storage-credential-guard",
    "test-postgres",
    "objectstore",
    "db-migrate",
    "objectstore-access-ready",
    "dev",
}
for name in started:
    assert not services[name].get("container_name"), (name, services[name].get("container_name"))
store = services["objectstore"]
volumes = store.get("volumes", [])
# Only an anonymous volume: never a bind and never the named development volume.
assert [(v.get("type"), v.get("source"), v.get("target")) for v in volumes] in (
    [("volume", None, "/data")], [("volume", "", "/data")]), volumes
assert store["environment"]["RUSTFS_ACCESS_KEY"] == "gc-test-root"
assert config["secrets"]["objectstore_root_password"]["file"] == sys.argv[2]
ready = services["objectstore-access-ready"]["environment"]
assert ready["S3_BUCKET"] == "gc-test-bucket"
assert ready["OBJECTSTORE_ROOT_USER"] == "gc-test-root"
assert ready["GC_S3_ACCESS_KEY"] == "robot-vault-gc"
assert ready["GC_S3_SECRET_KEY"] == sys.argv[3]
PY

if [ "$MODE" = check ]; then
    printf 'GC integration composition contract: ok\n'
    exit 0
fi

"${COMPOSE[@]}" build dev
STARTED=1
"${COMPOSE[@]}" up -d --wait --wait-timeout 120 test-postgres objectstore
"${COMPOSE[@]}" run --rm --no-deps \
    -e DATABASE_URL=postgres://robotvault:robotvault@test-postgres:5432/robotvault \
    db-migrate
"${COMPOSE[@]}" run --rm --no-deps objectstore-access-ready
# The root key reaches pytest only as OBJECTSTORE_ROOT_* (fixture seeding), never
# as AWS_*, so the GC under test cannot fall back to it.
OBJECTSTORE_ROOT_PASSWORD="$ROOT_PASSWORD" "${COMPOSE[@]}" run --rm --no-deps \
    -e REQUIRE_LIVE_STACK=1 \
    -e DATABASE_URL=postgres://robotvault:robotvault@test-postgres:5432/robotvault \
    -e S3_BUCKET=gc-test-bucket \
    -e S3_ENDPOINT=http://objectstore:9000 \
    -e S3_REGION=us-east-1 \
    -e S3_SECURE=false \
    -e GC_S3_ACCESS_KEY=robot-vault-gc \
    -e GC_S3_SECRET_KEY \
    -e OBJECTSTORE_ROOT_USER \
    -e OBJECTSTORE_ROOT_PASSWORD \
    dev bash -c \
    'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/integration/test_gc_reachability.py -v -s'
