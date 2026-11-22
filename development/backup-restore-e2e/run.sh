#!/bin/sh
# Run the backup/restore E2E on the dev stack's datastores only
# (postgres, objectstore and its bootstrap; no application service is needed).
#
#   COMPOSE_PROJECT_NAME=rv-backup-e2e sh development/backup-restore-e2e/run.sh OUT_DIR [RUNS]
#   docker compose -p rv-backup-e2e down -v     # remove the disposable stack afterwards
#
# Builds the dev and toolkit images, then runs e2e.py RUNS times (default 2). Reports:
# OUT_DIR/report-<n>.json. Exit 0 only when every run passes.
set -eu
# It empties and restores the store and database of the stack it runs on, so it
# refuses to run without an explicit, disposable Compose project.
if [ -z "${COMPOSE_PROJECT_NAME:-}" ]; then
  echo "run.sh: set COMPOSE_PROJECT_NAME to a disposable Compose project (never a stack you use)" >&2
  exit 2
fi
# A project that already has containers or volumes may hold real data: refuse it.
existing=$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME"; \
  docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")
if [ -n "$existing" ]; then
  echo "run.sh: Compose project '$COMPOSE_PROJECT_NAME' already has containers or volumes; use a new, empty project" >&2
  exit 2
fi
OUT=${1:?OUT_DIR}
RUNS=${2:-2}
mkdir -p "$OUT"
docker compose build dev
make build-toolkit
# Optional OBJECTSTORE_DATA_DIR: a caller that binds the named store volume to a
# folder it owns sets this to that folder's root. RustFS runs as uid 10001 and
# must be able to write the folder.
if [ -n "${OBJECTSTORE_DATA_DIR:-}" ]; then
	mkdir -p "$OBJECTSTORE_DATA_DIR/volumes/objectstore_data"
	chmod 0777 "$OBJECTSTORE_DATA_DIR/volumes/objectstore_data"
fi
docker compose up -d --no-deps --wait postgres objectstore || {
	docker compose logs --no-color objectstore | tail -n 40
	exit 1
}
rc=0
n=1
while [ "$n" -le "$RUNS" ]; do
	echo "=== backup-restore E2E run $n of $RUNS ==="
	python3 development/backup-restore-e2e/e2e.py "$OUT/report-$n.json" || rc=1
	n=$((n + 1))
done
exit "$rc"
