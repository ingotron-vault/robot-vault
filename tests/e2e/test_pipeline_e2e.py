"""Real, Postgres-backed end-to-end pipeline test (see docs/TESTING.md).

It asserts on Postgres rows and object-store keys; the Postgres-only ingest
server writes no `uploads/index.db` SQLite file.

Run against the LIVE docker-compose stack, from *inside* the `dev`
container (it has `rsm`/`rua`, `mcap`, `pyarrow`, `psycopg`, `boto3`,
`httpx`, and the generated `upload_pb2` gRPC stubs already installed):

    docker compose run --rm dev bash -c \
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/e2e/test_pipeline_e2e.py -v -s"

(this is also exactly what `make test-e2e` runs, over the whole
`tests/e2e` directory).

What it exercises, end to end:

  1. Generates a small, real MCAP file -- a handful of JSON-encoded
     `sensor_msgs/msg/Imu` messages -- under a unique
     `robot_id=e2e_test_<nonce>` so this test's data can never collide
     with, or be mistaken for, real fleet data.
  2. Drives the REAL upload path: `rsm scan` + `rsm hash` + `rua upload`,
     exactly like `development/seed.sh`, which talks real gRPC to
     `ingest:50051`. No hand-rolled gRPC client (see the dependency note
     below on why this file uses `httpx`).
  3. Polls Postgres `uploads` for a `status='completed'`,
     `integrity_verified=true` row (the end-to-end integrity guarantee) and
     checks the recorded byte count and sha256 against the source file.
  4. Waits for the processor (it polls every 5s) to convert the upload,
     via Postgres `processing_status` (the per-upload tracker),
     then asserts the query service reports the exact, non-doubled
     message count (a combined copy of every message next to the per-topic
     files would make a naive glob double every count).
  5. Cleans up every Postgres row, S3 object, and local file this
     test created in a `finally` block, keyed off the unique `robot_id`.

Dependency note: `requests` is not installed in the current `dev` image
(only `httpx` is -- both are already in `requirements-dev.txt`'s HTTP
client surface via FastAPI/query-service's own `httpx` dependency). This
file uses `httpx` throughout instead of `requests` so it runs unmodified
against the already-built, already-running stack without requiring an
image rebuild.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import boto3
import grpc
import httpx
import psycopg
import pytest
import upload_pb2 as pb
import upload_pb2_grpc as pb_grpc
from botocore.config import Config as BotoConfig
from mcap.writer import Writer as McapWriter


# --- Live-stack endpoints -------------------------------------------------
# In-network addresses (see docker-compose.yml service names), each
# env-overridable so this same file also runs unmodified from a CI job that
# injects its own environment/.env.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://robotvault:robotvault@postgres:5432/robotvault")
INGEST_ADMIN_URL = os.environ.get("INGEST_ADMIN_URL", "http://ingest:8080")
QUERY_BASE_URL = os.environ.get("QUERY_BASE_URL", "http://query:8003")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://objectstore:9000")
S3_BUCKET = os.environ.get("S3_BUCKET", "robotics-data")
# The root key seeds and removes this test's fixtures only (the dev compose file mounts the repository
# at /app, so the development root password file is readable there).
S3_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("OBJECTSTORE_ROOT_USER", "robot-vault-dev-root")
S3_SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("OBJECTSTORE_ROOT_PASSWORD") or (
    Path(os.environ.get("OBJECTSTORE_ROOT_PASSWORD_FILE", "/app/configs/objectstore-root-password.dev")).read_text().strip()
    if Path(os.environ.get("OBJECTSTORE_ROOT_PASSWORD_FILE", "/app/configs/objectstore-root-password.dev")).is_file() else "")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
VAULT_CONFIG_PATH = os.environ.get("VAULT_CONFIG_PATH", "/app/configs/vault.local.json")
ROBOT_DATA_ROOT = Path(os.environ.get("ROBOT_DATA_ROOT", "/tmp/robot_data"))
# Plaintext gRPC, matching configs/vault.local.json's `"tls": {"enabled": false}`
# and the stack's ingest.env (which sets no TLS_ENABLED, defaulting to false) --
# i.e. exactly the channel `rua` itself opens above.
INGEST_GRPC_TARGET = os.environ.get("INGEST_GRPC_TARGET", "ingest:50051")
# The ingest server rejects UploadService RPCs that carry no
# `authorization` metadata whenever INGEST_AUTH_TOKEN is set on its side.
# docker-compose.yml hands the same token to the `dev` service this test runs
# in, so reading it from the environment keeps the hand-rolled stub calls below
# in step with what `rua` sends (see transport.py's `_auth_metadata`).
INGEST_AUTH_TOKEN = os.environ.get("INGEST_AUTH_TOKEN", "")
GRPC_AUTH_METADATA = (("authorization", f"Bearer {INGEST_AUTH_TOKEN}"),) if INGEST_AUTH_TOKEN else ()
REQUIRE_LIVE_STACK = os.environ.get("REQUIRE_LIVE_STACK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
INGEST_MAIN_RS = REPO_ROOT / "phases" / "03-ingest-server-rs" / "src" / "main.rs"

RSM_BIN = os.environ.get("RSM_BIN", "rsm")
RUA_BIN = os.environ.get("RUA_BIN", "rua")

NUM_MESSAGES = 6
RAW_TOPIC = "/robot/imu"
# `/robot/imu` has no ang/gyro/lin/acc hint in it, so the processor's
# `_normalize_topic` falls through to the plain "imu" bucket -- see
# phases/04-processing-pipeline/tests/unit/test_processing_core.py's golden
# test, which this test's MCAP-generation mirrors.
NORMALIZED_TOPIC = "imu"

INGEST_READY_TIMEOUT_S = 60.0
UPLOAD_TIMEOUT_S = 60.0
PROCESS_TIMEOUT_S = 90.0
QUERY_TIMEOUT_S = 60.0


# --- Helpers ---------------------------------------------------------------

def _live_stack_unavailable(message: str) -> None:
    if REQUIRE_LIVE_STACK:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _run(cmd: list, timeout: float = 60.0) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    print(f"$ {' '.join(cmd)}\n{proc.stdout}{proc.stderr}")
    return proc


def _write_test_mcap(path: Path, num_messages: int) -> None:
    """Write a real MCAP with a handful of JSON-encoded sensor_msgs/msg/Imu
    messages.

    JSON, not CDR: the dev image this test runs in has no
    rclpy/rosidl_runtime_py, so a CDR-encoded message would decode to
    (None, None, None) via `processor.extractors`'s lazy-import fallback,
    making any assertion on the decoded values meaningless. JSON needs no
    ROS2 runtime -- exactly the choice
    `test_processing_core.py::TestGoldenMcapParquet` documents and makes.
    """
    readings = [
        {"x": round(0.1 + i, 3), "y": round(0.2 + i * 2, 3), "z": round(9.8 - i * 0.01, 3)}
        for i in range(num_messages)
    ]
    base_ts = time.time_ns()
    ts_values = [base_ts + i * 1_000_000 for i in range(num_messages)]  # 1 ms apart

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        writer = McapWriter(f)
        writer.start()
        schema_id = writer.register_schema(
            name="sensor_msgs/msg/Imu", encoding="jsonschema", data=b"{}",
        )
        channel_id = writer.register_channel(
            topic=RAW_TOPIC, message_encoding="json", schema_id=schema_id,
        )
        for reading, ts in zip(readings, ts_values):
            payload = {"linear_acceleration": reading}
            writer.add_message(
                channel_id=channel_id,
                log_time=ts,
                data=json.dumps(payload).encode("utf-8"),
                publish_time=ts,
            )
        writer.finish()


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _delete_s3_url(s3, s3_url: Optional[str]) -> None:
    if not s3_url or not s3_url.startswith("s3://"):
        return
    rest = s3_url[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if bucket and key:
        s3.delete_object(Bucket=bucket, Key=key)


def _delete_s3_prefix(s3, bucket: str, prefix: str) -> None:
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            keys.append({"Key": obj["Key"]})
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})


def _wait_for_ingest_ready(timeout: float = INGEST_READY_TIMEOUT_S) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{INGEST_ADMIN_URL}/healthz", timeout=3)
            if r.status_code == 200 and r.text.strip().strip('"') == "ok":
                return
            last_err = f"status={r.status_code} body={r.text!r}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(1)
    raise AssertionError(f"ingest not ready at {INGEST_ADMIN_URL}/healthz within {timeout}s: {last_err}")


def _register_robot(robot_id: str) -> None:
    """Provision this test's robot in the `robots` fleet registry.

    Ingest refuses an upload whose `robot_id` has no `robots` row
    (`lookup_upload_target` in main.rs), so a test that invents a fresh
    `e2e_test_<nonce>` robot has to provision it first -- exactly as an
    operator would for a real new machine. This exercises the registration
    endpoint itself, and step 2 below asserts the *un*registered case is
    genuinely refused, so the constraint is proven in both directions.
    """
    r = httpx.post(
        f"{INGEST_ADMIN_URL}/admin/robots",
        params={
            "robot_id": robot_id,
            "name": f"e2e fixture {robot_id}",
            "metadata": json.dumps({"source": "tests/e2e/test_pipeline_e2e.py"}),
        },
        headers={"authorization": f"Bearer {INGEST_AUTH_TOKEN}"} if INGEST_AUTH_TOKEN else {},
        timeout=15,
    )
    assert r.status_code == 200, f"robot registration failed: {r.status_code} {r.text}"
    assert r.json().get("created") is True, f"expected a newly created robot row: {r.text}"


def _write_robot_scoped_vault_config(robot_id: str, dest: Path) -> Path:
    """A copy of the stack's vault config with an explicit `robot_id`.

    Robot identity is declared config rather than a parse of the file path.
    Driving `rua` through a config that names the robot is what proves that
    path works end to end -- and it decouples this test's
    identity from where its bag happens to sit on disk.
    """
    cfg = json.loads(Path(VAULT_CONFIG_PATH).read_text())
    cfg["robot_id"] = robot_id
    dest.write_text(json.dumps(cfg, indent=2))
    return dest


def _wait_for_upload_row(robot_id: str, filename: str, timeout: float) -> dict:
    """Poll Postgres `uploads` until this robot/filename's row reaches a
    terminal status ('completed' or 'failed')."""
    cols = [
        "id", "client_upload_id", "status", "integrity_verified",
        "file_size_bytes", "bytes_received", "sha256_hash",
        "storage_path", "error_message",
        # Capture metadata: proto/upload.proto
        # UploadMetadata fields 11-15 -> uploads columns.
        "operator", "site", "permission_ref", "session_tags", "task_label",
    ]
    deadline = time.time() + timeout
    last_row = None
    while time.time() < deadline:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join(cols)}
                    FROM uploads
                    WHERE robot_id = %s AND original_filename = %s
                    ORDER BY upload_started_at DESC
                    LIMIT 1
                    """,
                    (robot_id, filename),
                )
                row = cur.fetchone()
        if row is not None:
            last_row = dict(zip(cols, row))
            if last_row["status"] in ("completed", "failed"):
                return last_row
        time.sleep(1)
    raise AssertionError(
        f"no terminal uploads row for robot_id={robot_id} filename={filename} "
        f"within {timeout}s (last observed: {last_row})"
    )


