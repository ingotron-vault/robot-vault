"""Live Intake detail cache across an atomic artifact publication.

The fixture owns one upload and Parquet object. It warms the public detail
route while the upload has no artifacts, publishes a new processing generation
through Postgres, then checks the next HTTP response without waiting for TTL.
It cleans its rows/object and leaves a repeatable JSON result in tmp/.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import uuid

import boto3
from botocore.config import Config
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

import pytest

# Local copies of the live-stack helpers.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgres://robotvault:robotvault@postgres:5432/robotvault")
QUERY_BASE_URL = os.environ.get("QUERY_BASE_URL", "http://query:8003")
REQUIRE_LIVE_STACK = os.environ.get("REQUIRE_LIVE_STACK", "").strip().lower() in {"1", "true", "yes", "on"}


def _live_stack_unavailable(message: str) -> None:
    if REQUIRE_LIVE_STACK:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


@pytest.fixture(scope="module")
def live_stack():
    try:
        r = httpx.get(f"{QUERY_BASE_URL}/healthz", timeout=5)
        if r.status_code != 200:
            _live_stack_unavailable(f"query service not healthy at {QUERY_BASE_URL}/healthz: {r.status_code}")
    except Exception as e:  # noqa: BLE001
        _live_stack_unavailable(f"query service not reachable at {QUERY_BASE_URL}: {e}")
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception as e:  # noqa: BLE001
        _live_stack_unavailable(f"configured Postgres is not reachable ({type(e).__name__})")


def _dev_token(roles: str = "viewer") -> str:
    r = httpx.get(f"{QUERY_BASE_URL}/auth/dev/token", params={"roles": roles}, timeout=15)
    assert r.status_code == 200, f"/auth/dev/token failed: {r.status_code} {r.text}"
    body = r.json()
    assert "access_token" in body, f"/auth/dev/token returned no access_token: {body}"
    return str(body["access_token"])


def test_intake_detail_refreshes_after_publication(live_stack):
    upload_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    robot_id = f"e2e_detail_{uuid.uuid4().hex[:10]}"
    bucket = os.environ.get("S3_PARQUET_BUCKET", os.environ.get("S3_BUCKET", "robotics-data"))
    object_key = f"processed/parquet/{robot_id}/_attempts/{attempt_id}/imu/2026/10/02/rows-{upload_id}.parquet"
    stored_path = f"{bucket}/{object_key}"
    times = [1_700_000_000_000_000_000 + offset * 1_000_000_000 for offset in range(3)]
    buffer = io.BytesIO()
    pq.write_table(pa.table({
        "ts": pa.array(times, type=pa.int64()),
        "topic": pa.array(["imu"] * 3),
        "x": pa.array([1.0, 2.0, 3.0]),
        "y": pa.array([0.0, 0.0, 0.0]),
        "z": pa.array([0.0, 0.0, 0.0]),
    }), buffer)
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://objectstore:9000"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("OBJECTSTORE_ROOT_USER", "robot-vault-dev-root"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("OBJECTSTORE_ROOT_PASSWORD")
        or Path(os.environ.get("OBJECTSTORE_ROOT_PASSWORD_FILE", "/app/configs/objectstore-root-password.dev")).read_text().strip(),
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    auth = {"authorization": "Bearer " + _dev_token("viewer")}
    url = f"{QUERY_BASE_URL}/api/v1/uploads/{upload_id}/detail"

    try:
        s3.put_object(Bucket=bucket, Key=object_key, Body=payload)
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("INSERT INTO robots (id,name) VALUES (%s,%s)", (robot_id, robot_id))
            conn.execute(
                "INSERT INTO uploads (id,client_upload_id,robot_id,original_filename,storage_path,"
                "file_size_bytes,status,upload_completed_at) "
                "VALUES (%s,%s,%s,%s,%s,0,'completed',NOW())",
                (upload_id, f"detail-{upload_id}", robot_id, "detail-fixture.mcap", f"/tmp/{upload_id}.mcap"),
            )
            conn.execute(
                "INSERT INTO processing_status (upload_id,status,attempts,completed_at) "
                "VALUES (%s,'completed_empty',1,NOW())",
                (upload_id,),
            )

        before_response = httpx.get(url, headers=auth, timeout=30)
        assert before_response.status_code == 200, before_response.text
        before = before_response.json()
        assert before["status"] == "completed" and before["processing_status"] == "completed_empty", before
        assert before["rows_total"] == 0 and before["ts_min"] is None, before

        # Simulate the processor's publication transaction. The upload row
        # stays completed, so only the new artifact generation can invalidate
        # the warm detail cache. The processor never selects completed_empty.
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(
                "UPDATE processing_status SET status='completed',attempts=2,attempt_id=%s,"
                "completed_at=NOW(),updated_at=NOW() WHERE upload_id=%s",
                (attempt_id, upload_id),
            )
            conn.execute(
                "INSERT INTO processed (upload_id,completed_at,rows_written,attempt_id) "
                "VALUES (%s,NOW(),3,%s)",
                (upload_id, attempt_id),
            )
            conn.execute(
                "INSERT INTO processed_topics (upload_id,topic,rows_written,checksum,parquet_path,attempt_id) "
                "VALUES (%s,'imu',3,%s,%s,%s)",
                (upload_id, digest, stored_path, attempt_id),
            )
            conn.execute(
                "INSERT INTO parquet_files (file_path,robot_id,message_type,upload_id,"
                "start_timestamp,end_timestamp,row_count,file_size_bytes,schema_hash,"
                "column_names,all_null_columns,sha256,attempt_id) "
                "VALUES (%s,%s,'imu',%s,to_timestamp(%s),to_timestamp(%s),3,%s,%s,"
                "ARRAY['ts','topic','x','y','z'],ARRAY['y','z'],%s,%s)",
                (stored_path, robot_id, upload_id, times[0] / 1e9, times[-1] / 1e9,
                 len(payload), "e2e-detail-fixture", digest, attempt_id),
            )

        after_response = httpx.get(url, headers=auth, timeout=30)
        assert after_response.status_code == 200, after_response.text
        after = after_response.json()
        assert after["status"] == "completed" and after["processing_status"] == "completed", after
        assert after["rows_total"] == 3 and after["ts_min"] and after["ts_max"], after
        assert len(after["topics"]) == 1 and after["topics"][0]["rows"] == 3, after

        artifact = Path("/app/tmp/e2e/upload-detail-publication-e2e.json")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({
            "pass": True,
            "upload_id": upload_id,
            "parquet_sha256": digest,
            "before": {"processing_status": before["processing_status"], "rows_total": before["rows_total"], "ts_min": before["ts_min"]},
            "after": {"processing_status": after["processing_status"], "rows_total": after["rows_total"], "ts_min": after["ts_min"], "ts_max": after["ts_max"]},
        }, indent=2) + "\n")
    finally:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute("DELETE FROM uploads WHERE id=%s", (upload_id,))
            conn.execute("DELETE FROM robots WHERE id=%s", (robot_id,))
        s3.delete_object(Bucket=bucket, Key=object_key)
