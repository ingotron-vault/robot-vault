# Phase 05 — Query Service (FastAPI + DuckDB)

This phase provides the Query API and React UI for fleet, upload,
observability and per-topic data workflows. Pro adds episode and dataset
workflows (see the Pro documentation). Postgres is the control
plane and authoritative registry; DuckDB reads Parquet payloads directly from
the object store (S3 API). Payload reads are S3-only; there is no local-filesystem Parquet
fallback.

## Recording status evidence

Upload list rows and upload detail include registered verification and processing
facts. List requests read these facts in one database batch for the page.

| Field | Meaning |
| --- | --- |
| `integrity_verified` | Database integrity flag, or `null` when evidence is unavailable. |
| `integrity_verification_basis` | `client_sha256` for current uploads; `null` when unavailable. |
| `upload_error`, `upload_updated_at` | Recorded upload error and last database update time. |
| `processing_status` | Actual `processing_status.status`, or `null` when no row exists. |
| `processing_error`, `processing_attempts` | Worker error and attempt count, or `null`. |
| `processing_started_at`, `processing_completed_at`, `processing_updated_at`, `processing_lease_expires_at` | Worker timestamps as UTC ISO strings, or `null`. |

Processing states are `pending`, `processing`, `completed`, `completed_empty`,
`failed`, and `dead_letter`. The API does not infer successful processing from
whether topics exist. `completed_empty` records a finished attempt with no
queryable rows. Detail requests refresh upload and processing evidence on every
request. Expensive topic, Parquet and raw MCAP metadata retain their existing
60-second cache.

`storage_path` is the registered server location. These endpoints do not check
current object existence or inspect the robot's local copy. An upload completion
timestamp is not a separate verification timestamp, and these fields do not
approve local deletion.

## Primary endpoints

