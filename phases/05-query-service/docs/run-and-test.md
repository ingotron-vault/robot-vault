# Phase 05 — Run and test

Use this runbook to start the query service and check its API and web UI. Run
commands from the repository root.

## Bring up the query service
```
make stack-up
```

This is the supported clean-checkout path: it builds images and SPAs,
initializes the development credentials and bucket, starts the whole stack, and
waits for readiness. With those prerequisites already complete,
`docker compose up -d query` starts query and its declared dependency closure.

- UI: `http://localhost:8003/ui`
- Health: `curl -s http://localhost:8003/healthz | jq`
- Metrics (development leaves `METRICS_AUTH_TOKEN` unset):
  `curl -s http://localhost:8003/metrics | head -n 20`. If you set a token,
  add `-H "Authorization: Bearer $METRICS_AUTH_TOKEN"`.

## Smoke test
```
# Local compose explicitly reports dev auth; obtain its short-lived token.
RV_TOKEN=$(curl -fsS "http://localhost:8003/auth/dev/token?roles=viewer" | jq -r .access_token)
# List robots from the authoritative Postgres registry
curl -fsS -H "Authorization: Bearer $RV_TOKEN" "http://localhost:8003/api/robots" | jq
# Uploads summary (all robots)
curl -fsS -H "Authorization: Bearer $RV_TOKEN" "http://localhost:8003/api/uploads/summary" | jq
# Timeseries for a topic
curl -fsS -H "Authorization: Bearer $RV_TOKEN" "http://localhost:8003/api/timeseries?topic=imu/linear_accel&bucket=minute" | jq
```

## Local run (no Docker)
The query service uses Postgres for its registries/control plane and the object store (S3 API)
for Parquet payloads. Both must be reachable when running outside Docker — the
default Compose stack publishes neither; `make debug-up` publishes them on
loopback (`5432` for Postgres, `9002` for the object store's S3 API).
```
python3 -m pip install -r requirements-dev.txt | cat
DATABASE_URL=postgresql://<query-user>:<password>@localhost:5432/robotvault \
S3_PARQUET_BUCKET=<bucket> \
S3_ENDPOINT=http://localhost:9002 \
AWS_ACCESS_KEY_ID=<key> \
AWS_SECRET_ACCESS_KEY=<secret> \
PYTHONPATH=phases/05-query-service/src \
  uvicorn query_service.main:app \
  --host 0.0.0.0 --port 8003
```

## Unit tests (local)
```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=phases/05-query-service/src \
  pytest -q phases/05-query-service/tests/unit | cat
```

Covers query double-counting, time-indexed cache correctness, the `bucket` param allowlist (SQL-injection guard), error-vs-empty handling on read endpoints, and the uploads-detail/files UI contract. No live Postgres/S3/Docker required — see the docstring in each test file under `phases/05-query-service/tests/unit` for how each one avoids live services.

The SPA has focused Vitest coverage, including PKCE callback state, session
storage, refresh single-flight, auth request boundaries, and sign-in/sign-out
controls:

```bash
# From the repository root; installs the locked npm dependencies in Docker,
# typechecks both SPAs, and runs every workspace's focused tests.
make ui-test
```

## Query hardening check (E2E)

`tests/e2e_query_hardening.py` checks these things against real services:

- DuckDB query connections read Parquet from the object store, spill to the temp
  directory, and refuse local files (for example `/run/secrets/...` and
  `/etc/passwd`), other S3 prefixes and buckets, HTTP URLs, extension
  installs, persistent secrets, and setting changes.
- API timestamps are UTC with a trailing `Z`, also for a TIMESTAMPTZ read in
  a Postgres session with `TimeZone=Europe/Berlin`.
- Under OIDC, `/api/obs/promql/range` refuses a viewer (403) and accepts an
  operator.

It starts nothing shared. Run it from the repository root:

```bash
N=rv-query-hardening-$$
OUT=$PWD/tmp/query-hardening && mkdir -p "$OUT"
docker network create $N
# Disposable object store from the first-party image (settings: development/objectstore/CONTRACT.md).
printf '%s' e2epass123 > "$OUT/root-password" && chmod 0644 "$OUT/root-password"
docker run -d --name $N-store --network $N --user 0 --tmpfs /data \
  -v "$OUT/root-password:/run/secrets/objectstore_root_password:ro" \
  -e RUSTFS_ACCESS_KEY=e2euser -e RUSTFS_SECRET_KEY_FILE=/run/secrets/objectstore_root_password \
  -e RUSTFS_VOLUMES=/data -e RUSTFS_ADDRESS=0.0.0.0:9000 -e RUSTFS_CONSOLE_ENABLE=false \
  robot-vault-objectstore:latest
until docker exec $N-store curl -fsS -o /dev/null http://127.0.0.1:9000/health/ready; do sleep 1; done
docker run -d --name $N-pg --network $N -e POSTGRES_PASSWORD=e2epass postgres:16.4-alpine
until docker exec $N-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
docker run --rm --network $N \
  -v $PWD/phases/05-query-service/src:/app/src:ro \
  -v $PWD/phases/05-query-service/tests:/app/tests:ro -v "$OUT":/report \
  -e PYTHONPATH=/app/src -e S3_ENDPOINT=http://$N-store:9000 \
  -e S3_PARQUET_BUCKET=robotics-data -e AWS_ACCESS_KEY_ID=e2euser \
  -e AWS_SECRET_ACCESS_KEY=e2epass123 \
  -e PG_DSN=postgresql://postgres:e2epass@$N-pg:5432/postgres \
  -e DUCKDB_TEMP_DIRECTORY=/var/lib/robot-vault/query -e DUCKDB_MEMORY_LIMIT_MB=64 \
  -e REPORT_PATH=/report/query-hardening.json \
  --entrypoint sh robot-vault-query:latest \
  -c 'mkdir -p /var/lib/robot-vault/query && python /app/tests/e2e_query_hardening.py'
docker rm -f $N-store $N-pg && docker network rm $N && rm -f "$OUT/root-password"
```

The command exits non-zero if a probe has the wrong result. The report
`tmp/query-hardening/query-hardening.json` lists every probe, its outcome,
and the DuckDB error text for each refusal.

## Notes
- Data endpoints resolve registered Parquet through Postgres, then read payloads
  from the object store. Real dependency failures are errors; only explicit
  unbootstrapped registries and genuinely absent data return empty results.
- Real OIDC browser login requires the HTTPS public-client inputs documented in
  `docs/user.md`; local compose deliberately uses the separate dev-token mode.
