# Phase 03 — Run and test

Use this runbook to start ingest, send a test upload, and check its status.
Run commands from the repository root unless a `cd` is shown.

## Build (Docker)
```
docker compose build
```

## Run (Docker Compose)
```
docker compose up -d ingest robot
```

### TLS variant
`TLS_ENABLED`/`TLS_MTLS`/`TLS_SERVER_CERT`/`TLS_SERVER_KEY`/`TLS_CA_CERT` are passed
through to the `ingest` service by `docker-compose.yml`. Variable
reference: `docs/CONFIG_REFERENCE.md` (ingest TLS/mTLS).
```
make certs   # dockerized development/generate-dev-certs.sh; writes ./certs
TLS_ENABLED=true TLS_MTLS=true docker compose up -d ingest robot
docker exec -it robot-vault-robot bash -lc "rua upload --root /tmp/robot_data --config configs/vault.tls.local.json"
```

The release topology is stricter: it publishes only gRPC `50051`, requires
`INGEST_PUBLIC_HOST` plus individual server certificate/key/CA files, and
hard-enables TLS and bearer authentication. `INGEST_SECURITY_PROFILE` is
`token` (default) or `mtls`; ingest derives `TLS_MTLS` from it. The server
refuses either profile if any of those runtime controls is disabled.
Under `mtls`, each robot client leaf must additionally carry exactly one URI SAN:
`spiffe://robot-vault/robots/<percent-encoded-robot-id>`. For example, robot id
`rover-7` uses `subjectAltName=URI:spiffe://robot-vault/robots/rover-7` and the
certificate must be issued by `INGEST_TLS_CA_FILE`. The SAN must match the
robot's configured `robot_id`; CN-only client certificates are rejected under
`mtls`. A missing fleet-registry migration is never bypassed. The shared
`INGEST_AUTH_TOKEN` remains required.

## Local (Rust toolchain)
```
cd phases/03-ingest-server-rs
cargo run
```
Requires `cargo`/`rustc` >= 1.94 (matching the `rust:1.94-bullseye` image the Dockerfile builds with) and `protoc` (the protobuf compiler) on `PATH`. `build.rs` looks for a local `phases/03-ingest-server-rs/proto/upload.proto` first and falls back to the repo-root `proto/upload.proto` automatically, so `cargo build`/`cargo run` work from the crate directory without copying anything in — no local `proto/` subdirectory is checked into this crate. The server also needs `DATABASE_URL` set (and reachable Postgres/S3 for uploads to actually succeed); without `DATABASE_URL` it panics at startup.

CI and image builds use `cargo build/test --locked`; use `--locked` locally as
well when validating the committed dependency graph.

## Upload from agent
```
rua upload --root /tmp/robot_data/rover-7 --config configs/vault.local.json
```

### Test an interrupted upload

The agent and ingest server always use version-one resume. Ingest keeps
confirmed multipart parts across a disconnect or restart, and the next attempt
sends only missing parts.

Run these commands in order:

1. Start an upload from the robot container.
2. Restart ingest during the transfer.
3. Inspect the interrupted upload and run the agent again.

```
# Start upload in background from robot container
docker exec -d robot-vault-robot bash -lc "rua upload --root /tmp/robot_data/rover-7 --config configs/vault.local.json > /tmp/rua.log 2>&1"

# Interrupt mid-stream by restarting ingest
sleep 5 && docker restart robot-vault-ingest | cat
sleep 2

# Inspect the interrupted row (status "uploading" or "failed") in the query
# UI, or through the query API:
#   GET /api/v1/uploads/list?include_in_progress=true&limit=5

# Resume. The agent sends only parts ingest has not confirmed.
# An expired session requires a full-file retry.
docker exec -i robot-vault-robot bash -lc "rua upload --root /tmp/robot_data/rover-7 --config configs/vault.local.json | cat"
```

## Check admin

The admin port is **not published to the host** (reach it as
`ingest:8080` inside the compose network, or run `make debug-up` to bind it on
`127.0.0.1:8080`). `POST /admin/robots` requires
`authorization: Bearer $INGEST_AUTH_TOKEN`. The probe endpoints below are open.

- Health: GET `/healthz`
- Readiness: GET `/readyz` (requires both Postgres `SELECT 1` and an object-store
  write/delete probe). Missing S3 client/bucket configuration is unready (503),
  because raw uploads have no local-filesystem fallback and would otherwise
  fail with `s3_not_configured`.
- Metrics: GET `/metrics`
- Development robot registration (authenticated): POST
  `/admin/robots?robot_id=<id>&name=<name>&metadata=<json>`. Repeating the POST
  does not overwrite the existing name or metadata. Servers with
  `INGEST_SHARED_TOKEN_POLICY=deny` refuse it with `409`.
- Upload and robot listings: use the query service
  (`GET /api/v1/uploads/list`, `GET /api/v1/robots/list`).

Quick health check:
```
docker compose exec -T ingest curl -fsS http://localhost:8080/healthz | cat
```

Raw uploads are stored in the object store (S3 API) only (multipart for large files). There is no local-disk index; uploads/progress/manifests are persisted in Postgres. With `ENABLE_FILE_CAS=true`, each unary upload has its own manifest registry row/object even when multiple uploads share one content digest and CAS data key.

## Tests
- Rust unit tests live inline in `src/main.rs` (`#[cfg(test)] mod tests`) and cover the mandatory unary digest, immutable upload-id reservation, per-upload CAS manifest identity, release certificate URI identity, and completed-row-write gating. Durable resume planning is covered in `src/resumable.rs`; `tests/e2e/test_upload_resume_e2e.py` covers interrupted streaming uploads end to end. Run `cargo test --locked` from `phases/03-ingest-server-rs` (same toolchain prerequisites as `cargo run` above).
- The live migration contract is covered by `db/migrate_test.py`. It checks
  that the baseline schema applies idempotently, fails closed on renamed or
  missing migrations, serializes concurrent migrators and keeps one manifest
  row per upload while a content digest is shared. The supported repository gate starts
  an isolated Postgres automatically: `make test-unit`.
- Cross-phase integration: use `rua` to send small and large files; verify on admin endpoints.
