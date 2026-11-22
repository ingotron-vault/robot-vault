"""Integration test for tools/s3_gc.py GC reachability and safety.

Runs against the LIVE dev stack (Postgres + the object store) rather than mocks, because
the GC reachability contract is about the real datastores: this seeds a
`processed_topics` row whose `parquet_path` is in
the EXACT format the processor writes (no `s3://` prefix, WITH the bucket
segment — see phases/04-processing-pipeline/src/processor/core.py
`_s3_parquet_root()`), puts a matching object at that key in the object store, and a
genuinely-orphan object under the same prefix, then runs the GC's real
reachable-set + sweep logic with dry_run=False and asserts:

  1. The processed Parquet object SURVIVES. Reachability must recognise the
     processor's scheme-less, bucket-prefixed paths: a query such as
     `LIKE 's3://%'` would match zero `processed_topics` rows, so every
     processed object would look unreachable and be deleted.
  2. The genuine orphan object is deleted. An unregistered object at the
     `_all/{upload_id}.parquet` path is an orphan too: the processor does
     not write that file and the GC does not derive it.
  3. A covered prefix whose registry result is genuinely empty can sweep an
     aged orphan, while an uncovered namespace such as `cas/files/` remains
     fail-closed even if objects exist there.

The invariant those assertions depend on is that
`tools/s3_gc.py` is the ONLY garbage collector in the repo. A second copy
(for example behind an admin endpoint) could drift: with a stale
reachable-set query and no coverage guard, its reachable set for
`prefix=processed/parquet/` would be empty and one admin click would sweep
the entire lake. `test_no_second_gc_implementation_*` in
`phases/05-query-service/tests/unit/test_single_gc_invariant.py` fail if any
non-test module either sweeps S3 objects or re-derives reachability.

`test_registered_media_file_survives_gc_only_orphan_deleted` has the same
positive-path shape as above but for the `media_files` registry
(db/migrations/001_baseline.sql) and the `processed/media/` prefix: a
registered object survives, an orphan under the same prefix is deleted, and
a dry-run pass first proves nothing is deleted while `GC_DRY_RUN=true`.

`test_registered_qa_report_survives_gc_only_orphan_deleted` does the same
for the `qa_reports` registry and the `qa/reports/` prefix, and
`test_registered_export_file_survives_gc_only_orphan_deleted` for the
`export_files` registry and the `exports/` prefix. That fixture also seeds
the `datasets`/`dataset_episodes` rows the schema defines, since an
`export_files` row is meaningless without a `dataset_id` to hang off.

These tests pin the GC contract for the media, QA and export registries in
the baseline schema, so it holds whichever service writes under those
prefixes.

Run (from repo root, from inside the dev container so `postgres`/`objectstore`
hostnames resolve on the compose network):

    docker compose run --rm dev bash -c \\
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/integration/test_gc_reachability.py -q"

Everything this test creates (Postgres rows, S3 objects) is scoped under a
random per-run robot_id / upload_id so it can never collide with or affect
real data, and is cleaned up in a fixture `finally` block. The GC sweep
itself is also scoped (via `GCConfig.prefixes`) to only the keys this test
created, so even a real, populated bucket/DB is untouched.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
boto3 = pytest.importorskip("boto3")
from botocore.config import Config as BotoConfig  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
import s3_gc  # noqa: E402  (tools/s3_gc.py, the module under test)


DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://robotvault:robotvault@postgres:5432/robotvault")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://objectstore:9000")
# Two identities (development/objectstore/CONTRACT.md section 8). The root key only seeds and removes fixtures; the GC under
# test always runs with the `robot-vault-gc` key, so its policy is part of what this test proves.
S3_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("OBJECTSTORE_ROOT_USER", "robot-vault-dev-root")
S3_SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("OBJECTSTORE_ROOT_PASSWORD", "")
GC_ACCESS_KEY = os.environ.get("GC_S3_ACCESS_KEY", "robot-vault-gc")
GC_SECRET_KEY = os.environ.get("GC_S3_SECRET_KEY", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "robotics-data")
REQUIRE_LIVE_STACK = os.environ.get("REQUIRE_LIVE_STACK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _live_stack_unavailable(message: str) -> None:
    if REQUIRE_LIVE_STACK:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _client(access_key: str, secret_key: str):
    # Same settings as the services (development/objectstore/CONTRACT.md section 3).
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _s3_client():
    """Fixture client (root): seeds and removes the test's objects."""
    return _client(S3_ACCESS_KEY, S3_SECRET_KEY)


def _gc_s3_client():
    """The client the GC under test runs with: the gc service key, never the root key."""
    if not GC_SECRET_KEY:
        _live_stack_unavailable("GC_S3_SECRET_KEY is not set; the GC test needs the robot-vault-gc key")
    return _client(GC_ACCESS_KEY, GC_SECRET_KEY)


def _object_exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise


@pytest.fixture(scope="module")
def live_stack():
    """Verify Postgres + the object store are reachable before running.

    This test is meant to run inside the dev container against the compose
    stack (see module docstring). Local runs skip an unavailable stack; CI sets
    REQUIRE_LIVE_STACK=1 so an unavailable dependency fails the gate.
    """
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception as e:
        _live_stack_unavailable(f"configured Postgres is not reachable ({type(e).__name__})")

    s3 = _s3_client()
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
    except Exception as e:
        _live_stack_unavailable(
            f"object store bucket '{S3_BUCKET}' not reachable at {S3_ENDPOINT}: {e}"
        )

    return {"s3": s3}


