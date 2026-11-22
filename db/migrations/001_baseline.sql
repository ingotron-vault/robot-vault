-- Migration 001: Robot Vault baseline schema.
--
-- This file builds the complete release schema on an empty database. It is
-- the baseline schema of the first public release, so it has no upgrade path,
-- backfill or legacy-row handling.
--
-- Rules that the rest of the system relies on:
--   * A derived row (processed, processed_topics, parquet_files, episodes,
--     media_files, qa_reports) always records the processing attempt that
--     wrote it. A row is published only when its attempt_id equals
--     processed.attempt_id for the same upload.
--   * Every parquet_files row carries the write-time SHA-256 of its bytes,
--     its size, row count, time range and column metadata.
--   * Status, kind and scope columns use CHECK constraints. Unit tests compare
--     these lists with the Python enums (phases/common-config) and the GC
--     wiring tests (phases/05-query-service/tests/unit/test_*_gc_wiring.py).
--
-- Change the schema by adding a new migration file (002_*.sql, ...). Do not
-- edit this file after a release: db/migrate.py records its checksum.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Shared trigger function for updated_at columns.
CREATE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- Robots
-- =====================================================================

-- `id` is VARCHAR(50) to match uploads.robot_id exactly; the FK needs
-- comparable types. `profile` holds nominal topic rates, kinematic limits
-- and calibration dates for QA checks. NULL means "no profile configured".
CREATE TABLE robots (
    id               VARCHAR(50) PRIMARY KEY,
    name             TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata         JSONB,
    profile          JSONB,
    state            TEXT NOT NULL DEFAULT 'enrolled',
    auth_mode        TEXT NOT NULL DEFAULT 'shared',
    enrolled_at      TIMESTAMPTZ,
    state_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state_reason     TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT robots_auth_mode_check
        CHECK (auth_mode IN ('shared', 'per_robot')),
    CONSTRAINT robots_enrolled_at_check
        CHECK (state <> 'enrolled' OR auth_mode = 'shared' OR enrolled_at IS NOT NULL),
    CONSTRAINT robots_state_check
        CHECK (state IN ('pending', 'enrolled', 'disabled', 'decommissioned'))
);

CREATE INDEX idx_robots_created_at ON robots(created_at DESC);
CREATE INDEX idx_robots_state ON robots(state);

COMMENT ON COLUMN robots.state IS
    'Registry lifecycle state: pending, enrolled, disabled, or decommissioned.';
COMMENT ON COLUMN robots.auth_mode IS
    'Authentication mode: shared for legacy fleet-token robots or per_robot for a robot credential.';
COMMENT ON COLUMN robots.enrolled_at IS
    'Timestamp of per-robot credential enrollment; legacy shared robots may be enrolled without it.';

