#!/bin/sh
# Object-store conformance gate. Run from the repository root, once per label (run it twice):
#   make test-objectstore-conformance CONFORMANCE_LABEL=run1   (own project, own teardown)
#
# It starts the pinned RustFS with the frozen settings, bootstraps the bucket and the five service
# keys, and runs every probe. The verdict is `gate.py`: the exit code is nonzero when any
# step failed or any required artifact or probe is missing. A runner or bootstrap failure is a failed
# step, never swallowed: no command here ends in a pipe or `|| true`, and a failed infrastructure
# command exits at once with the verdict written.
#
# Steps (each recorded in results/<label>/steps.txt):
#   keys            all five service keys and the root key are set (nothing starts without them)
#   bootstrap       bucket and five users through the product bootstrap program
#   expiry-red      a 20 s stale-upload expiry must abort an upload (proves the setting is applied)
#   expiry-green    the frozen 360h expiry keeps the upload
#   store-argv      the root password is in no command line of the store container
#   durability-log  the store log shows durability mode strict
#   bootstrap-argv  no secret in any command line or output of the bootstrap (and the control is caught)
#   iam             290 live probes against the reviewed policies: 0 over, 0 under, 0 errors
#   conformance     conformance.py: the S3 calls the services make, exact status codes, denied writes
#   contract-probe  the real query-service and GC clients (SigV4, path style, guarded delete)
#   crash           200 acknowledged writes survive kill -9 of the store
# Results: development/objectstore-conformance/results/<label>/ (artifacts and gate.json).
set -u
label=${1:?usage: run.sh <label>}
HERE=development/objectstore-conformance
OUT=$HERE/results/$label
mkdir -p "$OUT"
: > "$OUT/steps.txt"
: "${COMPOSE_FILE:=$HERE/compose.yml}"
: "${COMPOSE_PROJECT_NAME:=rv-objectstore-conformance}"
export COMPOSE_FILE COMPOSE_PROJECT_NAME
# Optional: BUILD_PREFIX is a command put in front of the image build (for example `nice -n 10`).
BUILD_PREFIX=${BUILD_PREFIX:-}
dc() { docker compose "$@"; }
R() { dc --profile run run --rm -T runner "$@"; }
up() { dc up -d --wait --force-recreate objectstore; }
note() { echo "$1 $2" >> "$OUT/steps.txt"; echo "-- $1: $2"; }
step() { name=$1; shift; if "$@"; then note "$name" pass; else note "$name" "fail rc=$?"; fi; }
# The verdict is the exit code of the whole run.
finish() {
    python3 "$HERE/gate.py" "$OUT"
    rc=$?
    # Optional KEEP_STACK: when set (non-empty), the caller owns teardown and this script keeps the stack.
    [ -n "${KEEP_STACK:-}" ] || dc --profile run down -v --remove-orphans >/dev/null 2>&1
    exit $rc
}
abort() { note "$1" "fail: $2"; finish; }

# objectstore is our image (deploy/release-images/objectstore.Dockerfile); the gate tests this build.
GIT_SHA=$(git rev-parse HEAD) $BUILD_PREFIX docker compose --profile run build objectstore runner || abort infrastructure "objectstore or runner image build failed"
step keys R "$HERE/suite_env.py"
grep -q '^keys pass' "$OUT/steps.txt" || abort infrastructure "service keys missing; nothing started"
secret=$(cat "$HERE/test-root-password")

# 1. Bootstrap on the frozen settings. The store keeps its data across the restarts below.
up || abort infrastructure "objectstore did not become healthy"
step bootstrap R development/objectstore/bootstrap.py

# 2. Stale multipart expiry. Red: 20 s expiry and a 5 s sweep; the 45 s hold must fail.
STALE_EXPIRY=20s STALE_INTERVAL=5s up || abort infrastructure "objectstore (20s expiry) did not become healthy"
docker image inspect "$(dc config --images objectstore | head -1)" \
    --format 'image {{.Id}} {{json .RepoDigests}} created={{.Created}}' > "$OUT/image.txt"
dc exec -T objectstore printenv RUSTFS_API_STALE_UPLOADS_EXPIRY > "$OUT/expiry-red.env"
if R "$HERE/expiry_test.py" red 45 "/out/$label"; then note expiry-red "fail (upload survived a 20s expiry)"
else note expiry-red pass; fi
# Green: the frozen 360h expiry with the same 5 s sweep keeps the upload.
STALE_INTERVAL=5s up || abort infrastructure "objectstore (360h expiry) did not become healthy"
dc exec -T objectstore printenv RUSTFS_API_STALE_UPLOADS_EXPIRY > "$OUT/expiry-green.env"
step expiry-green R "$HERE/expiry_test.py" green 45 "/out/$label"

# 3. Frozen settings from here on.
up || abort infrastructure "objectstore (frozen settings) did not become healthy"
dc exec -T objectstore sh -c 'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline; echo; done' > "$OUT/store-cmdlines.txt"
if grep -qF "$secret" "$OUT/store-cmdlines.txt"; then note store-argv fail; else note store-argv pass; fi
sleep 2
dc logs --no-color objectstore > "$OUT/objectstore-start.log" 2>&1
dc exec -T objectstore sh -c 'cat /logs/* 2>/dev/null' >> "$OUT/objectstore-start.log" 2>&1
if grep -q 'durability mode resolved.*strict\|mode.*strict.*durability' "$OUT/objectstore-start.log"; then
    note durability-log pass; else note durability-log "fail (no strict mode line)"; fi

# 4. Probes.
step bootstrap-argv R "$HERE/test_bootstrap_argv.py" "/out/$label"
step iam R "$HERE/iam_probe.py" "/out/$label"
conformance() { R "$HERE/conformance.py" "/out/$label" > "$OUT/conformance.log" 2>&1; }
step conformance conformance
tail -25 "$OUT/conformance.log"
step contract-probe R "$HERE/rustfs_contract_probe.py" "/out/$label"

# 5. Crash test: 200 acknowledged writes, kill -9 while more writes are in flight, restart, verify.
rm -f "$OUT/crash-ready"
R "$HERE/crash_test.py" write "$label" "/out/$label" > "$OUT/crash-write.log" 2>&1 &
writer=$!
i=0
while [ ! -f "$OUT/crash-ready" ] && [ $i -lt 900 ]; do sleep 0.2; i=$((i + 1)); done
if [ -f "$OUT/crash-ready" ]; then
    dc kill -s KILL objectstore
    echo "killed at $(date -Is)" > "$OUT/crash-kill.txt"
    docker inspect "$(dc ps -aq objectstore)" --format 'state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' \
        >> "$OUT/crash-kill.txt"
else
    echo "writer never reached 200 acknowledgements" > "$OUT/crash-kill.txt"
fi
wait $writer
tail -3 "$OUT/crash-write.log"
up || abort infrastructure "objectstore did not restart after kill -9"
step crash R "$HERE/crash_test.py" verify "$label" "/out/$label"
dc logs --no-color objectstore > "$OUT/objectstore.log" 2>&1

echo "== $label steps"; cat "$OUT/steps.txt"
finish
