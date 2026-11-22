# Configuration reference

Use this reference to configure the services shipped in this repository. It is
for operators and developers who need a setting's source, default, or effect.
It covers the Prometheus scrape jobs in `configs/prometheus.yml`, the
`configs/*.env` files loaded by Docker Compose, and the environment variables
read by the services. The entries were checked against source. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the data flow and service boundaries.

The example bundle has its own operational guide:
[`docs/EXAMPLES.md`](EXAMPLES.md). It documents the `make examples` source
cache and output controls plus `make seed-examples` runner/config overrides;
those Make variables are command inputs, not service environment variables.

## Prometheus jobs (metrics scraping)

`configs/prometheus.yml`, mounted read-only into the `prometheus` compose service
(profile `obs`):

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'ingest'
    static_configs: [{ targets: ['robot-vault-ingest:8080'] }]
    metrics_path: '/metrics'
    scrape_interval: 10s

  - job_name: 'query'
    static_configs: [{ targets: ['robot-vault-query:8003'] }]
    metrics_path: '/metrics'

  - job_name: 'processor'
    static_configs: [{ targets: ['robot-vault-processor:8000'] }]
    metrics_path: '/metrics'

  - job_name: 'gc'
    static_configs: [{ targets: ['robot-vault-gc:9108'] }]

  - job_name: 'exporter'   # Pro only
    static_configs: [{ targets: ['robot-vault-exporter:8001'] }]
