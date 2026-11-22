"""Live-Postgres integration tests for the schema migration contract.

Every test creates and drops its own throwaway database.  The configured
database and its data are never modified.

Run with::

    MIGRATION_TEST_DATABASE_URL=postgres://robotvault:robotvault@postgres:5432/robotvault \
        python -m pytest db/migrate_test.py -v

`MIGRATION_TEST_DATABASE_URL` is deliberately separate from the runtime
`DATABASE_URL`: the repository unit gate collects processor tests in the same
pytest process, and setting the runtime variable would make those isolated
tests perform unintended live database writes.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip(
    "psycopg",
    reason=(
        "live migration tests require psycopg[binary]; use the locked dev "
        "image with `make test-unit`"
    ),
)
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate  # noqa: E402

PROCESSOR_SRC = (
    Path(__file__).resolve().parent.parent
    / "phases"
    / "04-processing-pipeline"
    / "src"
)
sys.path.insert(0, str(PROCESSOR_SRC))
from processor.claim_fence import (  # noqa: E402
    PROCESSING_CLAIM_SQL,
    ProcessingClaimLostError,
    lock_processing_claim,
    prune_processing_claims,
)

ADMIN_DATABASE_URL = os.environ.get(
    "MIGRATION_TEST_DATABASE_URL",
    "postgres://robotvault:robotvault@localhost:5433/robotvault",
)


def _admin_connect():
    """Connect to the configured database to create/drop throwaway peers."""
    return psycopg.connect(ADMIN_DATABASE_URL, autocommit=True)


def _db_url_for(dbname: str) -> str:
    """Replace dbname without breaking URL escaping or query parameters."""
    params = conninfo_to_dict(ADMIN_DATABASE_URL)
    params["dbname"] = dbname
    return make_conninfo(**params)


@pytest.fixture
def scratch_database_url():
    test_db_name = f"robotvault_migtest_{uuid.uuid4().hex[:12]}"
    admin_conn = _admin_connect()
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{test_db_name}"')
        try:
            yield _db_url_for(test_db_name)
        finally:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (test_db_name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{test_db_name}"')
    finally:
        admin_conn.close()


def _write_migration(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def test_migration_runner_is_idempotent_and_records_integrity(scratch_database_url):
    applied_first = migrate.run_migrations(scratch_database_url)
    assert applied_first == ["001"]

    with psycopg.connect(scratch_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, name, checksum_sha256 "
                "FROM schema_version ORDER BY version"
            )
            ledger = cur.fetchall()
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = 'uploads'"
            )
            assert cur.fetchone() is not None

    assert [row[0] for row in ledger] == applied_first
    assert all(name and name.endswith(".sql") for _, name, _ in ledger)
    assert all(
        checksum and len(checksum) == 64
        for _, _, checksum in ledger
    )
    with psycopg.connect(scratch_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'schema_version'
                  AND column_name IN ('name', 'checksum_sha256')
                ORDER BY column_name
                """
            )
            assert cur.fetchall() == [
                ("checksum_sha256", "NO"),
                ("name", "NO"),
            ]

    applied_second = migrate.run_migrations(scratch_database_url)
    assert applied_second == []
    with psycopg.connect(scratch_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, name, checksum_sha256 "
                "FROM schema_version ORDER BY version"
            )
            assert cur.fetchall() == ledger


def test_foundations_lifecycle_invariants(scratch_database_url):
    """The schema, rather than callers, owns the lifecycle state machines."""
    migrate.run_migrations(scratch_database_url)
    with psycopg.connect(scratch_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # The robots checks reject unknown states and an enrolled
            # per-robot record that omits the durable enrollment instant.
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO robots (id, name, state) "
                    "VALUES ('bad-state', 'Bad state', 'invented')"
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO robots (id, name, state, auth_mode) "
                    "VALUES ('unmarked-enrolled', 'Unmarked', 'enrolled', 'per_robot')"
                )
            cur.execute(
                "INSERT INTO robots (id, name, state, auth_mode, enrolled_at) "
                "VALUES ('per-robot-enrolled', 'Per robot', 'enrolled', 'per_robot', NOW())"
            )

            # The first admin comes from the operator's IdP, so the baseline
            # has no bootstrap table for it.
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'appli" "ance%'"
            )
            assert cur.fetchall() == []


