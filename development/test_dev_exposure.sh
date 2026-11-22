#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
GUARD="$ROOT/development/check-dev-compose-credentials.sh"
OUTPUT=$(mktemp "${TMPDIR:-/tmp}/robot-vault-query-guard.XXXXXXXX")
trap 'rm -f -- "$OUTPUT"' EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

run_guard() {
    env -i PATH="$PATH" DEV_GUARD_SCOPE=query "$@" /bin/sh "$GUARD"
}

run_guard DEV_QUERY_BIND_HOST=127.0.0.1 DEV_UI_BIND_HOST=127.0.0.1 \
    DEV_QUERY_OIDC_DEV=true || fail 'loopback query/UI binds were refused'

if run_guard DEV_QUERY_BIND_HOST=0.0.0.0 DEV_QUERY_OIDC_DEV=true \
    >"$OUTPUT" 2>&1; then
    fail 'non-loopback OIDC_DEV bind passed without acknowledgement'
fi
grep -Fq 'can mint an admin token' "$OUTPUT" \
    || fail 'refusal did not explain the admin-token risk'

if run_guard DEV_QUERY_BIND_HOST=127.0.0.1 DEV_UI_BIND_HOST=0.0.0.0 \
    DEV_QUERY_OIDC_DEV=true >"$OUTPUT" 2>&1; then
    fail 'non-loopback UI OIDC_DEV bind passed without acknowledgement'
fi
grep -Fq 'QUERY_BIND_HOST and UI_BIND_HOST' "$OUTPUT" \
    || fail 'UI refusal did not name both guarded bind controls'

output=$(run_guard DEV_QUERY_BIND_HOST=127.0.0.1 DEV_UI_BIND_HOST=0.0.0.0 \
    DEV_QUERY_OIDC_DEV=true \
    DEV_QUERY_UNSAFE_EXPOSURE_ALLOWED=1 2>&1) \
    || fail 'explicitly acknowledged non-loopback UI bind was refused'
case "$output" in
    *'WARNING:'*'/dev/token can mint an admin token.'*) ;;
    *) fail 'acknowledged exposure did not print the admin-token warning' ;;
esac

python3 - "$ROOT/docker-compose.yml" <<'PY'
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
port = '"${QUERY_BIND_HOST:-127.0.0.1}:${QUERY_PORT:-8003}:8003"'
ui_port = '"${UI_BIND_HOST:-127.0.0.1}:${UI_PORT:-5173}:5173"'
if port not in text:
    raise SystemExit("query port is not loopback-bound by default")
if ui_port not in text:
    raise SystemExit("UI port is not loopback-bound by default")
if "DEV_UI_BIND_HOST=${UI_BIND_HOST:-127.0.0.1}" not in text:
    raise SystemExit("query guard does not receive the rendered UI bind host")
if "dev-query-credential-guard:" not in text:
    raise SystemExit("query service has no credential guard dependency")
query_limits = (
    "DUCKDB_QUERY_TIMEOUT_SECONDS",
    "QUERY_MAX_SOURCE_RANGE_SECONDS",
    "QUERY_MAX_SOURCE_FILES",
    "QUERY_MAX_SOURCE_ROWS",
    "QUERY_MAX_SOURCE_BYTES",
    "SQL_CONSOLE_MEMORY_LIMIT_MB",
)
for name in query_limits:
    if name not in text:
        raise SystemExit(f"base composition omitted {name}")
PY

# Prove the widened UI publication and the guard see the same rendered value.
env UI_BIND_HOST=0.0.0.0 docker compose --env-file /dev/null \
    -f "$ROOT/docker-compose.yml" config --format json >"$OUTPUT"
python3 - "$OUTPUT" <<'PY'
import json
import sys

services = json.load(open(sys.argv[1], encoding="utf-8"))["services"]
guard = services["dev-query-credential-guard"]["environment"]
ui_ports = services["ui"]["ports"]
assert guard["DEV_QUERY_BIND_HOST"] == "127.0.0.1", guard
assert guard["DEV_UI_BIND_HOST"] == "0.0.0.0", guard
assert any(
    port.get("host_ip") == "0.0.0.0" and port.get("target") == 5173
    for port in ui_ports
), ui_ports
PY

env DUCKDB_THREADS=3 DUCKDB_MEMORY_LIMIT_MB=640 DUCKDB_QUERY_CONCURRENCY=5 \
    DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS=8 DUCKDB_QUERY_TIMEOUT_SECONDS=41 \
    QUERY_MAX_SOURCE_RANGE_SECONDS=604800 QUERY_MAX_SOURCE_FILES=3210 \
    QUERY_MAX_SOURCE_ROWS=7654321 QUERY_MAX_SOURCE_BYTES=1234567890 \
    SQL_CONSOLE_MEMORY_LIMIT_MB=700 \
    docker compose --env-file /dev/null -f "$ROOT/docker-compose.yml" config --format json >"$OUTPUT"
python3 - "$OUTPUT" <<'PY'
import json
import sys

services = json.load(open(sys.argv[1], encoding="utf-8"))["services"]
query = services["query"]["environment"]
assert query["DUCKDB_THREADS"] == "3"
assert query["DUCKDB_MEMORY_LIMIT_MB"] == "640"
assert query["DUCKDB_QUERY_CONCURRENCY"] == "5"
assert query["DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS"] == "8"
assert query["DUCKDB_QUERY_TIMEOUT_SECONDS"] == "41"
assert query["QUERY_MAX_SOURCE_RANGE_SECONDS"] == "604800"
assert query["QUERY_MAX_SOURCE_FILES"] == "3210"
assert query["QUERY_MAX_SOURCE_ROWS"] == "7654321"
assert query["QUERY_MAX_SOURCE_BYTES"] == "1234567890"
assert query["SQL_CONSOLE_MEMORY_LIMIT_MB"] == "700"
# Core edition by default. The core processor refuses the edition stage
# switches, so the core stack sets none.
assert query["ROBOT_VAULT_EDITION"] == "core", query["ROBOT_VAULT_EDITION"]
assert services["processor"]["environment"]["ROBOT_VAULT_EDITION"] == "core"
stage_switches = {"SEGMENTATION_ENABLED", "QA_ENABLED", "MEDIA_ENCODE_ENABLED"}
for name in ("processor", "query"):
    leaked = stage_switches & set(services[name].get("environment") or {})
    assert not leaked, (name, sorted(leaked))
PY

printf 'dev exposure contract: ok\n'
