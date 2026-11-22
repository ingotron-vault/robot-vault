"""Object-store conformance probes: the S3 calls Robot Vault makes, against one store.

Same client libraries as the services
(boto3 1.43.64, pyarrow 25.0.0, DuckDB 1.5.5 httpfs), the real `tools/s3_gc.py`, and rclone
for backup. Rules:

- The suite exits 1 when any required probe fails or is missing (a skipped probe counts as
  missing), and 2 when a required variable is missing. It writes $RESULTS (JSON) in every case
  where it ran.
- Data-path probes use the service key that owns the path (`<SVC>_S3_ACCESS_KEY`). The root key
  only seeds fixtures and does backup and restore. There is no fallback to the root key.
- Negative probes assert the exact status (412 for a failed precondition, 403 for a denied
  call) and that the object's bytes, ETag, content type and metadata did not change.

The 290-probe policy matrix is `iam_probe.py`; the verdict over all artifacts is `gate.py`.

Usage: conformance.py [<out dir>]  (writes <out dir>/conformance.json, else $RESULTS)
Env: see suite_env.py, plus S3_PUBLIC_ENDPOINT (another host name for the same store),
HEALTH_URL, S3_REGION, RESTORE_BUCKET, RCLONE_PROVIDER.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

from gate import REQUIRED_FEATURES
from suite_env import SERVICES, require_env, service_creds

require_env(("S3_PUBLIC_ENDPOINT", "HEALTH_URL"))

REPO = os.environ.get("REPO", "/repo")
ENDPOINT = os.environ["S3_ENDPOINT"]
PUBLIC = os.environ["S3_PUBLIC_ENDPOINT"]
BUCKET = os.environ["S3_BUCKET"]
REGION = os.environ.get("S3_REGION", "us-east-1")
STORE = os.environ.get("STORE", "rustfs")
MIB = 1024 * 1024
P = "conformance"  # every probe key lives under <prefix>/conformance/
RESULTS: list[dict] = []


def client(service="root", endpoint=None, checksum="when_supported"):
    a, s = (os.environ["ROOT_ACCESS_KEY"], os.environ["ROOT_SECRET_KEY"]) if service == "root" else service_creds(service)
    return boto3.client(
        "s3", endpoint_url=endpoint or ENDPOINT, aws_access_key_id=a, aws_secret_access_key=s, region_name=REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      request_checksum_calculation=checksum, response_checksum_validation=checksum,
                      retries={"max_attempts": 1}))


ROOT = client()  # fixtures only


def feature(name):
    assert name in REQUIRED_FEATURES, f"probe {name!r} is not in gate.REQUIRED_FEATURES"

    def wrap(fn):
        def run():
            t = time.time()
            try:
                detail, status = fn(), "pass"
            except Exception as e:  # noqa: BLE001 - every failure is a recorded failed probe
                status, detail = "fail", f"{type(e).__name__}: {e}\n" + traceback.format_exc(limit=3)
            RESULTS.append({"feature": name, "status": status, "detail": str(detail),
                            "seconds": round(time.time() - t, 2)})
            print(f"[{STORE}] {status.upper():4} {name}: {str(detail)[:300]}", flush=True)
        return run
    return wrap


def status_of(e: ClientError) -> int:
    return int((e.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def expect_status(fn, want: int, what: str) -> str:
    """Run fn; it must fail with exactly HTTP status `want`."""
    try:
        fn()
    except ClientError as e:
        got = status_of(e)
        assert got == want, f"{what}: expected {want}, got {got} ({e.response['Error'].get('Code')})"
        return f"{what} -> {got}"
    raise AssertionError(f"{what}: succeeded, expected {want}")


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def snapshot(key: str) -> dict:
    """Everything about an object that a denied or failed write must leave alone (read as root)."""
    h = ROOT.head_object(Bucket=BUCKET, Key=key)
    body = ROOT.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return {"sha256": hashlib.sha256(body).hexdigest(), "size": h["ContentLength"], "etag": h["ETag"],
            "content_type": h["ContentType"], "metadata": h["Metadata"], "last_modified": h["LastModified"].isoformat()}


@feature("bucket reachable, path style, region (ingest key)")
def t_bucket():
    c = client("ingest")
    c.head_bucket(Bucket=BUCKET)
    loc = c.get_bucket_location(Bucket=BUCKET).get("LocationConstraint")
    return f"location={loc!r}"


@feature("PutObject with the SDK default CRC32 checksum; HeadObject ETag is the MD5 (ingest key)")
def t_put_default_checksum():
    c, key, body = client("ingest"), f"raw/{P}/put/default.bin", os.urandom(300_000)
    c.put_object(Bucket=BUCKET, Key=key, Body=body)
    h = c.head_object(Bucket=BUCKET, Key=key, ChecksumMode="ENABLED")
    assert h["ContentLength"] == len(body), h["ContentLength"]
    assert h["ETag"].strip('"') == md5(body), (h["ETag"], md5(body))
    assert c.get_object(Bucket=BUCKET, Key=key)["Body"].read() == body
    return "etag ok"


@feature("Content-MD5 is validated: a bad MD5 gives 400 and stores nothing (ingest key)")
def t_content_md5():
    c, body = client("ingest", checksum="when_required"), b"x" * 1000
    good = base64.b64encode(hashlib.md5(body).digest()).decode()
    c.put_object(Bucket=BUCKET, Key=f"raw/{P}/put/md5.bin", Body=body, ContentMD5=good)
    bad = base64.b64encode(hashlib.md5(b"other").digest()).decode()
    msg = expect_status(lambda: c.put_object(Bucket=BUCKET, Key=f"raw/{P}/put/md5-bad.bin", Body=body, ContentMD5=bad),
                        400, "bad Content-MD5")
    expect_status(lambda: c.head_object(Bucket=BUCKET, Key=f"raw/{P}/put/md5-bad.bin"), 404, "rejected object absent")
    return msg


@feature("x-amz-checksum-sha256 is validated: a bad checksum gives 400 and stores nothing (ingest key)")
def t_checksum_sha256():
    body, c = b"y" * 2000, client("ingest", checksum="when_required")
    c.put_object(Bucket=BUCKET, Key=f"raw/{P}/put/sha.bin", Body=body, ChecksumAlgorithm="SHA256")
    bad = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
    msg = expect_status(lambda: c.put_object(Bucket=BUCKET, Key=f"raw/{P}/put/sha-bad.bin", Body=body, ChecksumSHA256=bad),
                        400, "bad x-amz-checksum-sha256")
    expect_status(lambda: c.head_object(Bucket=BUCKET, Key=f"raw/{P}/put/sha-bad.bin"), 404, "rejected object absent")
    return msg


@feature("multipart resume after a client restart (ListParts), complete, ETag -N, ranged GET 206 (ingest key)")
def t_multipart_resume():
    key = f"raw/{P}/mpu/resume.bin"
    parts_data = [os.urandom(5 * MIB), os.urandom(5 * MIB), os.urandom(1 * MIB)]
    a = client("ingest")
    up = a.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]
    for n in (1, 2):
        a.upload_part(Bucket=BUCKET, Key=key, UploadId=up, PartNumber=n, Body=parts_data[n - 1])
    b = client("ingest")  # a restarted process knows only the key and the UploadId
    listed = b.list_parts(Bucket=BUCKET, Key=key, UploadId=up)["Parts"]
    assert [p["PartNumber"] for p in listed] == [1, 2], listed
    assert [p["Size"] for p in listed] == [5 * MIB, 5 * MIB], listed
    for p, d in zip(listed, parts_data):
        assert p["ETag"].strip('"') == md5(d), (p["ETag"], md5(d))
    b.upload_part(Bucket=BUCKET, Key=key, UploadId=up, PartNumber=3, Body=parts_data[2])
    listed = b.list_parts(Bucket=BUCKET, Key=key, UploadId=up)["Parts"]
    b.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=up, MultipartUpload={
        "Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in listed]})
    full = b"".join(parts_data)
    h = b.head_object(Bucket=BUCKET, Key=key)
    assert h["ContentLength"] == len(full)
    want = md5(b"".join(hashlib.md5(d).digest() for d in parts_data)) + "-3"
    assert h["ETag"].strip('"') == want, (h["ETag"], want)
    r = b.get_object(Bucket=BUCKET, Key=key, Range="bytes=5242870-5242889")
    assert r["Body"].read() == full[5242870:5242890]
    assert r["ResponseMetadata"]["HTTPStatusCode"] == 206
    return f"etag {want}"


@feature("ingest contract: MPU metadata kept, conditional writes and deletes give exactly 412 and change nothing, HEAD 404")
def t_conditional():
    c, key, data = client("ingest"), f"raw/{P}/cond/obj.bin", os.urandom(5 * MIB)

    def mpu_complete(k, **extra):
        up = c.create_multipart_upload(Bucket=BUCKET, Key=k, Metadata={"rdv-resume-session": "s-123"})["UploadId"]
        e = c.upload_part(Bucket=BUCKET, Key=k, UploadId=up, PartNumber=1, Body=data)["ETag"]
        assert e.strip('"'), "empty part ETag"
        try:
            return c.complete_multipart_upload(Bucket=BUCKET, Key=k, UploadId=up, IfNoneMatch="*", **extra,
                                               MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": e}]}), up
        except ClientError:
            c.abort_multipart_upload(Bucket=BUCKET, Key=k, UploadId=up)
            raise

    notes = []
    mpu_complete(key)
    h = c.head_object(Bucket=BUCKET, Key=key)
    assert h["Metadata"].get("rdv-resume-session") == "s-123", h["Metadata"]
    base = snapshot(key)
    notes.append(expect_status(lambda: mpu_complete(key), 412, "Complete If-None-Match * on an existing key"))
    notes.append(expect_status(lambda: c.put_object(Bucket=BUCKET, Key=key, Body=b"replace", IfNoneMatch="*"), 412,
                               "PutObject If-None-Match * on an existing key"))
    notes.append(expect_status(lambda: c.delete_object(Bucket=BUCKET, Key=key, IfMatch='"0123456789abcdef0123456789abcdef"'),
                               412, "DeleteObject If-Match wrong ETag"))
    notes.append(expect_status(lambda: c.get_object(Bucket=BUCKET, Key=key, IfMatch='"deadbeef"'), 412,
                               "GetObject If-Match wrong ETag"))
    assert snapshot(key) == base, "a failed precondition changed the object"
    c.get_object(Bucket=BUCKET, Key=key, IfMatch=h["ETag"])["Body"].read()
    c.delete_object(Bucket=BUCKET, Key=key, IfMatch=h["ETag"])
    notes.append(expect_status(lambda: c.head_object(Bucket=BUCKET, Key=key), 404, "HEAD after the guarded delete"))
    return "; ".join(notes)


@feature("ListParts paginates beyond MaxParts (ingest key)")
def t_listparts_paging():
    c, key = client("ingest"), f"raw/{P}/mpu/paging.bin"
    up = c.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]
    for n in range(1, 5):
        c.upload_part(Bucket=BUCKET, Key=key, UploadId=up, PartNumber=n, Body=os.urandom(5 * MIB))
    seen, marker = [], 0
    while True:
        r = c.list_parts(Bucket=BUCKET, Key=key, UploadId=up, MaxParts=2, PartNumberMarker=marker)
        seen += [p["PartNumber"] for p in r.get("Parts", [])]
        if not r.get("IsTruncated"):
            break
        marker = r["NextPartNumberMarker"]
    c.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=up)
    assert seen == [1, 2, 3, 4], seen
    return "pages of 2 ok"


@feature("ListMultipartUploads (gc key) shows Initiated; abort (ingest key) removes it; ListParts then gives 404")
def t_list_mpu_abort():
    ingest, gc, key = client("ingest"), client("gc"), f"raw/{P}/mpu/orphan.bin"
    up = ingest.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]
    ingest.upload_part(Bucket=BUCKET, Key=key, UploadId=up, PartNumber=1, Body=b"z" * MIB)
    mine = [u for u in gc.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) if u["UploadId"] == up]
    assert mine and mine[0]["Key"] == key, mine
    skew = abs((dt.datetime.now(dt.timezone.utc) - mine[0]["Initiated"]).total_seconds())
    assert skew < 120, f"Initiated skew {skew}s"
    ingest.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=up)
    assert not [u for u in gc.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []) if u["UploadId"] == up]
    msg = expect_status(lambda: ingest.list_parts(Bucket=BUCKET, Key=key, UploadId=up), 404, "ListParts after abort")
    return f"Initiated skew {skew:.1f}s; {msg}"


@feature("ListObjectsV2 prefix, delimiter, pagination, odd keys, LastModified (processor writes, query lists)")
def t_list_v2():
    w, q, base = client("processor"), client("query"), f"processed/parquet/{P}/list/"
    keys = [f"{base}k{i:03d}.bin" for i in range(25)] + [f"{base}sub/a b+c%25d.bin", f"{base}sub/ünï.bin"]
    for k in keys:
        w.put_object(Bucket=BUCKET, Key=k, Body=b"1")
    pag = q.get_paginator("list_objects_v2")
    got = [o for p in pag.paginate(Bucket=BUCKET, Prefix=base, PaginationConfig={"PageSize": 10}) for o in p.get("Contents", [])]
    assert sorted(o["Key"] for o in got) == sorted(keys), sorted(o["Key"] for o in got)
    now = dt.datetime.now(dt.timezone.utc)
    worst = max(abs((now - o["LastModified"]).total_seconds()) for o in got)
    assert worst < 120, f"LastModified skew {worst}s"
    d = q.list_objects_v2(Bucket=BUCKET, Prefix=base, Delimiter="/")
    assert [c["Prefix"] for c in d.get("CommonPrefixes", [])] == [f"{base}sub/"], d.get("CommonPrefixes")
    assert len(d.get("Contents", [])) == 25
    return f"27 keys over pages of 10; LastModified skew <= {worst:.1f}s"


@feature("DeleteObjects with Quiet=False reports every key as Deleted (gc key)")
def t_delete_objects():
    gc, keys = client("gc"), [f"manifests/{P}/delobjs/k{i}.json" for i in range(3)]
    for k in keys:
        ROOT.put_object(Bucket=BUCKET, Key=k, Body=b"{}")
    r = gc.delete_objects(Bucket=BUCKET, Delete={"Quiet": False, "Objects": [{"Key": k} for k in keys]})
    assert sorted(d["Key"] for d in r.get("Deleted", [])) == sorted(keys) and not r.get("Errors"), r
    for k in keys:
        expect_status(lambda k=k: ROOT.head_object(Bucket=BUCKET, Key=k), 404, "deleted key absent")
    return "3 keys deleted and reported"


@feature("presigned GET (query key, public host): SigV4, Range 206, tampered 403, expired 403")
def t_presign():
    body, key = os.urandom(100_000), f"processed/media/{P}/presign.bin"
    ROOT.put_object(Bucket=BUCKET, Key=key, Body=body)
    q = client("query", endpoint=PUBLIC)
    url = q.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=600)
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url, "presigned URL is not SigV4"
    r = requests.get(url, headers={"Range": "bytes=100-199"}, timeout=10)
    assert r.status_code == 206 and r.content == body[100:200], (r.status_code, r.text[:200])
    sig = url.index("X-Amz-Signature=") + len("X-Amz-Signature=")
    bad = url[:sig] + ("0" if url[sig] != "0" else "1") + url[sig + 1:]
    t = requests.get(bad, timeout=10).status_code
    assert t == 403, f"tampered signature -> {t}"
    short = q.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=1)
    time.sleep(2)
    e = requests.get(short, timeout=10).status_code
    assert e == 403, f"expired URL -> {e}"
    return f"SigV4 URL for {urllib.parse.urlsplit(PUBLIC).netloc}: range 206, tampered {t}, expired {e}"


@feature("pyarrow S3FileSystem writes and reads a multipart-sized Parquet file (processor key)")
def t_pyarrow():
    import pyarrow as pa
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq
    a, s = service_creds("processor")
    u = urllib.parse.urlsplit(ENDPOINT)
    fs = pafs.S3FileSystem(access_key=a, secret_key=s, endpoint_override=u.netloc, scheme=u.scheme,
                           region=REGION, force_virtual_addressing=False)
    n = 2_000_000
    t = pa.table({"i": pa.array(range(n), pa.int64()), "r": pa.array([float(i) * 1.5 for i in range(n)])})
    path = f"{BUCKET}/processed/parquet/{P}/robot=a/date=2026-11-21/part-0.parquet"
    pq.write_table(t, path, filesystem=fs, compression="none")
    info = fs.get_file_info(path)
    assert pq.read_table(path, filesystem=fs).num_rows == n
    pq.write_table(t.slice(0, 1000), f"{BUCKET}/processed/parquet/{P}/robot=b/date=2026-11-21/part-0.parquet", filesystem=fs)
    return f"{info.size // MIB} MiB parquet"


@feature("DuckDB httpfs secret and read_parquet glob (query key)")
def t_duckdb():
    import duckdb
    a, s = service_creds("query")
    con = duckdb.connect()
    con.execute("LOAD httpfs;")
    u = urllib.parse.urlsplit(ENDPOINT)
    con.execute(f"""CREATE OR REPLACE SECRET s3_lake (TYPE S3, KEY_ID '{a}', SECRET '{s}', REGION '{REGION}',
                    ENDPOINT '{u.netloc}', URL_STYLE 'path', USE_SSL false);""")
    n = con.execute(f"SELECT count(*) FROM read_parquet('s3://{BUCKET}/processed/parquet/{P}/**/*.parquet', "
                    "hive_partitioning=true)").fetchone()[0]
    assert n == 2_001_000, n
    m = con.execute(f"SELECT max(i) FROM read_parquet('s3://{BUCKET}/processed/parquet/{P}/robot=a/*/*.parquet')"
                    " WHERE i > 1999990").fetchone()[0]
    assert m == 1_999_999
    return f"rows={n}"


@feature("denied writes and deletes give exactly 403 and leave bytes, ETag and metadata unchanged")
def t_denied_unchanged():
    targets = {"raw": f"raw/{P}/denied/obj.bin", "processed": f"processed/parquet/{P}/denied/obj.bin",
               "exports": f"exports/{P}/denied/obj.bin", "manifests": f"manifests/{P}/denied/obj.bin"}
    for k in targets.values():
        ROOT.put_object(Bucket=BUCKET, Key=k, Body=os.urandom(4096), ContentType="application/x-denied",
                        Metadata={"owner": "fixture"})
    before = {n: snapshot(k) for n, k in targets.items()}
    listing = lambda: sorted(o["Key"] for o in ROOT.list_objects_v2(Bucket=BUCKET, Prefix="").get("Contents", []))  # noqa: E731
    keys_before = listing()
    # (service, target, operation): each one is denied by the reviewed policies.
    cases = [
        ("exporter", "raw", "put"), ("exporter", "processed", "put"), ("exporter", "raw", "delete"),
        ("query", "processed", "put"), ("query", "exports", "put"), ("query", "processed", "delete"),
        ("query", "raw", "mpu"), ("processor", "raw", "put"), ("processor", "raw", "delete"),
        ("processor", "exports", "put"), ("ingest", "processed", "put"), ("ingest", "processed", "delete"),
        ("ingest", "exports", "put"), ("gc", "raw", "put"), ("gc", "processed", "put"), ("gc", "exports", "put"),
    ]
    for svc, target, op in cases:
        c, key = client(svc), targets[target]
        call = {"put": lambda: c.put_object(Bucket=BUCKET, Key=key, Body=b"overwrite", Metadata={"owner": "attacker"}),
                "delete": lambda: c.delete_object(Bucket=BUCKET, Key=key),
                "mpu": lambda: c.create_multipart_upload(Bucket=BUCKET, Key=key)}[op]
        expect_status(call, 403, f"{svc} {op} {target}")
    # Bucket-level calls a service must not make.
    expect_status(lambda: client("ingest").list_multipart_uploads(Bucket=BUCKET), 403, "ingest ListMultipartUploads")
    expect_status(lambda: client("query").list_objects_v2(Bucket=BUCKET, Prefix="raw/"), 403, "query List raw/")
    after = {n: snapshot(k) for n, k in targets.items()}
    assert after == before, {n: (before[n], after[n]) for n in before if before[n] != after[n]}
    assert listing() == keys_before, "a denied call created or removed an object"
    return f"{len(cases) + 2} denied calls, 4 objects unchanged, key list unchanged"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@feature("GC (real tools/s3_gc.py, gc key): dry run deletes nothing, a real run deletes only unreachable objects and orphan uploads")
def t_gc():
    sys.path.insert(0, f"{REPO}/tools")
    gc = _load(f"{REPO}/tools/s3_gc.py", "s3_gc_conformance")
    c, pfx = client("gc"), f"manifests/{P}/gc/"
    keep = [f"{pfx}keep-{i}.json" for i in range(5)] + [f"{pfx}keep space+plus%25 ü.json"]
    drop = [f"{pfx}drop-{i}.json" for i in range(7)] + [f"{pfx}drop space+plus%25 ü.json"]
    for k in keep + drop:
        ROOT.put_object(Bucket=BUCKET, Key=k, Body=b"{}")
    orphan = ROOT.create_multipart_upload(Bucket=BUCKET, Key=f"{pfx}orphan.bin")["UploadId"]
    live = ROOT.create_multipart_upload(Bucket=BUCKET, Key=f"{pfx}live.bin")["UploadId"]
    snap = gc.ReachabilitySnapshot(keep, covered_prefixes=[pfx], protected_mpus={(f"{pfx}live.bin", live)},
                                   taken_at=time.time() + 5)
    cfg = gc.GCConfig(bucket=BUCKET, prefixes=[pfx], min_age_seconds=0, delete_limit=1000, dry_run=True,
                      mpu_max_age_seconds=0)
    gc.run_gc(cfg, snap, s3=c)
    listed = {o["Key"] for o in ROOT.list_objects_v2(Bucket=BUCKET, Prefix=pfx).get("Contents", [])}
    assert listed == set(keep + drop), "dry run changed objects"
    cfg.dry_run = False
    scanned, deleted = gc.run_gc(cfg, snap, s3=c)
    listed = {o["Key"] for o in ROOT.list_objects_v2(Bucket=BUCKET, Prefix=pfx).get("Contents", [])}
    assert listed == set(keep), f"after GC: missing={set(keep) - listed} extra={listed - set(keep)}"
    assert deleted == len(drop), deleted
    ups = {u["UploadId"] for u in ROOT.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])}
    assert orphan not in ups and live in ups, ("mpu", orphan in ups, live in ups)
    ROOT.put_object(Bucket=BUCKET, Key=f"{pfx}young.json", Body=b"{}")
    cfg.min_age_seconds = 3600  # the young-object guard keeps a fresh unreachable object
    gc.run_gc(cfg, gc.ReachabilitySnapshot(keep, covered_prefixes=[pfx], taken_at=time.time(),
                                           protected_mpus={(f"{pfx}live.bin", live)}), s3=c)
    ROOT.head_object(Bucket=BUCKET, Key=f"{pfx}young.json")
    ups = {u["UploadId"] for u in ROOT.list_multipart_uploads(Bucket=BUCKET).get("Uploads", [])}
    assert live in ups, "protected upload aborted by the young-object run"
    ROOT.abort_multipart_upload(Bucket=BUCKET, Key=f"{pfx}live.bin", UploadId=live)
    return f"scanned={scanned} deleted={deleted}; orphan upload aborted, protected upload kept, young object kept"


@feature("backup (root key): rclone sync bucket to a folder and back to an empty bucket, rclone check --download, metadata kept")
def t_backup():
    env = dict(os.environ, RCLONE_CONFIG_SRC_TYPE="s3", RCLONE_CONFIG_SRC_PROVIDER=os.environ.get("RCLONE_PROVIDER", "Other"),
               RCLONE_CONFIG_SRC_ACCESS_KEY_ID=os.environ["ROOT_ACCESS_KEY"],
               RCLONE_CONFIG_SRC_SECRET_ACCESS_KEY=os.environ["ROOT_SECRET_KEY"],
               RCLONE_CONFIG_SRC_ENDPOINT=ENDPOINT, RCLONE_CONFIG_SRC_REGION=REGION, RCLONE_CONFIG_SRC_FORCE_PATH_STYLE="true")
    ROOT.put_object(Bucket=BUCKET, Key=f"exports/{P}/meta.json", Body=b"{}", ContentType="application/json",
                    Metadata={"robot": "a"})
    restore = os.environ.get("RESTORE_BUCKET", BUCKET + "-restore")
    with tempfile.TemporaryDirectory() as d:
        def rc(*args):
            p = subprocess.run(["rclone", *args, "-q"], env=env, capture_output=True, text=True, timeout=900)
            assert p.returncode == 0, f"rclone {args}: {p.stderr[-800:]}"
        rc("sync", f"src:{BUCKET}", f"{d}/objects", "--metadata", "--checksum")
        rc("sync", f"{d}/objects", f"src:{restore}", "--metadata", "--checksum")
        rc("check", f"src:{BUCKET}", f"src:{restore}", "--download")
    pag = lambda b: {o["Key"]: o for p in ROOT.get_paginator("list_objects_v2").paginate(Bucket=b) for o in p.get("Contents", [])}  # noqa: E731
    src, dst = pag(BUCKET), pag(restore)
    assert src.keys() == dst.keys(), set(src) ^ set(dst)
    h = ROOT.head_object(Bucket=restore, Key=f"exports/{P}/meta.json")
    assert h["ContentType"] == "application/json" and h["Metadata"].get("robot") == "a", h
    mpu = f"raw/{P}/mpu/resume.bin"
    assert md5(ROOT.get_object(Bucket=BUCKET, Key=mpu)["Body"].read()) == md5(ROOT.get_object(Bucket=restore, Key=mpu)["Body"].read())
    return f"{len(src)} objects round-tripped; content type and user metadata kept"


@feature("health endpoint answers 200")
def t_health():
    r = requests.get(os.environ["HEALTH_URL"], timeout=5)
    assert r.status_code == 200, r.status_code
    return f"{os.environ['HEALTH_URL']} -> 200"


PROBES = (t_health, t_bucket, t_put_default_checksum, t_content_md5, t_checksum_sha256, t_multipart_resume,
          t_conditional, t_listparts_paging, t_list_mpu_abort, t_list_v2, t_delete_objects, t_presign, t_pyarrow,
          t_duckdb, t_denied_unchanged, t_gc, t_backup)


def main() -> int:
    for t in PROBES:
        t()
    out = f"{sys.argv[1]}/conformance.json" if len(sys.argv) > 1 else os.environ.get("RESULTS", f"/out/{STORE}.json")
    with open(out, "w") as f:
        json.dump({"store": STORE, "services": list(SERVICES), "results": RESULTS}, f, indent=1)
    failed = [r["feature"] for r in RESULTS if r["status"] != "pass"]
    missing = sorted(set(REQUIRED_FEATURES) - {r["feature"] for r in RESULTS})
    print(f"[{STORE}] {len(RESULTS) - len(failed)}/{len(REQUIRED_FEATURES)} required probes pass -> {out}")
    if failed or missing:
        print(f"[{STORE}] FAILED: {failed}; MISSING: {missing}", file=sys.stderr)
    return 1 if failed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