Every data-bearing endpoint below sits behind the router-wide viewer floor
(`main.create_app()` adds `require_role("viewer")` to each data-bearing
router; live under `AUTH_MODE=oidc`, a no-op otherwise — see the auth notes
under [Configuration (env)](#configuration-env)). Bullet annotations call out
only the additional **operator**/**admin** requirements.

- `GET /api/robots` — list robot IDs from the authoritative Postgres `robots`
  registry; returns `[]` only when the registry is not bootstrapped/configured
  yet, while real database failures propagate
- `GET /api/uploads/summary?robot_id=&since=&until=` — counts per day
- `GET /api/v1/uploads/list?robot_id=&since=&until=&limit=&offset=&order_by=&order_dir=&include_in_progress=&q=&status=` — list uploads from Postgres with pagination and sorting. Response is an object:
  - Request params:
    - `order_by`: one of `completed_at|file_size_bytes|original_filename|robot_id` (default `completed_at`)
    - `order_dir`: `asc|desc` (default `desc`)
    - `limit`, `offset` as before
    - `include_in_progress`: also include `status=uploading` rows (default `false`, i.e. `completed` only)
    - `q`: filter where filename or storage path contains this substring
    - `status`: optional comma-separated list of statuses to include (overrides `include_in_progress`)
  - Response shape:
    - `items`: array of rows `{ upload_id, robot_id, original_filename, storage_path, file_size_bytes, status, completed_at, bytes_received, operator, site, permission_ref, session_tags, task_label }`
    - `page`: `{ limit, offset, returned, total_count, has_prev, has_next, next_offset, prev_offset, order_by, order_dir }`
- `GET /api/v1/uploads/{upload_id}/detail` — detail for a single upload (topics, rows, bytes, time range)
  - Raw bytes per topic are computed by reading MCAP payload sizes and mapping schemas to normalized topic names (e.g., image, pointcloud2, imu split). The scan is capped by `UPLOAD_DETAIL_MCAP_MAX_BYTES` (default 64 MiB) and `UPLOAD_DETAIL_MCAP_MAX_MESSAGES` (default 250,000). If a cap is exceeded, `raw_bytes_mode` is `error`, `raw_bytes_error` is `raw_detail_byte_limit_exceeded` or `raw_detail_message_limit_exceeded`, and per-topic `raw_bytes` remains unavailable. The UI shows expandable sub-rows with per-ROS-topic (source ROS topic) breakdown under each normalized topic.
  - `ts_min`/`ts_max` are computed from the upload's own Parquet files (via DuckDB) so the UI's "Open in chart" button can enable itself with a real time range.
  - The response does not include storage paths. Use the `/files` endpoint below for download keys.
- `GET /api/v1/uploads/{upload_id}/files` — bucket-relative S3 object keys for an upload's per-topic Parquet files, for download. Response: `{ upload_id, parquet_keys: {topic: [key, ...]} }`. These keys are fed directly into `POST /api/v1/s3/presign`.
- `GET /api/timeseries?robot_id=&topic=&since=&until=&bucket=` — per-bucket
  averages of the numeric value columns the Parquet registry recorded for the
  matched files (`x,y,z` when no file matches). Response: `points` (each `{t, <one key per value column>, n}`),
  `columns`, and `all_null_columns` (columns that are entirely NULL in every
  matched file). When the topic files give no numeric value (for example
  JointState, whose arrays do not fit the scalar columns), the response also
  has a `series` payload with `series_source: "typed_joint_state"`, read from
  the v2 typed JointState file when one exists.
- `GET /api/v1/message-types?robot_id=` — distinct topics
- `GET /api/v1/robots/{robot_id}/data/{message_type}` — row data; supports `since,until,columns,limit`. When both `since` and `until` are given, this endpoint is backed by an in-process time-indexed cache (`time_index_cache.py`) keyed on robot/topic/time-range **and** the requested `columns`+`limit` (so differently-shaped requests never collide), serving a contained-range hit only when the cached limited result is provably complete, with entries expiring after a TTL so newly-written Parquet becomes visible without a restart.
- `GET /api/v1/robots/{robot_id}/stats/{message_type}` — counts and min/max timestamps; optional `bucket`
- `POST /api/v1/query/sql` — ad-hoc, read-only SQL over an in-memory DuckDB with
  a lazy `all_parquet` view backed by S3 Parquet; creating the view does not
  materialize the whole lake. Body is `{ "sql": "..." }`.
  Success returns `{ "columns": [...], "rows": [...] }`, bounded at 10,000
  rows — beyond that the service returns HTTP 422 with the stable
  `result_limit_exceeded` code. A missing, empty, non-string, or oversized SQL
  value gets HTTP 400 `invalid_sql`; SQL the guard refuses and permanent
  binding/catalog errors get HTTP 400. Backend, I/O, conversion, memory, and
  setup failures get HTTP 503 without exposing storage paths or connection
  details in either the response or application log.
  Every query connection, including the console's, can open only objects
  under the processed Parquet prefix (`s3://<bucket>/processed/parquet/`).
  It cannot read local files, other S3 prefixes or buckets, or HTTP URLs. It
  cannot install or auto-load extensions or store persistent secrets, and
  its configuration is locked. See `planner.restrict_connection`.
- **Episodes, media preview and datasets:** Pro adds these routes (see the
  Pro documentation). In core they return 404 and are absent from OpenAPI.
- `GET /api/v1/edition` — `{"edition": "core"|"pro"}`; present in both editions
- `GET /api/v1/auth/config` — open, no-store runtime config for the SPA; reports
  `none`, explicit `dev`, or only the non-secret inputs for the real OIDC
  public client (never API audience/JWKS details or a client secret)
- `GET /healthz` — health
- `GET /readyz` — dependency-aware readiness
- `GET /metrics` — Prometheus metrics; open when `METRICS_AUTH_TOKEN` is unset
  in development, otherwise requires that dedicated bearer token. Release
  requires a distinct token of at least 32 visible ASCII characters.
- `GET /ui` — React SPA (Vite build, charts via ECharts)
- `GET /api/obs/promql/range?query=&start=&end=&step=` — one bounded
  Prometheus range query (**operator** role). The OBS page sends free-form
  PromQL, so the route is not open to viewers.
- `GET /api/selfcheck` — operator-only runtime diagnostic: confirms DuckDB can
  execute a query and, when S3 is configured, reports the bucket and lake
  root. It also reports the number of published registry files. It never
  returns raw backend exception text
- `GET /api/v1/cache/stats` — stats for the time-indexed cache used by `/api/v1/robots/{robot_id}/data/{message_type}` (entry counts, memory usage, top query patterns)

## Configuration (env)

- `DATABASE_URL` — Postgres control-plane/registry connection (required for the
  fleet, upload, episode, dataset, export-status and registered-file surfaces).
  Release injects only the scoped `robot_vault_query` role; its sole mutation
  outside query-owned cut tables is column-scoped `episodes.task_label`.
- `S3_PARQUET_BUCKET` — bucket holding processed Parquet (required to return
  data from payload endpoints; no local-filesystem fallback)
- `S3_ENDPOINT` — e.g. `http://objectstore:9000`
- `S3_REGION` (default `us-east-1`)
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (release uses fixed access id
  `robot-vault-query`, read/list-scoped to the query surfaces; no writes/deletes)
- `S3_SECURE` (default `false`)
- `S3_PREFIX_SCOPE` (optional deployment-wide object-key prefix; default
  empty; this is not tenant isolation)
- `DUCKDB_THREADS` (default `2`)
- `DUCKDB_TEMP_DIRECTORY` (unset by default)
- `SQL_CONSOLE_MEMORY_LIMIT_MB` (default/set to `512`) — positive integer MiB
  ceiling applied when each ad-hoc SQL connection is created. It does not bound
  aggregate concurrency; admission and execution deadlines remain roadmap work.
- `REDIS_URL` (optional; enables the Redis-backed uploads/timeseries response cache and the Redis-backed rate limiter)
- `TIME_INDEX_CACHE_SIZE_MB` (default `512`) — conservative retained-object-graph budget for the in-process time-indexed cache; per-entry index overhead is included and both pattern-tracking maps are LRU-bounded to 4,096 keys
- `UPLOAD_DETAIL_MCAP_MAX_BYTES` (default `67108864`, 64 MiB) — positive maximum source-object bytes scanned to compute the upload detail's raw-byte breakdown
- `UPLOAD_DETAIL_MCAP_MAX_MESSAGES` (default `250000`) — positive maximum MCAP messages scanned for that breakdown
- `RATE_LIMIT_PER_MINUTE` (default `240`) — also used as the fallback for `RATE_LIMIT_DEFAULT_PER_MINUTE`; see "Security and Limits"
- API token verification: `AUTH_MODE=none|oidc`, `OIDC_ISSUER`,
  `OIDC_AUDIENCE`, `OIDC_JWKS_URL`, `OIDC_ROLE_CLAIM`, and the local-only
  `OIDC_DEV` switch.
- Real browser login (public client, no secret): `OIDC_CLIENT_ID`,
  `OIDC_AUTHORIZATION_URL`, and `OIDC_TOKEN_URL` are required;
  `OIDC_SCOPES` defaults to `openid profile email offline_access`, and
  `OIDC_END_SESSION_URL` is optional. All real-mode URLs must be explicit
  HTTPS URLs. Register `/ui/auth/callback/` at the externally visible vault
  origin; the token endpoint must permit that origin through CORS.
- Under OIDC, `main.create_app()` applies a viewer dependency to every
  data-bearing router (query/telemetry, fleet, S3, episodes, datasets, admin,
  and observability). Operator/admin route dependencies remain additive. The
  public surface is limited to the SPA and auth bootstrap, root, liveness,
  readiness, and edition discovery. Metrics use an independent scrape token
  when configured and always do so in release.

## File Layout Assumptions

```
processed/parquet/{robot_id}/_attempts/<attempt_id>/{topic}/YYYY/MM/DD/rows-<upload_id>.parquet
processed/parquet/v2/{family}/{robot_id}/_attempts/<attempt_id>/{topic}/rows-<upload_id>.parquet
```

The processor writes per-topic files, plus typed v2 files for message types
that have a typed extractor, and registers each file in Postgres
`parquet_files`. It does not write a combined `_all/<upload_id>.parquet` file.
Query planning uses the registry for exact file/schema/time pruning. The
registry is the only source: the service never lists the lake. A file is
visible only when its `attempt_id` equals `processed.attempt_id` for its
upload. With no `DATABASE_URL`, or before the migrations have run, reads
return an empty result.

Required columns: `ts` (ns epoch, or a native TIMESTAMP) and `topic`. Chart
value columns are whatever numeric value columns the registry recorded per
file. An empty result is typed with the `x,y,z` trio.

The processor's native `ts` column is a timezone-naive Arrow
`timestamp[ns]` whose clock value is UTC. Planner time-window predicates
normalize both that shape and legacy integer epoch-nanosecond columns to
DuckDB `TIMESTAMP_NS`, with exact integer `make_timestamp_ns(...)` bounds.
Consequently row pruning neither reinterprets native timestamps through the
host/session timezone nor rounds integer nanoseconds through floating-point
epoch seconds. Offset-less API bounds are treated as UTC, matching the lake
contract. Query connections still pin `TimeZone='UTC'` for UTC bucket labels
and display conversions.

Every timestamp in an API response is UTC ISO-8601 with a trailing `Z`
(`settings.iso_utc`). Postgres TIMESTAMPTZ values are converted to UTC first,
so a database with a non-UTC `TimeZone` still gives `Z` values. Naive values
(from DuckDB) are UTC by the lake contract.

## Metrics

- `query_service_requests_total{path,method,status}`
- `query_service_query_executions_total{type}`
- `query_engine_queries_executed_total{query_type,status}`
- `query_engine_duration_seconds{query_type}`
- Uploads-specific:
  - `uploads_list_requests_total{status}`
  - `uploads_detail_requests_total{status}`
  - `uploads_detail_duration_seconds`
  - `uploads_detail_rows_total_bucketed{bucket}` (buckets: `<=0, <=1, <=10, <=100, <=1000, <=10000, <=100000, >100000`)
  - `uploads_detail_files_scanned_bucketed{bucket}` (buckets: `<=0, <=1, <=5, <=10, <=20, <=50, <=100, >100`)
- Time-indexed cache (backs `GET /api/v1/robots/{robot_id}/data/{message_type}`):
  - `time_index_cache_hits_total{cache_type}` / `time_index_cache_misses_total{cache_type}`
  - `time_index_cache_evictions_total{reason}`
  - `time_index_query_duration_seconds`
  - `time_index_cache_memory_bytes`

## Security and Limits

- CORS allows only the two local Vite origins by default; add explicit trusted
  browser origins with `CORS_ALLOWED_ORIGINS` when developing from another host.
- Security headers: `X-Content-Type-Options=nosniff`, `X-Frame-Options=DENY`,
  `Referrer-Policy=no-referrer`, a `Content-Security-Policy`, and optional HSTS
  (`ENABLE_HSTS=true`). Scripts are self-hosted with no `unsafe-inline`; in
  real OIDC, `connect-src` adds only the configured token-endpoint origin.
- The SPA implements Authorization Code + S256 PKCE with single-use,
  ten-minute callback state. Real access/refresh tokens use `sessionStorage`,
  refresh is single-flighted across concurrent API calls, API bearer headers
  are restricted to the vault origin, and codes/refresh tokens leave that
  origin only for the configured token endpoint. Config and token calls abort
  after ten seconds.
- Per-IP rate limiting, windowed per minute, uses the same path-specific policy with either backend: `RATE_LIMIT_DEFAULT_PER_MINUTE` (falling back to `RATE_LIMIT_PER_MINUTE`, default `240`), `RATE_LIMIT_ADMIN_PER_MINUTE` (default `60`), and `RATE_LIMIT_PRESIGN_PER_MINUTE` (default `120`). `REDIS_URL` selects Redis-backed counters; without Redis, or while configured Redis is unavailable, the service uses in-memory counters with those same limits. Redis fallbacks increment `query_service_rate_limit_redis_degradations_total` and emit a once-per-minute redacted warning.
- The `bucket` query param on `/api/timeseries` and
  `/api/v1/robots/{robot_id}/stats/{message_type}` (a `date_trunc` granularity:
  `second|minute|hour|day`) is checked against a fixed allowlist before use,
  since DuckDB requires that argument as a SQL literal rather than a bind
  parameter. The caller-supplied `columns` identifiers are allowlisted against
  the registered/default schema before being inserted as quoted identifiers.
  Ordinary filter values such as `robot_id`, topic, and time bounds use DuckDB
  bind parameters. Invalid bucket or column values are rejected with `400`
  before the query runs.
- Read endpoints propagate genuine Postgres/S3 outages, authentication errors
  and DNS failures instead of silently returning an empty result. Only explicit
  unbootstrapped/absent-data states are empty: for example a missing `robots`
  table during initial migration or no registered Parquet objects for a
  requested robot.

## Implementation

- App: `phases/05-query-service/src/query_service/main.py`
- UI (SPA): source under `phases/05-query-service/src/ui-spa/` (Vite). The Docker build compiles to `/app/src/ui` which FastAPI serves at `/ui`.
- Package: `phases/05-query-service/pyproject.toml` (includes `mcap` for raw-bytes computation in detail views)

## Testing

- Unit tests under `phases/05-query-service/tests/unit`:
  - `test_registry_source.py` / `test_query_planner.py` — reads use only the published registry files, prune by robot, topic and time, and fail when a registered object is missing.
  - `test_time_index_cache.py` — the time-indexed cache key includes `columns`/`limit`, limited superset results cannot masquerade as complete contained windows, retained-object accounting enforces the quota, tracking maps stay bounded, compound indexes remain coherent under threads, and entries expire via TTL.
  - `test_middleware_hardening.py` / `test_obs_hardening.py` — request-body and Prometheus response bounds, atomic Redis counters, visible/redacted Redis fallback, and the absence of a generic Prometheus proxy.
  - `test_bucket_validation.py` — the `bucket` query param allowlist closes the `date_trunc` SQL-injection path; also drives both endpoints through FastAPI's `TestClient` to confirm the guard is wired in.
  - `test_error_vs_empty.py` — genuine backend outages surface as HTTP 500 instead of being swallowed into an empty result; also drives `list_robots` through FastAPI's `TestClient`.
  - `test_upload_files_contract.py` — `get_upload_files`/`get_upload_detail` return the field names/shapes the UI actually reads (`parquet_keys`, `ts_min`/`ts_max`).
  - `test_auth_browser_config.py` — real-browser inputs fail closed, runtime
    config is redacted, and CSP permits only the validated token origin.
- SPA Vitest includes `auth.test.ts` (PKCE, state, storage failure, refresh
  coordination, timeouts and origin boundaries) and `AuthControl.test.tsx`
  (sign-in/sign-out/development-mode UX).
- E2E covered in root `docs/TESTING.md` using Docker Compose.