```

Those are the five development scrape targets. Pro adds the exporter service;
a core stack has no exporter, so its target stays down. Release does not mount this
development file: its `obs` service generates service-DNS targets at startup,
uses verified HTTPS plus the explicit `QUERY_TLS_CA_FILE`/
`QUERY_PUBLIC_HOST` trust pair for query, and includes exporter only in `pro`.
The release profile also scrapes internal `ops-metrics:9110`.

### Optional release operational signals

The release `obs` profile uses `OPS_DATABASE_URL` for a separate
`robot_vault_ops` read-only login. `ops-roles-ready` bootstraps that role and
the collector reads only the authoritative upload, processing, and export
tables. `BACKUP_METRICS_DIR` (default
`/var/lib/robot-vault/backup-metrics`) mounts only the dedicated backup textfile
directory read-only. `make backup` writes `robot-vault-backup.prom` there (three
samples; see [Backup and restore](OPERATIONS.md#backup-and-restore)), and `make`
does not read the variable from the release env file, so pass the same value to
both; storage and spool volumes are also read-only. The
collector reads the public certificate chain without a private-key mount.
`PROCESSOR_MAX_ATTEMPTS` and `EXPORTER_MAX_ATTEMPTS` must match the worker
settings so backlog age uses the same retry eligibility.

`ALERTMANAGER_ENDPOINT` is optional. An empty value disables forwarding;
otherwise it must be `http(s)://hostname:port` without credentials, a path,
or query. HTTPS verifies the server certificate normally. HTTP is suitable
only on a trusted private network. Configure and test an operator-owned
receiver before treating a firing alert as a notification. See the
[release operational signals runbook](OPERATIONS.md#release-operational-signals)
for source-failure semantics and startup commands.

## Host port publishing (`docker-compose.yml` + `deploy/docker-compose.debug.yml`)

The base development topology publishes only the ports below. The optional
`obs` profile additionally publishes Prometheus:

| Service | Host port | Default bind | Overrides |
| --- | --- | --- | --- |
| query (API + UI) | `8003` | `127.0.0.1` | `QUERY_BIND_HOST` / `QUERY_PORT` |
| ui (Vite dev server) | `5173` | `127.0.0.1` | `UI_BIND_HOST` / `UI_PORT` |
| robot (upload UI) | `8090` | `127.0.0.1` | `ROBOT_BIND_HOST` / `ROBOT_PORT` |
| gc (metrics) | `9108` | `127.0.0.1` | `GC_BIND_HOST` / `GC_METRICS_PORT` |
| prometheus (optional `obs` profile) | `9090` | `127.0.0.1` | `PROMETHEUS_BIND_HOST` / `PROMETHEUS_PORT` |

`make robot-ui-dev` (a plain `docker run`, not a Compose service) additionally
publishes the robot-SPA dev server on `127.0.0.1:5174`; `ROBOT_UI_BIND_HOST`
widens it.

The development query service has `OIDC_DEV=true`, which exposes a local token
issuer capable of minting an administrator token. A non-loopback
`QUERY_BIND_HOST` is refused unless
`ALLOW_UNSAFE_DEV_QUERY_EXPOSURE=1` explicitly acknowledges the remaining risk;
the guard prints a warning even after that acknowledgement. This override is
for a trusted, firewalled demonstration network, not production deployment.

`VAULT_CONFIG_PATH` selects the config file inside the `robot` service and
defaults to `/app/configs/vault.local.json`.

`ingest` (50051, 8080), `postgres` (5432) and `objectstore` (9000) publish
nothing. `deploy/docker-compose.debug.yml` re-adds them on `127.0.0.1` (`make debug-up`),
with `DEBUG_BIND_HOST`, `INGEST_GRPC_PORT`, `INGEST_ADMIN_PORT`, `POSTGRES_PORT`,
and `OBJECTSTORE_API_PORT` (host port `9002`, container port `9000`) as the knobs.
The object-store console is off. Normal browser downloads
use the query service's same-origin proxy. Direct object-store publication is needed
for downloads only when an operator explicitly sets `S3_PUBLIC_ENDPOINT` to
that published URL.

`RUSTFS_API_STALE_UPLOADS_EXPIRY` (set to `360h` in the development and
release Compose files; it is a fixed setting of the `objectstore` service, not
an operator input) controls when the object store deletes the parts of an
incomplete multipart upload. Ingest keeps a resumable upload session for at
most 14 days and aborts expired sessions itself. Keep this value above 14 days,
or the store can delete the parts of a live session and force a full re-upload.
The full list of `objectstore` settings is in
[`development/objectstore/CONTRACT.md`](../development/objectstore/CONTRACT.md).

## Environment variables

### Ingest (Rust/Tonic) — `configs/ingest.env`

Keys set in the env file: `STORAGE_ROOT`, `S3_BUCKET`, `S3_ENDPOINT`, `S3_REGION`,
`S3_SECURE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `ENABLE_FILE_CAS`,
`DATABASE_URL`, `PER_ROBOT_CONCURRENCY`, `GLOBAL_CONCURRENCY`,
`INGEST_FIRST_MESSAGE_TIMEOUT_SECONDS`, `INGEST_STREAM_IDLE_TIMEOUT_SECONDS`.

Verified against `phases/03-ingest-server-rs/src/main.rs`:

- Concurrency: `GLOBAL_CONCURRENCY` (default `8`, valid `1..=64`),
  `PER_ROBOT_CONCURRENCY` (default `2`, valid `1..=16`). Invalid, zero, or
  over-limit values fail startup. Only the explicitly selected benchmark
  overlay replaces the latter with `BENCH_PER_ROBOT_CONCURRENCY` (default `4`)
  so the four-stream benchmark arm is not silently server-capped;
  `make bench-net-down` restores the base value.
- Stream deadlines: `INGEST_FIRST_MESSAGE_TIMEOUT_SECONDS` (default `10`) bounds
  the wait for stream metadata before any concurrency permit is taken;
  `INGEST_STREAM_IDLE_TIMEOUT_SECONDS` (default `600`) bounds each later silent
  gap while permits and multipart state are held. Both must be finite positive
  seconds no greater than `3600`; invalid values fail startup.
- `MAX_INFLIGHT_PARTS` (default `4`, clamped to `1..=32`) — S3 `upload_part` calls in
  flight per streaming upload. Not in `ingest.env`; set it on the `ingest`
  service to override. Chunks are still received and validated strictly in order — only
  the part *upload* overlaps. The server accepts streaming chunks in `[5 MiB, 8 MiB]`;
  `UploadSmall` separately retains its 48 MiB file threshold under the service-wide
  50 MiB decoder limit. A process-wide admission gate, shared across connections, limits
  payload-bearing RPCs before protobuf decoding. The enforced worst-case retained-payload
  formula is
  `GLOBAL_CONCURRENCY × (MAX_INFLIGHT_PARTS × 8 MiB + 50 MiB)`, and startup
  requires that value to be at most 1 GiB. The first term is
  accepted parts retained by S3 tasks and the second is one current tonic-decoded message
  that can exist before streaming validation.
  The defaults retain at most `8 × (4 × 8 MiB + 50 MiB) = 656 MiB`. This is a
  payload budget, not total process RSS; the 2 GiB release container leaves the other
  1 GiB for protobuf copies, gRPC, TLS, the AWS SDK, Postgres, task stacks, and allocator
  overhead. Each robot is additionally capped by `PER_ROBOT_CONCURRENCY`; setting
  `MAX_INFLIGHT_PARTS=1` restores serial part upload.
- `STORAGE_ROOT` (default `/app/uploads`) is a logical key prefix used when
  constructing raw S3 object keys; ingest has no local raw-file fallback.
- `DATABASE_URL` — required; the process panics on startup if unset
- Object store (S3 API; optional, enabled when `S3_BUCKET` is set): `S3_BUCKET`, `S3_ENDPOINT`,
  `S3_REGION` (default `us-east-1`), plus standard `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` picked up by the AWS SDK's default credential chain.
  Path-style addressing is hardcoded on (`force_path_style(true)`), so there is
  no path-style setting. `S3_SECURE` is present in `ingest.env` for parity with
  the other services but is **not read** by the ingest binary.
- TLS/mTLS (gRPC server): `TLS_ENABLED` (default `false`), `TLS_MTLS` (default
  `false`), `TLS_SERVER_CERT` (default `/app/certs/ingest.crt`), `TLS_SERVER_KEY`
  (default `/app/certs/ingest.key`), `TLS_CA_CERT` (default `/app/certs/ca.crt`).
  Set on the `ingest` service in `docker-compose.yml`, each overridable from the
  shell or `.env`. Certs come from
  `make certs` (`development/generate-dev-certs.sh`); see the phase 03
  [TLS walkthrough](../phases/03-ingest-server-rs/docs/run-and-test.md#tls-variant).
  Only the gRPC listener is wrapped — the admin/metrics HTTP port stays
  plaintext.
- `INGEST_AUTH_TOKEN` — shared bearer token required on every
  `UploadService` RPC and every `/admin/*` request (`authorization: Bearer <token>`).
  Unset/empty disables the check and logs a warning at startup; `/healthz`,
  `/readyz`, `/metrics` and the gRPC health/reflection services are never
  checked. `docker-compose.yml` passes the same value to `ingest` and to the
  `robot`/`seed`/`dev` clients, defaulting to the committed dev token
  `dev-ingest-token-change-me`; `make secrets` (run by `make stack-up`) replaces
  it with a random per-machine token in `.env`.
- `INGEST_PROVISIONING_TOKEN` — separate bearer token for `/admin/v2/robots*`
  and the query service's robot-provisioning proxy. It is optional in the local
  development profile and mandatory in the release profile. Release
  requires at least 32 visible ASCII characters, passes the same value to query
  and ingest, and rejects reuse of `INGEST_AUTH_TOKEN` or `METRICS_AUTH_TOKEN`.

- `ENABLE_FILE_CAS` (built-in default `false`; `configs/ingest.env` sets
  `true` for the development Compose stack) — enables content-addressed dedup
  only for unary `UploadSmall`. Streaming `Upload` always writes a raw MCAP
  object. The agent always requests resume version 1, and the ingest server must
  support it. Retries reuse confirmed multipart parts.
- Durable multipart sessions expire after seven idle days. Ingest checks for
  expired sessions at startup and every 15 minutes, up to 32 per pass, under
  the same upload lock used by active transfers. Storage operations and stalled
  verification reads have a 120-second timeout. These are fixed implementation
  limits, not additional environment variables.
  The ingest storage identity can read raw objects for final verification and
  conditionally delete unfinished session objects during cleanup. It also has
  `ListBucket` on the configured bucket because S3 requires that permission to
  distinguish a missing object from denied access during recovery.
- `METRICS_INCLUDE_ROBOT_ID` (default `false`) — adds a `robot_id` label to metrics
- Ports (hardcoded, not env-configurable): gRPC on `50051`, admin/health/metrics HTTP
  on `8080`

Release has a stricter contract than those development defaults. It publishes
only gRPC target `50051` (`INGEST_GRPC_PORT`, default host port `50051`), keeps
admin HTTP `8080` private, and hard-sets `TLS_ENABLED=true`.
`INGEST_PUBLIC_HOST`, `INGEST_TLS_CERT_FILE`, `INGEST_TLS_KEY_FILE`, and
`INGEST_TLS_CA_FILE` are required deployment inputs; the three files are mounted
individually under `/run/robot-vault/tls` so the container never receives a CA
or robot private key. Startup fails closed if release mode lacks TLS or a
generated token with at least 32 visible ASCII characters (the committed
development value and short secrets are rejected).

Release-only ingest settings:

- `INGEST_SECURITY_PROFILE` — `token` (default) or `mtls`; the development
  stack uses `dev`. `token`: TLS server authentication, a per-robot bearer
  credential, and the shared `INGEST_AUTH_TOKEN` denied on the data plane.
  `mtls`: the same, plus a client certificate whose URI SAN
  `spiffe://robot-vault/robots/<percent-encoded-robot-id>` matches the robot.
  The release preflight refuses any other value. Ingest derives `TLS_MTLS` from
  the profile (`token` off, `mtls` on); an explicit `TLS_MTLS` that contradicts
  the profile stops startup, so do not set it in a release.
- `INGEST_TLS_CA_FILE` (host path; Compose input) — the one root CA certificate
  that issued the ingest server certificate. The file must hold exactly one
  certificate with `CA:TRUE`. It is mounted at
  `/run/robot-vault/tls/ingest-ca.pem` as ingest's `TLS_CA_CERT`: robots pin it,
  and under `mtls` ingest also trusts it for client certificates. Query mounts
  the same file and serves it at `GET /ca.crt` (setting `INGEST_CA_CERT_FILE`,
  fixed by the release Compose file to the same container path).
- `INGEST_PUBLIC_PORT` — the port that robots are told to use for uploads. The
  release Compose file sets it from `INGEST_GRPC_PORT` (the published host port,
  default `50051`), not the container port. Ingest returns
  `INGEST_PUBLIC_HOST:INGEST_PUBLIC_PORT` with each enrollment code, and the
  Robots-page install command passes it as `--ingest`. `UploadSmall` requests in every profile must carry a
client-computed `expected_sha256`. Completed uploads are always
`integrity_verified` with basis `client_sha256`. A missing `robots`
registry table rejects uploads instead of bypassing fleet enrollment.
`/readyz` returns 503 unless both the S3 write/delete probe and Postgres
`SELECT 1` succeed.

### Processor (Python) — `configs/processor.env`

Keys set in the env file: `PYTHONPATH`, `PROMETHEUS_MULTIPROC_DIR`, `DATABASE_URL`,
`S3_BUCKET`, `S3_ENDPOINT`, `S3_REGION`, `S3_SECURE`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`.

Verified against `phases/04-processing-pipeline/src/processor/core.py`:

- S3 output is only enabled when `S3_BUCKET`, `S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, and
  `AWS_SECRET_ACCESS_KEY` are all set. `S3_SECURE` (default `false`) picks http vs.
  https for the PyArrow S3 filesystem. Addressing is always path style; there
  is no path-style setting.
- `DATABASE_URL` — used for the `processed` / `processed_topics` / `processing_status`
  bookkeeping writes.
- `PROCESSOR_MAX_ATTEMPTS` (default `3`) — not in `processor.env`; caps retries before
  an upload is marked `dead_letter` in `processing_status`.
- MCAP read limits (not in `processor.env`; positive integers). A file that
  breaks one becomes `dead_letter` on its first attempt, and other uploads
  continue. An invalid value stops the processor before it claims work.
  - `PROCESSOR_MCAP_MAX_CHUNK_BYTES` (default 512 MiB) — uncompressed size of one
    Chunk record. Decompression stops at the size the record declares.
  - `PROCESSOR_MCAP_MAX_MESSAGE_BYTES` (default 256 MiB) — one message.
  - `PROCESSOR_MCAP_MAX_CHANNELS` and `PROCESSOR_MCAP_MAX_SCHEMAS` (default 4096 each).
  - `PROCESSOR_MCAP_MAX_BUFFERED_BYTES` (default 1 GiB) — messages held while
    merging chunks into log-time order.
- Processing claims (not in `processor.env`):
  - `PROCESSOR_LEASE_SECONDS` (default `900`, minimum `30`) — duration of the
    renewable processing lease. The active worker renews it every one-third of this
    interval; after expiry another replica may reclaim the upload. The old
    attempt's token is fenced from terminal registry writes, and its derived
    objects are written under its own `_attempts/<attempt-id>/` prefix.
  - `PROCESSOR_BATCH_LIMIT` (default `50`, minimum `1`) — maximum uploads attempted by
    one `run_once` poll. The processor claims one upload immediately before processing
    it rather than leasing the whole batch up front.
  - `PROCESSOR_WORKER_ID` (default
    `<hostname>:<pid>:<random-uuid>`) — operator-visible claim-owner label. The
    per-attempt UUID in `processing_status.attempt_id`, not this label, is the fencing
    authority.
- Pro adds episode segmentation, media encoding, QA scoring and PII scrubbing
  to the processor (see the Pro documentation). In core, leave
  `SEGMENTATION_ENABLED`, `MEDIA_ENCODE_ENABLED` and `QA_ENABLED` unset or
  false. A true value stops the processor before it claims work, because core
  has no episode, media or QA stage. Any other value also stops it.
- Metrics: fixed Prometheus exporter port `8000` (not env-configurable).
- Run mode: the compose command is `processor.cli --watch --interval 5` (`--interval`
  is a CLI flag, default `10`, not an env var).

### Query Service (FastAPI) — development and release env files

Development loads `configs/query.env`. Keys set in that env file:
`S3_PARQUET_BUCKET`, `S3_ENDPOINT`, `S3_REGION`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_SECURE`,
`REDIS_URL`, `DATABASE_URL`, `AUTH_MODE`, `OIDC_DEV`, `OIDC_ROLE_CLAIM`.
(`S3_PUBLIC_ENDPOINT` appears in that file only as a commented-out example —
it is deliberately unset; see its entry below.)

The release topology instead loads the credential-free
`configs/query.release.env`, then overrides its security-sensitive values with
Compose `environment` entries. A release always receives
`ROBOT_VAULT_RELEASE=true`, `AUTH_MODE=oidc`, `OIDC_DEV=false`,
`OIDC_ALGORITHMS=RS256`, and `OIDC_ROLE_SOURCE=claim`; `OIDC_ISSUER`,
`OIDC_AUDIENCE`, `OIDC_JWKS_URL`,
`OIDC_CLIENT_ID`, `OIDC_AUTHORIZATION_URL`, and `OIDC_TOKEN_URL` are required
interpolation inputs. `OIDC_END_SESSION_URL` is optional and `OIDC_SCOPES`
defaults to `openid profile email offline_access`. Every browser endpoint must
be an explicit HTTPS URL without credentials or a fragment; providers may use
different origins. Put these in the mode-0600 file passed as
`RELEASE_ENV_FILE`, alongside the datastore credentials, ingest token,
provisioning token, `QUERY_PUBLIC_HOST`, and a distinct `METRICS_AUTH_TOKEN`.
All three tokens require at least 32 visible ASCII characters and must be
different. Absolute TLS certificate/key/CA paths are required too. A missing or
unsafe value prevents the preflight from succeeding before any container starts.

Verified against `phases/05-query-service/src/query_service/main.py`. The query
service is **S3-only** for reading Parquet — there is no local-filesystem fallback
in the code.

- Object store (read Parquet via DuckDB httpfs + list via boto3): `S3_PARQUET_BUCKET`,
  `S3_ENDPOINT`, `S3_REGION` (default `us-east-1`), `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `S3_SECURE` (default `false`; addressing is always
  path style), `S3_PREFIX_SCOPE` (default empty; optional key prefix scope, not set in
  `query.env`)
  - Caveat: the DuckDB read path (`settings.py`) reads `S3_PARQUET_BUCKET` only,
    while the S3 list/presign/download and upload-detail modules (`s3api.py`,
    `uploads.py`) fall back to `S3_BUCKET` when `S3_PARQUET_BUCKET` is unset.
    Setting only `S3_BUCKET` therefore leaves object listing working while
    every DuckDB Parquet read is silently disabled — set `S3_PARQUET_BUCKET`
    explicitly (as `query.env` does).
- `OIDC_ROLE_CLAIM` (release default `roles`) — the token claim that holds the
  user's roles. Only `viewer`, `operator` and `admin` are kept. The first
  administrator is whoever your identity provider gives `admin` in this claim;
  there is no user table (see
  [First administrator](OPERATIONS.md#first-administrator)). `/readyz` fetches
  and validates the keys at `OIDC_JWKS_URL` (300-second cache, then a 60-second
  stale grace for token checks); it fails when the provider cannot be reached.
- Robot onboarding (release): `PUBLIC_BASE_URL` (default
  `https://$QUERY_PUBLIC_HOST`; a bare `https://host[:port]`) is the vault
  address in the install command. Query serves the installer files without
  authentication: `GET /robot` and `GET /robot/agent-linux-amd64.tar.gz` from
  `ROBOT_BOOTSTRAP_DIR` (default `/opt/robot-vault/robot`, filled by the query
  image), and `GET /ca.crt` from `INGEST_CA_CERT_FILE` (default
  `/run/robot-vault/tls/ingest-ca.pem`). Without a readable installer file,
  `/robot` answers `503` and the Robots page shows the enrollment code with no
  command. `GET /api/admin/robots/bootstrap` (admin) returns the agent version,
  the installer digest and the upgrade command.
- `S3_PUBLIC_ENDPOINT` — optional externally reachable S3 URL for direct
  presigned browser downloads. It is unset by default; the API then returns a
  same-origin query-proxy URL, so normal downloads do not require a published
  object-store port. Set it only when the browser can reach that explicit endpoint.
- Redis cache (optional): `REDIS_URL` — backs the 30 s response cache used by
  `/api/uploads/summary`, `/api/timeseries` and `/api/v1/message-types`
  (`settings.cache_get`/`cache_set`) and the rate limiter. Without it those
  responses are simply uncached — `cache_get`/`cache_set` are no-ops, there is
  no in-process equivalent for them; only the rate limiter falls back to
  in-memory counters.
  - **It does not back the time-index cache**, despite the name.
    `TimeIndexedCache` accepts a Redis client, stores it as `self.redis` and
    never reads it — every entry lives in the worker's own dicts. So that cache
    is per-process, dies with the process, and is not shared between workers,
    with or without `REDIS_URL`.
- `TIME_INDEX_CACHE_SIZE_MB` (default `512`) — set via compose `environment:`, not
  `query.env`. It bounds the in-process time-index cache using a conservative
  walk of each retained Python response graph plus per-entry index overhead;
  compact JSON length is not treated as resident memory. Miss/admission and
  optimizer pattern maps are independently LRU-bounded to 4,096 keys so unique
  query shapes cannot grow untracked metadata forever. Measured effect of that
  cache: 3.1x on a 1 h window, 1.0x on 7 d — see
  `phases/05-query-service/src/query_service/time_index_cache.py`.
- DuckDB: `DUCKDB_THREADS` (default `2`) and `DUCKDB_TEMP_DIRECTORY` (default
  unset) are not set in `query.env`. Release fixes the temp directory to the
  query-only `/var/lib/robot-vault/query` named volume so spill data remains
  writable when the image root is read-only; baked extensions under `/opt`
  remain immutable. `DUCKDB_MEMORY_LIMIT_MB` (default `512`) caps each ordinary
  read connection, while `SQL_CONSOLE_MEMORY_LIMIT_MB` (default `512`) caps each
  ad-hoc SQL connection. Both must be positive integers. All DuckDB connections
  share `DUCKDB_QUERY_CONCURRENCY` admission (default `4`, valid `1..=64`);
  a caller waiting longer than `DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS` (default
  `5`, finite positive seconds) receives 503.
  `DUCKDB_QUERY_TIMEOUT_SECONDS` (default `30`, finite positive seconds)
  interrupts DuckDB execution after that connection's timer expires and
  returns a stable 504. Source plans come from the `parquet_files` registry.
  A plan is rejected before object access above `QUERY_MAX_SOURCE_FILES`
  (default `5000`), `QUERY_MAX_SOURCE_ROWS` (default `10000000`) or
  `QUERY_MAX_SOURCE_BYTES` (default `21474836480`, 20 GiB). The registry
  records the size of every file, so the byte total is always known. Explicit
  `since`/`until` windows may span at most `QUERY_MAX_SOURCE_RANGE_SECONDS`
  (default `2678400`, 31 days). The authenticated
  `GET /api/v1/query/limits` endpoint returns these active values, the timeout
  and the concurrency. This lets the UI constrain its controls without
  duplicating policy. Relations are lazy, so setup does not copy the selected lake range
  before the final filter, aggregate, or limit runs. The connection limit is
  aggregate admission, while the memory values remain per-connection ceilings.
- Auth: `AUTH_MODE` (default `none`; set to `oidc` in development `query.env` with
  `OIDC_DEV=true` for local dev, and forced to `oidc` with `OIDC_DEV=false` by the
  release topology), `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`,
  `OIDC_CLIENT_ID`, `OIDC_AUTHORIZATION_URL`, `OIDC_TOKEN_URL`, optional
  `OIDC_END_SESSION_URL`, `OIDC_SCOPES`, `OIDC_ROLE_CLAIM` (default `roles`),
  `OIDC_ROLE_SOURCE` (default `claim`; alternatives `realm_access` or
  `resource_access`), and `OIDC_RESOURCE_ROLE_CLIENT_ID` (resource-role client,
  falling back to `OIDC_CLIENT_ID`). Verification controls are
  `OIDC_ALGORITHMS` (comma-separated asymmetric allowlist, default `RS256`),
  `OIDC_LEEWAY_SECONDS` (`30`), `OIDC_MAX_TOKEN_LIFETIME_SECONDS` (`86400`),
  `OIDC_JWKS_TIMEOUT_SECONDS` (`3`), `OIDC_JWKS_CACHE_SECONDS` (`300`),
  `OIDC_JWKS_STALE_IF_ERROR_SECONDS` (`60`),
  `OIDC_JWKS_REFRESH_COOLDOWN_SECONDS` (`15`), `OIDC_NEGATIVE_KID_SECONDS`
  (`30`), `OIDC_JWKS_REFRESH_LOCK_TIMEOUT_SECONDS` (`0.1`),
  `OIDC_MAX_JWKS_BYTES` (`1048576`), and `OIDC_MAX_JWKS_KEYS` (`128`). Invalid
  ranges, symmetric/`none` algorithms, oversized key sets, and unsafe URLs fail
  closed. `OIDC_DEV` defaults to `false`; when true it exposes the dev-token
  endpoint instead of requiring a real IdP. `ENABLE_HSTS` defaults to `false`
  in code but is forced to `true` by the TLS-only release topology.
  `ROBOT_VAULT_RELEASE=true` makes invalid/fail-open combinations a startup
  error. Release also hard-pins `OIDC_ALGORITHMS=RS256` and
  `OIDC_ROLE_SOURCE=claim`; neither the env file nor inherited shell can widen
  those policies.
- Metrics: `METRICS_AUTH_TOKEN` is optional in development. When set, `/metrics`
  requires `Authorization: Bearer <token>`. Release requires a dedicated token
  of at least 32 visible ASCII characters, distinct from the ingest token;
  release Prometheus reads it from a mode-0600 file generated inside its own
  container, so the public HTTPS listener does not expose metrics anonymously.
  Metrics are served on API port `8003`, not a separate `METRICS_PORT`;
  `DISABLE_PROM_METRICS` (default unset/enabled) disables application
  request/query metric collection but does not remove the endpoint. It is a
  presence check, not a parsed boolean: **any** non-empty value disables
  collection, including `DISABLE_PROM_METRICS=false`.
- Rate limiting is a fixed one-minute window per client IP and bounded policy bucket,
  with the same path-specific limits in both modes:
  `RATE_LIMIT_DEFAULT_PER_MINUTE` (falls back to `RATE_LIMIT_PER_MINUTE`, default
  `240`), `RATE_LIMIT_ADMIN_PER_MINUTE` (default `60`, for `/api/admin/*`), and
  `RATE_LIMIT_PRESIGN_PER_MINUTE` (default `120`, for `/api/v1/s3/presign`). Redis is
  the shared counter store when configured; without Redis, or while a configured Redis
  is unavailable, the process uses its in-memory counter instead of failing open. None
  of these variables are set in `query.env`. Redis network calls are bounded by
  `REDIS_CONNECT_TIMEOUT_SECONDS` and `REDIS_SOCKET_TIMEOUT_SECONDS` (both
  set to/default `1` and must be finite positive seconds); the increment and
  TTL are committed atomically so a failed request cannot leave an immortal
  rate key. Runtime fallbacks increment
  `query_service_rate_limit_redis_degradations_total` and emit a throttled
  warning containing only the exception class (never the Redis URL or error
  text); client-construction failure emits the same redacted startup warning.
- Query admission metrics use bounded labels:
  `query_service_query_active`, `query_service_query_capacity`,
  `query_service_query_admission_rejections_total{reason=...}`, and
  `query_service_query_timeouts_total{query_type=...}`.
- `QUERY_MAX_REQUEST_BODY_BYTES` (set to/default `8388608`, 8 MiB) is enforced before
  FastAPI parses a request body, including chunked requests with no
  `Content-Length`. Oversized requests return HTTP 413. It is independent of
  rate limiting so one accepted request cannot consume unbounded body memory.
- `CORS_ALLOWED_ORIGINS` (default `http://localhost:5173,http://127.0.0.1:5173`) —
  comma-separated browser origins the API trusts for CORS; entries of `*` are
  ignored rather than honored. Extend it when developing the SPA from another
  host. It is not set in `query.env`; `query.release.env` pins it empty because
  the shipped release UI is same-origin. A release that serves its browser UI
  from a separate origin must list that exact origin explicitly.
- Upload-detail raw scan bounds (read at startup, not set in `query.env`):
  `UPLOAD_DETAIL_MCAP_MAX_BYTES` (default `67108864`, 64 MiB) caps the source MCAP
  downloaded/read into memory, and `UPLOAD_DETAIL_MCAP_MAX_MESSAGES` (default
  `250000`) caps messages inspected for per-topic raw-byte breakdowns. Both must be
  positive integers or startup fails. Exceeding either bound does not fail the upload
  detail request: registry/Parquet detail is returned with `raw_bytes_mode="error"`
  and `raw_bytes_error` naming the exceeded limit.
- Pro adds dataset cuts and provenance signing (see the Pro documentation).
  Core has no dataset-cut routes.
- Prometheus integration (set via compose `environment:`, not `query.env`):
  `PROMETHEUS_BASE` (default `http://prometheus:9090`). Backs the opt-in
  `/ui/obs` page through the bounded `/api/obs/promql/range` (operator role
  only), `/api/obs/alerts`, and `/api/obs/health` endpoints only. There is no generic
  Prometheus reverse proxy; the health endpoint consumes only upstream status
  headers and never buffers its response body. Overview's own KPI tiles
  (`GET /api/v1/metrics/overview`) read Postgres directly and need no
  configuration here. There is no Grafana service; `/ui/obs` is the
  observability page.
- API port: fixed at container port `8003`. Development publishes it as host
  port `8003` over HTTP. Release overrides the image command with Uvicorn TLS,
  publishes HTTPS on `${QUERY_HTTPS_PORT:-443}`, and exposes no plaintext
  listener or redirect. `QUERY_TLS_CERT_FILE`, `QUERY_TLS_KEY_FILE`, and the
  Prometheus trust bundle `QUERY_TLS_CA_FILE` are required read-only host-file
  binds with `create_host_path:false`;
  `QUERY_PUBLIC_HOST` is the certificate DNS name used for the public URL and
  for the release Prometheus scrape's verified TLS `server_name`.
  Docker and Make use `/readyz` for readiness: it performs bounded JWKS
  initialization and Postgres `SELECT 1` (Pro adds its own readiness checks).
  Any failed check returns 503. `/healthz` remains liveness-only
  and never contacts the identity provider or database.
- MCP server at `/mcp` (full guide [MCP.md](MCP.md)). Read at startup,
  none set in `query.env`:
  - `MCP_ENABLED` (default `true`): `false` removes `/mcp` and
    `/.well-known/oauth-protected-resource` (both 404).
  - `MCP_WRITE_TOOLS` (default `false`): `true` lists and allows the Pro write
    tools; their routes still check the role.
  - `MCP_SQL_ENABLED` (default `true`): `false` removes `query_sql`.
  - `MCP_TOOLS_DENY` (default empty): comma-separated tool names to remove.
  - `MCP_ALLOW_NO_AUTH` (default `false`): with `AUTH_MODE=none`, MCP is off
    unless this is `true` (development only).
  - `MCP_ALLOWED_ORIGINS` (default empty): browser origins allowed to call
    `/mcp`; any other supplied `Origin` gets 403.
  - `MCP_INGRESS_RATE` (default `240`): `/mcp` requests per client address per
    minute, counted before authentication. Each tool call also uses the REST
    operation limit (`RATE_LIMIT_DEFAULT_PER_MINUTE`) once.
  - `MCP_MAX_RESPONSE_BYTES` (default `1048576`) and `MCP_MAX_FIELD_BYTES`
    (default `65536`): larger tool results, or larger single values, become
    `response_too_large` / `field_too_large` tool errors.
  - `PUBLIC_BASE_URL` (default unset): `scheme://host[:port]` that clients use.
    It is the allowed `Host` for `/mcp` (with `localhost`, `127.0.0.1`,
    `[::1]`), the protected-resource id, and the base of Pro ticket URLs.
    Protected-resource metadata is served only with `AUTH_MODE=oidc`,
    `OIDC_DEV=false`, `OIDC_ISSUER` and this variable set.
  - In release, `deploy/docker-compose.release.yml` sets `MCP_ENABLED`
    (default `true`), `MCP_WRITE_TOOLS` (default `false`) and
    `PUBLIC_BASE_URL` (default `https://${QUERY_PUBLIC_HOST}`) from the
    release env file. Set `PUBLIC_BASE_URL` when clients reach query on a
    port other than 443. The Pro overlay inherits these values. The release
    preflight does not validate them.

### GC service (mark-and-sweep) — `configs/gc.yaml` + env overrides

- **The `gc` service is the only garbage collector, and the only thing in the
  system that deletes S3 objects.** There is no HTTP trigger for GC. Do not add
  one: reachability is a whole-system invariant, and a second copy of it would
  drift from this one. Tune GC
  through the settings below; `tests/integration/test_gc_reachability.py` pins
  the single-implementation invariant.
- Config file: `configs/gc.yaml` (YAML), mounted at `GC_CONFIG_PATH` (default
  `/app/configs/gc.yaml`). Entrypoint: `tools/s3_gc.py`.
- Precedence: an env var, if set, overrides the matching `gc.yaml` key; `gc.yaml`
  overrides the script's own hardcoded fallback.
- `DATABASE_URL` — overrides `gc.yaml`'s development URL. Release renders the
  dedicated `GC_DATABASE_URL` here for the fixed `robot_vault_gc` role; no
  runtime receives the owner or another service's URI.
- S3 connection (`s3.bucket`, `s3.endpoint`, `s3.region` in YAML): compose overrides
  via `S3_BUCKET`, `S3_ENDPOINT`, `S3_REGION`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`.
- `GC_MIN_AGE_HOURS` (script fallback `24`; `gc.yaml` sets `72`; compose default
  `24`) — finite range: one second (`1/3600` hour) through `87600` hours
- `GC_MPU_MAX_AGE_HOURS` (script and release-compose default `24`) — minimum
  age before an orphaned multipart upload is eligible for abort. Registered
  resumable sessions are protected regardless of age; ingest expires them
  under the upload lock so cleanup cannot interrupt an active attempt.
- `GC_DELETE_LIMIT` (script fallback `5000`; `gc.yaml` sets `1000`; compose
  default `2000`) — integer range `1..100000`
- `GC_DRY_RUN` (compose default `true` — first real deploy only logs `DRY-DELETE`
  lines; flip to `false` once reviewed). Only literal `true` or `false` is
  accepted; a typo fails startup and release preflight instead of enabling
  deletion.
- `GC_PREFIXES` (comma-separated S3 prefixes to sweep; compose default
  `raw/,manifests/,processed/parquet/,cas/files/,processed/media/,qa/reports/,exports/`).
  Active registries cover raw uploads, CAS manifests, Parquet, media, QA and
  exports. A successfully queried but empty registry authorizes orphan
  collection; a missing/failed registry query skips that namespace before S3
  listing. CAS object reachability is intentionally not modelled, so
  `cas/files/` objects are never deleted; that prefix is present only for
  stale-multipart aborts.
- `GC_INTERVAL_SECONDS` (script, YAML and release-compose default `3600`) —
  integer range `1..604800`
- `METRICS_PORT` (default `9108`; hard-fixed to `9108` in release) — Prometheus
  exporter, scraped by the `gc` job above

Example `configs/gc.yaml`:

```yaml
database_url: "postgres://robotvault:robotvault@postgres:5432/robotvault"
s3:
  bucket: "robotics-data"
  endpoint: "http://objectstore:9000"
  region: "us-east-1"
gc:
  min_age_hours: 72
  delete_limit: 1000
  dry_run: true
  interval_seconds: 3600
  prefixes: ["raw/", "manifests/", "processed/parquet/", "cas/files/", "processed/media/", "qa/reports/", "exports/"]
  metrics_port: 9108
```

### Exporter service (Pro)

Pro adds the dataset exporter service (see the Pro documentation). The core
release file still requires `EXPORTER_DATABASE_URL` and `EXPORTER_S3_SECRET_KEY`:
the bootstrap creates the `robot_vault_exporter` role and the
`robot-vault-exporter` object-store user as reserved identities, so a later move
to Pro finds them. No core service uses them.

## Service `env_file` wiring (docker-compose.yml)

```yaml
  ingest:
    env_file:
      - ./configs/ingest.env

  processor:
    env_file:
      - ./configs/processor.env

  query:
    env_file:
      - ./configs/query.env

  gc:
    environment:
      - GC_CONFIG_PATH=/app/configs/gc.yaml
      # (plus the GC_* overrides listed above; gc has no env_file)
```

`deploy/docker-compose.release.yml` uses `../configs/query.release.env` only for
credential-free query defaults. `RELEASE_ENV_FILE` supplies one percent-encoded
owner `DATABASE_URL`, five service-specific runtime URLs
(`INGEST|PROCESSOR|EXPORTER|QUERY|GC_DATABASE_URL`), the object-store root password (`OBJECTSTORE_ROOT_USER` and
`OBJECTSTORE_ROOT_PASSWORD`), and five distinct service S3 secrets. Runtime usernames and access ids are
fixed (`robot_vault_<service>` and `robot-vault-<service>`); each long-running
service receives only its own credential and prefix/table policy. The file also
supplies the shared bucket/region, real OIDC inputs, query TLS identity/trust,
and the ingest TLS identity and its one root CA (`INGEST_TLS_CA_FILE`).
`ROBOT_VAULT_SECRETS_GID` is optional (default `10001`) and names the one host
group allowed to read root-owned query and ingest private keys; release query
and ingest receive that supplementary numeric group.
Make validates its mode without sourcing it, renders both certificate mounts,
then uses OpenSSL to prove PEM parsing, key matches, hostname SANs, server-auth
EKU, at least seven days' remaining validity, CA constraints/key usage and each
signature chain. Inherited database, S3, OIDC, TLS and GC variables are removed
before Compose runs, so the named env file is
authoritative. Three gated one-shots run before any runtime: `db-migrate`,
`postgres-roles-ready` (explicit table/column ACLs), and `objectstore-access-ready`
(five explicit JSON policies). An EXIT trap registered before Compose startup
makes `release-up` remove their containers on success, readiness/edition
failure, and HUP/INT/TERM; cleanup failure fails an otherwise successful run.
Bootstrap secrets therefore do not remain in stopped-container metadata. The
release file publishes only query HTTPS and ingest TLS
gRPC; all first-party images are resolved locally only (`pull_policy: never`),
which is a supply-chain rule rather than a network-reachability claim. They run
as UID/GID `10001:10001` with all capabilities dropped,
`no-new-privileges`, a read-only root, and a bounded hardened `/tmp` tmpfs.
Persistent writable paths are named volumes: uploads for ingest, a work
directory for the processor, and a DuckDB spill directory for the query service.
Each first-party runtime also has a bounded, hardened temporary-memory
filesystem at `/tmp`. The
stateful upstream Postgres/Redis/Prometheus entrypoint contracts are not
overridden by this first-party policy. The object store is the first-party
`robot-vault-objectstore` image with its own user. Its access bootstrap runs in
the release toolkit image with one tracked script and five policies mounted
as read-only Compose configs, not a source-tree bind.

The release topology also applies hard single-host resource and log bounds. The
defaults are ceilings, not reservations, and should be tightened from measured
peak usage on the target host:

- `POSTGRES_MEMORY_LIMIT=4g`, `OBJECTSTORE_MEMORY_LIMIT=4g`,
  `REDIS_MEMORY_LIMIT=1g`, `INGEST_MEMORY_LIMIT=2g`,
  `PROCESSOR_MEMORY_LIMIT=8g`,
  `QUERY_MEMORY_LIMIT=4g`, `GC_MEMORY_LIMIT=2g`, and
  `PROMETHEUS_MEMORY_LIMIT=2g` set container memory ceilings. The two database
  one-shots default to `1g`; the object-store bootstrap (`OBJECTSTORE_BOOTSTRAP_MEMORY_LIMIT`) defaults to `512m`.
- Matching `<SERVICE>_CPU_LIMIT` values default to 1, 2, or 4 CPUs according to
  workload. Matching `<SERVICE>_PIDS_LIMIT` values cap process creation;
  `FIRST_PARTY_PIDS_LIMIT=512` is the inherited fallback for one-shots.
  `POSTGRES_SHM_SIZE=256m` bounds its explicit shared-memory mount.
- `ROBOT_VAULT_LOG_MAX_SIZE=25m` and `ROBOT_VAULT_LOG_MAX_FILES=5` rotate every
  release container's `json-file` log. Do not set either to an unbounded
  logging mode on a release host.
- With the `obs` profile, `PROMETHEUS_RETENTION_TIME=30d` and
  `PROMETHEUS_RETENTION_SIZE=25GB` bound the TSDB by both age and size. The
  tracked `configs/prometheus/release-alerts.yml` rules cover target outage,
  query 5xx responses, query saturation and timeouts, upload commit failures,
  GC failures, Redis rate-limit degradation, volume space, backlog age,
  backups, certificate expiry and operational collection. Pro adds exporter
  alerts (see the Pro documentation). Prometheus exposes active rules
  through the query service; notification delivery still requires an operator
  owned Alertmanager or equivalent receiver.

## Notes

- See `ARCHITECTURE.md` for deployment topology and security guidance (TLS/mTLS).
- Defaults documented here are for local dev (`docker-compose.yml`); adjust ports,
  credentials, and rate limits for production.
- This file only documents what ships in `configs/*.env`, `configs/*.yaml`, and the
  code that reads them. If you add a new env var, update the relevant section above.