# --- completed rows are immutable to overwrite -----------------------------
# Every field the completed-row upsert's DO UPDATE can touch, plus robot_id
# (which the SET list never touched, but which the guard is equally supposed to
# pin). Snapshotted before the overwrite attempts and compared after.
#
# Capture metadata: the five capture columns ARE in
# `COMPLETED_UPLOAD_UPSERT_SQL`'s SET list (main.rs) -- unlike storage_path
# et al, which the guard protects from ever changing, these are restated on
# every transition into `completed` because `upload_small` has no other
# writer for them. They still belong in this immutability check: the
# guard (`WHERE uploads.status <> 'completed'`) must stop that restatement
# from touching an ALREADY-completed row, exactly like every other column
# here.
IMMUTABLE_ROW_COLS = (
    "robot_id", "original_filename", "status", "sha256_hash", "expected_sha256", "storage_path",
    "file_size_bytes", "bytes_received", "content_type", "integrity_verified",
    "operator", "site", "permission_ref", "session_tags", "task_label",
)


def _fetch_upload_row(client_upload_id: str) -> dict:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(IMMUTABLE_ROW_COLS)} FROM uploads WHERE client_upload_id = %s",
                (client_upload_id,),
            )
            row = cur.fetchone()
    assert row is not None, f"no uploads row for client_upload_id={client_upload_id}"
    return dict(zip(IMMUTABLE_ROW_COLS, row))