@pytest.fixture
def gc_test_data(live_stack):
    """Seed a processed upload + a genuine orphan, matching the processor's
    exact on-disk/S3 format, and clean everything up afterward."""
    s3 = live_stack["s3"]
    nonce = uuid.uuid4().hex[:10]
    robot_id = f"gctest-{nonce}"
    client_upload_id = f"gctest-upload-{nonce}"
    topic = "test_topic"

    # Format the processor writes for a per-topic Parquet path
    # (`_s3_parquet_root()` in phases/04-processing-pipeline/src/processor/core.py):
    # "{bucket}/processed/parquet/<robot>/<topic>/YYYY/MM/DD/<file>.parquet"
    # -- note: NO "s3://" prefix, but WITH the bucket segment.
    topic_parquet_path_db = (
        f"{S3_BUCKET}/processed/parquet/{robot_id}/{topic}/2026/07/21/part-{client_upload_id}.parquet"
    )
    # Bare object key (what's actually listed by S3 / what run_gc compares against)
    topic_key = f"processed/parquet/{robot_id}/{topic}/2026/07/21/part-{client_upload_id}.parquet"

    # A combined "_all" path. Nothing registers it, and the GC does not
    # derive it from a path template, so it is an orphan.
    all_key = f"processed/parquet/{robot_id}/_all/{client_upload_id}.parquet"

    # A genuinely unreachable object under the same robot's processed-parquet tree.
    orphan_key = f"processed/parquet/{robot_id}/orphan/{nonce}.parquet"

    upload_row_id = None
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # `uploads.robot_id` is a FK to the `robots` fleet
            # registry, so this fixture's synthetic robot has
            # to be provisioned before its upload row can exist -- exactly as
            # a real machine would be. Torn down below, after the uploads row.
            cur.execute(
                """
                INSERT INTO robots (id, name, metadata)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                (robot_id, robot_id, '{"source": "tests/integration/test_gc_reachability.py"}'),
            )

            cur.execute(
                """
                INSERT INTO uploads
                    (client_upload_id, robot_id, original_filename, storage_path,
                     file_size_bytes, status, upload_completed_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', NOW())
                RETURNING id
                """,
                (
                    client_upload_id,
                    robot_id,
                    "test.mcap",
                    f"s3://{S3_BUCKET}/raw/mcap/{robot_id}/2026/07/21/{client_upload_id}.mcap",
                    12345,
                ),
            )
            upload_row_id = cur.fetchone()[0]

            attempt_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO processed (upload_id, completed_at, rows_written, attempt_id) "
                "VALUES (%s, NOW(), %s, %s::uuid)",
                (upload_row_id, 10, attempt_id),
            )

            cur.execute(
                """
                INSERT INTO processed_topics
                    (upload_id, topic, rows_written, checksum, parquet_path, attempt_id)
                VALUES (%s, %s, %s, %s, %s, %s::uuid)
                """,
                (upload_row_id, topic, 10, "deadbeef" * 8, topic_parquet_path_db, attempt_id),
            )
            conn.commit()

    body = b"parquet-placeholder-bytes"
    s3.put_object(Bucket=S3_BUCKET, Key=topic_key, Body=body)
    s3.put_object(Bucket=S3_BUCKET, Key=all_key, Body=body)
    s3.put_object(Bucket=S3_BUCKET, Key=orphan_key, Body=body)

    data = {
        "robot_id": robot_id,
        "client_upload_id": client_upload_id,
        "upload_row_id": upload_row_id,
        "topic_key": topic_key,
        "all_key": all_key,
        "orphan_key": orphan_key,
        "s3": s3,
    }
    try:
        yield data
    finally:
        # Clean up S3 objects (ignore already-deleted / missing)
        for key in (topic_key, all_key, orphan_key):
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                pass
        # Clean up Postgres rows (cascade handles processed/processed_topics).
        # The `robots` row goes last: the schema's FK is ON DELETE NO ACTION,
        # so a robot that still has uploads cannot be removed -- which is the
        # intended protection, not something to work around here.
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM uploads WHERE id = %s", (upload_row_id,))
                cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
                conn.commit()


def test_processed_parquet_survives_gc_unregistered_objects_deleted(gc_test_data):
    """A registered processed Parquet file survives GC; unregistered objects go.

    The processor's `parquet_path` is bucket-prefixed and scheme-less, so a
    reachability query that filtered on `LIKE 's3://%'` would never match it
    and `topic_key` below would be deleted. The unregistered `_all` path and
    the orphan are both unreachable and are deleted.
    """
    s3 = gc_test_data["s3"]
    robot_id = gc_test_data["robot_id"]
    topic_key = gc_test_data["topic_key"]
    all_key = gc_test_data["all_key"]
    orphan_key = gc_test_data["orphan_key"]

    # Sanity: all three objects exist before GC runs.
    assert _object_exists(s3, topic_key)
    assert _object_exists(s3, all_key)
    assert _object_exists(s3, orphan_key)

    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)

    # The bare object key for the processed per-topic Parquet file must be
    # present in the reachable set (it is never matched by `LIKE 's3://%'`).
    assert topic_key in reachable, (
        "processed_topics.parquet_path was not recognized as reachable — "
        "the s3:// / bucket-prefix normalization is broken"
    )
    # No path-template derivation: only registered paths are reachable.
    assert all_key not in reachable
    assert orphan_key not in reachable

    # Scope the sweep to just this test's robot_id prefix so a real,
    # populated bucket is never touched by this test.
    cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[f"processed/parquet/{robot_id}/"],
        min_age_seconds=-3600,  # objects were just created; use a negative floor so minor
        # clock skew between the test process and the object store container can never make an
        # object look "too young" and get skipped -- this test is about reachability,
        # not age filtering.
        delete_limit=1000,
        dry_run=False,
    )
    scanned, deleted = s3_gc.run_gc(cfg, reachable, s3=_gc_s3_client())

    assert scanned == 3, f"expected to scan exactly the 3 test objects, scanned={scanned}"
    assert deleted == 2, f"expected the two unregistered objects to be deleted, deleted={deleted}"

    # The registered processed object must survive.
    assert _object_exists(s3, topic_key), "processed Parquet object was deleted by GC"
    assert not _object_exists(s3, all_key), "unregistered _all object was NOT deleted by GC"
    # The orphan must be gone.
    assert not _object_exists(s3, orphan_key), "genuine orphan object was NOT deleted by GC"


def test_known_empty_covered_subprefix_sweeps_orphan(gc_test_data):
    """An empty live-key subset is safe when namespace coverage is known.

    Uses a sub-prefix under this test's own robot_id that has zero
    reachable keys (nothing in Postgres maps there) but does have an
    old-enough orphan. `processed/parquet/` is completely derived, so the
    successful empty result is authoritative and the orphan is collectable.
    """
    s3 = gc_test_data["s3"]
    robot_id = gc_test_data["robot_id"]
    orphan_key = gc_test_data["orphan_key"]

    empty_prefix = f"processed/parquet/{robot_id}/orphan/"
    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)
    pfx_reachable = s3_gc.reachable_for_prefix(reachable, empty_prefix)
    assert pfx_reachable == set(), "test setup invariant broken: expected zero reachable keys under this prefix"
    assert s3_gc.reachability_known_for_prefix(reachable, empty_prefix)

    # Direct unit-level check: the known-empty prefix is scanned and produces
    # the orphan candidate. Computing candidates alone does not delete it.
    scanned, to_delete, skipped = s3_gc.sweep_prefix(
        _gc_s3_client(),
        S3_BUCKET,
        empty_prefix,
        pfx_reachable,
        min_age_seconds=-3600,
        delete_limit=1000,
        reachability_known=True,
    )
    assert skipped is False
    assert to_delete == [{"Key": orphan_key}]
    assert scanned == 1
    assert _object_exists(s3, orphan_key)

    # End-to-end through the coverage-aware snapshot: a real pass deletes it.
    cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[empty_prefix],
        min_age_seconds=-3600,
        delete_limit=1000,
        dry_run=False,
    )
    scanned2, deleted2 = s3_gc.run_gc(cfg, reachable, s3=_gc_s3_client())
    assert (scanned2, deleted2) == (1, 1)
    assert not _object_exists(s3, orphan_key)


def test_unknown_cas_prefix_stays_fail_closed(gc_test_data):
    """Unmodelled CAS coverage is never inferred from an empty key subset."""
    s3 = gc_test_data["s3"]
    cas_key = f"cas/files/gctest-orphan-{uuid.uuid4().hex}"
    s3.put_object(Bucket=S3_BUCKET, Key=cas_key, Body=b"cas-orphan")
    try:
        reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)
        assert not s3_gc.reachability_known_for_prefix(reachable, "cas/files/")

        cfg = s3_gc.GCConfig(
            bucket=S3_BUCKET,
            prefixes=["cas/files/"],
            min_age_seconds=-3600,
            delete_limit=1000,
            dry_run=False,
        )
        scanned, deleted = s3_gc.run_gc(cfg, reachable, s3=_gc_s3_client())

        assert (scanned, deleted) == (0, 0)
        assert _object_exists(s3, cas_key)
    finally:
        s3.delete_object(Bucket=S3_BUCKET, Key=cas_key)


@pytest.fixture
def live_attempt_gc_test_data(live_stack):
    """Two uploads in `processing`: one with a live lease, one expired.

    Ways the attempt protection can fail:

    1. An object that a live attempt uploaded but has not registered yet is
       deleted in the middle of the media or QA stage.
    2. The protection also covers an expired lease, so a dead attempt's
       objects are never collected.
    3. A key whose `_attempts` segment only STARTS WITH a live attempt id is
       treated as protected (substring match instead of segment match).
    4. A claim that starts after the snapshot loses an object uploaded
       between the snapshot and the listing (the sweep must judge age at the
       snapshot time, not at listing time).
    5. Plain-set callers without coverage data still sweep.
    """
    s3 = live_stack["s3"]
    nonce = uuid.uuid4().hex[:10]
    robot_id = f"gctest-live-{nonce}"
    live_attempt = str(uuid.uuid4())
    expired_attempt = str(uuid.uuid4())
    upload_ids = []
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO robots (id, name, metadata) VALUES (%s, %s, '{}'::jsonb) "
                "ON CONFLICT (id) DO NOTHING",
                (robot_id, robot_id),
            )
            for name, attempt, lease in (
                ("live", live_attempt, "NOW() + INTERVAL '1 hour'"),
                ("expired", expired_attempt, "NOW() - INTERVAL '1 minute'"),
            ):
                client_upload_id = f"gctest-{name}-{nonce}"
                cur.execute(
                    """
                    INSERT INTO uploads
                        (client_upload_id, robot_id, original_filename, storage_path,
                         file_size_bytes, status, upload_completed_at)
                    VALUES (%s, %s, 'test.mcap', %s, 1, 'completed', NOW())
                    RETURNING id
                    """,
                    (
                        client_upload_id,
                        robot_id,
                        f"s3://{S3_BUCKET}/raw/mcap/{robot_id}/{client_upload_id}.mcap",
                    ),
                )
                upload_id = cur.fetchone()[0]
                upload_ids.append(upload_id)
                cur.execute(
                    f"""
                    INSERT INTO processing_status
                        (upload_id, status, attempts, started_at, attempt_id,
                         claimed_by, lease_expires_at)
                    VALUES (%s, 'processing', 1, NOW(), %s::uuid, 'gc-test', {lease})
                    """,
                    (upload_id, attempt),
                )
            conn.commit()

    keys = {
        "live_media": f"processed/media/{robot_id}/_attempts/{live_attempt}/ep/cam/chunk-000.mp4",
        "live_qa": f"qa/reports/{robot_id}/_attempts/{live_attempt}/ep/report.json",
        "live_parquet": (
            f"processed/parquet/{robot_id}/_attempts/{live_attempt}/imu/2026/10/08/rows-u.parquet"
        ),
        "expired_media": (
            f"processed/media/{robot_id}/_attempts/{expired_attempt}/ep/cam/chunk-000.mp4"
        ),
        "lookalike_media": (
            f"processed/media/{robot_id}/_attempts/{live_attempt}x/ep/cam/chunk-000.mp4"
        ),
    }
    for key in keys.values():
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=b"unregistered-stage-output")
    try:
        yield {
            "s3": s3,
            "robot_id": robot_id,
            "keys": keys,
            "live_attempt": live_attempt,
        }
    finally:
        for key in keys.values():
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                pass
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM uploads WHERE id = ANY(%s)", (upload_ids,))
                cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
                conn.commit()


def test_live_attempt_prefix_survives_gc_expired_attempt_collected(
    live_attempt_gc_test_data,
):
    data = live_attempt_gc_test_data
    s3 = data["s3"]
    robot_id = data["robot_id"]
    keys = data["keys"]

    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)
    assert data["live_attempt"] in reachable.protected_attempts
    for key in keys.values():
        assert key not in reachable, "test setup: nothing here is registered"

    cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[
            f"processed/media/{robot_id}/",
            f"processed/parquet/{robot_id}/",
            f"qa/reports/{robot_id}/",
        ],
        min_age_seconds=-3600,
        delete_limit=1000,
        dry_run=False,
    )
    scanned, deleted = s3_gc.run_gc(cfg, reachable, s3=_gc_s3_client())

    assert scanned == 5
    assert deleted == 2
    for name in ("live_media", "live_qa", "live_parquet"):
        assert _object_exists(s3, keys[name]), f"{name} of a live lease was deleted"
    assert not _object_exists(s3, keys["expired_media"]), "expired lease kept its objects"
    assert not _object_exists(s3, keys["lookalike_media"]), "prefix matched a partial id"


def test_objects_newer_than_the_snapshot_are_never_collected(live_attempt_gc_test_data):
    data = live_attempt_gc_test_data
    s3 = data["s3"]
    robot_id = data["robot_id"]
    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)
    late_key = f"processed/media/{robot_id}/_attempts/{uuid.uuid4()}/ep/cam/chunk-000.mp4"
    import time as _time

    # The object store records LastModified with one-second precision. Wait so the object
    # is clearly newer than the snapshot.
    _time.sleep(1.5)
    s3.put_object(Bucket=S3_BUCKET, Key=late_key, Body=b"late")
    try:
        cfg = s3_gc.GCConfig(
            bucket=S3_BUCKET,
            prefixes=[f"processed/media/{robot_id}/"],
            min_age_seconds=0,
            delete_limit=1000,
            dry_run=False,
        )
        s3_gc.run_gc(cfg, reachable, s3=_gc_s3_client())
        assert _object_exists(s3, late_key), "object created after the snapshot was deleted"
    finally:
        s3.delete_object(Bucket=S3_BUCKET, Key=late_key)


def test_plain_set_without_coverage_never_sweeps(gc_test_data):
    s3 = gc_test_data["s3"]
    robot_id = gc_test_data["robot_id"]
    cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[f"processed/parquet/{robot_id}/"],
        min_age_seconds=-3600,
        delete_limit=1000,
        dry_run=False,
    )
    plain = {gc_test_data["topic_key"]}
    assert not s3_gc.reachability_known_for_prefix(plain, cfg.prefixes[0])
    assert s3_gc.run_gc(cfg, plain, s3=_gc_s3_client()) == (0, 0)
    assert _object_exists(s3, gc_test_data["orphan_key"])


@pytest.fixture
def media_gc_test_data(live_stack):
    """Positive-path fixture: an episode with a `media_files`-
    registered object under `processed/media/...`, plus a genuine orphan
    under the same prefix. Mirrors `gc_test_data` above (same robot/uploads
    provisioning, same S3-then-Postgres seed order, same teardown shape) but
    exercises the new `media_files` registry (db/migrations/
    001_baseline.sql) instead of `parquet_files`/`processed_topics`.

    This fixture writes the media object itself with a placeholder body,
    which is all `reachable_keys_from_pg`/`sweep_prefix` ever look at (key +
    existence + age, never content).
    """
    s3 = live_stack["s3"]
    nonce = uuid.uuid4().hex[:10]
    robot_id = f"gctest-media-{nonce}"
    client_upload_id = f"gctest-media-upload-{nonce}"
    topic = "test_camera"

    upload_row_id = None
    episode_id = None
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Same FK provisioning as gc_test_data: uploads.robot_id is
            # FK'd to robots, so the synthetic robot must exist first.
            cur.execute(
                """
                INSERT INTO robots (id, name, metadata)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                (robot_id, robot_id, '{"source": "tests/integration/test_gc_reachability.py"}'),
            )

            cur.execute(
                """
                INSERT INTO uploads
                    (client_upload_id, robot_id, original_filename, storage_path,
                     file_size_bytes, status, upload_completed_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', NOW())
                RETURNING id
                """,
                (
                    client_upload_id,
                    robot_id,
                    "test.mcap",
                    f"s3://{S3_BUCKET}/raw/mcap/{robot_id}/2026/07/21/{client_upload_id}.mcap",
                    12345,
                ),
            )
            upload_row_id = cur.fetchone()[0]

            # episodes.upload_id is FK'd to uploads, ON DELETE CASCADE --
            # a media_files row is meaningless without an episode to hang off.
            # A derived row is visible only when its attempt matches the
            # published attempt in `processed`.
            attempt_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO processed (upload_id, completed_at, rows_written, attempt_id) "
                "VALUES (%s, NOW(), 1, %s::uuid)",
                (upload_row_id, attempt_id),
            )
            cur.execute(
                """
                INSERT INTO episodes (upload_id, robot_id, status, attempt_id)
                VALUES (%s, %s, 'segmented', %s::uuid)
                RETURNING episode_id
                """,
                (upload_row_id, robot_id, attempt_id),
            )
            episode_id = cur.fetchone()[0]

            # Bare object key -- the schema's documented convention for
            # media_files.key (no s3:// scheme, no bucket segment), using the
            # processed/media/{robot_id}/{episode_id}/{topic}/chunk-*.mp4
            # layout.
            media_key = f"processed/media/{robot_id}/{episode_id}/{topic}/chunk-000.mp4"
            cur.execute(
                """
                INSERT INTO media_files
                    (episode_id, topic, key, codec, container, attempt_id)
                VALUES (%s, %s, %s, %s, %s, %s::uuid)
                """,
                (episode_id, topic, media_key, "av1", "mp4", attempt_id),
            )
            conn.commit()

    # A genuinely unreachable object under the same robot's processed/media tree.
    orphan_key = f"processed/media/{robot_id}/{episode_id}/{topic}/chunk-orphan-{nonce}.mp4"

    body = b"media-placeholder-bytes"
    s3.put_object(Bucket=S3_BUCKET, Key=media_key, Body=body)
    s3.put_object(Bucket=S3_BUCKET, Key=orphan_key, Body=body)

    data = {
        "robot_id": robot_id,
        "episode_id": episode_id,
        "upload_row_id": upload_row_id,
        "media_key": media_key,
        "orphan_key": orphan_key,
        "s3": s3,
    }
    try:
        yield data
    finally:
        # Clean up S3 objects (ignore already-deleted / missing).
        for key in (media_key, orphan_key):
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                pass
        # Clean up Postgres rows: deleting the uploads row cascades to
        # episodes which cascades to media_files. The `robots`
        # row goes last, same reasoning as gc_test_data's teardown -- the
        # uploads.robot_id FK is ON DELETE NO ACTION, so a robot with live uploads cannot be
        # removed.
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM uploads WHERE id = %s", (upload_row_id,))
                cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
                conn.commit()


