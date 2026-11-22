# Database schema

Use this summary to find what each table stores and which service writes it.
It is for developers and operators who investigate stored data. Do not build a
database from this summary.

The source of truth is `db/migrations/001_baseline.sql`. It builds the
complete release schema on an empty database. Later schema changes go in new
files (`002_*.sql`, `003_*.sql`, ...). Do not edit `001_baseline.sql` after a
release.

The runner `db/migrate.py` applies the files in filename order.
`make db-migrate` and the one-shot `db-migrate` Compose service run it.

## How the runner protects the schema

- The runner holds one Postgres advisory lock for the whole check-and-apply
  run, so two migrators cannot run at the same time.
- Each file runs in its own transaction.
- The `schema_version` table records the version, the exact filename and the
  SHA-256 of the SQL bytes. Both the filename and the checksum are mandatory.
- A renamed, removed or edited file that is already applied stops the run.
- Duplicate versions (for example `001` and `1`), duplicate names and badly
  named `.sql` files stop the run before it connects.
- A ledger with a gap (a later file applied, an earlier file missing) stops
  the run.

## Tables

### Robots

- **`robots`** — the fleet registry: `id` (VARCHAR(50), the same type as
  `uploads.robot_id`), `name`, `created_at`, JSONB `metadata`, JSONB
  `profile`, and the lifecycle columns `state`, `auth_mode`, `enrolled_at`,
  `state_changed_at`, `state_reason` and `updated_at`.
  - `uploads.robot_id` is a foreign key to `robots.id` with `ON UPDATE
    CASCADE` and no `ON DELETE` action. Removing a robot never deletes the
    record of its stored bytes.
  - `profile` holds nominal topic rates, kinematic limits and calibration
    dates for the QA checks. NULL means "no profile". A check never treats
    NULL as zero limits.
  - Provision robots with ingest's `POST /admin/robots`. Read the fleet with
    the query service's `GET /api/robots`.
- **`robot_credentials`** — per-robot bearer credentials. Only the SHA-256 of
  a token is stored. Rotation, supersede grace and revocation are recorded in
  place.
- **`robot_enrollment_codes`** — one-use enrollment codes. Only the SHA-256 of
  a code is stored. A row stays after use or revocation for audit.
- **`robot_lifecycle_events`** — the audit trail for robot lifecycle events.

### Uploads (ingest)

- **`uploads`** — one row per upload. This is ingest's system of record.
  - Columns include `client_upload_id` (the idempotency key), `robot_id`,
    `storage_path`, `file_size_bytes`, `bytes_received`, `sha256_hash`,
    `expected_sha256`, `status`, `integrity_verified` and
    `integrity_verification_basis`.
  - `client_upload_id` is UNIQUE. Ingest upserts on it with
    `ON CONFLICT (client_upload_id)`.
  - The client-declared digest is part of the upload reservation. A retry
    must match the upload id, robot, filename, size and digest before any S3
    write. An exact completed replay is idempotent. A conflicting reuse
    fails.
  - `status` is one of `pending`, `uploading`, `completed` or `failed`
    (`uploads_status_check`). The list is defined once in
    `phases/common-config/src/robot_vault_config/upload_status.py` and
    mirrored in `proto/upload.proto`. The wire-only `unknown` value is never
    stored.
  - `integrity_verification_basis` is `client_sha256` when
    `integrity_verified` is true, and `unverified` otherwise. Ingest writes
    `client_sha256` when it completes an upload; the client hash is mandatory.
    The trigger `trg_uploads_integrity_verification_basis` stops a completed,
    unverified upload from becoming verified later.
  - The processor claims only completed rows whose `integrity_verified` is
    true.
  - Five nullable capture-metadata columns hold operator-declared session
    context: `operator`, `site`, `permission_ref`, `session_tags` and
    `task_label`. They come from the robot's `vault.json` `capture` section
    through the manifest and `UploadMetadata` fields 11-15. NULL means "not
    declared".