def _completed_upsert_sql() -> str:
    """Return the ingest server's OWN completed-row upsert, read out of the Rust
    source (`COMPLETED_UPLOAD_UPSERT_SQL` in
    phases/03-ingest-server-rs/src/main.rs).

    Read rather than copied so this test can never drift from the statement the
    server actually issues: if someone edits the upsert, this test replays the
    edited one, and a dropped `WHERE uploads.status <> 'completed'` fails here
    instead of quietly passing against a stale copy.
    """
    src = INGEST_MAIN_RS.read_text(encoding="utf-8")
    m = re.search(r'const COMPLETED_UPLOAD_UPSERT_SQL: &str = "(.+?)";', src)
    assert m, f"could not find COMPLETED_UPLOAD_UPSERT_SQL in {INGEST_MAIN_RS}"
    return m.group(1)


def _upload_small(robot_id: str, filename: str, upload_id: str, data: bytes, sha256: str):
    """Drive one real `UploadSmall` RPC against the live ingest server.

    Used to replay a completion for an upload_id that is already `completed`,
    which `rua` has no way to ask for on demand.
    """
    channel = grpc.insecure_channel(
        INGEST_GRPC_TARGET,
        options=[("grpc.max_send_message_length", 50 * 1024 * 1024)],
    )
    try:
        stub = pb_grpc.UploadServiceStub(channel)
        md = pb.UploadMetadata(
            robot_id=robot_id,
            filename=filename,
            file_size=len(data),
            content_type="application/octet-stream",
            upload_id=upload_id,
            expected_sha256=sha256,
        )
        return stub.UploadSmall(
            pb.UploadSmallRequest(metadata=md, data=data),
            timeout=30,
            metadata=GRPC_AUTH_METADATA,
        )
    finally:
        channel.close()


