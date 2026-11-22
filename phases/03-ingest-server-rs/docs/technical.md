# Phase 03 — Ingest server technical guide

## Purpose in the whole system

This guide is for developers and operators working on the Rust ingest server.
It describes the upload protocol, storage, identity checks, and admin endpoints.

- Receives files from robots (Phase 2 agent) over gRPC.
- Verifies integrity (SHA256), writes raw files to storage, and acknowledges completion.
- Enforces fairness and concurrency limits per robot and globally.
- Exposes health, readiness and metrics endpoints, the robot provisioning
  API, and a development robot-registration route.

## Protocol
- Uses `proto/upload.proto`. Streaming uploads require `resume_version=1`; the server rejects any other value with `INVALID_ARGUMENT`.
- RPCs (`UploadService`):
  - `UploadSmall(UploadSmallRequest) → UploadResponse` (unary, whole file in one message)
  - `Upload(stream UploadRequest) → UploadResponse` (client streaming, chunked; S3 multipart under the hood)
  - `GetUploadStatus`, `ListUploads` (read from Postgres `uploads`)
  - `ResumeUpload` returns a durable missing-part plan. It accepts only `resume_version=1`. See Durable partial resume below.
  - `HealthCheck(…)`
- gRPC listens on `:50051`; a separate admin HTTP server (axum) listens on `:8080` (see Admin endpoints).

## Durable partial resume

The tables `upload_multipart_sessions` and `upload_multipart_parts` hold
durable multipart state. An authenticated version-one `ResumeUpload` reply
contains the session token, pinned chunk size, expiry, zero-based missing chunk
numbers, and the sum of bytes in committed receipts. A fresh session has an empty
token and zero bytes. A completed upload returns its full file size and accepts
a metadata-only `Upload` for immutable-identity validation and completion replay.

The client echoes version, token and chunk size in `UploadMetadata`, then sends
only missing chunks in ascending original-number order with their original
offsets. `is_final` marks the last chunk transmitted in this attempt. If all
parts already have receipts, metadata alone can finish the upload. Exact
hash-matched duplicates that committed after the resume probe are skipped.
Changed tokens or incompatible plans return retryable `ABORTED`.

S3 part writes retain the bounded pipeline. Each part's ETag, size, offset and
SHA-256 enter Postgres only after S3 confirms success. Successful parts after a
hole survive disconnects and process restarts. The upload advisory lock and an
attempt generation guard serialize claims and receipt writes across replicas.
A new storage session gets a distinct raw key containing its UUID, published
atomically with the session row in `uploads.storage_path`. Existing sessions
retain their key. Delayed storage requests from an expired session cannot touch
its replacement, and completed upload paths remain immutable.

Finalization persists a `finalizing` marker, assembles the recorded ETags, then
streams the assembled object through a SHA-256 check with a 256 KiB buffer.
Only a full byte-count and digest match permits the completed upload row. This
adds a storage-side read, without retransmitting previously accepted robot
bytes. The object's session metadata allows retry after a crash between S3
completion and the database commit. Missing storage MPUs reset the session and
request a fresh plan; they do not retry an unusable token indefinitely.

A session expires after seven days without a stored part or a valid claim.
It also expires 14 days after it was created, whatever its activity. Object
storage counts the age of an incomplete multipart upload from its creation, so
the 14-day limit lets a storage-side expiry stay finite. The `objectstore`
service sets `RUSTFS_API_STALE_UPLOADS_EXPIRY` to `360h` (15 days), which is
more than 14 days plus one sweep interval. The RustFS default of `24h` removes the parts of any
session older than one day. A cleanup loop starts with the server and checks up to 32
sessions every 15 minutes. It takes the same advisory lock, marks cleanup as
`expiring`, and removes only session-owned incomplete artifacts. One failed
cleanup is logged without preventing the rest of that batch. Registered sessions
remain protected from the separate GC until cleanup succeeds.

S3 operations and individual verification-read stalls have a 120-second timeout.
These expiry and S3 timeout values are fixed defaults, with no new environment
settings. The existing metadata and stream-idle timeouts still apply. Release
ingest permissions include reading raw objects for verification, deleting
session-owned expired objects, and `ListBucket` on the one configured bucket so
HEAD can distinguish absent objects from permission failures. The server does
not treat an arbitrary 403 as proof that an object is absent.