- **`upload_multipart_sessions`** — one unfinished storage session per upload:
  the multipart ID, the session token, the pinned chunk size, the attempt
  generation, the phase and the expiry. A replacement session gets a new raw
  object key. A retry of the same session keeps its key. The upload advisory
  lock serializes transfers and cleanup. Completion removes the session in the
  same transaction that publishes the verified upload.
- **`upload_multipart_parts`** — accepted part number, byte range, ETag and
  SHA-256. Ingest inserts a receipt only after storage acknowledges the part.
  Deleting a session removes its receipts.
- **`manifests`** — content-addressed storage (CAS) metadata for one upload:
  `upload_id` (NOT NULL and UNIQUE), `file_sha256`, `cas_key`,
  `manifest_s3_path` and `storage_path`. Two uploads with identical bytes each
  keep their own row and manifest object, so `file_sha256` has a non-unique
  lookup index. This row is the GC root for
  `manifests/{robot_id}/{client_upload_id}.json`.

### Processing

- **`processing_status`** — one row per upload (unique on `upload_id`). It
  tracks `status` (`pending`, `processing`, `completed`, `completed_empty`,
  `failed`, `dead_letter`), `attempts` and a JSONB `metadata` blob.
  `status = 'processing'` is a renewable lease: `attempt_id` (the fencing
  token), `claimed_by` and `lease_expires_at` are then required. Heartbeats
  renew only the live token.
- **`processed`** — the publication pointer, keyed by `upload_id`.
  `attempt_id` is NOT NULL. The processor writes this row in the same
  transaction that marks the attempt completed.
- **`processed_topics`** — per-topic output: `upload_id`, `topic`,
  `rows_written`, `checksum`, `parquet_path` and `attempt_id` (NOT NULL).
- **`parquet_files`** — one row for every Parquet file the processor writes.
  The query service resolves every read through this table.
  - Every column that describes the file is NOT NULL: `upload_id`,
    `start_timestamp`, `end_timestamp`, `row_count`, `file_size_bytes`,
    `schema_version`, `schema_hash`, `column_names`, `all_null_columns`,
    `sha256` and `attempt_id`.
  - `kind` is always `topic`. Typed-family files use the reserved
    `message_type` namespace `_vN/<family>/<topic>`.
  - `sha256` is 64 lowercase hexadecimal characters
    (`parquet_files_sha256_format_check`). The processor tees it from the
    bytes as it writes them. It never re-reads the object. `schema_hash`
    describes the column layout, not the bytes.
  - Paths live below an `_attempts/{attempt_id}/` segment.
- **`episodes`** — `episode_id` (UUID), `upload_id` (`ON DELETE CASCADE`),
  `robot_id`, `t_start_ns`/`t_end_ns`, `n_messages`, `task_label`, `status`
  and `attempt_id` (NOT NULL).
  - `status` is one of `pending`, `segmented`, `qa_scored`, `exported` or
    `excluded` (`episodes_status_check`). The list is defined once in
    `phases/common-config/src/robot_vault_config/episode_status.py`.
  - `upload_id` is not unique: one upload can hold many episodes.
  - Pro segmentation deletes the attempt's episodes and inserts the new ones
    in one transaction.
- **`episode_topics`** — per-topic composition of an episode: `episode_id`
  (`ON DELETE CASCADE`), `topic`, `n_messages` and `achieved_hz`. Unique on
  `(episode_id, topic)`. `achieved_hz` is NULL (never 0.0) for an episode
  shorter than 1 s or a topic with fewer than 2 messages.