def _upload_small_raw(robot_id: str, filename: str, upload_id: str, data: bytes):
    """Like `_upload_small`, but returns the `grpc.RpcError` instead of raising.

    Used for the unregistered-robot negative probe, where the *rejection* is the thing
    under test. Returns None if the call unexpectedly succeeded.
    """
    try:
        _upload_small(robot_id, filename, upload_id, data, hashlib.sha256(data).hexdigest())
        return None
    except grpc.RpcError as e:
        return e


def _wait_for_processing_status(upload_pk: str, timeout: float) -> dict:
    """Poll Postgres `processing_status` (the per-upload
    attempt/status tracker, upserted by the processor on every attempt)
    until this upload's row reaches a terminal status."""
    deadline = time.time() + timeout
    last_row = None
    while time.time() < deadline:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, attempts, metadata FROM processing_status WHERE upload_id = %s",
                    (upload_pk,),
                )
                row = cur.fetchone()
        if row is not None:
            last_row = {"status": row[0], "attempts": row[1], "metadata": row[2]}
            if last_row["status"] in ("completed", "completed_empty", "dead_letter", "failed"):
                return last_row
        time.sleep(1)
    raise AssertionError(
        f"processing_status for upload_id={upload_pk} did not reach a terminal "
        f"state within {timeout}s (last observed: {last_row}) -- is the processor "
        f"container running and healthy (`docker compose ps processor`)?"
    )


def _dev_token(roles: str = "viewer") -> str:
    """Mint a short-lived dev-mode bearer token via the query service's
    `/auth/dev/token` bootstrap route -- the same route `lib/auth.ts`'s
    `ensureDevToken` calls for the SPA's own dev-mode login (see
    `phases/05-query-service/src/ui-spa/src/lib/auth.ts`).

    `configs/query.env` sets `AUTH_MODE=oidc`/`OIDC_DEV=true`, which puts a
    viewer floor (`main.py`'s `viewer_guard`) on every data-bearing query
    router, so the query-service calls below need this like any other
    caller.
    """
    r = httpx.get(f"{QUERY_BASE_URL}/auth/dev/token", params={"roles": roles}, timeout=15)
    assert r.status_code == 200, f"/auth/dev/token failed: {r.status_code} {r.text}"
    body = r.json()
    assert "access_token" in body, f"/auth/dev/token returned no access_token: {body}"
    return str(body["access_token"])


def _auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def _wait_for_query_count(robot_id: str, topic: str, timeout: float, token: str) -> int:
    """Poll `/api/v1/robots/{robot_id}/stats/{topic}` until it reports a
    non-zero row count, then return it. The short poll absorbs the gap
    between the processor's S3 write completing and it being visible to a
    fresh DuckDB-over-httpfs read."""
    url = f"{QUERY_BASE_URL}/api/v1/robots/{robot_id}/stats/{topic}"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, headers=_auth(token), timeout=10)
            if r.status_code == 200:
                data = r.json()
                rows = data.get("rows") or []
                if rows and rows[0][0]:
                    return int(rows[0][0])
                last = data
            else:
                last = f"status={r.status_code} body={r.text!r}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1)
    raise AssertionError(f"query count for robot_id={robot_id} topic={topic} never became non-zero within {timeout}s: {last}")