## Storage
- Raw upload bytes go to the object store (S3 API) only — there is no local-disk write path. `UploadSmall` does a single `PutObject`; `Upload` streams chunks into an S3 multipart upload (one part per chunk) and completes it once the stream ends.
- **Part upload is pipelined.** Chunks are received and validated one at a time, but the `upload_part` call for an already-validated chunk runs in a bounded task set instead of blocking the receive loop, so the S3 round-trip overlaps the next chunk's arrival. Streaming chunks are accepted only in `[5 MiB, 8 MiB]`; the service-wide 50 MiB decoder limit remains larger solely so `UploadSmall` can retain its separate 48 MiB file threshold. `MAX_INFLIGHT_PARTS` (default `4`) bounds the retained S3 task depth per stream; reaching it stops the server reading until a part lands. A process-wide admission gate shared by all connections limits `Upload` and `UploadSmall` before protobuf decoding. Consequently, the enforced retained-payload bound is
`GLOBAL_CONCURRENCY x (MAX_INFLIGHT_PARTS x 8 MiB + 50 MiB)`: accepted S3 parts plus one current tonic-decoded message that can exist before streaming validation. Startup rejects any combination above 1 GiB. The defaults retain at most `8 x (4 x 8 MiB + 50 MiB) = 656 MiB`; the remaining 1 GiB of the 2 GiB release container is reserved for protobuf copies, gRPC, TLS, the AWS SDK, Postgres, task stacks, allocator overhead, and other process memory. `CompleteMultipartUpload` runs only when every part has a committed receipt, and the assembled object is then re-hashed (see Durable partial resume). Measured on one development host (500 MiB, 5 MiB parts, 100 parts, single stream, loopback, no TLS): **6.49 s → 2.67 s wall clock, 77 → 187.5 MiB/s** (~2.4x; medians of 3 trials, 2.49-2.80 s). A later re-run measured 2.40 s / 208 MiB/s but another workload was active on that host during it, so the slower, quiet-run figure is the one quoted here.
- The `STORAGE_ROOT` env var (default `/app/uploads`) is not a local storage location for upload data; it is only used to derive the S3 object key layout:
```
raw/mcap/{robot_id}/{YYYY}/{MM}/{DD}/{upload_id}.{ext}
# Durable session keys append the session UUID before the extension:
raw/mcap/{robot_id}/{YYYY}/{MM}/{DD}/{upload_id}-{session_id}.{ext}
```
- When `ENABLE_FILE_CAS=true`, `UploadSmall` instead writes to the
  content-addressed key `cas/files/{sha256}` and writes a JSON manifest to
  `manifests/{robot_id}/{upload_id}.json`. Distinct uploads of identical bytes
  share the CAS data key but each owns a separate `manifests` row and JSON
  object; `file_sha256` is an indexed lookup value, not the row
  identity. A first write re-PUTs its verified unary bytes to confirm the CAS
  object rather than treating a successful HEAD as content verification. `Upload`
  (the streaming RPC) does not use CAS — it always writes to the raw per-upload
  key.

