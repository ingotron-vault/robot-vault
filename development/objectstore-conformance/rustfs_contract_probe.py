"""Live check of the service-code client contract against the object store (RustFS), as the service keys.

Step "contract-probe" of the gate (run.sh), after the bootstrap. Checks F1 to F3 below.
Uses the REAL clients: query_service.s3api._s3_client (with fastapi stubbed out) and
tools/s3_gc._make_s3_client. Exit code 1 on any failed check; writes <out>/contract-probe.json.

  F1  a URL signed for the public host: SigV4, GET 200, Range 206, tampered signature 403.
  F2  the ingest guarded delete (resumable.rs: HEAD, then DeleteObject If-Match <etag>) on a
      completed multipart object, with the ingest key: wrong ETag -> 412 and the object stays;
      right ETag -> deleted. Complete with If-None-Match * on an existing key -> 412.
  F3  the GC client lists objects and incomplete uploads, aborts one, deletes one (gc key).
Env: S3_ENDPOINT, S3_PUBLIC_ENDPOINT, S3_BUCKET, ROOT_*, INGEST_S3_*, QUERY_S3_*, GC_S3_*.
"""
import importlib
import json
import os
import sys
import time
import types
import uuid
from pathlib import Path

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

from suite_env import require_env

require_env(("S3_PUBLIC_ENDPOINT",))

REPO = Path(os.environ.get("REPO", Path(__file__).resolve().parents[2]))
ENDPOINT, PUBLIC, BUCKET = os.environ["S3_ENDPOINT"], os.environ["S3_PUBLIC_ENDPOINT"], os.environ["S3_BUCKET"]
RUN = uuid.uuid4().hex[:8]
results: list[dict] = []


def check(name, ok, detail=""):
    results.append({"check": name, "pass": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def code(fn):
    try:
        fn()
        return "ok"
    except ClientError as e:
        return str(e.response["ResponseMetadata"]["HTTPStatusCode"])


def plain_client(prefix):
    # Same settings as the services (development/objectstore/CONTRACT.md section 3).
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                        aws_access_key_id=os.environ[f"{prefix}_ACCESS_KEY"] if prefix == "ROOT" else os.environ[f"{prefix}_S3_ACCESS_KEY"],
                        aws_secret_access_key=os.environ[f"{prefix}_SECRET_KEY"] if prefix == "ROOT" else os.environ[f"{prefix}_S3_SECRET_KEY"],
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 1}))


