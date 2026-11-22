"""Durability crash test for the object store.

`write`: make 200 acknowledged writes, record each one only after the store acknowledged it,
then keep sending unacknowledged writes until the store is killed. Kinds of acknowledged write:
  put        single PutObject (small inline objects and larger ones), content type + user metadata
  mpu        completed multipart upload (5 MiB part + small tail), metadata from CreateMultipartUpload
  part       UploadPart of an upload that stays open (in-progress MPU)
After the 200th acknowledgement it writes <out>/crash-ready; the driver then runs
`docker compose kill -s KILL objectstore` while more writes are in flight.

`verify`: after the restart, every acknowledged write must be there with the same bytes
(SHA-256), size, ETag, content type and user metadata; every acknowledged part must be listed
with its ETag and size, and the open uploads must still complete to the exact bytes. An
unacknowledged write may be missing, but if it is present its bytes must be complete.
Writes <out>/crash-verify.json and exits 1 on any loss or mismatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from suite_env import require_env, service_creds

require_env()
ENDPOINT = os.environ["S3_ENDPOINT"]
BUCKET = os.environ["S3_BUCKET"]
REGION = os.environ.get("S3_REGION", "us-east-1")
MIB = 1024 * 1024
CT = "application/x-rdv-crash"


def client():
    return boto3.client(
        "s3", endpoint_url=ENDPOINT, region_name=REGION,
        aws_access_key_id=service_creds("ingest")[0], aws_secret_access_key=service_creds("ingest")[1],
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1},
                      connect_timeout=5, read_timeout=30, max_pool_connections=16),
    )


def body(seed: str, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def plan(run: str):
    """200 acknowledged items: 110 puts (55 inline-sized, 55 larger), 60 completed MPUs, 30 open parts."""
    items = []
    for i in range(110):
        size = random.Random(f"{run}-sz-{i}").randint(1, 60_000) if i % 2 else random.Random(f"{run}-sz-{i}").randint(300_000, 2 * MIB)
        items.append({"kind": "put", "key": f"raw/crash/{run}/put-{i:03d}.bin", "size": size, "meta": {"rdv-resume-session": f"{run}-p{i}"}})
    for i in range(60):
        items.append({"kind": "mpu", "key": f"raw/crash/{run}/mpu-{i:03d}.bin", "sizes": [5 * MIB, 1000 + i],
                      "meta": {"rdv-resume-session": f"{run}-m{i}"}})
    for u in range(10):
        for n in (1, 2, 3):
            items.append({"kind": "part", "key": f"raw/crash/{run}/open-{u:02d}.bin", "upload": u, "part": n,
                          "size": 5 * MIB if n < 3 else 2000 + u, "meta": {"rdv-resume-session": f"{run}-o{u}"}})
    return items


class Ledger:
    """Acknowledgements are appended and fsynced before they count."""

    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()
        self.count = 0

    def add(self, rec):
        with self.lock:
            self.f.write(json.dumps(rec) + "\n")
            self.f.flush()
            os.fsync(self.f.fileno())
            if rec.get("acked"):
                self.count += 1
            return self.count


def write(run: str, out: str) -> int:
    c = client()
    bucket = BUCKET  # the bootstrapped bucket; every key is under raw/crash/<run>/, the ingest key's own prefix
    ledger = Ledger(f"{out}/crash-{run}.ledger.jsonl")
    items = plan(run)
    # Open uploads are created first (creation itself is not one of the 200 counted acks).
    uploads = {}
    for it in items:
        if it["kind"] == "part" and it["upload"] not in uploads:
            uploads[it["upload"]] = c.create_multipart_upload(Bucket=bucket, Key=it["key"], ContentType=CT, Metadata=it["meta"])["UploadId"]
            ledger.add({"kind": "open", "key": it["key"], "upload_id": uploads[it["upload"]], "meta": it["meta"]})

    def do(it):
        cl = client()
        if it["kind"] == "put":
            b = body(it["key"], it["size"])
            r = cl.put_object(Bucket=bucket, Key=it["key"], Body=b, ContentType=CT, Metadata=it["meta"])
            rec = {**it, "sha256": sha(b), "etag": r["ETag"]}
        elif it["kind"] == "mpu":
            up = cl.create_multipart_upload(Bucket=bucket, Key=it["key"], ContentType=CT, Metadata=it["meta"])["UploadId"]
            parts, whole = [], b""
            for n, size in enumerate(it["sizes"], 1):
                b = body(f"{it['key']}-{n}", size)
                parts.append({"PartNumber": n, "ETag": cl.upload_part(Bucket=bucket, Key=it["key"], UploadId=up, PartNumber=n, Body=b)["ETag"]})
                whole += b
            r = cl.complete_multipart_upload(Bucket=bucket, Key=it["key"], UploadId=up, MultipartUpload={"Parts": parts})
            rec = {**it, "size": len(whole), "sha256": sha(whole), "etag": r["ETag"]}
        else:
            b = body(f"{it['key']}-{it['part']}", it["size"])
            r = cl.upload_part(Bucket=bucket, Key=it["key"], UploadId=uploads[it["upload"]], PartNumber=it["part"], Body=b)
            rec = {**it, "upload_id": uploads[it["upload"]], "sha256": sha(b), "etag": r["ETag"]}
        ledger.add({**rec, "acked": True})

    with ThreadPoolExecutor(8) as pool:
        for f in [pool.submit(do, it) for it in items]:
            f.result()
    assert ledger.count == len(items), ledger.count
    # Unacknowledged traffic: each attempt is recorded *before* it is sent.
    stop = time.time() + 120
    noise = [0]

    def churn(t):
        cl = client()
        while time.time() < stop:
            i = noise[0] = noise[0] + 1
            key = f"raw/crash/{run}/noise-{t}-{i}.bin"
            b = body(key, 1_500_000 if i % 2 else 40_000)
            ledger.add({"kind": "noise", "key": key, "size": len(b), "sha256": sha(b)})
            try:
                cl.put_object(Bucket=bucket, Key=key, Body=b, ContentType=CT)
            except (BotoCoreError, ClientError, OSError):
                return

    threads = [threading.Thread(target=churn, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    with open(f"{out}/crash-ready", "w") as f:  # the driver kills the store now
        f.write(run)
    for t in threads:
        t.join()
    print(f"write {run}: {ledger.count} acknowledged, {noise[0]} unacknowledged attempts until the store died")
    return 0


def verify(run: str, out: str) -> int:
    c = client()
    bucket = BUCKET
    recs = [json.loads(line) for line in open(f"{out}/crash-{run}.ledger.jsonl", encoding="utf-8")]
    acked = [r for r in recs if r.get("acked")]
    opens = {r["key"]: r for r in recs if r["kind"] == "open"}
    problems, checked = [], {"put": 0, "mpu": 0, "part": 0, "open_completed": 0, "noise_present": 0, "noise_missing": 0}
    for r in acked:
        try:
            if r["kind"] in ("put", "mpu"):
                h = c.head_object(Bucket=bucket, Key=r["key"])
                got = c.get_object(Bucket=bucket, Key=r["key"])["Body"].read()
                diffs = [n for n, ok in (("size", h["ContentLength"] == r["size"]), ("etag", h["ETag"] == r["etag"]),
                                         ("content-type", h["ContentType"] == CT), ("metadata", h["Metadata"] == r["meta"]),
                                         ("bytes", sha(got) == r["sha256"])) if not ok]
                if diffs:
                    problems.append(f"{r['key']}: {diffs}")
                checked[r["kind"]] += 1
        except ClientError as e:
            problems.append(f"{r['key']}: {e.response['Error'].get('Code')}")
    # Open uploads: every acknowledged part listed with the same ETag and size, then complete them.
    for key, o in opens.items():
        want = sorted((r for r in acked if r["kind"] == "part" and r["key"] == key), key=lambda r: r["part"])
        try:
            listed = c.list_parts(Bucket=bucket, Key=key, UploadId=o["upload_id"])["Parts"]
        except ClientError as e:
            problems.append(f"{key}: open upload lost ({e.response['Error'].get('Code')})")
            continue
        have = {p["PartNumber"]: p for p in listed}
        for r in want:
            p = have.get(r["part"])
            if not p or p["ETag"] != r["etag"] or p["Size"] != r["size"]:
                problems.append(f"{key} part {r['part']}: {'missing' if not p else (p['ETag'], p['Size'])}")
            else:
                checked["part"] += 1
        if len(want) == 3 and all(have.get(r["part"]) for r in want):
            c.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=o["upload_id"], MultipartUpload={
                "Parts": [{"PartNumber": r["part"], "ETag": r["etag"]} for r in want]})
            whole = b"".join(body(f"{key}-{r['part']}", r["size"]) for r in want)
            h = c.head_object(Bucket=bucket, Key=key)
            got = c.get_object(Bucket=bucket, Key=key)["Body"].read()
            if sha(got) != sha(whole) or h["Metadata"] != o["meta"] or h["ContentType"] != CT:
                problems.append(f"{key}: completed after restart but bytes/metadata differ")
            else:
                checked["open_completed"] += 1
    for r in (r for r in recs if r["kind"] == "noise"):
        try:
            got = c.get_object(Bucket=bucket, Key=r["key"])["Body"].read()
            checked["noise_present"] += 1
            if sha(got) != r["sha256"]:
                problems.append(f"{r['key']}: unacknowledged object present but torn ({len(got)} of {r['size']} bytes)")
        except ClientError:
            checked["noise_missing"] += 1
    result = {"run": run, "acknowledged": len(acked), "checked": checked, "problems": problems,
              "status": "pass" if not problems and len(acked) >= 200
              and checked["put"] + checked["mpu"] + checked["part"] == len(acked) else "fail"}
    with open(f"{out}/crash-verify.json", "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    mode, run, out = sys.argv[1:4]
    sys.exit({"write": write, "verify": verify}[mode](run, out))