def test_registered_media_file_survives_gc_only_orphan_deleted(media_gc_test_data):
    """Positive-path reachability, mirroring
    test_processed_parquet_survives_gc_unregistered_objects_deleted above but
    for the new `media_files` registry: a registered object under
    `processed/media/...` must survive a real GC sweep, and a genuine orphan
    under the same prefix must be deleted.

    Dry-run first (proves GC_DRY_RUN=true never deletes anything, including
    the orphan), then a real sweep -- scoped to this test's own
    robot_id/episode_id namespace, so even a real, populated bucket is never
    touched by this test.
    """
    s3 = media_gc_test_data["s3"]
    robot_id = media_gc_test_data["robot_id"]
    media_key = media_gc_test_data["media_key"]
    orphan_key = media_gc_test_data["orphan_key"]

    # Sanity: both objects exist before GC runs.
    assert _object_exists(s3, media_key)
    assert _object_exists(s3, orphan_key)

    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)

    # The bare object key registered in media_files must be reachable.
    assert media_key in reachable, (
        "media_files.key was not recognized as reachable -- the media_files "
        "reachability source in reachable_keys_from_pg is broken"
    )
    # The orphan must never appear.
    assert orphan_key not in reachable

    prefix = f"processed/media/{robot_id}/"

    # Dry-run first: nothing may be deleted, including the orphan.
    dry_cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[prefix],
        min_age_seconds=-3600,  # see gc_test_data's identical comment: avoids
        # clock-skew false negatives, this test is about reachability, not age.
        delete_limit=1000,
        dry_run=True,
    )
    scanned, deleted = s3_gc.run_gc(dry_cfg, reachable, s3=_gc_s3_client())
    assert deleted == 0, "GC_DRY_RUN=true must never delete anything"
    assert _object_exists(s3, media_key)
    assert _object_exists(s3, orphan_key)

    # Real sweep: only the orphan may go.
    real_cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[prefix],
        min_age_seconds=-3600,
        delete_limit=1000,
        dry_run=False,
    )
    scanned, deleted = s3_gc.run_gc(real_cfg, reachable, s3=_gc_s3_client())

    assert scanned == 2, f"expected to scan exactly the 2 test objects, scanned={scanned}"
    assert deleted == 1, f"expected exactly the orphan to be deleted, deleted={deleted}"

    # The registered media object must survive.
    assert _object_exists(s3, media_key), "media_files-registered object was deleted by GC"
    # The orphan must be gone.
    assert not _object_exists(s3, orphan_key), "genuine orphan media object was NOT deleted by GC"