def _cleanup(robot_id: str, root: Path, extra_s3_urls: Optional[list] = None) -> None:
    """Best-effort teardown of every Postgres row, S3 object, and
    local file this test created -- keyed off the unique robot_id so a
    real robot's data is never at risk. Collects errors instead of raising
    so one failed step doesn't skip the rest, and doesn't mask a real test
    failure that's already propagating.

    `extra_s3_urls` covers content-addressed objects this test wrote outside
    the robot-id prefixes. A conflicting upload-id replay is intentionally not
    included: the identity reservation requires that refusal to happen before S3 I/O.
    """
    errors = []

    storage_paths = list(extra_s3_urls or [])
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT storage_path FROM uploads WHERE robot_id = %s", (robot_id,))
                storage_paths.extend(r[0] for r in cur.fetchall() if r[0])
    except Exception as e:  # noqa: BLE001
        errors.append(f"select storage_path failed: {e}")

    try:
        s3 = _s3_client()
        for sp in storage_paths:
            try:
                _delete_s3_url(s3, sp)
            except Exception as e:  # noqa: BLE001
                errors.append(f"delete {sp} failed: {e}")
        for prefix in (f"processed/parquet/{robot_id}/", f"manifests/{robot_id}/"):
            try:
                _delete_s3_prefix(s3, S3_BUCKET, prefix)
            except Exception as e:  # noqa: BLE001
                errors.append(f"delete prefix {prefix} failed: {e}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"s3 client setup failed: {e}")

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Cascades to processing_status / processed / processed_topics /
                # manifests -- all FK uploads(id) ON DELETE CASCADE
                # (db/migrations/001_baseline.sql).
                cur.execute("DELETE FROM uploads WHERE robot_id = %s", (robot_id,))
                # The fleet-registry row this test provisioned.
                # Ordered after the uploads delete on purpose: the
                # uploads.robot_id FK is ON DELETE NO ACTION, so a robot that still
                # has uploads cannot be removed -- which is the intended
                # protection, not something to work around here.
                cur.execute("DELETE FROM robots WHERE id = %s", (robot_id,))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        errors.append(f"delete uploads rows failed: {e}")

    try:
        if root.exists():
            shutil.rmtree(root)
    except Exception as e:  # noqa: BLE001
        errors.append(f"rmtree {root} failed: {e}")

    if errors:
        print(f"e2e cleanup warnings (robot_id={robot_id}):\n" + "\n".join(errors))


# --- The test ---------------------------------------------------------------

def test_pipeline_upload_process_query_e2e():
    """Robot -> ingest -> processor -> query, against the live stack."""
    robot_id = f"e2e_test_{uuid.uuid4().hex[:10]}"
    root = ROBOT_DATA_ROOT / robot_id
    filename = "e2e_sample.mcap"
    mcap_path = root / filename
    # S3 objects this test writes that no `uploads` row will point at (see
    # `_cleanup`). Ingest refuses the step 3b overwrite before any S3 write,
    # so it stays empty.
    orphan_s3_urls: list = []

    try:
        _wait_for_ingest_ready()

        # --- Step 1: generate a small real MCAP --------------------------
        _write_test_mcap(mcap_path, NUM_MESSAGES)
        assert mcap_path.exists()
        # `rua upload` skips files whose mtime is younger than
        # upload_agent.min_stable_age_seconds (default 5s) -- on a real robot
        # such a file is still being recorded, and uploading it would ship a
        # partial file under a sha256 that no longer matches. This one is
        # complete the instant the writer closes, so back-date its mtime
        # instead of making the test sleep out the window.
        _settled = time.time() - 3600
        os.utime(mcap_path, (_settled, _settled))
        expected_size = mcap_path.stat().st_size
        expected_sha256 = _sha256_of_file(mcap_path)

        # --- Step 2: drive the REAL upload path (rsm scan/hash, rua upload) ---
        # Mirrors development/seed.sh exactly, with two additions:
        # the robot is provisioned in the `robots` registry first, and `rua`
        # runs against a config that DECLARES its robot_id instead of leaving
        # the agent to parse one out of the path.
        vault_config = _write_robot_scoped_vault_config(robot_id, root / "vault.e2e.json")

        # An unregistered robot is refused before anything is stored.
        # Asserted here, while `robot_id` genuinely has no `robots` row, so the
        # registry gate is proven to bite rather than merely to exist.
        unregistered = _upload_small_raw(
            robot_id, "e2e_unregistered_probe.mcap",
            f"unregistered-probe-{uuid.uuid4().hex[:8]}", b"abc",
        )
        assert unregistered is not None, "expected the unregistered upload to be refused"
        assert unregistered.code() == grpc.StatusCode.FAILED_PRECONDITION, (
            f"an unregistered robot_id should be FAILED_PRECONDITION, got "
            f"{unregistered.code()}: {unregistered.details()}"
        )
        assert "not registered" in (unregistered.details() or ""), unregistered.details()
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM uploads WHERE robot_id = %s", (robot_id,))
                assert cur.fetchone()[0] == 0, (
                    "a refused upload must not leave an `uploads` row behind"
                )

        _register_robot(robot_id)

        scan = _run([RSM_BIN, "scan", "--config", str(vault_config), "--root", str(root)])
        assert scan.returncode == 0, f"rsm scan failed: {scan.stderr}"
        scan_result = json.loads(scan.stdout)
        assert scan_result["files_scanned"] == 1, scan_result

        hash_proc = _run([RSM_BIN, "hash", "--config", str(vault_config), "--root", str(root)])
        assert hash_proc.returncode == 0, f"rsm hash failed: {hash_proc.stderr}"
        hash_result = json.loads(hash_proc.stdout)
        assert hash_result["files_hashed"] == 1, hash_result

        upload_proc = _run([RUA_BIN, "upload", "--config", str(vault_config), "--root", str(root)], timeout=60)
        assert upload_proc.returncode == 0, f"rua upload failed: {upload_proc.stderr}"
        upload_result = json.loads(upload_proc.stdout)
        assert upload_result.get("uploaded"), f"rua upload did not report success: {upload_result}"

        # --- Step 3: Postgres row is completed + integrity_verified, bytes match ---
        row = _wait_for_upload_row(robot_id, filename, UPLOAD_TIMEOUT_S)
        assert row["status"] == "completed", (
            f"upload did not complete: status={row['status']} error={row['error_message']}"
        )
        assert row["integrity_verified"] is True, (
            "A rua-driven upload must carry a non-empty expected_sha256 "
            f"that matches the server-computed hash; got integrity_verified={row['integrity_verified']}"
        )
        assert row["file_size_bytes"] == expected_size, (
            f"file_size_bytes {row['file_size_bytes']} != source file size {expected_size}"
        )
        assert row["bytes_received"] == expected_size, (
            f"bytes_received {row['bytes_received']} != source file size {expected_size}"
        )
        assert row["sha256_hash"] == expected_sha256, (
            f"server-recorded sha256 {row['sha256_hash']} != source file sha256 {expected_sha256}"
        )

        # --- Step 3a: capture metadata rode the whole chain -- vault config `capture` section -> manifest
        # (robot_storage_manager) -> UploadMetadata fields 11-15 (transport.py)
        # -> uploads columns (main.rs). `_write_robot_scoped_vault_config`
        # copies VAULT_CONFIG_PATH verbatim except for `robot_id`, so whatever
        # `capture` section configs/vault.local.json declares is exactly what
        # this upload's row should carry.
        vault_capture = json.loads(Path(VAULT_CONFIG_PATH).read_text()).get("capture") or {}
        for _key in ("operator", "site", "permission_ref", "session_tags", "task_label"):
            assert row[_key] == vault_capture.get(_key), (
                f"uploads.{_key} = {row[_key]!r} does not match vault config "
                f"capture.{_key} = {vault_capture.get(_key)!r} -- the capture-metadata chain "
                "(manifest -> proto -> ingest SQL writer) is broken"
            )

        # --- Step 3b: a `completed` row is immutable to overwrite --
        # The `uploads` row is now also the pre-S3 idempotency reservation. A
        # later caller reusing this client_upload_id with a different robot,
        # filename, size or digest must fail before object storage; the guarded
        # completion upsert remains defense in depth for direct SQL callers.
        # The Rust crate has no live-Postgres harness (no dev-dependencies, and
        # the `rust` CI job runs no Postgres service), so this is where the
        # guard is actually exercised; `cargo test`'s
        # `completed_upsert_sql_guards_already_completed_rows` is only a
        # string-level tripwire.
        before = _fetch_upload_row(row["client_upload_id"])
        assert before["status"] == "completed", before

        # (a) The legitimate retry: byte-identical re-upload under the same
        #     upload_id. A full-restart resume restarts from zero, so a client
        #     retrying a file whose row already completed replays exactly this
        #     RPC -- it must still answer `completed`, not error, or the normal
        #     retry flow becomes an error storm. The row is left untouched
        #     because it already says the same thing.
        mcap_bytes = mcap_path.read_bytes()
        replay = _upload_small(robot_id, filename, row["client_upload_id"], mcap_bytes, expected_sha256)
        assert replay.status == "completed", (
            "An identical re-upload of an already-completed upload_id must still "
            f"report completed (the legitimate retry/resume path), got {replay.status!r} "
            f"error={replay.error_message!r}"
        )
        assert replay.storage_path == before["storage_path"], (
            f"identical re-upload reported storage_path {replay.storage_path!r}, "
            f"but the system of record holds {before['storage_path']!r}"
        )
        assert _fetch_upload_row(row["client_upload_id"]) == before, (
            "an identical re-upload must not rewrite the completed row"
        )

        # (b) The attack/bug case: same upload_id, DIFFERENT bytes. The RPC must
        #     fail (the server may not certify a completion whose hash/path the
        #     system of record does not hold) and the row must be untouched.
        overwrite_bytes = f"not-the-original-mcap-{uuid.uuid4().hex}".encode()
        overwrite_sha = hashlib.sha256(overwrite_bytes).hexdigest()
        # Ingest reserves/compares the immutable tuple before any S3
        # request. With ENABLE_FILE_CAS=true, this is the key a write before
        # the refusal would leak; it must remain absent.
        overwrite_key = f"cas/files/{overwrite_sha}"
        assert not (_s3_client().list_objects_v2(
            Bucket=S3_BUCKET, Prefix=overwrite_key, MaxKeys=1,
        ).get("Contents") or []), "overwrite test digest unexpectedly already exists"
        try:
            bad = _upload_small(
                robot_id, "e2e_overwrite_attempt.mcap", row["client_upload_id"],
                overwrite_bytes, overwrite_sha,
            )
            raise AssertionError(
                "Overwriting an already-completed upload_id with different content "
                f"must fail, but the server answered status={bad.status!r} "
                f"storage_path={bad.storage_path!r}"
            )
        except grpc.RpcError as e:
            assert e.code() == grpc.StatusCode.ALREADY_EXISTS, (
                f"expected ALREADY_EXISTS for a conflicting reservation, got {e.code()}: {e.details()}"
            )
            assert "immutable metadata" in (e.details() or ""), (
                f"expected the pre-S3 reservation to reject metadata, got: {e.details()!r}"
            )
        assert not (_s3_client().list_objects_v2(
            Bucket=S3_BUCKET, Prefix=overwrite_key, MaxKeys=1,
        ).get("Contents") or []), (
            "conflicting client_upload_id wrote S3 bytes before its database refusal"
        )

        # (c) The raw statement, replayed by hand: any other caller issuing the
        #     server's own upsert against a completed row must update ZERO rows.
        upsert_sql = _completed_upsert_sql()
        assert "WHERE uploads.status <> 'completed'" in upsert_sql, (
            "The ingest server's completed-row upsert lost its immutability guard; "
            f"got: {upsert_sql}"
        )
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    # Postgres $N placeholders -> psycopg named params (the
                    # statement reuses $5, so positional binding won't do).
                    re.sub(r"\$(\d+)", r"%(p\1)s", upsert_sql),
                    {
                        "p1": row["client_upload_id"],
                        "p2": f"{robot_id}_impostor",
                        "p3": "e2e_overwrite_attempt.mcap",
                        "p4": f"s3://{S3_BUCKET}/cas/files/{overwrite_sha}",
                        "p5": len(overwrite_bytes),
                        "p6": overwrite_sha,
                        "p7": "application/octet-stream",
                        "p8": True,
                        # The upsert's SET list also
                        # restates operator/site/permission_ref/session_tags/
                        # task_label ($9-$13) -- these impostor values must
                        # never land on the already-completed row either.
                        "p9": "impostor-operator",
                        "p10": "impostor-site",
                        "p11": "impostor-permission-ref",
                        "p12": "impostor,tags",
                        "p13": "impostor_task",
                    },
                )
                rows_written = cur.rowcount
            conn.commit()
        assert rows_written == 0, (
            "The guarded upsert must write no row when the target is already "
            f"completed, but it reported {rows_written}"
        )

        after = _fetch_upload_row(row["client_upload_id"])
        assert after == before, (
            "An already-completed uploads row must be immutable to overwrite.\n"
            f"  before: {before}\n  after:  {after}"
        )

        # --- Step 4: processor converts it -------------------------------
        proc_status = _wait_for_processing_status(row["id"], PROCESS_TIMEOUT_S)
        assert proc_status["status"] == "completed", (
            f"processor did not cleanly complete this upload: {proc_status}"
        )
        assert proc_status["metadata"]["rows_written"] == NUM_MESSAGES, (
            f"processor wrote {proc_status['metadata'].get('rows_written')} rows, "
            f"expected {NUM_MESSAGES}: {proc_status['metadata']}"
        )

        # --- Step 5: query API reports the true, non-doubled count -------
        # A combined copy of every message next to the per-topic files would
        # make a naive glob double every count. Reads resolve only the registered per-topic files. Check both
        # the topic-scoped endpoint and the robot-wide, all-topics endpoint.
        # AUTH_MODE=oidc/OIDC_DEV=true (configs/query.env) puts a
        # viewer floor on both query-service routes below; mint one dev token
        # and reuse it for both, same as a real (dev-mode) SPA session would.
        query_token = _dev_token()

        # Intake reads the upload's own Parquet registry rows, not the
        # robot-wide query below. The processor stores bucket/key paths here;
        # the detail endpoint must resolve those objects through DuckDB S3 so
        # its time range and chart links work after processing.
        detail_resp = httpx.get(
            f"{QUERY_BASE_URL}/api/v1/uploads/{row['id']}/detail",
            headers=_auth(query_token),
            timeout=30,
        )
        assert detail_resp.status_code == 200, detail_resp.text
        detail = detail_resp.json()
        assert detail["rows_total"] == NUM_MESSAGES, detail
        assert detail["ts_min"] and detail["ts_max"], (
            "a processed upload must expose timestamps for Intake chart links",
            detail,
        )
        assert detail["ts_min"] <= detail["ts_max"], detail

        topic_count = _wait_for_query_count(robot_id, NORMALIZED_TOPIC, QUERY_TIMEOUT_S, query_token)
        assert topic_count == NUM_MESSAGES, (
            f"/api/v1/robots/{{robot_id}}/stats/{{topic}} returned {topic_count} messages, "
            f"expected exactly {NUM_MESSAGES} (not {2 * NUM_MESSAGES} -- double-count regression)"
        )

        summary_resp = httpx.get(
            f"{QUERY_BASE_URL}/api/uploads/summary",
            params={"robot_id": robot_id},
            headers=_auth(query_token),
            timeout=15,
        )
        assert summary_resp.status_code == 200, summary_resp.text
        summary_total = sum(day["count"] for day in summary_resp.json().get("series", []))
        assert summary_total == NUM_MESSAGES, (
            f"/api/uploads/summary (robot-wide, all-topics) returned {summary_total} messages "
            f"for robot_id={robot_id}, expected exactly {NUM_MESSAGES} "
            f"(not {2 * NUM_MESSAGES} -- double-count regression)"
        )
    finally:
        _cleanup(robot_id, root, orphan_s3_urls)