-- Per-robot credentials. expires_at is NULL for a normal credential and is
-- set for a superseded credential during its rotation overlap.
CREATE TABLE robot_credentials (
    credential_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    robot_id               VARCHAR(50) NOT NULL
                             REFERENCES robots(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    token_sha256           BYTEA NOT NULL,
    fingerprint            VARCHAR(12) NOT NULL,
    issued_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    issued_via             TEXT NOT NULL,
    rotated_from           UUID REFERENCES robot_credentials(credential_id),
    first_used_at          TIMESTAMPTZ,
    last_used_at           TIMESTAMPTZ,
    expires_at             TIMESTAMPTZ,
    superseded_at          TIMESTAMPTZ,
    superseded_grace_until TIMESTAMPTZ,
    revoked_at             TIMESTAMPTZ,
    revoked_reason         TEXT,
    CONSTRAINT robot_credentials_token_sha256_len
        CHECK (octet_length(token_sha256) = 32),
    CONSTRAINT robot_credentials_issued_via_check
        CHECK (issued_via IN ('enrollment', 'rotation')),
    CONSTRAINT robot_credentials_supersede_pair
        CHECK ((superseded_at IS NULL) = (superseded_grace_until IS NULL)),
    CONSTRAINT robot_credentials_supersede_expiry
        CHECK (superseded_at IS NULL OR expires_at = superseded_grace_until),
    CONSTRAINT robot_credentials_expiry_after_issue
        CHECK (expires_at IS NULL OR expires_at > issued_at),
    CONSTRAINT robot_credentials_fingerprint_format
        CHECK (fingerprint ~ '^[0-9a-f]{12}$')
);

CREATE UNIQUE INDEX ux_robot_credentials_token_sha256
    ON robot_credentials(token_sha256);
CREATE INDEX idx_robot_credentials_live
    ON robot_credentials(robot_id) WHERE revoked_at IS NULL;

COMMENT ON COLUMN robot_credentials.token_sha256 IS
    'SHA-256 of the raw per-robot bearer token; the plaintext token is never stored.';
COMMENT ON COLUMN robot_credentials.fingerprint IS
    'First 12 lowercase hexadecimal characters of token_sha256 for logs and operator display.';
COMMENT ON COLUMN robot_credentials.expires_at IS
    'NULL means no expiry; a superseded credential expires exactly at superseded_grace_until.';

-- One-use enrollment codes. Hashes stay after use or revocation for audit.
CREATE TABLE robot_enrollment_codes (
    code_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    robot_id               VARCHAR(50) NOT NULL
                             REFERENCES robots(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    code_sha256            BYTEA NOT NULL,
    fingerprint            VARCHAR(12) NOT NULL,
    purpose                TEXT NOT NULL DEFAULT 'enroll',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by             TEXT,
    expires_at             TIMESTAMPTZ NOT NULL,
    consumed_at            TIMESTAMPTZ,
    consumed_from_ip       INET,
    consumed_credential_id UUID REFERENCES robot_credentials(credential_id),
    revoked_at             TIMESTAMPTZ,
    revoked_reason         TEXT,
    CONSTRAINT robot_enrollment_codes_hash_len
        CHECK (octet_length(code_sha256) = 32),
    CONSTRAINT robot_enrollment_codes_ttl
        CHECK (expires_at > created_at),
    CONSTRAINT robot_enrollment_codes_purpose
        CHECK (purpose IN ('enroll', 'reenroll')),
    CONSTRAINT robot_enrollment_codes_consumed_pair
        CHECK ((consumed_at IS NULL) = (consumed_credential_id IS NULL))
);

CREATE UNIQUE INDEX ux_robot_enrollment_codes_code_sha256
    ON robot_enrollment_codes(code_sha256);
CREATE INDEX idx_robot_enrollment_codes_open
    ON robot_enrollment_codes(robot_id, expires_at)
    WHERE consumed_at IS NULL AND revoked_at IS NULL;

COMMENT ON COLUMN robot_enrollment_codes.code_sha256 IS
    'SHA-256 of the one-use enrollment code; the plaintext code is never stored.';
COMMENT ON COLUMN robot_enrollment_codes.fingerprint IS
    'First 12 lowercase hexadecimal characters of code_sha256 for logs and operator display.';

CREATE TABLE robot_lifecycle_events (
    event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    robot_id    VARCHAR(50) NOT NULL
                  REFERENCES robots(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    event       TEXT NOT NULL,
    actor       TEXT,
    detail      JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_robot_lifecycle_events_robot
    ON robot_lifecycle_events(robot_id, occurred_at DESC);

COMMENT ON COLUMN robot_lifecycle_events.event IS
    'Lifecycle audit event: created, code_minted, code_revoked, code_redeemed, code_rejected, enrolled, rotated, credential_revoked, disabled, enabled, decommissioned, reinstated, or deregistered.';

-- =====================================================================
-- Uploads (ingest)
-- =====================================================================

-- `status` values are PERSISTED_UPLOAD_STATUSES in
-- phases/common-config/src/robot_vault_config/upload_status.py, in order.
-- `unknown` is a wire-only value for "no such upload" and is never stored.
-- `client_upload_id` is UNIQUE because ingest upserts with
-- ON CONFLICT (client_upload_id); no second index is needed.
-- The robot FK has no ON DELETE action: deleting a robot must never delete
-- the record of its stored bytes.
CREATE TABLE uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_upload_id TEXT UNIQUE,
    robot_id VARCHAR(50) NOT NULL
        CONSTRAINT uploads_robot_id_fkey
        REFERENCES robots(id) ON UPDATE CASCADE,
    original_filename VARCHAR(255) NOT NULL,
    storage_path VARCHAR(500) NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    sha256_hash VARCHAR(64),
    content_type VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'uploading',
    error_message TEXT,
    upload_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    upload_completed_at TIMESTAMPTZ,
    chunk_size_bytes INTEGER DEFAULT 5242880,
    bytes_received BIGINT DEFAULT 0,
    chunks_received INTEGER DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    integrity_verified BOOLEAN NOT NULL DEFAULT false,
    operator TEXT,
    site TEXT,
    permission_ref TEXT,
    session_tags TEXT,
    task_label TEXT,
    expected_sha256 VARCHAR(64),
    integrity_verification_basis TEXT NOT NULL DEFAULT 'unverified',
    CONSTRAINT uploads_expected_sha256_format_check
        CHECK (expected_sha256 IS NULL OR expected_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uploads_integrity_verification_basis_check
        CHECK (
            (integrity_verified IS FALSE
                AND integrity_verification_basis = 'unverified')
            OR
            (integrity_verified IS TRUE
                AND integrity_verification_basis = 'client_sha256')
        ),
    CONSTRAINT uploads_status_check
        CHECK (status IN ('pending', 'uploading', 'completed', 'failed'))
);

CREATE INDEX idx_uploads_robot_id ON uploads(robot_id);
CREATE INDEX idx_uploads_status ON uploads(status);
CREATE INDEX idx_uploads_created_at ON uploads(created_at DESC);
CREATE INDEX idx_uploads_updated_at ON uploads(updated_at DESC);
CREATE INDEX idx_uploads_robot_status ON uploads(robot_id, status);
CREATE INDEX idx_uploads_storage_path ON uploads USING HASH(storage_path);

CREATE TRIGGER update_uploads_updated_at
    BEFORE UPDATE ON uploads
    FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();

COMMENT ON COLUMN uploads.expected_sha256 IS
    'Lowercase client-declared (or unary server-computed) SHA-256 reserved before S3 object I/O; immutable for a client_upload_id.';
COMMENT ON COLUMN uploads.integrity_verification_basis IS
    'Why the processing integrity gate is satisfied: client_sha256 (the client-declared SHA-256 matched the stored bytes) or unverified.';

-- The integrity basis has two values. The CHECK above keeps it consistent
-- with integrity_verified. This trigger adds two rules:
--   * An INSERT that sets integrity_verified = TRUE and leaves the default
--     basis means "client SHA-256 matched" (fixtures and imports use this).
--   * A completed upload that was stored unverified can never become
--     verified later. Only ingest's completing write can verify an upload.
CREATE FUNCTION uploads_enforce_integrity_verification_basis()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT'
       AND NEW.integrity_verified IS TRUE
       AND NEW.integrity_verification_basis = 'unverified' THEN
        NEW.integrity_verification_basis := 'client_sha256';
    END IF;

    IF TG_OP = 'UPDATE'
       AND OLD.status = 'completed'
       AND OLD.integrity_verified IS FALSE
       AND NEW.integrity_verified IS TRUE THEN
        RAISE EXCEPTION
            'completed unverified upload % cannot become verified later',
            OLD.id;
    END IF;

    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_uploads_integrity_verification_basis
    BEFORE INSERT OR UPDATE OF integrity_verified, integrity_verification_basis
    ON uploads
    FOR EACH ROW
    EXECUTE FUNCTION uploads_enforce_integrity_verification_basis();

-- Durable multipart receipts. Multipart bytes belong to one uploads row; no
-- progress is acknowledged until its S3 UploadPart call succeeds.
CREATE TABLE upload_multipart_sessions (
    upload_id UUID PRIMARY KEY REFERENCES uploads(id) ON DELETE CASCADE,
    session_id UUID NOT NULL UNIQUE,
    s3_upload_id TEXT NOT NULL CHECK (length(s3_upload_id) > 0),
    chunk_size_bytes BIGINT NOT NULL CHECK (chunk_size_bytes BETWEEN 5242880 AND 8388608),
    attempt_generation BIGINT NOT NULL DEFAULT 0 CHECK (attempt_generation >= 0),
    state TEXT NOT NULL DEFAULT 'uploading' CHECK (state IN ('uploading', 'finalizing', 'expiring')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '7 days'
);

CREATE INDEX idx_upload_multipart_sessions_expiry
    ON upload_multipart_sessions(expires_at);

CREATE TABLE upload_multipart_parts (
    upload_id UUID NOT NULL REFERENCES upload_multipart_sessions(upload_id) ON DELETE CASCADE,
    part_number INTEGER NOT NULL CHECK (part_number BETWEEN 1 AND 10000),
    offset_bytes BIGINT NOT NULL CHECK (offset_bytes >= 0),
    size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 1 AND 8388608),
    etag TEXT NOT NULL CHECK (length(etag) > 0),
    sha256_hash TEXT NOT NULL CHECK (sha256_hash ~ '^[0-9a-f]{64}$'),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (upload_id, part_number)
);

-- CAS manifest registry. upload_id is the owner and the idempotency key.
-- file_sha256 is a non-unique lookup key: distinct uploads may hold
-- identical bytes, and each upload keeps its own manifest object.
CREATE TABLE manifests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id UUID NOT NULL UNIQUE REFERENCES uploads(id) ON DELETE CASCADE,
    file_sha256 VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    content_type VARCHAR(100),
    cas_key TEXT NOT NULL,
    manifest_s3_path TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_manifests_file_sha256 ON manifests(file_sha256);

COMMENT ON COLUMN manifests.upload_id IS
    'Authoritative per-upload owner; exactly one manifest registry row may exist for each upload.';
COMMENT ON COLUMN manifests.file_sha256 IS
    'Content digest lookup key; intentionally non-unique because distinct uploads may contain identical bytes.';

-- =====================================================================
-- Processing (claims, publication, derived registries)
-- =====================================================================

-- One row per upload. status='processing' is a renewable lease whose
-- attempt_id is the fencing token for every derived write.
CREATE TABLE processing_status (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id UUID REFERENCES uploads(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    attempts INTEGER NOT NULL DEFAULT 0,
    attempt_id UUID,
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    CONSTRAINT processing_status_active_claim_check
        CHECK (
            status <> 'processing'
            OR (
                attempt_id IS NOT NULL
                AND claimed_by IS NOT NULL
                AND lease_expires_at IS NOT NULL
            )
        ),
    CONSTRAINT processing_status_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'completed_empty', 'failed', 'dead_letter'))
);

CREATE UNIQUE INDEX ux_processing_status_upload_id ON processing_status(upload_id);
CREATE UNIQUE INDEX ux_processing_status_attempt_id
    ON processing_status(attempt_id)
    WHERE attempt_id IS NOT NULL;
CREATE INDEX idx_processing_status_recoverable_lease
    ON processing_status(lease_expires_at)
    WHERE status = 'processing';

-- The publication pointer. The processor writes this row in the same
-- transaction that marks the attempt completed.
CREATE TABLE processed (
    upload_id UUID PRIMARY KEY REFERENCES uploads(id) ON DELETE CASCADE,
    completed_at TIMESTAMPTZ,
    rows_written BIGINT,
    attempt_id UUID NOT NULL
);

CREATE INDEX idx_processed_attempt_id ON processed(attempt_id);

COMMENT ON COLUMN processed.attempt_id IS
    'Authoritative processing generation; derived rows are published only when their attempt_id matches it.';

CREATE TABLE processed_topics (
    upload_id UUID REFERENCES uploads(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    rows_written BIGINT NOT NULL,
    checksum TEXT NOT NULL,
    parquet_path TEXT NOT NULL,
    attempt_id UUID NOT NULL,
    PRIMARY KEY (upload_id, topic)
);

CREATE INDEX idx_processed_topics_attempt_owner
    ON processed_topics(upload_id, attempt_id);

COMMENT ON COLUMN processed_topics.attempt_id IS
    'Processing generation that registered this topic output';

-- Registry of every Parquet file the processor writes. `kind` is always
-- 'topic' (per-topic partitions and typed-family files). Typed files use the
-- reserved message_type namespace `_vN/<family>/<topic>`.
-- `schema_hash` describes the column layout. `sha256` is the content digest
-- that the processor tees from the write itself.
CREATE TABLE parquet_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path TEXT UNIQUE NOT NULL,
    robot_id VARCHAR(255) NOT NULL,
    message_type VARCHAR(255) NOT NULL,
    start_timestamp TIMESTAMPTZ NOT NULL,
    end_timestamp TIMESTAMPTZ NOT NULL,
    row_count BIGINT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    schema_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    upload_id UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'topic',
    schema_version INTEGER NOT NULL DEFAULT 1,
    column_names TEXT[] NOT NULL,
    all_null_columns TEXT[] NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    attempt_id UUID NOT NULL,
    CONSTRAINT parquet_files_kind_check CHECK (kind = 'topic'),
    CONSTRAINT parquet_files_sha256_format_check CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX idx_parquet_files_robot_type ON parquet_files(robot_id, message_type);
CREATE INDEX idx_parquet_files_upload_id ON parquet_files(upload_id);
CREATE INDEX idx_parquet_files_start_timestamp ON parquet_files(start_timestamp);
CREATE INDEX idx_parquet_files_sha256 ON parquet_files(sha256);
CREATE INDEX idx_parquet_files_attempt_owner ON parquet_files(upload_id, attempt_id);

CREATE TRIGGER update_parquet_files_updated_at
    BEFORE UPDATE ON parquet_files
    FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();

COMMENT ON COLUMN parquet_files.sha256 IS
    'Lowercase SHA-256 of the Parquet bytes, observed by the processor during the write.';
COMMENT ON COLUMN parquet_files.attempt_id IS
    'Processing generation that registered this object';

-- `status` values are PERSISTED_EPISODE_STATUSES in
-- phases/common-config/src/robot_vault_config/episode_status.py, in order.
-- An upload can hold many episodes, so upload_id is not unique.
CREATE TABLE episodes (
    episode_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    robot_id VARCHAR(50) NOT NULL,
    t_start_ns BIGINT,
    t_end_ns BIGINT,
    n_messages BIGINT,
    task_label TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_id UUID NOT NULL,
    CONSTRAINT episodes_status_check
        CHECK (status IN ('pending', 'segmented', 'qa_scored', 'exported', 'excluded'))
);

CREATE INDEX idx_episodes_robot_status ON episodes(robot_id, status);
CREATE INDEX idx_episodes_upload_id ON episodes(upload_id);
CREATE INDEX idx_episodes_t_start_ns ON episodes(t_start_ns);
CREATE INDEX idx_episodes_attempt_owner ON episodes(upload_id, attempt_id);

CREATE TRIGGER update_episodes_updated_at
    BEFORE UPDATE ON episodes
    FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();

COMMENT ON COLUMN episodes.attempt_id IS
    'Processing generation that segmented this episode';

CREATE TABLE episode_topics (
    episode_id UUID NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    n_messages BIGINT,
    achieved_hz DOUBLE PRECISION,
    UNIQUE (episode_id, topic)
);

-- Encoded video chunks. `key` is a bare S3 object key (no scheme, no bucket).
-- `pii_status` values:
--   raw            -- encoded from source, no PII processing applied.
--   scrubbed       -- a PII scrubber processed this chunk.
--   not_applicable -- the topic is known not to carry PII.
CREATE TABLE media_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id UUID NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    key TEXT UNIQUE NOT NULL,
    codec TEXT NOT NULL,
    container TEXT NOT NULL DEFAULT 'mp4',
    sha256 VARCHAR(64),
    n_frames BIGINT,
    t_start_ns BIGINT,
    t_end_ns BIGINT,
    file_size_bytes BIGINT,
    pii_status TEXT NOT NULL DEFAULT 'raw',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_id UUID NOT NULL,
    CONSTRAINT media_files_pii_status_check
        CHECK (pii_status IN ('raw', 'scrubbed', 'not_applicable'))
);

CREATE INDEX idx_media_files_episode_id ON media_files(episode_id);
CREATE INDEX idx_media_files_topic ON media_files(topic);
CREATE INDEX idx_media_files_pii_status ON media_files(pii_status);
CREATE INDEX idx_media_files_attempt_id ON media_files(attempt_id);

CREATE TRIGGER update_media_files_updated_at
    BEFORE UPDATE ON media_files
    FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();

COMMENT ON COLUMN media_files.attempt_id IS
    'Processing generation that registered this media object';

-- QA reports are append-only: a re-score inserts a new row. A report scores
-- exactly one upload or one episode:
--   upload   -- scores a whole upload.
--   episode  -- scores a single episode.
-- `detail_key` is a bare S3 object key.
CREATE TABLE qa_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id UUID REFERENCES uploads(id) ON DELETE CASCADE,
    episode_id UUID REFERENCES episodes(episode_id) ON DELETE CASCADE,
    scope TEXT NOT NULL,
    defect_score DOUBLE PRECISION,
    risk_score DOUBLE PRECISION,
    subscores JSONB NOT NULL DEFAULT '{}'::jsonb,
    checks_version TEXT,
    detail_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_id UUID NOT NULL,
    CONSTRAINT qa_reports_scope_check
        CHECK (scope IN ('upload', 'episode')),
    CONSTRAINT qa_reports_scope_fk_check
        CHECK (
            (scope = 'upload' AND upload_id IS NOT NULL AND episode_id IS NULL)
            OR
            (scope = 'episode' AND episode_id IS NOT NULL AND upload_id IS NULL)
        )
);

CREATE INDEX idx_qa_reports_episode_created ON qa_reports(episode_id, created_at DESC);
CREATE INDEX idx_qa_reports_upload_created ON qa_reports(upload_id, created_at DESC);
CREATE INDEX idx_qa_reports_attempt_id ON qa_reports(attempt_id);

COMMENT ON COLUMN qa_reports.attempt_id IS
    'Processing generation that published this QA report';

-- =====================================================================
-- Datasets (cuts) and exports
-- =====================================================================

-- `dataset_id` is the SHA-256 of the canonical manifest, computed by the cut
-- API. A changed cut is a new row with parent_dataset_id set; rows are never
-- updated. The parent FK has no ON DELETE action so lineage cannot be cut by
-- accident. `datacard` is the derived, unhashed description.
CREATE TABLE datasets (
    dataset_id VARCHAR(64) PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT,
    filter_spec JSONB,
    manifest JSONB NOT NULL,
    parent_dataset_id VARCHAR(64) REFERENCES datasets(dataset_id),
    signature TEXT,
    signing_pubkey TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    datacard JSONB
);

CREATE INDEX idx_datasets_parent_dataset_id ON datasets(parent_dataset_id);
CREATE INDEX idx_datasets_created_at ON datasets(created_at DESC);

CREATE FUNCTION datasets_prevent_update()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'datasets is an append-only, content-addressed registry: row '
        'dataset_id=% cannot be UPDATEd. A changed cut is a NEW dataset row '
        'with parent_dataset_id set to this one, not a mutation of it.',
        OLD.dataset_id;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER datasets_immutable
    BEFORE UPDATE ON datasets
    FOR EACH ROW
    EXECUTE PROCEDURE datasets_prevent_update();

-- Episode membership of a cut. The episode FK is RESTRICT: an episode that a
-- dataset cites cannot disappear through an upload cascade.
CREATE TABLE dataset_episodes (
    dataset_id VARCHAR(64) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    episode_id UUID NOT NULL REFERENCES episodes(episode_id) ON DELETE RESTRICT,
    UNIQUE (dataset_id, episode_id)
);

CREATE INDEX idx_dataset_episodes_episode_id ON dataset_episodes(episode_id);

-- Append-only eval outcomes attached to a dataset.
CREATE TABLE dataset_evals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id VARCHAR(64) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    outcome JSONB NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_dataset_evals_dataset_created ON dataset_evals(dataset_id, created_at DESC);

-- Exporter queue: one row per (dataset_id, format). status='exporting' is a
-- renewable lease whose attempt_id fences every write.
-- `format` values:
--   lerobot_v3        -- the LeRobot v3.0 native writer.
--   mcap_passthrough  -- re-packages the source MCAP files of a cut.
-- `status` values:
--   pending, exporting, completed, failed, dead_letter
CREATE TABLE export_status (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id VARCHAR(64) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    metadata JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_id UUID,
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    UNIQUE (dataset_id, format),
    CONSTRAINT export_status_active_claim_check
        CHECK (
            status <> 'exporting'
            OR (
                attempt_id IS NOT NULL
                AND claimed_by IS NOT NULL
                AND lease_expires_at IS NOT NULL
            )
        ),
    CONSTRAINT export_status_format_check
        CHECK (format IN ('lerobot_v3', 'mcap_passthrough')),
    CONSTRAINT export_status_status_check
        CHECK (status IN ('pending', 'exporting', 'completed', 'failed', 'dead_letter'))
);

CREATE INDEX idx_export_status_dataset_id ON export_status(dataset_id);
CREATE INDEX idx_export_status_status ON export_status(status);
CREATE UNIQUE INDEX ux_export_status_attempt_id
    ON export_status(attempt_id)
    WHERE attempt_id IS NOT NULL;
CREATE INDEX idx_export_status_recoverable_lease
    ON export_status(lease_expires_at)
    WHERE status = 'exporting';

CREATE TRIGGER update_export_status_updated_at
    BEFORE UPDATE ON export_status
    FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();

-- Every object the exporter writes under exports/{dataset_id}/{format}/.
-- `key` is a bare S3 object key. `format` is a denormalized copy of the
-- export run's format.
CREATE TABLE export_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset_id VARCHAR(64) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    key TEXT UNIQUE NOT NULL,
    sha256 VARCHAR(64),
    file_size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    export_status_id UUID
        CONSTRAINT export_files_export_status_fk
        REFERENCES export_status(id) ON DELETE CASCADE,
    attempt_id UUID,
    CONSTRAINT export_files_attempt_ownership_check
        CHECK (
            (export_status_id IS NULL AND attempt_id IS NULL)
            OR (export_status_id IS NOT NULL AND attempt_id IS NOT NULL)
        )
);

CREATE INDEX idx_export_files_dataset_id ON export_files(dataset_id);
CREATE INDEX idx_export_files_attempt_owner
    ON export_files(export_status_id, attempt_id)
    WHERE export_status_id IS NOT NULL;