def test_renamed_and_partially_recorded_migrations_fail_closed(
    scratch_database_url, tmp_path: Path
):
    path = _write_migration(tmp_path, "001_original.sql", "SELECT 1;\n")
    assert migrate.run_migrations(scratch_database_url, tmp_path) == ["001"]

    renamed = tmp_path / "001_renamed.sql"
    path.rename(renamed)
    with pytest.raises(migrate.MigrationIntegrityError, match="was recorded as"):
        migrate.run_migrations(scratch_database_url, tmp_path)

    renamed.rename(path)
    with psycopg.connect(scratch_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Simulate a corrupted ledger.  A normal writer cannot create this
            # state because both integrity columns are NOT NULL.
            cur.execute(
                "ALTER TABLE schema_version "
                "ALTER COLUMN checksum_sha256 DROP NOT NULL"
            )
            cur.execute(
                "UPDATE schema_version SET checksum_sha256 = NULL WHERE version = '001'"
            )
    with pytest.raises(
        migrate.MigrationIntegrityError, match="missing integrity metadata"
    ):
        migrate.run_migrations(scratch_database_url, tmp_path)


def test_missing_earlier_migration_is_not_applied_after_a_later_one(
    scratch_database_url, tmp_path: Path
):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    assert migrate.run_migrations(scratch_database_url, tmp_path) == ["001", "002"]

    with psycopg.connect(scratch_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM schema_version WHERE version = '001'")

    with pytest.raises(migrate.MigrationIntegrityError, match="out-of-order gap"):
        migrate.run_migrations(scratch_database_url, tmp_path)


def test_concurrent_migrators_are_serialized(scratch_database_url, tmp_path: Path):
    _write_migration(
        tmp_path,
        "001_race_probe.sql",
        "SELECT pg_sleep(0.25);\n"
        "CREATE TABLE race_probe (id INTEGER PRIMARY KEY);\n",
    )
    start = threading.Barrier(2)

    def run():
        start.wait(timeout=5)
        return migrate.run_migrations(scratch_database_url, tmp_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(results, key=len) == [[], ["001"]]
    with psycopg.connect(scratch_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM schema_version")
            assert cur.fetchone() == (1,)
            cur.execute("SELECT to_regclass('public.race_probe')")
            assert cur.fetchone()[0] == "race_probe"


def test_manifest_registry_is_per_upload_while_digest_is_shareable(
    scratch_database_url,
):
    """The schema keeps one GC root for every upload manifest object."""
    migrate.run_migrations(scratch_database_url)
    digest = "a" * 64
    with psycopg.connect(scratch_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO robots (id, name) VALUES ('cas-test', 'CAS test')"
            )
            upload_ids = []
            for suffix in ("one", "two"):
                cur.execute(
                    """
                    INSERT INTO uploads
                        (client_upload_id, robot_id, original_filename,
                         storage_path, file_size_bytes, sha256_hash,
                         expected_sha256, status)
                    VALUES (%s, 'cas-test', %s, %s, 4, %s, %s, 'completed')
                    RETURNING id
                    """,
                    (
                        f"upload-{suffix}",
                        f"{suffix}.mcap",
                        f"s3://robotics-data/cas/files/{digest}",
                        digest,
                        digest,
                    ),
                )
                upload_ids.append(cur.fetchone()[0])

            for suffix, upload_id in zip(("one", "two"), upload_ids):
                cur.execute(
                    """
                    INSERT INTO manifests
                        (upload_id, file_sha256, size_bytes, content_type,
                         cas_key, manifest_s3_path, storage_path)
                    VALUES (%s, %s, 4, 'application/octet-stream', %s, %s, %s)
                    """,
                    (
                        upload_id,
                        digest,
                        f"cas/files/{digest}",
                        f"s3://robotics-data/manifests/cas-test/upload-{suffix}.json",
                        f"s3://robotics-data/cas/files/{digest}",
                    ),
                )

            cur.execute(
                "SELECT upload_id, file_sha256 FROM manifests "
                "WHERE file_sha256 = %s ORDER BY manifest_s3_path",
                (digest,),
            )
            assert cur.fetchall() == [
                (upload_ids[0], digest),
                (upload_ids[1], digest),
            ]
            cur.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='manifests' "
                "AND column_name='upload_id'"
            )
            assert cur.fetchone() == ("NO",)
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND indexname='idx_manifests_file_sha256'"
            )
            indexdef = cur.fetchone()[0]
            assert " UNIQUE " not in f" {indexdef.upper()} "

            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO manifests
                        (upload_id, file_sha256, size_bytes, cas_key,
                         manifest_s3_path, storage_path)
                    VALUES (%s, %s, 4, %s, 's3://robotics-data/manifests/duplicate.json', %s)
                    """,
                    (
                        upload_ids[0],
                        digest,
                        f"cas/files/{digest}",
                        f"s3://robotics-data/cas/files/{digest}",
                    ),
                )

            with pytest.raises(psycopg.errors.NotNullViolation):
                cur.execute(
                    """
                    INSERT INTO manifests
                        (upload_id, file_sha256, size_bytes, cas_key,
                         manifest_s3_path, storage_path)
                    VALUES (NULL, %s, 4, %s, 's3://robotics-data/manifests/ownerless.json', %s)
                    """,
                    (
                        digest,
                        f"cas/files/{digest}",
                        f"s3://robotics-data/cas/files/{digest}",
                    ),
                )


def test_processor_generation_handoff_fences_a_worker_across_lease_expiry(
    scratch_database_url,
):
    """Two real transactions cross a lease boundary without sharing output.

    Worker A owns and locks the row before expiry, then pauses beyond it.
    Worker B begins the exact production claim statement after expiry and
    waits for A. A's already-started registry transaction may commit, but B's
    claim and old-generation prune commit atomically afterwards. Any later A
    publication is rejected; only B can register and become authoritative.
    """

    migrate.run_migrations(scratch_database_url)
    attempt_a = "10000000-0000-0000-0000-000000000001"

    with psycopg.connect(scratch_database_url) as setup:
        with setup.cursor() as cur:
            cur.execute(
                "INSERT INTO robots (id, name) VALUES ('fence-robot', 'Fence robot')"
            )
            cur.execute(
                """
                INSERT INTO uploads
                    (client_upload_id, robot_id, original_filename, storage_path,
                     file_size_bytes, sha256_hash, status, upload_completed_at,
                     integrity_verified)
                VALUES (
                    'fence-upload', 'fence-robot', 'fence.mcap',
                    's3://robotics-data/raw/mcap/fence.mcap', 7, repeat('a', 64),
                    'completed', NOW(), TRUE
                )
                RETURNING id
                """
            )
            upload_pk = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO processing_status
                    (upload_id, status, attempts, started_at, attempt_id,
                     claimed_by, lease_expires_at)
                VALUES (
                    %s, 'processing', 1, NOW(), %s::uuid, 'worker-a',
                    clock_timestamp() + INTERVAL '2 seconds'
                )
                """,
                (upload_pk, attempt_a),
            )
        setup.commit()

    claim_started = threading.Event()

    def worker_b_claim():
        with psycopg.connect(scratch_database_url) as conn:
            with conn.cursor() as cur:
                claim_started.set()
                cur.execute(
                    PROCESSING_CLAIM_SQL,
                    (3, [], 1, "worker-b", 60, 3),
                )
                row = cur.fetchone()
                if row is not None:
                    prune_processing_claims(cur, [row[3]])
                conn.commit()
        return row

    with psycopg.connect(scratch_database_url) as worker_a:
        with worker_a.cursor() as cur_a:
            assert lock_processing_claim(
                cur_a, "fence-upload", attempt_a
            ) == 1
            # Hold the row lock while crossing the server-clock lease boundary.
            # The lease is long enough that A always locks before it ends, even
            # on a loaded host; then A sleeps until just past the stored expiry.
            cur_a.execute(
                "SELECT pg_sleep(EXTRACT(EPOCH FROM (SELECT lease_expires_at"
                " FROM processing_status WHERE upload_id = %s) - clock_timestamp()) + 0.1)",
                (upload_pk,),
            )

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future_b = pool.submit(worker_b_claim)
                assert claim_started.wait(timeout=2)

                key_a = (
                    "robotics-data/processed/parquet/fence-robot/_attempts/"
                    f"{attempt_a}/imu/2026/08/19/rows-fence-upload.parquet"
                )
                cur_a.execute(
                    """
                    INSERT INTO parquet_files
                        (file_path, robot_id, message_type, kind, upload_id,
                         start_timestamp, end_timestamp, row_count,
                         file_size_bytes, schema_hash, column_names,
                         all_null_columns, sha256, attempt_id)
                    VALUES (%s, 'fence-robot', 'imu', 'topic', %s,
                            NOW(), NOW(), 1, 10, repeat('e', 64),
                            ARRAY['ts', 'topic'], '{}', repeat('f', 64),
                            %s::uuid)
                    """,
                    (key_a, upload_pk, attempt_a),
                )
                cur_a.execute(
                    """
                    INSERT INTO processed_topics
                        (upload_id, topic, rows_written, checksum, parquet_path,
                         attempt_id)
                    VALUES (%s, 'imu', 1, repeat('a', 64), %s, %s::uuid)
                    """,
                    (upload_pk, key_a, attempt_a),
                )
                cur_a.execute(
                    """
                    INSERT INTO episodes
                        (upload_id, robot_id, t_start_ns, t_end_ns,
                         n_messages, status, attempt_id)
                    VALUES (%s, 'fence-robot', 1, 2, 1, 'qa_scored', %s::uuid)
                    RETURNING episode_id
                    """,
                    (upload_pk, attempt_a),
                )
                episode_a = cur_a.fetchone()[0]
                cur_a.execute(
                    """
                    INSERT INTO media_files
                        (episode_id, topic, key, codec, container, attempt_id)
                    VALUES (%s, '/camera', %s, 'av1', 'mp4', %s::uuid)
                    """,
                    (
                        episode_a,
                        f"processed/media/fence-robot/_attempts/{attempt_a}/a.mp4",
                        attempt_a,
                    ),
                )
                cur_a.execute(
                    """
                    INSERT INTO qa_reports
                        (upload_id, scope, subscores, detail_key, attempt_id)
                    VALUES (%s, 'upload', '{}'::jsonb, %s, %s::uuid)
                    """,
                    (
                        upload_pk,
                        f"qa/reports/fence-robot/_attempts/{attempt_a}/a.json",
                        attempt_a,
                    ),
                )
                worker_a.commit()
                claimed = future_b.result(timeout=5)

    assert claimed is not None
    assert claimed[:3] == (
        "fence-upload",
        "fence-robot",
        "s3://robotics-data/raw/mcap/fence.mcap",
    )
    attempt_b = claimed[3]
    assert attempt_b != attempt_a

    # B's claim transaction pruned every attempt-owned row A committed
    # immediately before it, including descendants removed by episode cascade.
    with psycopg.connect(scratch_database_url) as verify:
        with verify.cursor() as cur:
            for table in ("processed_topics", "parquet_files", "episodes"):
                cur.execute(
                    f"SELECT attempt_id::text FROM {table} WHERE upload_id=%s",
                    (upload_pk,),
                )
                assert cur.fetchall() == []
            cur.execute(
                "SELECT attempt_id::text FROM media_files WHERE episode_id=%s",
                (episode_a,),
            )
            assert cur.fetchall() == []
            cur.execute(
                "SELECT attempt_id::text FROM qa_reports WHERE upload_id=%s",
                (upload_pk,),
            )
            assert cur.fetchall() == []

    # A wakes after B owns the row. It cannot begin another registry mutation.
    with psycopg.connect(scratch_database_url) as stale:
        with stale.cursor() as cur:
            with pytest.raises(ProcessingClaimLostError):
                lock_processing_claim(cur, "fence-upload", attempt_a)

    key_b = (
        "robotics-data/processed/parquet/fence-robot/_attempts/"
        f"{attempt_b}/imu/2026/08/19/rows-fence-upload.parquet"
    )
    with psycopg.connect(scratch_database_url) as winner:
        with winner.cursor() as cur:
            assert lock_processing_claim(cur, "fence-upload", attempt_b) == 2
            cur.execute(
                """
                INSERT INTO parquet_files
                    (file_path, robot_id, message_type, kind, upload_id,
                     start_timestamp, end_timestamp, row_count,
                     file_size_bytes, schema_hash, column_names,
                     all_null_columns, sha256, attempt_id)
                VALUES (%s, 'fence-robot', 'imu', 'topic', %s,
                        NOW(), NOW(), 2, 20, repeat('e', 64),
                        ARRAY['ts', 'topic'], '{}', repeat('f', 64), %s::uuid)
                """,
                (key_b, upload_pk, attempt_b),
            )
            cur.execute(
                """
                INSERT INTO processed_topics
                    (upload_id, topic, rows_written, checksum, parquet_path,
                     attempt_id)
                VALUES (%s, 'imu', 2, repeat('b', 64), %s, %s::uuid)
                """,
                (upload_pk, key_b, attempt_b),
            )
            cur.execute(
                """
                INSERT INTO episodes
                    (upload_id, robot_id, t_start_ns, t_end_ns,
                     n_messages, status, attempt_id)
                VALUES (%s, 'fence-robot', 3, 4, 2, 'qa_scored', %s::uuid)
                RETURNING episode_id
                """,
                (upload_pk, attempt_b),
            )
            episode_b = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO media_files
                    (episode_id, topic, key, codec, container, attempt_id)
                VALUES (%s, '/camera', %s, 'av1', 'mp4', %s::uuid)
                """,
                (
                    episode_b,
                    f"processed/media/fence-robot/_attempts/{attempt_b}/b.mp4",
                    attempt_b,
                ),
            )
            cur.execute(
                """
                INSERT INTO qa_reports
                    (upload_id, scope, subscores, detail_key, attempt_id)
                VALUES (%s, 'upload', '{}'::jsonb, %s, %s::uuid)
                """,
                (
                    upload_pk,
                    f"qa/reports/fence-robot/_attempts/{attempt_b}/b.json",
                    attempt_b,
                ),
            )
            cur.execute(
                """
                INSERT INTO processed
                    (upload_id, completed_at, rows_written, attempt_id)
                VALUES (%s, NOW(), 2, %s::uuid)
                """,
                (upload_pk, attempt_b),
            )
            cur.execute(
                """
                UPDATE processing_status
                SET status='completed', completed_at=NOW(),
                    lease_expires_at=NULL
                WHERE upload_id=%s AND attempt_id=%s::uuid
                """,
                (upload_pk, attempt_b),
            )
        winner.commit()

    with psycopg.connect(scratch_database_url) as verify:
        with verify.cursor() as cur:
            for table in ("processed", "processed_topics", "parquet_files", "episodes"):
                cur.execute(
                    f"SELECT attempt_id::text FROM {table} WHERE upload_id=%s",
                    (upload_pk,),
                )
                assert cur.fetchall() == [(attempt_b,)]
            cur.execute(
                "SELECT attempt_id::text FROM media_files WHERE episode_id=%s",
                (episode_b,),
            )
            assert cur.fetchall() == [(attempt_b,)]
            cur.execute(
                "SELECT attempt_id::text FROM qa_reports WHERE upload_id=%s",
                (upload_pk,),
            )
            assert cur.fetchall() == [(attempt_b,)]

    # Fault injection: even if privileged/off-protocol SQL leaves a stale
    # generation behind after publication, reader-side authority predicates
    # must expose only B. This models the residue that cleanup is designed to
    # remove without making cleanup a prerequisite for confidentiality.
    with psycopg.connect(scratch_database_url) as inject:
        with inject.cursor() as cur:
            stale_key = (
                "robotics-data/processed/parquet/fence-robot/_attempts/"
                f"{attempt_a}/stale/rows-fence-upload.parquet"
            )
            cur.execute(
                """
                INSERT INTO processed_topics
                    (upload_id, topic, rows_written, checksum, parquet_path,
                     attempt_id)
                VALUES (%s, 'stale', 9, repeat('c', 64), %s, %s::uuid)
                """,
                (upload_pk, stale_key, attempt_a),
            )
            cur.execute(
                """
                INSERT INTO parquet_files
                    (file_path, robot_id, message_type, kind, upload_id,
                     start_timestamp, end_timestamp, row_count,
                     file_size_bytes, schema_hash, column_names,
                     all_null_columns, sha256, attempt_id)
                VALUES (%s, 'fence-robot', 'stale', 'topic', %s,
                        NOW(), NOW(), 9, 90, repeat('e', 64),
                        ARRAY['ts', 'topic'], '{}', repeat('f', 64), %s::uuid)
                """,
                (stale_key, upload_pk, attempt_a),
            )
            cur.execute(
                """
                INSERT INTO episodes
                    (upload_id, robot_id, t_start_ns, t_end_ns,
                     n_messages, status, attempt_id)
                VALUES (%s, 'fence-robot', 5, 6, 9, 'qa_scored', %s::uuid)
                RETURNING episode_id
                """,
                (upload_pk, attempt_a),
            )
            stale_episode = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO media_files
                    (episode_id, topic, key, codec, container, attempt_id)
                VALUES (%s, '/camera', %s, 'av1', 'mp4', %s::uuid)
                """,
                (
                    stale_episode,
                    f"processed/media/fence-robot/_attempts/{attempt_a}/stale.mp4",
                    attempt_a,
                ),
            )
            cur.execute(
                """
                INSERT INTO qa_reports
                    (upload_id, scope, subscores, detail_key, attempt_id)
                VALUES (%s, 'upload', '{}'::jsonb, %s, %s::uuid)
                """,
                (
                    upload_pk,
                    f"qa/reports/fence-robot/_attempts/{attempt_a}/stale.json",
                    attempt_a,
                ),
            )
        inject.commit()

    with psycopg.connect(scratch_database_url) as visible:
        with visible.cursor() as cur:
            for table in ("processed_topics", "parquet_files", "episodes"):
                cur.execute(
                    f"""
                    SELECT derived.attempt_id::text
                    FROM {table} AS derived
                    LEFT JOIN processed AS published
                      ON published.upload_id = derived.upload_id
                    WHERE derived.upload_id = %s
                      AND (
                          derived.attempt_id IS NULL
                          OR derived.attempt_id = published.attempt_id
                      )
                    """,
                    (upload_pk,),
                )
                assert cur.fetchall() == [(attempt_b,)]
            cur.execute(
                """
                SELECT file.attempt_id::text
                FROM media_files AS file
                LEFT JOIN episodes AS episode ON episode.episode_id = file.episode_id
                LEFT JOIN processed AS published ON published.upload_id = episode.upload_id
                WHERE episode.upload_id = %s
                  AND (episode.attempt_id IS NULL OR episode.attempt_id = published.attempt_id)
                  AND (file.attempt_id IS NULL OR file.attempt_id = published.attempt_id)
                """,
                (upload_pk,),
            )
            assert cur.fetchall() == [(attempt_b,)]
            cur.execute(
                """
                SELECT report.attempt_id::text
                FROM qa_reports AS report
                LEFT JOIN episodes AS episode ON episode.episode_id = report.episode_id
                LEFT JOIN processed AS published
                  ON published.upload_id = COALESCE(report.upload_id, episode.upload_id)
                WHERE COALESCE(report.upload_id, episode.upload_id) = %s
                  AND (report.attempt_id IS NULL OR report.attempt_id = published.attempt_id)
                  AND (
                      episode.episode_id IS NULL
                      OR episode.attempt_id IS NULL
                      OR episode.attempt_id = published.attempt_id
                  )
                """,
                (upload_pk,),
            )
            assert cur.fetchall() == [(attempt_b,)]