def test_unauthenticated_calls_are_rejected():
    """The data plane refuses callers without the shared token.

    Both authenticated surfaces are exercised against the live stack:
      - the gRPC `UploadService` (no metadata at all, and a wrong token);
      - the admin router's development registration route `/admin/robots`.
    A rejected call is stopped in the interceptor/middleware, before any
    handler runs. The one authenticated admin call registers the fixed robot
    `e2e_auth_probe`; repeating it is a no-op, so it needs no cleanup. The
    upload happy path is already covered by the pipeline test above, which
    uploads through `rua` with the same token.

    Local runs skip when the server has no token configured. CI's strict live
    stack mode treats that as a configuration failure.
    """
    if not INGEST_AUTH_TOKEN:
        _live_stack_unavailable(
            "INGEST_AUTH_TOKEN not set in this container; ingest auth is disabled server-side"
        )

    md = pb.UploadMetadata(
        robot_id="auth_probe",
        filename="auth_probe.mcap",
        file_size=3,
        content_type="application/octet-stream",
        upload_id=f"auth-probe-{uuid.uuid4().hex[:8]}",
        expected_sha256=hashlib.sha256(b"abc").hexdigest(),
    )
    req = pb.UploadSmallRequest(metadata=md, data=b"abc")

    channel = grpc.insecure_channel(INGEST_GRPC_TARGET)
    try:
        stub = pb_grpc.UploadServiceStub(channel)
        for label, call_md in (
            ("no token", ()),
            ("wrong token", (("authorization", "Bearer not-the-token"),)),
        ):
            with pytest.raises(grpc.RpcError) as excinfo:
                stub.UploadSmall(req, timeout=15, metadata=call_md)
            assert excinfo.value.code() == grpc.StatusCode.UNAUTHENTICATED, (
                f"UploadSmall with {label} should fail UNAUTHENTICATED, got "
                f"{excinfo.value.code()}: {excinfo.value.details()}"
            )
    finally:
        channel.close()

    probe = {"robot_id": "e2e_auth_probe"}
    unauth = httpx.post(f"{INGEST_ADMIN_URL}/admin/robots", params=probe, timeout=10)
    assert unauth.status_code == 401, (
        f"/admin/robots without a token returned {unauth.status_code}, expected 401"
    )
    authed = httpx.post(
        f"{INGEST_ADMIN_URL}/admin/robots",
        headers={"authorization": f"Bearer {INGEST_AUTH_TOKEN}"},
        params=probe,
        timeout=10,
    )
    assert authed.status_code == 200, authed.text
    # The unauthenticated probe surface must keep working: the compose
    # healthcheck polls /readyz and the obs-profile Prometheus job /metrics.
    assert httpx.get(f"{INGEST_ADMIN_URL}/healthz", timeout=10).status_code == 200
    assert httpx.get(f"{INGEST_ADMIN_URL}/readyz", timeout=10).status_code == 200
    assert httpx.get(f"{INGEST_ADMIN_URL}/metrics", timeout=10).status_code == 200
