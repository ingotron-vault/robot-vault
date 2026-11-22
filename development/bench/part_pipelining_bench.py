#!/usr/bin/env python3
"""Benchmark: server-side S3 part-upload pipelining (single large file).

Why this exists instead of driving `rua upload`: the number under test is the
ingest server's `upload_part` loop, and `rua upload` adds ~0.59 s of Python CLI
start + handshake per file plus whatever the upload agent is
doing that week. This driver speaks the real `Upload` RPC into the real S3
multipart path with nothing else in the way, so a before/after delta is
attributable to the server.

It never touches the disk: one chunk-sized pseudorandom buffer (fixed seed) is
re-sent for every chunk, so robot-side read cost is zero and the payload is
incompressible. The declared `expected_sha256` is computed over exactly the
bytes that will be sent, so the server's integrity gate is exercised
for real -- a trial that does not come back `completed` with
`integrity_verified` is not a result.

Usage (from the repo root, inside the compose network):

    docker compose run --rm --no-deps \
      -v $PWD/development/bench:/bench dev \
      python /bench/part_pipelining_bench.py --robot-id bench_pipelining --trials 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import uuid

sys.path.insert(0, "/app/phases/02-robot-upload-agent/src")

import grpc  # noqa: E402
import upload_pb2  # noqa: E402
import upload_pb2_grpc  # noqa: E402

MIB = 1024 * 1024


def build_chunk(chunk_bytes: int) -> bytes:
    """One pseudorandom, incompressible buffer, reused for every chunk."""
    rng = random.Random(20260725)
    return bytes(rng.getrandbits(8) for _ in range(4096)) * (chunk_bytes // 4096)


def plan(total_bytes: int, chunk_bytes: int) -> list[int]:
    """Chunk sizes, in order. The last one carries the remainder."""
    sizes = [chunk_bytes] * (total_bytes // chunk_bytes)
    rest = total_bytes % chunk_bytes
    if rest:
        sizes.append(rest)
    return sizes


def digest(buf: bytes, sizes: list[int]) -> str:
    h = hashlib.sha256()
    for n in sizes:
        h.update(buf[:n])
    return h.hexdigest()


def run_trial(stub, token, robot_id, upload_id, filename, buf, sizes, total, chunk) -> float:
    """One resume-version-1 Upload RPC, timed end to end. Returns wall-clock seconds.

    A fresh `upload_id` with an empty session token opens a new multipart
    session, so every part is sent with its fixed boundary.
    """

    def messages():
        yield upload_pb2.UploadRequest(
            metadata=upload_pb2.UploadMetadata(
                robot_id=robot_id,
                filename=filename,
                file_size=total,
                content_type="application/octet-stream",
                upload_id=upload_id,
                expected_sha256=digest(buf, sizes),
                resume_version=1,
                chunk_size_bytes=chunk,
            )
        )
        offset = 0
        for i, n in enumerate(sizes):
            yield upload_pb2.UploadRequest(
                chunk=upload_pb2.UploadChunk(
                    data=buf[:n],
                    offset=offset,
                    chunk_number=i,
                    is_final=(i == len(sizes) - 1),
                )
            )
            offset += n

    meta = (("authorization", f"Bearer {token}"),) if token else ()
    started = time.perf_counter()
    resp = stub.Upload(messages(), metadata=meta)
    elapsed = time.perf_counter() - started
    if resp.status != "completed":
        raise SystemExit(f"trial did not complete: status={resp.status} err={resp.error_message}")
    if resp.bytes_received != total:
        raise SystemExit(f"bytes_received={resp.bytes_received} != {total}")
    return elapsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="ingest:50051")
    ap.add_argument("--robot-id", required=True)
    ap.add_argument("--size-mib", type=int, default=500)
    # Resume version 1 pins part boundaries between 5 and 8 MiB.
    ap.add_argument("--chunk-mib", type=int, default=5, choices=range(5, 9))
    ap.add_argument("--trials", type=int, default=3)
    # The processor's 5 s poll races the benchmark. It only
    # becomes a candidate once the row flips to `completed`, i.e. after the
    # timed window closes, so idling between trials long enough for it to
    # finish (and dead-letter this non-MCAP payload) keeps every timed window
    # clean without stopping a service the rest of the stack shares.
    ap.add_argument("--settle-seconds", type=float, default=0.0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    total = args.size_mib * MIB
    chunk = args.chunk_mib * MIB
    sizes = plan(total, chunk)
    buf = build_chunk(chunk)
    token = os.environ.get("INGEST_AUTH_TOKEN", "")

    opts = [
        ("grpc.max_send_message_length", 64 * MIB),
        ("grpc.max_receive_message_length", 64 * MIB),
    ]
    channel = grpc.insecure_channel(args.target, options=opts)
    stub = upload_pb2_grpc.UploadServiceStub(channel)

    results = []
    for trial in range(args.trials):
        if trial and args.settle_seconds:
            time.sleep(args.settle_seconds)
        upload_id = f"{args.robot_id}-{uuid.uuid4()}"
        elapsed = run_trial(
            stub, token, args.robot_id, upload_id,
            f"{args.robot_id}_{trial}.mcap", buf, sizes, total, chunk,
        )
        results.append({
            "trial": trial,
            "upload_id": upload_id,
            "seconds": round(elapsed, 3),
            "mib_per_s": round(args.size_mib / elapsed, 1),
        })
        print(json.dumps(results[-1]), flush=True)

    secs = sorted(r["seconds"] for r in results)
    median = secs[len(secs) // 2]
    print(json.dumps({
        "label": args.label,
        "size_mib": args.size_mib,
        "chunk_mib": args.chunk_mib,
        "parts": len(sizes),
        "trials": args.trials,
        "median_seconds": median,
        "range_seconds": [secs[0], secs[-1]],
        "median_mib_per_s": round(args.size_mib / median, 1),
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
