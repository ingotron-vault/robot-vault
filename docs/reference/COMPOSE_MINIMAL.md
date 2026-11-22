# Minimal Compose for development

Use this page to start selected development services and check the local upload
pipeline. It is for developers with Docker Compose and a repository checkout.
Run commands from the repository root unless a step changes directory.

## Build and run

```bash
cd /path/to/robot-vault
docker compose build ingest robot processor query
docker compose up -d ingest robot processor query
# Seeding is manual, not automatic. To populate sample data for the UI:
#   docker compose --profile seed run --rm seed
# See "Local pipeline smoke test" below for a walkthrough.
```

Run `make ui-build` first if you also want the robot-side browser UI; its
generated SPA assets are not committed. The robot JSON API works without the
SPA and returns a clear 503 build instruction at the web root.

You can start either service independently. Compose still starts its declared
datastore and migration dependencies.

To start ingest:

```bash
cd /path/to/robot-vault
docker compose build ingest
docker compose up -d ingest
```
To start only the robot UI and API, run the command below. Uploads fail until
the configured ingest service is reachable.

```bash
cd /path/to/robot-vault
docker compose up -d robot
```

## Use the container
```
docker exec -it robot-vault-robot bash
rsm --help
rua --help
```

## Generate Python stubs
```
make proto
```

The target runs `grpc_tools.protoc` in the dev image and writes the generated,
gitignored stubs as the invoking user.

## Run the supported backend unit gate
```
make test-unit
```

The target runs the five phase-level Python suites (common configuration plus
phases 01, 02, 04 and 05), the example-bundle tests, and the database
migration, PostgreSQL-role-bootstrap, and object-store access tests. It creates an
isolated temporary PostgreSQL service for the live migration cases and removes
it afterward; a bare `pytest phases/**/tests/unit` command is only a subset.
See [docs/TESTING.md](../TESTING.md) for the full matrix (Python, Rust, browser,
release-contract, and end-to-end gates).

## Validate ingest is up (host)

The ingest admin port is not published to the host (see README
"Network boundaries"), and the ingest image has no `curl`. Run `make debug-up`
to publish the port on `127.0.0.1:8080`. Read upload records through the query
API.

```
cd /path/to/robot-vault
make debug-up
curl -fsS http://127.0.0.1:8080/healthz | cat
curl -fsS http://127.0.0.1:8080/metrics | head -n 10 | cat
RV_TOKEN="$(curl -fsS 'http://localhost:8003/auth/dev/token?roles=viewer' | jq -r .access_token)"
curl -fsS -H "Authorization: Bearer $RV_TOKEN" \
  "http://localhost:8003/api/v1/uploads/list?robot_id=demo&limit=5" | cat
# Query UI (published)
xdg-open http://localhost:8003/ui || echo "Open http://localhost:8003/ui"
```

## Local pipeline smoke test (real MCAP through the real services)

The query service reads Parquet only from the object store (S3 API). This test does
not write Parquet by hand and needs **no local installs beyond Docker**: it
runs `development/seed.sh` inside a container, which generates a tiny MCAP and
pushes it through the real `rsm scan` / `rsm hash` / `rua upload` path against
the live `ingest` service, then lets the real `processor` convert it to
Parquet in the object store before querying it back out.

```
cd /path/to/robot-vault
docker compose up -d ingest robot processor query

# Real seed path: generates a tiny MCAP, then rsm scan/hash + rua upload against ingest
docker compose --profile seed run --rm seed
# Manual equivalent, against an already-running robot container:
#   docker exec -it robot-vault-robot bash -lc "./development/seed.sh"

# Give the processor a few seconds to convert the upload to Parquet, then query it back
sleep 10
RV_TOKEN="$(curl -fsS 'http://localhost:8003/auth/dev/token?roles=viewer' | \
  docker compose exec -T query python -c \
  'import json,sys; print(json.load(sys.stdin)["access_token"])')"
curl -fsS -H "Authorization: Bearer $RV_TOKEN" \
  http://localhost:8003/api/robots | cat
curl -fsS -H "Authorization: Bearer $RV_TOKEN" \
  "http://localhost:8003/api/timeseries?robot_id=demo&topic=imu/linear_accel&bucket=minute" | cat
```

`/api/robots` reads the PostgreSQL robot registry and should list `"demo"` once
ingest has accepted the upload. `/api/timeseries` returns the processed seeded
`imu/linear_accel` points after the processor finishes.