def test_processor_final_expired_claim_becomes_terminal(scratch_database_url):
    """A crash on the last attempt must not strand a processing row forever."""
    migrate.run_migrations(scratch_database_url)
    cases = {
        "exhausted": ("processing", 3, "-1 minute"),
        "retryable": ("processing", 2, "-1 minute"),
        "live": ("processing", 3, "1 hour"),
        "completed": ("completed", 3, "-1 minute"),
    }
    with psycopg.connect(scratch_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO robots (id, name) VALUES ('expiry-robot', 'Expiry robot')")
            for name, (status, attempts, lease) in cases.items():
                cur.execute(
                    """
                    INSERT INTO uploads
                        (client_upload_id, robot_id, original_filename, storage_path,
                         file_size_bytes, sha256_hash, status, upload_completed_at,
                         integrity_verified)
                    VALUES (%s, 'expiry-robot', 'recording.mcap', %s, 7,
                            repeat('a', 64), 'completed', NOW(), TRUE)
                    RETURNING id
                    """,
                    (name, f"s3://robotics-data/raw/{name}.mcap"),
                )
                upload_pk = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO processing_status
                        (upload_id, status, attempts, attempt_id, claimed_by, lease_expires_at)
                    VALUES (%s, %s, %s, gen_random_uuid(), 'old-worker', NOW() + %s::interval)
                    """,
                    (upload_pk, status, attempts, lease),
                )
            cur.execute(PROCESSING_CLAIM_SQL, (3, [], 10, "new-worker", 60, 3))
            claimed = cur.fetchall()
            assert [row[0] for row in claimed] == ["retryable"]
            prune_processing_claims(cur, [row[3] for row in claimed])
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.client_upload_id, ps.status, ps.attempts,
                       ps.claimed_by, ps.lease_expires_at, ps.completed_at, ps.error_message
                FROM processing_status ps JOIN uploads u ON u.id = ps.upload_id
                """
            )
            rows = {row[0]: row[1:] for row in cur.fetchall()}
            assert rows["exhausted"][:4] == ("dead_letter", 3, None, None)
            assert rows["exhausted"][4] is not None
            assert "final allowed attempt" in rows["exhausted"][5]
            assert rows["retryable"][:3] == ("processing", 3, "new-worker")
            assert rows["live"][:3] == ("processing", 3, "old-worker")
            assert rows["completed"][:2] == ("completed", 3)
            # Repeated polls neither retry terminal work nor consume active leases.
            cur.execute(PROCESSING_CLAIM_SQL, (3, [], 10, "new-worker", 60, 3))
            assert cur.fetchall() == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