@pytest.fixture
def qa_report_gc_test_data(live_stack):
    """Positive-path fixture: an episode with a `qa_reports`-
    registered blob under `qa/reports/...`, plus a genuine orphan under the
    same prefix. Mirrors `media_gc_test_data` above (same robot/uploads/
    episode provisioning, same S3-then-Postgres seed order, same teardown
    shape) but exercises the new `qa_reports` registry (db/migrations/
    001_baseline.sql) instead of `media_files`.

    This fixture writes the QA report blob itself with a placeholder body,
    which is all `reachable_keys_from_pg`/`sweep_prefix`
    ever look at (key + existence + age, never content).
    """
    s3 = live_stack["s3"]
    nonce = uuid.uuid4().hex[:10]
    robot_id = f"gctest-qa-{nonce}"
    client_upload_id = f"gctest-qa-upload-{nonce}"

    upload_row_id = None
    episode_id = None
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Same FK provisioning as media_gc_test_data: uploads.robot_id
            # is FK'd to robots, so the synthetic robot must exist first.
            cur.execute(
                """
                INSERT INTO robots (id, name, metadata)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                (robot_id, robot_id, '{"source": "tests/integration/test_gc_reachability.py"}'),
            )

            cur.execute(
                """
                INSERT INTO uploads
                    (client_upload_id, robot_id, original_filename, storage_path,
                     file_size_bytes, status, upload_completed_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', NOW())
                RETURNING id
                """,
                (
                    client_upload_id,
                    robot_id,
                    "test.mcap",
                    f"s3://{S3_BUCKET}/raw/mcap/{robot_id}/2026/07/21/{client_upload_id}.mcap",
                    12345,
                ),
            )
            upload_row_id = cur.fetchone()[0]

            # episodes.upload_id is FK'd to uploads, ON DELETE CASCADE --
            # a qa_reports row scoped to an episode is meaningless without one.
            # A derived row is visible only when its attempt matches the
            # published attempt in `processed`.
            attempt_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO processed (upload_id, completed_at, rows_written, attempt_id) "
                "VALUES (%s, NOW(), 1, %s::uuid)",
                (upload_row_id, attempt_id),
            )
            cur.execute(
                """
                INSERT INTO episodes (upload_id, robot_id, status, attempt_id)
                VALUES (%s, %s, 'segmented', %s::uuid)
                RETURNING episode_id
                """,
                (upload_row_id, robot_id, attempt_id),
            )
            episode_id = cur.fetchone()[0]

            # Bare object key -- the schema's documented convention for
            # qa_reports.detail_key (no s3:// scheme, no bucket segment, same
            # convention as media_files.key), using the
            # qa/reports/{robot_id}/{episode_id}/{...}.json layout.
            detail_key = f"qa/reports/{robot_id}/{episode_id}/{nonce}.json"
            cur.execute(
                """
                INSERT INTO qa_reports (episode_id, scope, detail_key, attempt_id)
                VALUES (%s, %s, %s, %s::uuid)
                """,
                (episode_id, "episode", detail_key, attempt_id),
            )
            conn.commit()

    # A genuinely unreachable object under the same robot's qa/reports tree.
    orphan_key = f"qa/reports/{robot_id}/{episode_id}/orphan-{nonce}.json"

    body = b'{"placeholder": true}'
    s3.put_object(Bucket=S3_BUCKET, Key=detail_key, Body=body)
    s3.put_object(Bucket=S3_BUCKET, Key=orphan_key, Body=body)

    data = {
        "robot_id": robot_id,
        "episode_id": episode_id,
        "upload_row_id": upload_row_id,
        "detail_key": detail_key,
        "orphan_key": orphan_key,
        "s3": s3,
    }
    try:
        yield data
    finally:
        # Clean up S3 objects (ignore already-deleted / missing).
        for key in (detail_key, orphan_key):
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                pass
        # Clean up Postgres rows: deleting the uploads row cascades to
        # episodes which cascades to qa_reports. The `robots`
        # row goes last, same reasoning as media_gc_test_data's teardown --
        # the uploads.robot_id FK is ON DELETE NO ACTION, so a robot with live uploads
        # cannot be removed.
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM uploads WHERE id = %s", (upload_row_id,))
                cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
                conn.commit()


def test_registered_qa_report_survives_gc_only_orphan_deleted(qa_report_gc_test_data):
    """Positive-path reachability, mirroring
    test_registered_media_file_survives_gc_only_orphan_deleted above but for
    the new `qa_reports` registry: a registered report blob under
    `qa/reports/...` must survive a real GC sweep, and a genuine orphan
    under the same prefix must be deleted.

    Dry-run first (proves GC_DRY_RUN=true never deletes anything, including
    the orphan), then a real sweep -- scoped to this test's own
    robot_id/episode_id namespace, so even a real, populated bucket is never
    touched by this test.
    """
    s3 = qa_report_gc_test_data["s3"]
    robot_id = qa_report_gc_test_data["robot_id"]
    detail_key = qa_report_gc_test_data["detail_key"]
    orphan_key = qa_report_gc_test_data["orphan_key"]

    # Sanity: both objects exist before GC runs.
    assert _object_exists(s3, detail_key)
    assert _object_exists(s3, orphan_key)

    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)

    # The bare object key registered in qa_reports must be reachable.
    assert detail_key in reachable, (
        "qa_reports.detail_key was not recognized as reachable -- the "
        "qa_reports reachability source in reachable_keys_from_pg is broken"
    )
    # The orphan must never appear.
    assert orphan_key not in reachable

    prefix = f"qa/reports/{robot_id}/"

    # Dry-run first: nothing may be deleted, including the orphan.
    dry_cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[prefix],
        min_age_seconds=-3600,  # see gc_test_data's identical comment: avoids
        # clock-skew false negatives, this test is about reachability, not age.
        delete_limit=1000,
        dry_run=True,
    )
    scanned, deleted = s3_gc.run_gc(dry_cfg, reachable, s3=_gc_s3_client())
    assert deleted == 0, "GC_DRY_RUN=true must never delete anything"
    assert _object_exists(s3, detail_key)
    assert _object_exists(s3, orphan_key)

    # Real sweep: only the orphan may go.
    real_cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[prefix],
        min_age_seconds=-3600,
        delete_limit=1000,
        dry_run=False,
    )
    scanned, deleted = s3_gc.run_gc(real_cfg, reachable, s3=_gc_s3_client())

    assert scanned == 2, f"expected to scan exactly the 2 test objects, scanned={scanned}"
    assert deleted == 1, f"expected exactly the orphan to be deleted, deleted={deleted}"

    # The registered report blob must survive.
    assert _object_exists(s3, detail_key), "qa_reports-registered object was deleted by GC"
    # The orphan must be gone.
    assert not _object_exists(s3, orphan_key), "genuine orphan qa report object was NOT deleted by GC"


@pytest.fixture
def export_gc_test_data(live_stack):
    """Positive-path fixture: a dataset with an `export_files`-
    registered object under `exports/...`, plus a genuine orphan under the
    same prefix. Mirrors `qa_report_gc_test_data` above (same S3-then-
    Postgres seed order, same teardown shape) but exercises the new
    `datasets`/`dataset_episodes`/`export_files` registries (db/migrations/
    001_baseline.sql) instead of `qa_reports`.

    Kept minimal: a dataset needs at least one episode member
    (dataset_episodes.episode_id is ON DELETE RESTRICT, provenance-first — see
    the baseline schema), so this fixture provisions the same
    robot/upload/episode chain the other fixtures do, then a `datasets` row
    whose manifest just names that one episode, a `dataset_episodes` link,
    and finally the `export_files` row this test is actually about.

    This fixture writes the export object itself with a placeholder body,
    which is all `reachable_keys_from_pg`/`sweep_prefix`
    ever look at (key + existence + age, never content).
    """
    s3 = live_stack["s3"]
    nonce = uuid.uuid4().hex[:10]
    robot_id = f"gctest-export-{nonce}"
    client_upload_id = f"gctest-export-upload-{nonce}"
    dataset_id = f"gctestexport{nonce}".ljust(64, "0")[:64]

    upload_row_id = None
    episode_id = None
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Same FK provisioning as qa_report_gc_test_data: uploads.robot_id
            # is FK'd to robots, so the synthetic robot must exist first.
            cur.execute(
                """
                INSERT INTO robots (id, name, metadata)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                (robot_id, robot_id, '{"source": "tests/integration/test_gc_reachability.py"}'),
            )

            cur.execute(
                """
                INSERT INTO uploads
                    (client_upload_id, robot_id, original_filename, storage_path,
                     file_size_bytes, status, upload_completed_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', NOW())
                RETURNING id
                """,
                (
                    client_upload_id,
                    robot_id,
                    "test.mcap",
                    f"s3://{S3_BUCKET}/raw/mcap/{robot_id}/2026/07/21/{client_upload_id}.mcap",
                    12345,
                ),
            )
            upload_row_id = cur.fetchone()[0]

            # episodes.upload_id is FK'd to uploads, ON DELETE CASCADE.
            # A derived row is visible only when its attempt matches the
            # published attempt in `processed`.
            attempt_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO processed (upload_id, completed_at, rows_written, attempt_id) "
                "VALUES (%s, NOW(), 1, %s::uuid)",
                (upload_row_id, attempt_id),
            )
            cur.execute(
                """
                INSERT INTO episodes (upload_id, robot_id, status, attempt_id)
                VALUES (%s, %s, 'segmented', %s::uuid)
                RETURNING episode_id
                """,
                (upload_row_id, robot_id, attempt_id),
            )
            episode_id = cur.fetchone()[0]

            # datasets: dataset_id is content-addressed in real usage
            # (sha256 of the canonicalized manifest) — this fixture just needs a stable, unique 64-char
            # stand-in, since nothing here exercises the hashing itself.
            cur.execute(
                """
                INSERT INTO datasets (dataset_id, name, manifest)
                VALUES (%s, %s, %s::jsonb)
                """,
                (dataset_id, f"gc-test-dataset-{nonce}", '{"episodes": ["%s"]}' % episode_id),
            )

            # dataset_episodes: the membership link this test's
            # dataset_id/episode_id pair needs to exist under.
            cur.execute(
                "INSERT INTO dataset_episodes (dataset_id, episode_id) VALUES (%s, %s)",
                (dataset_id, episode_id),
            )

            # Bare object key — the schema's documented convention for
            # export_files.key (no s3:// scheme, no bucket segment, same
            # convention as media_files.key/qa_reports.detail_key), using the
            # exports/{dataset_id}/{format}/... layout.
            export_format = "lerobot_v3"  # a value the schema's format CHECK allows
            export_key = f"exports/{dataset_id}/{export_format}/registered-{nonce}.parquet"
            cur.execute(
                """
                INSERT INTO export_files (dataset_id, format, key)
                VALUES (%s, %s, %s)
                """,
                (dataset_id, export_format, export_key),
            )
            conn.commit()

    # A genuinely unreachable object under the same dataset's exports tree.
    orphan_key = f"exports/{dataset_id}/{export_format}/orphan-{nonce}.parquet"

    body = b"export-placeholder-bytes"
    s3.put_object(Bucket=S3_BUCKET, Key=export_key, Body=body)
    s3.put_object(Bucket=S3_BUCKET, Key=orphan_key, Body=body)

    data = {
        "robot_id": robot_id,
        "dataset_id": dataset_id,
        "episode_id": episode_id,
        "upload_row_id": upload_row_id,
        "export_key": export_key,
        "orphan_key": orphan_key,
        "s3": s3,
    }
    try:
        yield data
    finally:
        # Clean up S3 objects (ignore already-deleted / missing).
        for key in (export_key, orphan_key):
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                pass
        # Clean up Postgres rows. The `datasets` row must go before the
        # `uploads`/`episodes` chain: dataset_episodes.episode_id is
        # ON DELETE RESTRICT (the provenance-first guarantee), so deleting
        # `uploads` first — which would cascade to `episodes` — is refused
        # while this dataset still references the episode. Deleting
        # `datasets` first cascades to `dataset_episodes`/`export_files`
        # (both ON DELETE CASCADE), clearing that reference; only then can
        # `uploads` (-> episodes) be deleted. `robots` goes last, same
        # reasoning as every other fixture's teardown here — the uploads.robot_id FK is
        # ON DELETE NO ACTION, so a robot with live uploads cannot be removed.
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM datasets WHERE dataset_id = %s", (dataset_id,))
                cur.execute("DELETE FROM uploads WHERE id = %s", (upload_row_id,))
                cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
                conn.commit()


