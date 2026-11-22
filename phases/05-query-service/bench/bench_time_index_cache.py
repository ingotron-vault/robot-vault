"""Measure the time-index cache.

What is actually being measured: `/api/v1/robots/{robot}/data/{type}` with
both `since` and `until` set, which is the ONLY path that consults
`time_index_cache` (see `robot_message_data` — the optimizer is skipped
entirely when either bound is missing).

Cold = the cache is empty, so the request runs the full registry ->
DuckDB -> S3 read. Warm = the identical request immediately afterwards.
Both are timed in-process through FastAPI's TestClient, so the numbers are
server-side work and exclude network time; the point is the cold/warm
ratio, not an absolute API latency.

Usage (needs a live object store + Postgres and a lake from generate_lake.py):
    S3_ENDPOINT=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
    S3_PARQUET_BUCKET=... DATABASE_URL=... \
    python3 bench_time_index_cache.py --out results.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List


def _import_app():
    """main.py mounts StaticFiles over the SPA build output, which only
    exists inside the service image."""
    import fastapi.staticfiles as fastapi_staticfiles

    class _Stub:
        def __init__(self, *a, **k):
            pass

    original = fastapi_staticfiles.StaticFiles
    fastapi_staticfiles.StaticFiles = _Stub
    try:
        return importlib.import_module("query_service.main")
    finally:
        fastapi_staticfiles.StaticFiles = original


def _counter_value(counter, **labels) -> float:
    """Current value of a prometheus counter child, or 0."""
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", default="bench-robot-1")
    ap.add_argument("--topic", default="imu/linear_accel")
    ap.add_argument("--trials", type=int, default=7)
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    qsm = _import_app()
    from fastapi.testclient import TestClient

    from query_service import settings
    from query_service.time_index_cache import (
        TIME_INDEX_HITS,
        TIME_INDEX_MISSES,
        initialize_time_index_cache,
        get_time_index_cache,
    )

    client = TestClient(qsm.app)
    now = datetime.now(timezone.utc)

    ranges = {
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "all_time": timedelta(days=30),
    }

    def request(since: datetime, until: datetime):
        t0 = time.perf_counter()
        resp = client.get(
            f"/api/v1/robots/{args.robot}/data/{args.topic}",
            params={
                "since": since.isoformat(),
                "until": until.isoformat(),
                "limit": args.limit,
            },
        )
        elapsed = time.perf_counter() - t0
        if resp.status_code != 200:
            raise SystemExit(f"request failed {resp.status_code}: {resp.text[:400]}")
        body = resp.json()
        return elapsed, len(body.get("rows", []))

    results: Dict[str, Dict] = {}

    for label, delta in ranges.items():
        until = now
        since = now - delta
        cold: List[float] = []
        warm: List[float] = []
        rows_seen = 0
        cached_flags: List[bool] = []

        for _trial in range(args.trials):
            # Cold: a brand-new cache instance, so nothing can carry over.
            initialize_time_index_cache(
                settings.TIME_INDEX_CACHE_SIZE_MB
            )
            t_cold, rows = request(since, until)
            cold.append(t_cold)
            rows_seen = rows

            hits_before = sum(
                _counter_value(TIME_INDEX_HITS, cache_type=t)
                for t in ("hot", "time_index", "range_merge")
            )
            t_warm, _ = request(since, until)
            warm.append(t_warm)
            hits_after = sum(
                _counter_value(TIME_INDEX_HITS, cache_type=t)
                for t in ("hot", "time_index", "range_merge")
            )
            cached_flags.append(hits_after > hits_before)

        cache = get_time_index_cache()
        results[label] = {
            "rows_returned": rows_seen,
            "trials": args.trials,
            "cold_ms": {
                "median": round(statistics.median(cold) * 1000, 1),
                "min": round(min(cold) * 1000, 1),
                "max": round(max(cold) * 1000, 1),
            },
            "warm_ms": {
                "median": round(statistics.median(warm) * 1000, 1),
                "min": round(min(warm) * 1000, 1),
                "max": round(max(warm) * 1000, 1),
            },
            "warm_was_a_cache_hit": {
                "hits": sum(cached_flags),
                "of": len(cached_flags),
            },
            "speedup_median": (
                round(statistics.median(cold) / statistics.median(warm), 1)
                if statistics.median(warm) > 0
                else None
            ),
            "cache_entries_after": len(cache.time_index) if cache else 0,
        }
        print(f"{label}: {json.dumps(results[label])}", flush=True)

    # How many identical requests a long-lived process needs before a range
    # is cached at all. `_should_cache` admits an entry when the range is
    # contained in a pre-computed block, or once the same key has been asked
    # for more than twice -- so a range that matches no block is served
    # uncached until the third request.
    admission: Dict[str, Dict] = {}
    for label, delta in ranges.items():
        initialize_time_index_cache(
            settings.TIME_INDEX_CACHE_SIZE_MB
        )
        cache = get_time_index_cache()
        until = now
        since = now - delta
        first_hit_at = None
        for attempt in range(1, 6):
            hits_before = sum(
                _counter_value(TIME_INDEX_HITS, cache_type=t)
                for t in ("hot", "time_index", "range_merge")
            )
            request(since, until)
            hits_after = sum(
                _counter_value(TIME_INDEX_HITS, cache_type=t)
                for t in ("hot", "time_index", "range_merge")
            )
            if hits_after > hits_before and first_hit_at is None:
                first_hit_at = attempt
                break
        tr_blocks = [
            name
            for name, b in cache.range_blocks.items()
            if b["start_ns"] <= int((now - delta).timestamp() * 1e9)
            and b["end_ns"] >= int(now.timestamp() * 1e9)
        ]
        admission[label] = {
            "first_request_served_from_cache": first_hit_at,
            "containing_precomputed_blocks": tr_blocks,
        }
        print(f"admission {label}: {json.dumps(admission[label])}", flush=True)

    # Hit rate over a repeated-query workload: one cold fill then N repeats,
    # which is what a dashboard polling the same window looks like.
    initialize_time_index_cache(settings.TIME_INDEX_CACHE_SIZE_MB)
    until = now
    since = now - timedelta(hours=24)
    repeats = 20
    misses_before = _counter_value(TIME_INDEX_MISSES, cache_type="not_found")
    hits_before = sum(
        _counter_value(TIME_INDEX_HITS, cache_type=t)
        for t in ("hot", "time_index", "range_merge")
    )
    for _ in range(repeats):
        request(since, until)
    hits = (
        sum(
            _counter_value(TIME_INDEX_HITS, cache_type=t)
            for t in ("hot", "time_index", "range_merge")
        )
        - hits_before
    )
    misses = _counter_value(TIME_INDEX_MISSES, cache_type="not_found") - misses_before

    payload = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "disclosure": (
            "Single-box loopback: the object store, Postgres and the query service all on "
            "one host, in-process TestClient (no network hop, no TLS). Not a "
            "figure for a deployed multi-host stack."
        ),
        "cache_ttl_seconds": get_time_index_cache().ttl_seconds,
        "ranges": results,
        "admission": admission,
        "repeat_workload": {
            "requests": repeats,
            "hits": int(hits),
            "misses": int(misses),
            "hit_rate": round(hits / repeats, 3) if repeats else None,
        },
    }

    text = json.dumps(payload, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
