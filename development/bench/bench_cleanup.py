#!/usr/bin/env python3
"""Tear down everything a `part_pipelining_bench.py` run left behind.

Scoped by `robot_id`. The scoping alone is NOT a safety property -- this script
irreversibly deletes whatever robot_id it is given -- so it refuses any id that
does not look like a benchmark robot (`bench_` prefix) unless --i-know-what-i-am-doing
is passed. Handing it a real fleet id would destroy that robot's raw objects,
manifests, processed Parquet and every Postgres row. Deletes the S3 objects the bench uploads
wrote, aborts any multipart upload still open under the bench prefix, then
deletes the `uploads` rows (which cascades to upload_chunks / processing_status /
processed / manifests) and finally the `robots` registry row.

    docker compose exec -T dev python /tmp/bench_cleanup.py bench_pipelining
"""

import os
import sys
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config

DSN = "postgres://robotvault:robotvault@postgres:5432/robotvault"
BUCKET = "robotics-data"


BENCH_PREFIX = "bench_"
OVERRIDE_FLAG = "--i-know-what-i-am-doing"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != OVERRIDE_FLAG]
    override = OVERRIDE_FLAG in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    if not args[0].startswith(BENCH_PREFIX) and not override:
        print(
            f"refusing to delete robot_id={args[0]!r}: it does not start with "
            f"{BENCH_PREFIX!r}, so it may be a real robot. This deletes S3 objects "
            f"and Postgres rows irreversibly. Pass {OVERRIDE_FLAG} if you are sure.",
            file=sys.stderr,
        )
        return 2
    sys.argv = [sys.argv[0], args[0]]
    robot = sys.argv[1]
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://objectstore:9000"),
        # Root key of the bench stack: it removes the bench robot's objects (fixtures, never a service).
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("OBJECTSTORE_ROOT_USER", "robot-vault-dev-root"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("OBJECTSTORE_ROOT_PASSWORD")
        or Path(os.environ.get("OBJECTSTORE_ROOT_PASSWORD_FILE", "/app/configs/objectstore-root-password.dev")).read_text().strip(),
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT storage_path FROM uploads WHERE robot_id=%s", (robot,))
        paths = [r[0] for r in cur.fetchall() if r[0]]

    for p in paths:
        bucket, key = p[len("s3://"):].split("/", 1)
        s3.delete_object(Bucket=bucket, Key=key)
        print("deleted", p)

    for prefix in (f"raw/mcap/{robot}/", f"manifests/{robot}/", f"processed/parquet/{robot}/"):
        token = {}
        while True:
            resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, **token)
            for obj in resp.get("Contents", []):
                s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
                print("deleted", obj["Key"])
            if not resp.get("IsTruncated"):
                break
            token = {"ContinuationToken": resp["NextContinuationToken"]}

    for mpu in s3.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []):
        if f"/{robot}/" in mpu["Key"]:
            s3.abort_multipart_upload(Bucket=BUCKET, Key=mpu["Key"], UploadId=mpu["UploadId"])
            print("aborted mpu", mpu["Key"])

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM uploads WHERE robot_id=%s", (robot,))
        print("uploads rows deleted:", cur.rowcount)
        cur.execute("DELETE FROM robots WHERE id=%s", (robot,))
        print("robots rows deleted:", cur.rowcount)
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