def test_registered_export_file_survives_gc_only_orphan_deleted(export_gc_test_data):
    """Positive-path reachability, mirroring
    test_registered_qa_report_survives_gc_only_orphan_deleted above but for
    the new `export_files` registry: a registered file under `exports/...`
    must survive a real GC sweep, and a genuine orphan under the same prefix
    must be deleted.

    Dry-run first (proves GC_DRY_RUN=true never deletes anything, including
    the orphan), then a real sweep — scoped to this test's own dataset_id
    namespace, so even a real, populated bucket is never touched by this
    test.
    """
    s3 = export_gc_test_data["s3"]
    dataset_id = export_gc_test_data["dataset_id"]
    export_key = export_gc_test_data["export_key"]
    orphan_key = export_gc_test_data["orphan_key"]

    # Sanity: both objects exist before GC runs.
    assert _object_exists(s3, export_key)
    assert _object_exists(s3, orphan_key)

    reachable = s3_gc.reachable_keys_from_pg(DATABASE_URL, S3_BUCKET)

    # The bare object key registered in export_files must be reachable.
    assert export_key in reachable, (
        "export_files.key was not recognized as reachable -- the "
        "export_files reachability source in reachable_keys_from_pg is broken"
    )
    # The orphan must never appear.
    assert orphan_key not in reachable

    prefix = f"exports/{dataset_id}/"

    # Dry-run first: nothing may be deleted, including the orphan.
    dry_cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[prefix],
        min_age_seconds=-3600,  # see gc_test_data's identical comment: avoids
        # clock-skew false negatives, this test is about reachability, not age.
        delete_limit=1000,
        dry_run=True,
    )
    scanned, deleted = s3_gc.run_gc(dry_cfg, reachable, s3=_gc_s3_client())
    assert deleted == 0, "GC_DRY_RUN=true must never delete anything"
    assert _object_exists(s3, export_key)
    assert _object_exists(s3, orphan_key)

    # Real sweep: only the orphan may go.
    real_cfg = s3_gc.GCConfig(
        bucket=S3_BUCKET,
        prefixes=[prefix],
        min_age_seconds=-3600,
        delete_limit=1000,
        dry_run=False,
    )
    scanned, deleted = s3_gc.run_gc(real_cfg, reachable, s3=_gc_s3_client())

    assert scanned == 2, f"expected to scan exactly the 2 test objects, scanned={scanned}"
    assert deleted == 1, f"expected exactly the orphan to be deleted, deleted={deleted}"

    # The registered export file must survive.
    assert _object_exists(s3, export_key), "export_files-registered object was deleted by GC"
    # The orphan must be gone.
    assert not _object_exists(s3, orphan_key), "genuine orphan export object was NOT deleted by GC"