- **`media_files`** — encoded video chunks: `episode_id` (`ON DELETE
  CASCADE`), `topic`, `key`, `codec`, `container`, `sha256`, `n_frames`,
  `t_start_ns`/`t_end_ns`, `file_size_bytes`, `pii_status` and `attempt_id`
  (NOT NULL).
  - `key` is UNIQUE and is a bare S3 object key (no scheme, no bucket), for
    example
    `processed/media/{robot_id}/_attempts/{attempt_id}/{episode_id}/{topic}/chunk-000.mp4`.
  - `pii_status` is `raw` (default), `scrubbed` or `not_applicable`. The
    default PII backend leaves camera media raw. Pro PII scrubbing can produce
    `scrubbed` evidence.
- **`qa_reports`** — append-only QA results: `upload_id` or `episode_id`,
  `scope`, `defect_score`, `risk_score`, `subscores` (JSONB, default `{}`),
  `checks_version`, `detail_key`, `created_at` and `attempt_id` (NOT NULL).
  - `scope` is `upload` or `episode`. `qa_reports_scope_fk_check` requires
    exactly the matching foreign key: the other one must be NULL.
  - A re-score inserts a new row. There is no `updated_at` column. "Latest"
    means `ORDER BY created_at DESC`.
  - `detail_key` is a bare S3 key below
    `qa/reports/{robot_id}/_attempts/{attempt_id}/`.

### Datasets and exports

- **`datasets`** — immutable dataset cuts.
  - `dataset_id` (VARCHAR(64)) is the SHA-256 of the canonical `manifest`.
    The query cut API computes it.
  - `manifest` (JSONB NOT NULL) holds the name, `filter_spec`, parent,
    creation time, episode ids and per-artifact SHA-256 digests. Every
    Parquet entry carries a digest.
  - `datacard` (nullable JSONB) is the derived, human-facing description. It
    is not part of the `dataset_id` hash.
  - `parent_dataset_id` points at the version this cut replaces. The foreign
    key has no `ON DELETE` action.
  - `signature`/`signing_pubkey` can be NULL in development. Pro release
    preflight requires a valid Ed25519 key.
  - The trigger `datasets_immutable` rejects every UPDATE. A changed cut is a
    new row. DELETE is allowed, subject to the foreign keys.
- **`dataset_episodes`** — cut membership, unique on `(dataset_id,
  episode_id)`. The `episode_id` foreign key is `ON DELETE RESTRICT`: an
  episode in a cut cannot disappear through an upload-delete cascade.
- **`dataset_evals`** — append-only eval outcomes for a dataset: `name`,
  `outcome` (JSONB NOT NULL), `notes`, `created_at`.
- **`export_status`** — the exporter's queue, one row per `(dataset_id,
  format)`.
  - `format` is `lerobot_v3` or `mcap_passthrough`.
  - `status` is `pending`, `exporting`, `completed`, `failed` or
    `dead_letter`.
  - `status = 'exporting'` requires `attempt_id`, `claimed_by` and
    `lease_expires_at`. `attempt_id` is NULL while the row is pending.
- **`export_files`** — every object the exporter writes below `exports/`:
  `key` (UNIQUE, bare S3 key), `format`, `sha256`, `file_size_bytes`,
  `export_status_id` and `attempt_id`. The two ownership columns are both set
  or both NULL. `tools/s3_gc.py` reads this table to keep `exports/` objects.

## Processing-generation publication

`processed.attempt_id` is the authority pointer for the six attempt-owned
tables: `processed`, `processed_topics`, `parquet_files`, `episodes`,
`media_files` and `qa_reports`.

1. A worker writes only while it owns an unexpired
   `processing_status.attempt_id`. Its object keys are unique to that attempt.
2. Completion locks the claim, removes rows that other attempts own, and
   publishes the winning token through `processed.attempt_id` in the same
   transaction.
3. Readers show a derived row only when `derived.attempt_id =
   processed.attempt_id`. Partial, expired and replaced generations stay
   invisible.
4. Registry GC keeps a published generation and an unexpired live
   generation. Other objects become eligible only through the normal
   minimum-age and dry-run controls.

See `ARCHITECTURE.md` for the end-to-end data flow.
