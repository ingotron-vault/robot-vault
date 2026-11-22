#!/usr/bin/env bash
set -euo pipefail

# Non-interactive seed script to generate a tiny MCAP, run rsm/rua, and populate Parquet
# Usage inside containers: robot has dev image with Python, rsm, rua installed.

ROOT=${1:-/tmp/robot_data/demo}
BASE_CONFIG=${2:-configs/vault.local.json}
# Which robot this seed uploads as. The agent reads its identity only from the
# config's `robot_id`, so write a copy of the base config with this id set.
ROBOT_ID=${SEED_ROBOT_ID:-$(basename "$ROOT")}

mkdir -p "$ROOT"
CONFIG_DIR=$(mktemp -d)
trap 'rm -rf "$CONFIG_DIR"' EXIT
CONFIG="$CONFIG_DIR/vault.json"
BASE_CONFIG="$BASE_CONFIG" CONFIG="$CONFIG" ROBOT_ID="$ROBOT_ID" python3 - <<'PY'
import json, os
with open(os.environ["BASE_CONFIG"], encoding="utf-8") as handle:
    config = json.load(handle)
config["robot_id"] = os.environ["ROBOT_ID"]
with open(os.environ["CONFIG"], "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
PY

# Wait for ingest readiness, including its Postgres and object-store checks.
# Keep the probe inside the `if`: with `set -e`, a standalone failed Python
# command would terminate the script on the first attempt instead of retrying.
echo "Waiting for ingest at http://ingest:8080/readyz ..."
ingest_ready=0
for i in $(seq 1 60); do
  if python3 - <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen('http://ingest:8080/readyz', timeout=1) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  then
    ingest_ready=1
    echo "Ingest is ready"
    break
  fi
  sleep 1
done
if [ "$ingest_ready" -ne 1 ]; then
  echo "Ingest did not become ready at http://ingest:8080/readyz within 60 seconds" >&2
  exit 1
fi

# Ingest refuses uploads from robots that are not in the `robots`
# fleet registry, so provision this one first. Idempotent server-side
# (ON CONFLICT DO NOTHING), so re-seeding is safe.
echo "Registering robot '$ROBOT_ID' in the fleet registry ..."
ROBOT_ID="$ROBOT_ID" python3 - <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request

robot_id = os.environ["ROBOT_ID"]
url = "http://ingest:8080/admin/robots?" + urllib.parse.urlencode({
    "robot_id": robot_id,
    "name": robot_id,
    "metadata": json.dumps({"source": "development/seed.sh"}),
})
req = urllib.request.Request(url, method="POST")
token = os.environ.get("INGEST_AUTH_TOKEN", "").strip()
if token:
    req.add_header("authorization", f"Bearer {token}")
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.read().decode())
except urllib.error.HTTPError as e:
    print(f"robot registration failed ({e.code}): {e.read().decode()}", file=sys.stderr)
    raise SystemExit(1)
PY

# Opt-in (SEED_SIM=1): seed a real AMR-shaped simulation MCAP from
# development/sim_gen instead of the 5-message IMU toy below. Same rsm/rua
# upload flow either way -- only the file changes. Deterministic, so re-running
# this produces byte-identical bytes for a given SEED_SIM_SEED.
#
# The generator also drops a `<name>.sim.json` sidecar next to the MCAP
# recording exactly what it emitted (and what it deliberately broke in the bad
# episode). `rsm scan` globs *.mcap only, so the sidecar is never uploaded --
# it stays on the robot side as ground truth for QA tests.
if [ "${SEED_SIM:-0}" = "1" ]; then
  echo "SEED_SIM=1: generating simulation MCAP into $ROOT ..."
  SIM_MCAP="$(python3 -m sim_gen \
    --out "$ROOT" \
    --episodes "${SEED_SIM_EPISODES:-3}" \
    --duration "${SEED_SIM_DURATION:-15}" \
    --seed "${SEED_SIM_SEED:-42}" \
    --bad-episode "${SEED_SIM_BAD_EPISODE:-2}" \
    --robot-id "$ROBOT_ID")"
  # Same reason as the toy path below: `rua upload` skips files whose mtime is
  # younger than upload_agent.min_stable_age_seconds.
  SIM_MCAP="$SIM_MCAP" python3 - <<'PY'
import os, time
path = os.environ["SIM_MCAP"]
past = time.time() - 3600
os.utime(path, (past, past))
print(path)
PY
else
SEED_ROOT="$ROOT" python3 - <<'PY'
from mcap.writer import Writer
import time, json, os, sys
root = os.environ.get('SEED_ROOT', '/tmp/robot_data/demo')
path = os.path.join(root, 'seed_sample.mcap')
os.makedirs(root, exist_ok=True)
with open(path, 'wb') as f:
    w = Writer(f)
    w.start()
    # The processor only keeps messages whose schema name is a known ROS 2 type
    # (processor/supported_types.py ROS2_TYPE_NORMALIZATION). A schema named
    # 'json' is skipped by that allowlist, so a seeded upload with it would complete
    # with zero rows written and no data in the UI.
    sid = w.register_schema(name='sensor_msgs/msg/Imu', encoding='jsonschema', data=b'{}')
    cid = w.register_channel(schema_id=sid, topic='imu/linear_accel', message_encoding='json', metadata={})
    now = int(time.time()*1e9)
    for i in range(5):
        data = json.dumps({'x': i, 'y': i*2, 'z': i*3}).encode()
        w.add_message(channel_id=cid, log_time=now+i, publish_time=now+i, sequence=i, data=data)
    w.finish()
# `rua upload` skips files whose mtime is younger than
# upload_agent.min_stable_age_seconds (default 5s) -- on a real robot such a
# file is still being recorded. This one is complete the moment the writer
# closes, so back-date its mtime rather than making the seed sleep.
past = time.time() - 3600
os.utime(path, (past, past))
print(path)
PY
fi

rsm scan --config "$CONFIG" --root "$ROOT"
rsm hash --config "$CONFIG" --root "$ROOT"
rua upload --config "$CONFIG" --root "$ROOT"

echo "Seed complete. Check http://localhost:8003/ui, or list uploads through the query API:"
echo "  TOKEN=\$(curl -fsS 'http://localhost:8003/auth/dev/token?roles=viewer' | jq -r .access_token)"
echo "  curl -fsS -H \"Authorization: Bearer \$TOKEN\" 'http://localhost:8003/api/v1/uploads/list?robot_id=$ROBOT_ID&limit=5'"