@pytest.fixture
def mpu_test_data(live_stack):
    """Create an incomplete multipart upload (create + one part, never
    completed) under a per-run key so the sweep can never touch unrelated MPUs.

    This is exactly the orphan a crashed/interrupted streaming upload leaves
    behind: parts are stored in the object store but the MPU was never
    completed, so `list_objects_v2` never shows them -- only
    `list_multipart_uploads` does. Any MPU still live at teardown is aborted.
    """
    s3 = live_stack["s3"]
    nonce = uuid.uuid4().hex[:10]
    prefix = f"raw/mcap/mputest-{nonce}/"
    key = f"{prefix}incomplete-{nonce}.mcap"

    create = s3.create_multipart_upload(Bucket=S3_BUCKET, Key=key)
    upload_id = create["UploadId"]
    # Upload one part but never complete -> the MPU is now an orphan. A single
    # never-completed part has no minimum-size constraint (the 5MiB floor is only
    # enforced for non-final parts at complete time), so 1KiB is fine here.
    s3.upload_part(Bucket=S3_BUCKET, Key=key, UploadId=upload_id, PartNumber=1, Body=b"x" * 1024)

    data = {"s3": s3, "key": key, "upload_id": upload_id, "prefix": prefix}
    try:
        yield data
    finally:
        try:
            s3.abort_multipart_upload(Bucket=S3_BUCKET, Key=key, UploadId=upload_id)
        except Exception:
            pass


