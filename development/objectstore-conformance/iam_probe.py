"""Live least-privilege probe: the five service keys against the reviewed policy JSON.

Step "iam" of the gate (`run.sh`).
For each service key and each managed prefix (plus `other/`) it sends 7 object probes,
plus 2 bucket-level probes per key: 5 x (8 x 7 + 2) = 290 probes. The oracle is the static
evaluator `_allows()` (ORACLE, default development/release/test_objectstore_access.py) reading the
policies in development/release/objectstore-policies/. Every live answer must match:
0 over-permit, 0 under-permit, 0 other errors. Writes <out>/iam.json; exit 1 on any mismatch.

Env: S3_ENDPOINT, S3_BUCKET (bootstrapped), ROOT_ACCESS_KEY/ROOT_SECRET_KEY (fixtures),
<SVC>_S3_ACCESS_KEY/<SVC>_S3_SECRET_KEY for the five services.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from suite_env import require_env

require_env()
REPO = Path(os.environ.get("REPO", Path(__file__).resolve().parents[2]))
ENDPOINT = os.environ.get("S3_ENDPOINT", "http://objectstore:9000")
BUCKET = os.environ["S3_BUCKET"]
SERVICES = ("ingest", "processor", "exporter", "query", "gc")


def client(access, secret):
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name=os.environ.get("S3_REGION", "us-east-1"),
                        aws_access_key_id=access, aws_secret_access_key=secret,
                        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))


def outcome(fn):
    try:
        fn()
        return "allow"
    except ClientError as e:
        c = e.response.get("Error", {}).get("Code", "") or str(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode"))
        return "deny" if c in ("AccessDenied", "403", "Forbidden") else f"error:{c}"


def main(out: str) -> int:
    spec = importlib.util.spec_from_file_location("policy_oracle", REPO / os.environ.get(
        "ORACLE", "development/release/test_objectstore_access.py"))
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    ev.POLICY_DIR = REPO / "development/release/objectstore-policies"
    R = client(os.environ["ROOT_ACCESS_KEY"], os.environ["ROOT_SECRET_KEY"])
    prefixes = list(ev.MANAGED_PREFIXES) + ["other/"]
    for p in prefixes:
        for f in ["probe-get"] + [f"probe-del-{s}" for s in SERVICES]:
            R.put_object(Bucket=BUCKET, Key=f"{p}iam/{f}.bin", Body=b"p")
    over, under, errors, total = [], [], [], 0

    def judge(svc, action, kw, fn):
        nonlocal total
        total += 1
        want = "allow" if ev._allows(svc, action, **kw) else "deny"
        got = outcome(fn)
        row = f"{svc} {action} {kw}"
        if got.startswith("error"):
            errors.append(f"{row} -> {got}")
        elif got != want:
            (over if got == "allow" else under).append(row)

    for svc in SERVICES:
        c = client(os.environ[f"{svc.upper()}_S3_ACCESS_KEY"], os.environ[f"{svc.upper()}_S3_SECRET_KEY"])
        for p in prefixes:
            k = f"{p}iam/mpu-{svc}.bin"
            mpu = R.create_multipart_upload(Bucket=BUCKET, Key=k)["UploadId"]
            R.upload_part(Bucket=BUCKET, Key=k, UploadId=mpu, PartNumber=1, Body=b"m")
            ck = f"{p}iam/cmpu-{svc}.bin"
            for action, kw, fn in [
                ("s3:GetObject", dict(key=f"{p}iam/probe-get.bin"),
                 lambda: c.get_object(Bucket=BUCKET, Key=f"{p}iam/probe-get.bin")["Body"].read()),
                ("s3:PutObject", dict(key=f"{p}iam/put-{svc}.bin"),
                 lambda: c.put_object(Bucket=BUCKET, Key=f"{p}iam/put-{svc}.bin", Body=b"w")),
                ("s3:PutObject", dict(key=ck),
                 lambda: R.abort_multipart_upload(Bucket=BUCKET, Key=ck, UploadId=c.create_multipart_upload(Bucket=BUCKET, Key=ck)["UploadId"])),
                ("s3:DeleteObject", dict(key=f"{p}iam/probe-del-{svc}.bin"),
                 lambda: c.delete_object(Bucket=BUCKET, Key=f"{p}iam/probe-del-{svc}.bin")),
                ("s3:ListBucket", dict(prefix=p), lambda: c.list_objects_v2(Bucket=BUCKET, Prefix=p)),
                ("s3:ListMultipartUploadParts", dict(key=k), lambda: c.list_parts(Bucket=BUCKET, Key=k, UploadId=mpu)),
                ("s3:AbortMultipartUpload", dict(key=k), lambda: c.abort_multipart_upload(Bucket=BUCKET, Key=k, UploadId=mpu)),
            ]:
                judge(svc, action, kw, fn)
            try:
                R.abort_multipart_upload(Bucket=BUCKET, Key=k, UploadId=mpu)
            except ClientError:
                pass
        judge(svc, "s3:ListBucket", dict(prefix=""), lambda: c.list_objects_v2(Bucket=BUCKET))
        judge(svc, "s3:ListBucketMultipartUploads", {}, lambda: c.list_multipart_uploads(Bucket=BUCKET))
    ok = total == 290 and not (over or under or errors)
    result = {"probes": total, "over_permit": over, "under_permit": under, "errors": errors,
              "status": "pass" if ok else "fail"}
    with open(f"{out}/iam.json", "w") as f:
        json.dump(result, f, indent=1)
    print(f"iam: probes={total} over-permit={len(over)} under-permit={len(under)} errors={len(errors)} -> {result['status']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
