"""Stale multipart expiry test (proof first).

Rule: an incomplete multipart upload must survive at least 14 days + 15 minutes
(phases/03-ingest-server-rs/src/resumable.rs:11-18). The test opens an upload with one
part (ingest key, raw/), waits HOLD seconds, and requires that the upload and its part are
still there (listed with the gc key, ListParts with the ingest key).

Red: the store runs with a short RUSTFS_API_STALE_UPLOADS_EXPIRY (shorter than HOLD) and a
5 s sweep; the upload is aborted by the store, so the test fails. That proves the setting is
read and applied. Green: the frozen setting (360h) with the same 5 s sweep keeps the upload.

Usage: expiry_test.py <label> <hold seconds> <out dir>. Exit 0 = upload survived.
Writes <out dir>/expiry-<label>.json.
"""

from __future__ import annotations

import json
import os
import sys
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from suite_env import require_env, service_creds

require_env()
label, hold, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]


def client(service):
    a, s = service_creds(service)
    return boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name="us-east-1",
                        aws_access_key_id=a, aws_secret_access_key=s,
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 1}))


ingest, gc = client("ingest"), client("gc")
bucket, key = os.environ["S3_BUCKET"], f"raw/expiry/{label}/open.bin"
up = ingest.create_multipart_upload(Bucket=bucket, Key=key, Metadata={"rdv-resume-session": label})["UploadId"]
etag = ingest.upload_part(Bucket=bucket, Key=key, UploadId=up, PartNumber=1, Body=b"p" * 1024)["ETag"]
start = time.time()
time.sleep(hold)
listed = [u["UploadId"] for u in gc.list_multipart_uploads(Bucket=bucket).get("Uploads", [])]
try:
    parts = [(p["PartNumber"], p["ETag"]) for p in ingest.list_parts(Bucket=bucket, Key=key, UploadId=up)["Parts"]]
except ClientError as e:
    parts = e.response["Error"].get("Code")
survived = up in listed and parts == [(1, etag)]
result = {"label": label, "hold_seconds": hold, "elapsed": round(time.time() - start, 1),
          "listed": up in listed, "parts": parts, "status": "pass" if survived else "fail"}
with open(f"{out}/expiry-{label}.json", "w") as f:
    json.dump(result, f, indent=1)
print(json.dumps(result))
sys.exit(0 if survived else 1)