def _mpu_exists(s3, upload_id: str) -> bool:
    """True iff a multipart upload with `upload_id` is currently listed.

    Lists unfiltered and matches on the globally-unique upload id, rather than
    passing a `Prefix` to `list_multipart_uploads`: some S3-compatible stores
    do not honor a true key prefix on that call (it only matches when
    Prefix == the full key), so a prefix filter here could spuriously report
    the MPU as gone.
    """
    paginator = s3.get_paginator("list_multipart_uploads")
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for up in page.get("Uploads", []) or []:
            if up.get("UploadId") == upload_id:
                return True
    return False


def test_mpu_reclaim_aborts_old_incomplete_upload(mpu_test_data):
    """`reclaim_incomplete_mpus` aborts an orphaned incomplete MPU.

    Asserts both directions:
      - dry_run=True lists the MPU as a candidate but does NOT abort it.
      - dry_run=False with a 0-hour threshold aborts it (no longer listed).

    Scoped to this test's own key prefix, so no other in-flight upload -- real
    or from another test -- can ever be affected.
    """
    s3 = mpu_test_data["s3"]
    key = mpu_test_data["key"]
    upload_id = mpu_test_data["upload_id"]
    prefix = mpu_test_data["prefix"]

    # The incomplete MPU is live before the sweep, and invisible to the normal
    # object listing (the reachability sweep can't reclaim it -- only this can).
    assert _mpu_exists(s3, upload_id), "test setup: the incomplete MPU should be listed"
    listed_objs = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix).get("Contents", []) or []
    assert listed_objs == [], "an incomplete MPU must not appear as a completed object"

    # dry_run=True: must list the candidate but abort nothing.
    aborted, candidates = s3_gc.reclaim_incomplete_mpus(
        _gc_s3_client(), S3_BUCKET, max_age_seconds=0, dry_run=True, prefix=prefix
    )
    assert aborted == 0, "dry_run=True must abort nothing"
    assert any(c["UploadId"] == upload_id for c in candidates), "dry-run should still list the MPU as a candidate"
    assert _mpu_exists(s3, upload_id), "dry_run=True must NOT abort the MPU"

    # dry_run=False with a 0-hour threshold: the MPU is old enough -> aborted.
    aborted, candidates = s3_gc.reclaim_incomplete_mpus(
        _gc_s3_client(), S3_BUCKET, max_age_seconds=0, dry_run=False, prefix=prefix
    )
    assert aborted == 1, f"expected exactly the test MPU to be aborted, aborted={aborted}"
    assert any(c["UploadId"] == upload_id for c in candidates)
    assert not _mpu_exists(s3, upload_id), "the MPU must no longer be listed after abort"


def test_normalize_object_key_strips_scheme_and_bucket():
    """Unit check of key normalization in isolation (no I/O)."""
    bucket = "robotics-data"
    assert s3_gc.normalize_object_key("s3://robotics-data/raw/mcap/x.mcap", bucket) == "raw/mcap/x.mcap"
    # The processor's format: no scheme, WITH bucket.
    assert (
        s3_gc.normalize_object_key("robotics-data/processed/parquet/r1/t1/2026/07/21/part-u1.parquet", bucket)
        == "processed/parquet/r1/t1/2026/07/21/part-u1.parquet"
    )
    # Already-bare key is left unchanged.
    assert s3_gc.normalize_object_key("processed/parquet/r1/_all/u1.parquet", bucket) == "processed/parquet/r1/_all/u1.parquet"
    assert s3_gc.normalize_object_key("", bucket) == ""
    assert s3_gc.normalize_object_key(None, bucket) == ""