def load_s3api():
    """Import the real s3api with fastapi, settings and the ticket module stubbed (not installed here)."""
    src = REPO / "phases/05-query-service/src"
    pkg = types.ModuleType("query_service")
    pkg.__path__ = [str(src / "query_service")]
    sys.modules["query_service"] = pkg
    for name in ("query_service.settings", "query_service.download_tickets", "fastapi", "fastapi.responses"):
        sys.modules[name] = types.ModuleType(name)
    fastapi = sys.modules["fastapi"]
    for name in ("APIRouter", "HTTPException", "Query", "Request"):
        setattr(fastapi, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
    fastapi.APIRouter.get = fastapi.APIRouter.post = lambda self, *a, **k: (lambda f: f)
    sys.modules["fastapi.responses"].StreamingResponse = object
    return importlib.import_module("query_service.s3api")


def f1_presign(root):
    os.environ.update(AWS_ACCESS_KEY_ID=os.environ["QUERY_S3_ACCESS_KEY"], AWS_SECRET_ACCESS_KEY=os.environ["QUERY_S3_SECRET_KEY"])
    s3api = load_s3api()
    key = f"processed/parquet/contract-probe-{RUN}.bin"
    body = bytes(range(256)) * 64
    root.put_object(Bucket=BUCKET, Key=key, Body=body)
    try:
        url = s3api._s3_client(PUBLIC).generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=120)
        check("F1 presigned URL is SigV4", "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url and "Signature=" not in url.replace("X-Amz-Signature=", ""), url.split("?")[0])
        check("F1 URL host is the public host", url.startswith(PUBLIC + "/"), url.split("?")[0])
        r = requests.get(url, timeout=20)
        check("F1 GET 200 with the full body", r.status_code == 200 and r.content == body, r.status_code)
        r = requests.get(url, headers={"Range": "bytes=10-19"}, timeout=20)
        check("F1 Range request gives 206 and the slice", r.status_code == 206 and r.content == body[10:20], r.status_code)
        sig = url.index("X-Amz-Signature=") + len("X-Amz-Signature=")
        bad = url[:sig] + ("0" if url[sig] != "0" else "1") + url[sig + 1:]
        check("F1 tampered signature gives 403", requests.get(bad, timeout=20).status_code == 403)
        check("F1 URL for another host is rejected", requests.get(url.replace(PUBLIC, ENDPOINT), timeout=20).status_code == 403)
    finally:
        root.delete_object(Bucket=BUCKET, Key=key)


def mpu(ingest, key, session):
    up = ingest.create_multipart_upload(Bucket=BUCKET, Key=key, Metadata={"rdv-resume-session": session})
    parts = []
    for n, size in ((1, 5 * 1024 * 1024), (2, 1024)):
        etag = ingest.upload_part(Bucket=BUCKET, Key=key, UploadId=up["UploadId"], PartNumber=n, Body=b"x" * size)["ETag"]
        parts.append({"PartNumber": n, "ETag": etag})
    return up["UploadId"], parts


def f2_guarded_delete(ingest):
    key = f"raw/mcap/contract-probe-{RUN}/u.mcap"
    session = str(uuid.uuid4())
    uid, parts = mpu(ingest, key, session)
    ingest.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=uid, MultipartUpload={"Parts": parts}, IfNoneMatch="*")
    head = ingest.head_object(Bucket=BUCKET, Key=key)
    etag = head["ETag"]
    check("F2 object carries the session metadata and a multipart ETag", head["Metadata"].get("rdv-resume-session") == session and "-" in etag.strip('"'), etag)
    uid2, parts2 = mpu(ingest, key, session)
    c = code(lambda: ingest.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=uid2, MultipartUpload={"Parts": parts2}, IfNoneMatch="*"))
    check("F2 Complete If-None-Match * on an existing key gives 412", c == "412", c)
    code(lambda: ingest.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=uid2))
    c = code(lambda: ingest.delete_object(Bucket=BUCKET, Key=key, IfMatch='"deadbeef-2"'))
    still = code(lambda: ingest.head_object(Bucket=BUCKET, Key=key))
    check("F2 delete with a wrong ETag gives 412 and keeps the object", c == "412" and still == "ok", f"delete={c} head={still}")
    c = code(lambda: ingest.delete_object(Bucket=BUCKET, Key=key, IfMatch=etag))
    gone = code(lambda: ingest.head_object(Bucket=BUCKET, Key=key))
    check("F2 delete with the HEAD ETag succeeds and removes the object", c == "ok" and gone == "404", f"delete={c} head={gone}")


def f3_gc(root, ingest):
    sys.path.insert(0, str(REPO / "tools"))
    os.environ.update(S3_ENDPOINT=ENDPOINT, AWS_ACCESS_KEY_ID=os.environ["GC_S3_ACCESS_KEY"], AWS_SECRET_ACCESS_KEY=os.environ["GC_S3_SECRET_KEY"])
    import s3_gc
    gc = s3_gc._make_s3_client()
    key = f"processed/parquet/contract-probe-{RUN}/orphan.parquet"
    root.put_object(Bucket=BUCKET, Key=key, Body=b"o")
    listed = [o["Key"] for o in s3_gc._list_keys(gc, BUCKET, f"processed/parquet/contract-probe-{RUN}/")]
    check("F3 GC client lists objects", listed == [key], listed)
    mkey = f"raw/mcap/contract-probe-gc-{RUN}/u.mcap"
    uid = ingest.create_multipart_upload(Bucket=BUCKET, Key=mkey)["UploadId"]
    time.sleep(1.2)
    dry, cands = s3_gc.reclaim_incomplete_mpus(gc, BUCKET, 1, True, prefix=mkey)
    check("F3 GC dry run lists the old upload and aborts nothing", dry == 0 and [c["UploadId"] for c in cands] == [uid], cands)
    n, _ = s3_gc.reclaim_incomplete_mpus(gc, BUCKET, 1, False, prefix=mkey)
    left = [u for u in gc.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) if u["UploadId"] == uid]
    check("F3 GC aborts the old upload", n == 1 and not left, f"aborted={n} left={len(left)}")
    gc.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": key}], "Quiet": False})
    check("F3 GC deletes the object", code(lambda: root.head_object(Bucket=BUCKET, Key=key)) == "404")


def main(out):
    root, ingest = plain_client("ROOT"), plain_client("INGEST")
    f1_presign(root)
    f2_guarded_delete(ingest)
    f3_gc(root, ingest)
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "contract-probe.json").write_text(json.dumps(results, indent=1))
    bad = [r["check"] for r in results if not r["pass"]]
    print(f"{len(results)} checks, {len(bad)} failed", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/out"))
