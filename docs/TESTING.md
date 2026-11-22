# Testing guide

Use this guide to choose and run Robot Vault's automated checks, then verify a
running stack by hand when needed. It is for developers and release operators
using Docker. The stack uses PostgreSQL, the RustFS object store, and Redis.
`.github/workflows/core-ci.yml` runs the core checks on GitHub-hosted runners
(see [Repository gates](#repository-gates)). A workflow
run is test evidence for its commit only. A workflow badge or the absence of a
failure is not evidence.

Pro adds its own test suites (see the Pro documentation). This guide covers
the core checks only.

The default source workflow runs on one Linux PC against its local Docker
daemon. A separate remote build host is not required. `make check`
runs the self-contained gates; `make ci-local` adds the disposable live GC
integration check. For the live application path, run `make stack-up`, then
`make test-e2e`, and finish with `make stack-down`. The development Compose
stack is loopback-bound; network-facing deployment uses the distinct release
topology and its TLS, authentication, protected environment, and accepted
versioned-image checks.

### Postgres-only ingest

The ingest and query services use PostgreSQL as the single source of truth for
uploads and progress (`bytes_received`). There is no SQLite upload index.

### Prerequisites
- Docker Engine with the Docker Compose plugin and GNU Make
- Linux, or macOS with Docker Desktop
- Run `make check` as a non-root user. Its packaging targets preserve that UID
  inside the pinned dev image, so tests can check the root refusal and never
  leave root-owned checkout files.

Every supported command below runs its language toolchain in a container. A
host Rust, Python, or Node installation is optional.

## Automated test suites

### Focused browser and upload-job E2E gates

```bash
make test-browser
make test-upload-jobs
```

Both commands are part of `make check`. The browser gate runs the real query
UI and robot UI in pinned Chromium with controlled API responses. It checks
UTC, a half-hour-offset time zone and New York time filters, switching robot roots while status is
pending, and retaining status after a failed refresh of the same robot. JSON
results, screenshots,
traces and server logs are saved under `tmp/browser-e2e/`. See the
[browser gate guide](../development/browser-e2e/README.md).

The upload-job gate runs the ordinary HTTP application, configuration loader,
queue, manifests and gRPC transport against a controlled remote service. A
held response reproduces overlapping jobs; it also checks completion, repeated
selection, remote rejection, partial success after a filesystem error and
replacement of a recording after upload. It uses an isolated robot-data tmpfs and saves
JSON results and the HTTP log under `tmp/upload-jobs-e2e/`. See the
[scenario inventory](../development/upload-jobs-e2e.md).

These focused checks do not replace the complete live data-path suite. Missing
browsers or failed assertions make the commands fail. The core CI workflow
runs the browser gate; `make check` remains the operative local command.

```bash
make test-objectstore-conformance CONFORMANCE_LABEL=run1
```

This separate gate starts the pinned RustFS image from
[`development/objectstore/CONTRACT.md`](../development/objectstore/CONTRACT.md)
in its own disposable Compose project. It checks the bootstrap (bucket and five
service users), every access policy, S3 client behavior, durability after a
crash and stale-upload expiry. A skipped or missing probe counts as a failure.
It takes about ten minutes. Run it before you change the object-store image or
a bucket policy. See the
[gate guide](../development/objectstore-conformance/README.md); the verdict is
`results/<label>/gate.json`.

### Hermetic robot-agent runtime suite

This suite is part of `make check` and uses the digest-pinned dev image built
from the root Dockerfile.

```bash
make test-robot-agent-runtime
```

`test-robot-agent-runtime` runs the complete `packaging/robot-agent/tests`
pytest collection. It first generates the protobuf stubs and verifies the
required Python modules and shell tools, then treats any pytest skip as a
failure. These are local, hermetic checks: they do not install anything on the host,
use host `/etc` or `/var`, or contact a live service.

The privileged dash/busybox no-bash fixture (`packaging/robot-agent/tests/run-in-container.sh`),
systemd installation, live enrollment, and live release fixtures are not local
host gates. Run them only in a verified disposable VM. `make check` also leaves
live-stack E2E, registry, mutation, and vulnerability database checks opt-in
as documented below.

`make test-observability-config` validates the Prometheus and Alertmanager
configuration, then runs the operational metrics collector tests in the same
dev image. It uses temporary filesystem fixtures and mocked database access;
it does not start the ops sidecar or connect to PostgreSQL.

### Unit tests (Python, Dockerized)

```bash
make test-unit
```

Runs the unit suites for the Python phases inside the `dev` container:
`phases/common-config/tests/unit`, `phases/01-robot-storage/tests/unit`,
`phases/02-robot-upload-agent/tests/unit`, `phases/04-processing-pipeline/tests/unit`,
`phases/05-query-service/tests/unit`.
The gate also runs the example-data tests and four standalone DB/release suites:
`development/example_data/tests`,
`db/test_migrate_unit.py`, live `db/migrate_test.py` against an isolated
PostgreSQL container, `development/release/test_postgres_role_bootstrap.py`,
and `development/release/test_objectstore_access.py`. The contract is that the command
exits zero; volatile pass counts belong in CI output or a dated review, not this
runbook.

Anything that must not regress belongs in an explicitly collected path.
Most `tests/integration/` files are not part of the unit gate; the live GC
reachability suite is the deliberate exception and runs in `make ci-local`
(`make test-gc-integration`). These
unit tests follow that rule: `test_s3_streaming.py` (the
processor must never buffer a whole S3 object), `test_sqlguard.py` and
`test_sql_identifier_allowlists.py` (the SQL console gate), `test_s3_credential_exposure.py` (the S3 credentials
must not be readable by a query), `test_upload_history.py` (history survives a
restart) and `test_netem_shaping.py` (the shaped-link harness must refuse to
run unshaped).

### Rust ingest tests

```bash
make test-rust
```

The ingest server (Phase 03) is Rust/Tonic and has its own `cargo test` suite; it is
not part of `make test-unit`. `make test-rust` runs it in the `rust-test` container
(the ingest image's `builder` stage, which already carries `protoc` and the fetched
crate deps), so no host Rust toolchain is needed; compiled artifacts are cached in the
`cargo_target` volume between runs.

If you do have a host toolchain and prefer it, `cd phases/03-ingest-server-rs && cargo
test` still works — it needs Rust ≥ 1.94 plus `protoc`, `pkg-config` and `libssl-dev`.

### Durable upload recovery

Durable upload recovery has a separate fault suite against disposable services:

```bash
INGEST_RESUME_BINARY=/absolute/path/to/ingest-server \
  PYTHONPATH=phases/common-config/src:phases/01-robot-storage/src:phases/02-robot-upload-agent/src \
  python3 -m pytest tests/e2e/test_upload_resume_e2e.py -v
```

Build the current ingest binary and regenerate Python stubs from
`proto/upload.proto` first. Docker must be available. This suite creates and
cleans up its own PostgreSQL, object-store and ingest containers, uses the release
service permissions, and restarts ingest and the client between attempts.
It checks transmitted bytes, sparse missing parts, lost completion responses,
session expiry, cleanup protection, and the final stored SHA-256. Without
`INGEST_RESUME_BINARY`, these isolated fault tests are skipped.

### Robot enrollment E2E

This test checks robot enrollment and per-robot credentials against real
services. Run it from the repository root on a host with Docker:

```bash
make test-robot-enrollment
```

The target builds the `dev`, `ingest`, `query`, `processor` and `objectstore`
images from the core Compose file only, then runs `tests/e2e/test_robot_enrollment_e2e.py` with `ROBOT_ENROLLMENT_E2E=1`.
The test starts its own unprivileged containers on a private Docker network:
PostgreSQL, the object store, ingest in the `token` security profile (TLS on,
shared token refused), query with the provisioning proxy, and the processor. It does
not use or change the development stack, and it removes its containers,
network and volumes when it ends. A run takes about one minute.

The test acts as an administrator through `/api/admin/robots` and as two
robots through the shipped `ingotron_agent`. It checks that:

- anonymous and viewer callers cannot use the admin API;
- each robot enrolls with a one-use code, and a used code is refused;
- the shared operator token cannot upload;
- robot A uploads a real MCAP, the stored object hash matches the source,
  and the processor writes Parquet topics for it;
- robot A's credential cannot upload as robot B or read robot B's uploads;
- a revoked credential and a disabled robot are refused within 30 seconds,
  robot B keeps working after robot A is revoked, and robot B works again
  after it is enabled.

Expected result: `1 passed`. The JSON report is in
`tmp/robot-enrollment-e2e/<run id>/report.json`, with a copy at
`tmp/robot-enrollment-e2e/latest.json`. Each check has an `id`, a `passed`
value and its evidence. The report holds no tokens or enrollment codes. If a
check fails, the report also holds the last log lines of each container.
Without `ROBOT_ENROLLMENT_E2E=1` the test is skipped, so `make test-e2e`
does not start containers.

`packaging/robot-agent/tests/live_enrollment_gate.sh` is an older,
robot-side check. It calls ingest's `/admin/v2` API directly in the `dev`
profile, and it checks the pin in the code and the agent's file layout.

### Backup and restore round trip

`development/backup-restore-e2e/` runs the real `make backup` and `make restore`
(`tools/robot_vault_backup.py`) against the real PostgreSQL and the RustFS
object store of the development stack. No application service is needed. The
test empties and restores the store and the database of the stack it runs on.
Run it only on a disposable stack, never on a stack you use:
`COMPOSE_PROJECT_NAME=<disposable project> sh development/backup-restore-e2e/run.sh <out dir>`.
`run.sh` refuses to start without `COMPOSE_PROJECT_NAME`; `e2e.py` is the test
itself. The runner builds the dev and toolkit images, then runs
`e2e.py` the given number of times (default 2). Each run has 15 cases:

- **Round trip.** A backup copied without file attributes to other storage; the
  database and the store data are deleted. The restore must give back every
  table, every object byte, every content type and all user metadata
  (including a three-part multipart object and an empty object).
- **Damage.** Wrong bytes of the same size, a metadata-only change, an extra
  object and an extra row. The restore repairs all of them and stops every
  service.
- **Refusals.** Truncated or missing files, a changed manifest, a duplicate
  key, an extra or missing object file, a same-size hash mismatch and a path
  that leaves `objects/`. Each must make the restore exit nonzero with the
  database and every object unchanged. A key that cannot be stored as a file
  fails the backup and publishes nothing.
- **Metrics.** `robot-vault-backup.prom` has exactly the three
  `robot_vault_backup_*` samples, with the right values after one good and
  one failed backup.

Report: `<out dir>/report-<n>.json`. The runner exits zero only when every
case of every run passes.

### Backend static quality

```bash
make backend-quality
```

This Dockerized gate runs the narrow product-source mypy contract from
`mypy-backend.ini`, then `cargo fmt --all -- --check` and strict Clippy
(`--locked --all-targets --all-features -- -D warnings`) for the ingest server. It is
included in `make check`; no host Python or Rust toolchain is required.

### End-to-end pipeline test

```bash
make test-e2e
```

`make test-e2e` invokes `pytest tests/e2e -q`, so it runs the complete current
collection rather than a hand-maintained test list. Live cases need
`make stack-up`. Local runs skip when the required
stack is absent; set `REQUIRE_LIVE_STACK=1` for a release evidence run so the
same preflight condition becomes a hard failure.
The separate `test_upload_resume_e2e.py` Docker fault cases require
`INGEST_RESUME_BINARY` and skip when it is unset; strict live-stack mode does
not turn that explicit opt-in into a false live-chain result. Report those
skips separately from the live upload and processing cases.

`tests/e2e/test_pipeline_e2e.py` drives an upload through the actual `rsm scan`
/ `rsm hash` / `rua upload` path inside the `dev` container (real gRPC to
`ingest:50051`, no mocks), then asserts:
- Postgres `uploads` row reaches `status='completed'` with `integrity_verified=true`,
  and recorded byte count / sha256 match the source file.
- The processor converts the upload (polling `processing_status`) and the query
  service reports the **exact** message count.

A second test in that file, `test_unauthenticated_calls_are_rejected`, checks
authentication: a gRPC upload with no token and with a wrong token must both fail
`UNAUTHENTICATED`, `POST /admin/robots` must answer `401` without the token and `200`
with it, and `/healthz` `/readyz` `/metrics` must stay open (the compose healthcheck and
Prometheus depend on that).

Pro adds its own end-to-end collection (see the Pro documentation). By default
the tests clean up every Postgres row,
object-store object, and local file it creates, scoped to
a unique `robot_id=e2e_test_<nonce>` so it can never collide with real fleet data.
`make test-e2e` requires the stack to already be built/running. Locally:

```bash
make stack-up
docker compose run --rm dev bash -c "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/e2e -v -s"
```

If running the test from *outside* Docker (services exposed on the host instead of
resolved by Docker DNS), publish the data plane first (`make debug-up`; by default only `8003` is
bound) and override the in-network defaults, e.g.
`QUERY_BASE_URL=http://localhost:8003 INGEST_ADMIN_URL=http://localhost:8080
INGEST_GRPC_TARGET=localhost:50051 DATABASE_URL=postgres://robotvault:robotvault@localhost:5432/robotvault`.
Also export the stack's `INGEST_AUTH_TOKEN` (see `.env`): the test authenticates its
gRPC and `/admin` calls with it. The auth case skips locally when the variable
is empty and fails under the strict `REQUIRE_LIVE_STACK=1` contract.

### Upload-detail publication E2E check

`tests/e2e/test_upload_detail_publication_e2e.py` uses real HTTP, PostgreSQL,
and the object store with unique disposable fixtures. It warms an empty Intake
detail response, publishes Parquet, and checks that the next response includes
the current rows and times. Pro adds export download checks (see the Pro
documentation).

To repeat the check after you start the source stack:

```bash
docker compose run --rm -e REQUIRE_LIVE_STACK=1 dev bash -c \
  "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/e2e/test_upload_detail_publication_e2e.py -q"
```

The fixtures remove their own database rows and storage objects. The test
writes its JSON result under `tmp/`.

### SPA build check

```bash
make ui-build
```

Builds the npm workspaces in `node:20-slim` — no host Node.
`@robot-vault/ui-kit` emits nothing because it has no build script and is
consumed as TypeScript source by the two SPAs. Two application bundles are the
expected output.

`make ui-test` runs every workspace's available typecheck and Vitest scripts in
the same digest-pinned Node container. The ui-kit suite covers focused
component behavior; both SPAs exercise the kit in their consuming contexts.
`make presubmit` also audits the npm dependencies of every independently
shipped web surface, through `make dep-audit`.

### GC reachability integration test

```bash
make test-gc-integration
```

Runs the real mark-and-sweep GC logic (`tools/s3_gc.py`) against a
disposable PostgreSQL + object-store project, asserting processed Parquet and the
objects of a running processing attempt survive while genuine
orphans are swept. The target creates and tears down its own isolated project,
database volume, and object-store volume; it never points at the normal
development data. It is not part of `make test-unit` or `make test-e2e`, so run
it explicitly after changing registry reachability or deletion policy.

### Simulation MCAP generator tests (not in the default gate)

```bash
docker compose run --rm dev pytest development/sim_gen/tests -q
```

`development/sim_gen` is the deterministic AMR simulation MCAP generator that
the extractor tests use. The suite checks
determinism (same seed → byte-identical file), episode/gap structure (N episodes, each
separated by >10 s of silence and internally free of any gap a 5 s segmenter would
split on), task-label markers, and that the `--bad-episode` sidecar describes defects
that are genuinely in the bytes.

**Deliberately not in the unit-gate paths above**: `sim_gen` is dev tooling under
`development/`, ships no runtime code, and adding it to the gate would mean the release
gate depends on Pillow/rosbags. Run it after touching the generator.

This suite cannot prove that the Common Data Representation (CDR) bytes decode
through the processor's schema-bound `mcap-ros2-support` path. Run that check in
the processor image:

```bash
docker compose run --rm --no-deps \
  -v $PWD/tmp_simgen:/fixtures -v $PWD/development:/development \
  processor python3 /development/sim_gen/verify_cdr.py \
    /fixtures/sim_sim-amr-01_seed42.mcap --sidecar /fixtures/sim_sim-amr-01_seed42.sim.json
```

See `development/sim_gen/README.md` for the CLI and the documented degradations.

### Repository gates

Run `make check` for every self-contained repository gate. It includes Python,
Rust, the root workspaces, documentation, and static release contracts. Run `make presubmit`
before a release candidate; it adds the source dependency audit
(`make dep-audit`, below), which covers the Python, Cargo and npm lock files. The live E2E, registry, mutation, and isolated GC
integration checks remain explicit because they need external state, a
finalized tagged source identity, or heavyweight runtimes.

The public workflow `.github/workflows/core-ci.yml` runs the core checks on
GitHub-hosted runners. Its jobs map to the local `make` targets above.

### Source dependency audit

```bash
make dep-audit
```

This gate checks the dependency lock files before a release candidate exists.
It finds a vulnerable pin while the fix is still a lock-file change. It needs Docker and
network access to the vulnerability databases. Nothing is installed on the
host.

| Lock files | Tool | Container |
|---|---|---|
| Every tracked `requirements*.lock*` file and `phases/03-ingest-server-rs/Cargo.lock` | Grype 0.116.1, directory mode | `anchore/grype:v0.116.1@sha256:1e71065c…` |
| Every tracked `package-lock.json`, development dependencies included | `npm audit --package-lock-only` | `NODE_CHECK_IMAGE` |

The Grype image holds a pinned binary (SHA-256 `a8fff88f…`). Grype's cataloguer skips npm development dependencies, so npm lock files go to
`npm audit` instead.

**Policy.** The gate fails if a High or Critical finding has a published fix,
unless `development/dep-audit/ignore.toml` has a current exception for it.
Findings below High, and findings without a fix, appear in the report but do
not fail the gate.

The gate also fails, with exit status 2, if it cannot trust a result:

- the Grype database is not valid or is older than 24 hours;
- Grype's exit status does not agree with its report, or the report shows
  changed policy settings;
- Grype did not read every package that a staged Python or Cargo lock file pins;
- `npm audit` returned an error, for example because the registry was
  unreachable;
- `ignore.toml` has an invalid entry.

A registry or database outage can therefore fail this gate. For that reason it
is in `make presubmit` and not in `make check`.

**Result.** The command prints a short summary and saves the evidence in
`tmp/dep-audit/run-<UTC time>/`. `tmp/dep-audit/latest` points to the last run.
Read `report.md` first. `report.json` has every finding.
`grype.json`, `inventory.cdx.json`, and `npm-audit/*.json` are the raw scanner
outputs, and `inputs/` holds the exact lock files that were scanned. The Grype
database cache is in `tmp/dep-audit/cache/`. The first run downloads the
database and takes about three minutes.

**If the gate fails on a finding:**

1. Bump the package in its lock file to the smallest fixed version in the
   report. For a Python lock, run the lock command from the `lock-python-*`
   Make target and add `-P <package>==<fixed version>`. For an npm lock, run
   `npm update <package> --package-lock-only` in `NODE_CHECK_IMAGE`. For
   `Cargo.lock`, run `cargo update -p <crate> --precise <version>`.
2. Run the test suites that use the package, and rebuild the images that
   install it.
3. Run `make dep-audit` again.

If no lock-file bump can fix the finding, or the vulnerable code cannot be
reached here, add an exception to `ignore.toml`. Each exception names the
advisory `id` and `package`, can limit itself to one `lock` file, and must have
a `reason`, a `reviewed` date, and an `expires` date at most 90 days later. An
expired exception stops working, so the finding fails the gate again. The
report warns about exceptions that no longer match a finding; remove them.

## Manual / exploratory verification

The steps below are for poking at a running stack by hand — they overlap with (and
are simpler versions of) what the automated suites above already check.

### Start server-side stack (auto bucket, health waits)
From the repo root:
```bash
make stack-up
```
Or see [README.md](../README.md) for the manual `docker compose up` equivalent.

### Robot data folder (host) and robot UI
Pick a host folder that contains your `.mcap` files (any subfolder layout is fine). Mount it in the robot container and start the robot UI:
```bash
ROBOT_DATA_HOST_DIR=/absolute/path/to/rosbags make robot-ui-start
# UI at http://localhost:8090
```

Upload via browser: open http://localhost:8090, select a folder with `.mcap`, click Upload.

**Example with a specific directory:**
```bash
export ROBOT_DATA_HOST_DIR=/absolute/path/to/rosbags
make robot-ui-start
```

### API smoke test (optional, no browser)
```bash
# List a few files
curl -fsS 'http://localhost:8090/api/files?root=/tmp/robot_data' | jq '.[0:5]'

# Start an upload (replace paths with entries from the list above)
JOB_ID=$(curl -sS -X POST http://localhost:8090/api/upload/start \
  -H 'Content-Type: application/json' \
  -d '{"root":"/tmp/robot_data","files":["/tmp/robot_data/<robot-id>/<date>/file1.mcap","/tmp/robot_data/<robot-id>/<date>/file2.mcap"]}' \
  | jq -r .job_id) && echo "JOB_ID=$JOB_ID"

# Poll until completed
for i in $(seq 1 40); do \
  sleep 0.25; \
  curl -sS http://localhost:8090/api/upload/jobs/$JOB_ID | jq -c .; \
  status=$(curl -sS http://localhost:8090/api/upload/jobs/$JOB_ID | jq -r .status); \
  [ "$status" = "completed" ] && break; \
done
```

### Verify uploads (API and UI)
```bash
curl -sf 'http://localhost:8003/api/v1/uploads/list?limit=10&include_in_progress=true' \
  | jq -r '.page.total_count, (.items[]? | [.upload_id,.status,.original_filename] | @tsv)'
# UI uploads page: http://localhost:8003/ui/uploads
```

### Upload performance / stress test

Generate and upload a larger test file to sanity-check throughput and multipart chunking:
```bash
# Generate small test file via the seed script
docker compose exec robot bash -c "cd /app && SEED_ROOT=/tmp/robot_data/demo ./development/seed.sh"

# Generate a larger file (>10MB) directly
docker compose exec robot bash -c "cd /tmp/robot_data && mkdir -p stress_test && cd stress_test && python3 -c '
from mcap.writer import Writer
import json, time, os
output_file = \"stress_test_15mb.mcap\"
with open(output_file, \"wb\") as f:
    w = Writer(f)
    w.start()
    sid = w.register_schema(name=\"json\", encoding=\"json\", data=b\"{}\")
    cid = w.register_channel(schema_id=sid, topic=\"/test/data\", message_encoding=\"json\")
    now = int(time.time()*1e9)
    for i in range(50000):
        data = json.dumps({\"id\": i, \"data\": \"x\" * 300}).encode()
        w.add_message(channel_id=cid, log_time=now+i, publish_time=now+i, sequence=i, data=data)
    w.finish()
print(f\"Created {output_file}: {os.path.getsize(output_file)/1024/1024:.1f}MB\")
'"

# Upload it
docker compose exec robot bash -c "cd /app && rsm scan --config configs/vault.local.json --root /tmp/robot_data/stress_test"
docker compose exec robot bash -c "cd /app && rsm hash --config configs/vault.local.json --root /tmp/robot_data/stress_test"
docker compose exec robot bash -c "cd /app && time rua upload --config configs/vault.local.json --root /tmp/robot_data/stress_test"
```

Check results:
```bash
# List recent uploads
curl -sf 'http://localhost:8003/api/v1/uploads/list?limit=10' | jq -r '.items[]? | [.original_filename,.status,.robot_id,(.file_size_bytes/1024/1024|floor)] | @tsv'

# Open web UI
open http://localhost:8003/ui/uploads

# List the bucket with rclone from the toolkit image (`make build-toolkit`).
# The object store is not published to the host, so join the compose
# network — substitute your project's network name (`docker network ls`).
# The root password goes in by variable name, never on the command line.
export RCLONE_CONFIG_RV_SECRET_ACCESS_KEY="$(cat configs/objectstore-root-password.dev)"
docker run --rm --network robot-vault_default -e RCLONE_CONFIG_RV_SECRET_ACCESS_KEY \
  -e RCLONE_CONFIG_RV_TYPE=s3 -e RCLONE_CONFIG_RV_PROVIDER=Other \
  -e RCLONE_CONFIG_RV_ENDPOINT=http://objectstore:9000 -e RCLONE_CONFIG_RV_FORCE_PATH_STYLE=true \
  -e RCLONE_CONFIG_RV_ACCESS_KEY_ID=robot-vault-dev-root \
  --entrypoint rclone robot-vault-toolkit:latest ls rv:robotics-data
```

### Configuration notes

`chunk_size_bytes` and `bandwidth_mbps` are documented authoritatively in the
[phase 02 usage guide](../phases/02-robot-upload-agent/docs/usage.md). Two
consequences matter when testing: a `chunk_size_bytes` outside the allowed range
is a configuration error, and a nonzero `bandwidth_mbps` is a
*real* client-side pacing cap — setting it to `50` throttles the agent to
6.25 MiB/s, which will make upload tests look broken.

Progress is computed from `bytes_received / file_size_bytes` in Postgres. The UI polls every 10s.

### Frozen robot-agent release gate

Run `packaging/robot-agent/tests/check_tarball_determinism.sh` to check the
frozen agent's artifact boundary. It performs two builds with the pinned Debian 11 PyInstaller
toolchain and requires identical tar bytes, then proves the bundle has no
Python source or host-Python fallback and exercises its imports and systemd
wrappers in clean pinned Debian 11 and Ubuntu 20.04 containers.

### Troubleshooting
- Bucket missing (NoSuchBucket): `objectstore-access-ready` should create it; `docker compose up -d --force-recreate objectstore-access-ready` to retry.
- Permission denied on host data dir: fix host permissions for the path passed via `ROBOT_DATA_HOST_DIR`.
- Selected files warning in UI: set `ROBOT_DATA_HOST_DIR` to the correct host folder and restart `robot`.
- Host vs container path: the Robot API expects the container path `/tmp/robot_data` for `root`. Using a host path will return an empty list.
- Robot UI job hangs: jobs are marked failed on backend exceptions; check `docker logs robot-vault-robot` and `robot-vault-ingest`.
- Agent refuses its config with a `chunk_size_bytes` error: the value is outside the allowed range. Values below 5242880 (5 MiB) are rejected.
- Uploads suddenly crawl at a few MiB/s: check `bandwidth_mbps`. It is a real pacing cap; set it to `0` for unlimited.
- Full reset: `make stack-reset CONFIRM=1` then `make stack-up` to start clean (see [OPERATIONS.md](OPERATIONS.md) to back up first).