## Security
- TLS via rustls; optional mTLS (client cert validation). Only the gRPC listener is wrapped — the admin/metrics HTTP port stays plaintext.
- Enabled/paths are env-var driven: `TLS_ENABLED`, `TLS_MTLS`, `TLS_SERVER_CERT`, `TLS_SERVER_KEY`, `TLS_CA_CERT` (see Config below); `docker-compose.yml` passes all five through to the container, and `make certs` generates the dev CA/server/client certs. See the [TLS walkthrough](run-and-test.md#tls-variant).
- Shared-token auth (`INGEST_AUTH_TOKEN`): a tonic interceptor in front of `UploadServiceServer` and an axum middleware on `POST /admin/robots` both require `authorization: Bearer <token>` (the `Bearer ` scheme is optional and case-insensitive), rejecting with `UNAUTHENTICATED` / `401` and counting `ingest_auth_rejected_total{reason=missing|invalid}`. The comparison is constant-time. `/healthz`, `/readyz`, `/metrics` and the gRPC health + reflection services are deliberately unauthenticated (no upload data; the compose healthcheck and Prometheus poll them). An unset/empty `INGEST_AUTH_TOKEN` disables the check and logs a warning at startup, so upgrading the image cannot silently start rejecting a fleet that has no token configured.
- Host exposure: `docker-compose.yml` publishes neither `50051` nor `8080`; `deploy/docker-compose.debug.yml` (`make debug-up`) is the explicit, loopback-only opt-in.
- Enrollment rate limit (`RobotEnrollment.Enroll`): each source may make 5
  attempts per minute with a burst of 10, and the server accepts 30 per minute
  in total. Ten failures inside 5 minutes block a source for 15 minutes. One
  IPv6 /64 counts as one source; an IPv4-mapped IPv6 peer counts as its IPv4
  address. The server tracks at most 4096 sources and removes idle ones every
  minute. When the table is full of active sources, a new source gets
  `RESOURCE_EXHAUSTED` and
  `ingest_enrollment_rejected_total{reason="rate_limited_source_table_full"}`.
- Release exception: `deploy/docker-compose.release.yml` publishes gRPC `50051`
  only, hard-sets TLS and requires a non-development token. Its
  `INGEST_SECURITY_PROFILE` is `token` by default; the operator can choose
  `mtls`. It mounts only the ingest server certificate/key and the ingest CA
  certificate (`INGEST_TLS_CA_FILE`, exactly one root certificate), never the
  whole development cert directory. Startup rejects mTLS-without-TLS in every
  mode and rejects any missing release control.
- **CA pin:** ingest mints the pin in each enrollment code from `TLS_CA_CERT`,
  the CA that issued its server certificate. The query service serves the same
  host file at `GET /ca.crt` (`INGEST_CA_CERT_FILE`).
- **Per-robot identity under `mtls`:** a client leaf must contain exactly one
  URI SAN of the form
  `spiffe://robot-vault/robots/<percent-encoded-robot-id>`. The server parses the
  authenticated leaf certificate and requires that decoded id to equal
  `UploadMetadata.robot_id` before a registry lookup or write. A missing,
  malformed, ambiguous or cross-robot SAN is rejected; certificate CN is never
  used as a fallback. Robot ids cannot contain `/`, `\`, or ASCII control
  characters and cannot be the exact path segments `.` or `..`; benign embedded
  dots such as `arm.left.02` remain valid. A missing `robots` table is also a
  closed gate, not a compatibility bypass. The bearer token remains an additional
  independent check.
  Outside `INGEST_SECURITY_PROFILE=mtls`, the SAN binding is not required.
  Under `token` each robot authenticates with its own `ignr1.` bearer
  credential from enrollment; development certificates and plaintext local
  workflows also work.

## Integrity Enforcement
An upload row becomes `completed` only after these checks pass:
- **Final SHA256:** streaming `Upload` and `UploadSmall` both require a valid
  64-hex `expected_sha256` in every security profile. A missing, malformed or
  mismatched digest is rejected with `INVALID_ARGUMENT` before any S3 or
  Postgres side effect for `UploadSmall`, and before reservation for `Upload`.
- **Streaming chunk shape:** chunk numbers must ascend and match the session's
  pinned part boundaries (offset and size). The last chunk transmitted in an
  attempt must carry `is_final=true`. A chunk number whose S3 part number would
  exceed the 10000-part cap is rejected.
- **Per-chunk hash:** if `chunk_hash` is provided, the server validates it and
  rejects mismatched bytes. The server always computes each part's SHA-256 and
  stores it with the part receipt in `upload_multipart_parts`.
- **Assembled object check:** after `CompleteMultipartUpload`, the server reads
  the assembled object back and requires its size and SHA-256 to match the
  declared values.
- **Immutable idempotency reservation:** before `PutObject` or
  `CreateMultipartUpload`, Postgres durably reserves
  `(client_upload_id, robot_id, filename, size, expected_sha256) and its initial storage path`.
  A session advisory lock keyed by `client_upload_id` is held through S3 and the
  terminal row write across ingest replicas; a concurrent attempt gets
  `ABORTED` and retries instead of waiting without a bound. Conflicting metadata
  fails before object I/O. An exact replay of a completed upload returns the
  recorded response without repeating object I/O. An exact retry of a
  failed/crashed attempt reuses the current session's path, even on a later
  date. Expired or reset durable sessions get a new key.
- **Integrity gate and basis** (the baseline schema): ingest always writes
  `integrity_verified=true` with
  `integrity_verification_basis='client_sha256'`. The processor claims only
  rows whose gate is true.
- **Completed-row write gating**: the RPC only returns `completed` after the
  Postgres `uploads` row write is confirmed. In CAS mode, completion is later
  still: the reserved upload first roots the CAS key, then ingest writes the
  data object, inserts/validates the per-upload `manifests` row, writes that
  row's JSON object, and only then transitions the upload to `completed`.
  Manifest database and S3 failures are returned as stable internal errors;
  none are logged-and-ignored. Registering the manifest path before its object
  means a crash cannot leave an unregistered manifest object for GC to delete,
  while an exact retry fills any registered-but-not-yet-written object. If the
  terminal upload-row write fails, the RPC returns `Status::internal` so the
  client retries instead of accepting a partial publication.
- **Streaming failures keep durable parts:** a failed or interrupted streaming
  attempt marks its session failed but keeps committed part receipts, so the
  next attempt sends only missing parts. Expired sessions are cleaned up as
  described in Durable partial resume.

## Concurrency/Fairness
- Global semaphore for total concurrent uploads.
- Per-robot semaphore to prevent one robot from monopolizing.

## Admin endpoints (HTTP, port 8080)
- `/healthz`: liveness (always 200 OK)
- `/readyz`: readiness — writes and deletes a unique
  `_health/ingest/<uuid>` S3 object when S3 is configured and always executes
  Postgres `SELECT 1`; returns 503 when a configured dependency fails. Bucket
  creation and access-policy administration belong to the release one-shot
  bootstrap, so the long-running ingest identity does not need `CreateBucket`.
  Its `ListBucket` permission supports accurate missing-object detection during
  durable upload recovery. Missing S3 client/bucket configuration returns 503 rather
  than claiming readiness: raw upload RPCs have no local-filesystem fallback
  and would fail with `s3_not_configured`. Used as the Docker
  healthcheck for `ingest`.
- `/metrics`: Prometheus metrics exporter
  - Counters: `uploads_completed{method,robot_id?}`, `upload_bytes_total{method,robot_id?}`, `completed_row_write_failures_total{method,robot_id?}`, `ingest_auth_rejected_total{reason}`, `ingest_robot_rejected_total{reason}`, `ingest_robot_identity_rejected_total{reason}`, `ingest_upload_reservation_rejected_total{reason}`, and `ingest_upload_idempotent_replays_total{method}`.
  - Histograms: `ingest_write_seconds{method}`, `ingest_part_upload_seconds` (one S3 `upload_part`; with pipelining these overlap each other, so their sum exceeds the wall clock of the upload)
  - Gauges: `ingest_inflight_uploads` — sampled from the global concurrency semaphore when the endpoint is scraped, rather than tracked with an increment/decrement pair that an early RPC return could leak.
  - The `robot_id` label is only attached when `METRICS_INCLUDE_ROBOT_ID=true` (off by default, to bound cardinality).
  - Earlier builds answered `200` with a **0-byte body** after any amount of real traffic. Two causes, both required: (1) `Cargo.toml` pinned `metrics = "0.21"` while `metrics-exporter-prometheus = "0.14"` depends on `metrics 0.22`, so the binary held two independent global recorders — `install_recorder()` filled the exporter's while every `counter!`/`histogram!` in the crate wrote to the other, still-no-op one; (2) the `describe_*` calls ran *before* the recorder was installed, so the descriptions were handed to the no-op sink and lost. Keep the two `metrics` crate versions in step, and keep `install_recorder()` above the `describe_*` block.
- `POST /admin/robots`: registers a development robot from the `robot_id`
  query parameter, with optional `name` and JSON `metadata`. The row is
  `state='enrolled'`, `auth_mode='shared'`, so it uploads with the shared
  token. Registration is idempotent and does not overwrite an existing row.
  It needs `INGEST_AUTH_TOKEN` and returns `409 shared_credentials_denied`
  when `INGEST_SHARED_TOKEN_POLICY=deny` (`token` and `mtls`).
- `/admin/v2/robots*`: the provisioning API for robots with their own
  credential. Only `INGEST_PROVISIONING_TOKEN` authorizes it.
- Upload lists, upload files and the robot registry are served by the query
  service, not by ingest.

## Config (environment variables)
The server is configured entirely through environment variables (see `configs/ingest.env`, wired into the `ingest` service via `env_file` in `docker-compose.yml`). It does **not** read `configs/vault.json` — that file is the *client-side* config consumed by the Phase 2 `rua` agent to find the server's host/port/TLS settings.
- `DATABASE_URL` (required): Postgres connection string; the process panics at
  startup if unset. Release injects only the scoped `robot_vault_ingest` role,
  never the migration owner.
- `STORAGE_ROOT` (default `/app/uploads`): used only to derive the S3 object key layout (see Storage above), not an actual write path.
- `S3_BUCKET`, `S3_ENDPOINT`, `S3_REGION` (default `us-east-1`): target
  object-store bucket. Release injects the fixed `robot-vault-ingest` access id and
  its scoped secret; it can write raw/CAS/manifest objects and the health probe
  and can read/delete raw upload objects for durable verification and cleanup.
  `ListBucket` is limited to the configured bucket; it cannot administer buckets.
- `PER_ROBOT_CONCURRENCY` (default `2`, valid `1..=16`),
  `GLOBAL_CONCURRENCY` (default `8`, valid `1..=64`): semaphore limits (see
  Concurrency/Fairness). Invalid, zero, and over-limit values fail startup. A
  value within this individual range can still fail the combined retained-payload
  startup check described under Storage.
- `MAX_INFLIGHT_PARTS` (default `4`, clamped to `1..=32`): S3 part uploads in flight per streaming upload (see Storage). Raising it deepens the pipeline and raises the retained-payload bound. After clamping, the server requires
  `GLOBAL_CONCURRENCY x (MAX_INFLIGHT_PARTS x 8 MiB + 50 MiB) <= 1 GiB`; a
  value of `1` restores fully serial part upload.
- `INGEST_FIRST_MESSAGE_TIMEOUT_SECONDS` defaults to 10 seconds and limits the
  wait for upload metadata. `INGEST_STREAM_IDLE_TIMEOUT_SECONDS` defaults to 600
  seconds and limits gaps between subsequent client messages. Both must be
  finite, positive, and no greater than 3600 seconds.
- `ENABLE_FILE_CAS` (built-in default `false`; the development
  `configs/ingest.env` sets `true`): enables content-addressed storage for
  `UploadSmall` (see Storage above).
- `METRICS_INCLUDE_ROBOT_ID` (default `false`): attach `robot_id` as a metrics label.
- `TLS_ENABLED`, `TLS_MTLS`, `TLS_SERVER_CERT` (default `/app/certs/ingest.crt`), `TLS_SERVER_KEY` (default `/app/certs/ingest.key`), `TLS_CA_CERT` (default `/app/certs/ca.crt`): TLS/mTLS configuration (see Security).
- `INGEST_AUTH_TOKEN` (default unset = auth disabled + startup warning): shared bearer token for the upload RPCs and `POST /admin/robots` (see Security).
- `INGEST_SECURITY_PROFILE` (default `dev`; also `token` or `mtls`; the release
  stack defaults to `token`): selects the startup checks. `token` requires TLS
  and sets `TLS_MTLS=false`; `mtls` requires TLS, sets `TLS_MTLS=true` and
  requires a certificate identity for each robot. Under these two profiles a
  `TLS_MTLS` value that contradicts the profile stops startup; only `dev` reads
  `TLS_MTLS`. Both require `INGEST_SHARED_TOKEN_POLICY=deny` and two different
  secrets of at least 32 visible ASCII characters.
  Ingest does not read `ROBOT_VAULT_RELEASE`.
- `INGEST_PUBLIC_HOST` and `INGEST_PUBLIC_PORT` (default: the gRPC listen port):
  the `host:port` returned to robots as `vault_ingest_endpoint` and
  `ingest_endpoint`. The release stack sets the port to the published
  `INGEST_GRPC_PORT`. A port outside 1-65535 stops startup.

## Acceptance Criteria
- Phase 2 client uploads small and large files successfully; final SHA256 computed and returned.
- Concurrency caps enforced under load (unit/integration tests).
- Probe endpoints return health, readiness and metrics.

## Implementation Notes
- Crate: `phases/03-ingest-server-rs` with tonic build in `build.rs`. `build.rs` looks for `phases/03-ingest-server-rs/proto/upload.proto` first and falls back to the repo-root `proto/upload.proto` if that local copy doesn't exist — so `cargo build`/`cargo run` work directly from the crate directory (no manual proto copying) as long as `protoc` is on `PATH`.
- Docker build (`phases/03-ingest-server-rs/Dockerfile`) is multi-stage: a pinned `rust:1.94-bullseye` builder installs `protobuf-compiler`/`libprotobuf-dev`, copies the tracked `Cargo.lock` plus the repo-root proto, and requires both cache and final builds to succeed with `cargo build --release --locked`. Bullseye's older GNU/glibc build output is compatible with the scanned `ubuntu:24.04` runtime image. Its Docker healthcheck calls the binary's built-in loopback `/readyz` probe, so the runtime does not carry curl solely for health checks.
- Dependencies: `tonic`, `prost`, `tokio`, `axum`, `tracing`, `metrics` (+ prometheus exporter), `rustls` (+ pemfile), `deadpool-postgres`, `tokio-postgres`.
- The server is Postgres-only: there is no local SQLite index. Uploads, chunk
  progress, capture metadata, immutable expected-digest reservations, and CAS
  manifests, durable multipart sessions and accepted-part receipts persist in
  the tables of `db/migrations/001_baseline.sql`; see `docs/DB_SCHEMA.md` for
  the exact contract.
