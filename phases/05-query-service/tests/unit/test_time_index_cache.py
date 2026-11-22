"""
Regression tests for the time-indexed cache (wrong/stale results).

Covers the three bugs fixed in
phases/05-query-service/src/query_service/time_index_cache.py:

1. The cache key omitted `columns`/`limit`, so a `columns=x,y&limit=10`
   request could be served the body computed for `columns=*&limit=100000`.
2. `_merge_overlapping_data` returned a DIFFERENT time window's data
   whenever `_coverage_score > 0.8` (an up-to-20%-off fuzzy match) and
   reported it as a normal hit -- even when the cached range didn't fully
   contain what was requested.
3. Entries never expired, so newly-written Parquet after a cache fill was
   never reflected in repeat queries.

The cache is in-process (the `redis_client` passed at construction is never
used for storage -- see `TimeIndexedCache.get`/`put`), so these tests build
a `TimeIndexedCache` directly. No live services required.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

from query_service.time_index_cache import (
    ReductStoreStyleQueryOptimizer,
    TimeIndexedCache,
    TimeRange,
)


ROBOT_ID = "robotA"
MESSAGE_TYPE = "imu"


def _range(start_ns: int, end_ns: int) -> TimeRange:
    return TimeRange(start_ns=start_ns, end_ns=end_ns)


def _payload(tag: str):
    """A distinguishable body so tests can assert *which* body was served."""
    return {"columns": ["x"], "rows": [[tag]]}


# ---------------------------------------------------------------------------
# Bug 1: the cache key must include columns + limit
# ---------------------------------------------------------------------------

def test_different_columns_do_not_collide():
    cache = TimeIndexedCache()
    tr = _range(1_000_000_000, 2_000_000_000)

    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("narrow"), columns=("x", "y"), limit=10)

    # Same robot/type/time-range, different columns -- must be a miss, not
    # "narrow"'s body served under the wrong shape.
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=None, limit=100000) is None

    # It gets its own, independently-stored entry rather than colliding.
    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("wide"), columns=None, limit=100000)
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=("x", "y"), limit=10) == _payload("narrow")
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=None, limit=100000) == _payload("wide")


def test_different_limit_does_not_collide():
    cache = TimeIndexedCache()
    tr = _range(1_000_000_000, 2_000_000_000)

    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("limit10"), columns=("x",), limit=10)

    # Same columns/time-range, different limit -- must be a miss.
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=("x",), limit=100000) is None

    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("limit100000"), columns=("x",), limit=100000)
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=("x",), limit=10) == _payload("limit10")
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=("x",), limit=100000) == _payload("limit100000")


def test_exact_match_is_still_a_hit():
    """Sanity check: the key fix must not turn every lookup into a miss."""
    cache = TimeIndexedCache()
    tr = _range(0, 1_000_000_000)
    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("exact"), columns=("x",), limit=50)
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=("x",), limit=50) == _payload("exact")


# ---------------------------------------------------------------------------
# Bug 2: no fuzzy/partial-coverage hits -- only exact containment
# ---------------------------------------------------------------------------

def test_partial_coverage_is_not_served_as_a_complete_hit():
    """A cached range covering 85% of the requested range (well above the
    old >0.8 fuzzy threshold) does NOT fully contain the request, and must
    not be served as a complete hit."""
    cache = TimeIndexedCache()

    cached_range = _range(0, 8_500_000_000)
    cache.put(ROBOT_ID, MESSAGE_TYPE, cached_range, _payload("partial"), columns=None, limit=1000)

    requested_range = _range(0, 10_000_000_000)
    result = cache.get(ROBOT_ID, MESSAGE_TYPE, requested_range, columns=None, limit=1000)

    assert result is None


def test_containment_without_filterable_timestamps_falls_back_to_miss():
    """A cached range that DOES fully contain the request (so the old fuzzy
    coverage score would have been 1.0, well over 0.8) but whose stored
    payload has no recognizable timestamp column can't be safely trimmed
    down to the requested window -- it must miss rather than return the
    wrong (wider) window's rows."""
    cache = TimeIndexedCache()

    cached_range = _range(0, 20_000_000_000)
    cache.put(ROBOT_ID, MESSAGE_TYPE, cached_range, _payload("wrong-window"), columns=None, limit=1000)

    requested_range = _range(15_000_000_000, 16_000_000_000)
    result = cache.get(ROBOT_ID, MESSAGE_TYPE, requested_range, columns=None, limit=1000)

    assert result is None


def test_exact_containment_with_filterable_rows_returns_only_requested_window():
    """When the cached range fully contains the request AND the payload has
    a recognizable timestamp column, the fix trims to the requested window
    instead of returning the raw, wider stored payload."""
    cache = TimeIndexedCache()

    cached_range = _range(0, 20_000_000_000)
    data = {
        "columns": ["ts", "x"],
        "rows": [
            [1_000_000_000, "a"],
            [5_000_000_000, "b"],
            [15_500_000_000, "c"],  # inside the requested window below
            [19_000_000_000, "d"],
        ],
    }
    cache.put(ROBOT_ID, MESSAGE_TYPE, cached_range, data, columns=("ts", "x"), limit=1000)

    requested_range = _range(15_000_000_000, 16_000_000_000)
    result = cache.get(ROBOT_ID, MESSAGE_TYPE, requested_range, columns=("ts", "x"), limit=1000)

    assert result is not None
    assert result["rows"] == [[15_500_000_000, "c"]]


def test_containment_does_not_reuse_a_possibly_truncated_limited_result():
    """A wide query that reached LIMIT does not prove a contained window is
    empty: the backing rows for that window may have fallen outside the
    unordered limited slice."""
    cache = TimeIndexedCache()
    cached_range = _range(0, 100)
    data = {
        "columns": ["ts"],
        "rows": [[timestamp] for timestamp in range(10)],
    }
    cache.put(ROBOT_ID, MESSAGE_TYPE, cached_range, data, limit=10)

    # The cached slice contains no late rows, but the cache cannot infer that
    # the backing data has none. This must execute a fresh narrow query.
    assert cache.get(
        ROBOT_ID,
        MESSAGE_TYPE,
        _range(90, 100),
        limit=10,
    ) is None


def test_overlap_without_containment_is_a_miss():
    """A cached range that merely overlaps -- neither side fully contains
    the other -- must never be served, fuzzy score or not."""
    cache = TimeIndexedCache()

    cached_range = _range(0, 5_000_000_000)
    cache.put(ROBOT_ID, MESSAGE_TYPE, cached_range, _payload("shifted"), columns=None, limit=1000)

    requested_range = _range(4_000_000_000, 9_000_000_000)
    result = cache.get(ROBOT_ID, MESSAGE_TYPE, requested_range, columns=None, limit=1000)

    assert result is None


# ---------------------------------------------------------------------------
# Bug 3: TTL expiry -- stale entries must not be served
# ---------------------------------------------------------------------------

def test_stale_entry_past_ttl_is_not_served():
    cache = TimeIndexedCache(ttl_seconds=0.05)
    tr = _range(0, 1_000_000_000)

    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("stale"), columns=None, limit=1000)

    # Sanity: immediately after put, it's a hit.
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=None, limit=1000) == _payload("stale")

    time.sleep(0.1)

    result = cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=None, limit=1000)
    assert result is None


def test_stale_entry_is_evicted_not_just_skipped():
    """After expiry the entry should actually be removed from the indexes
    (not merely skipped on read), so it doesn't linger in stats/memory
    accounting forever."""
    cache = TimeIndexedCache(ttl_seconds=0.05)
    tr = _range(0, 1_000_000_000)
    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("stale"), columns=None, limit=1000)

    time.sleep(0.1)
    cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=None, limit=1000)

    assert cache.get_stats()["total_entries"] == 0


def test_fresh_entry_within_ttl_is_served():
    """Sanity check: a generous TTL must not make every lookup miss."""
    cache = TimeIndexedCache(ttl_seconds=60.0)
    tr = _range(0, 1_000_000_000)
    cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("fresh"), columns=None, limit=1000)

    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, columns=None, limit=1000) == _payload("fresh")


# ---------------------------------------------------------------------------
# Quota and concurrent-index integrity
# ---------------------------------------------------------------------------

def test_memory_pressure_evicts_only_the_bytes_required():
    cache = TimeIndexedCache(max_memory_mb=1)
    payload = {"rows": ["x" * 300_000]}

    for index in range(4):
        tr = _range(index * 2_000_000_000, index * 2_000_000_000 + 1_000_000_000)
        assert cache.put(ROBOT_ID, MESSAGE_TYPE, tr, payload, limit=index)

    # Four entries do not fit, but evicting one does. The old free-space
    # formula evicted two and left only half the useful cache behind.
    assert cache.get_stats()["total_entries"] == 3
    assert cache.current_memory_bytes <= cache.max_memory_bytes


def test_entry_larger_than_the_entire_quota_is_not_inserted():
    now = time.time_ns()
    cache = TimeIndexedCache(max_memory_mb=1)
    tr = _range(now - 1_000_000_000, now)

    assert not cache.put(
        ROBOT_ID,
        MESSAGE_TYPE,
        tr,
        {"rows": ["x" * (2 * 1024 * 1024)]},
    )
    assert cache.get_stats()["total_entries"] == 0
    assert cache.current_memory_bytes == 0


def test_quota_counts_retained_python_rows_not_compact_json_bytes():
    payload = {
        "columns": ["ts", "x", "topic"],
        "rows": [
            [index, index / 10, f"topic-{index % 100}"]
            for index in range(10_000)
        ],
    }
    # This is the adversarial shape the old estimator missed: the compact JSON
    # is well below 1 MiB while the retained list/row/scalar graph is much larger.
    assert len(json.dumps(payload).encode()) < 1024 * 1024

    now = time.time_ns()
    cache = TimeIndexedCache(max_memory_mb=1)
    tr = _range(now - 1_000_000_000, now)

    assert not cache.put(ROBOT_ID, MESSAGE_TYPE, tr, payload, limit=10_000)
    assert cache.current_memory_bytes == 0
    assert cache.get_stats()["total_entries"] == 0


def test_same_key_replacement_does_not_duplicate_indexes_or_accounting():
    cache = TimeIndexedCache(max_memory_mb=1)
    tr = _range(0, 1_000_000_000)

    assert cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("old"), limit=10)
    assert cache.put(ROBOT_ID, MESSAGE_TYPE, tr, _payload("new"), limit=10)

    key = cache._generate_cache_key(ROBOT_ID, MESSAGE_TYPE, tr, None, 10)
    assert cache.robot_index[ROBOT_ID] == [key]
    assert cache.type_index[MESSAGE_TYPE] == [key]
    assert cache.get_stats()["total_entries"] == 1
    assert cache.current_memory_bytes == cache.time_index[key].size_bytes
    assert cache.get(ROBOT_ID, MESSAGE_TYPE, tr, limit=10) == _payload("new")


def test_concurrent_same_key_writes_preserve_one_consistent_entry():
    cache = TimeIndexedCache(max_memory_mb=1)
    tr = _range(0, 1_000_000_000)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda index: cache.put(
                    ROBOT_ID,
                    MESSAGE_TYPE,
                    tr,
                    _payload(str(index)),
                    limit=25,
                ),
                range(100),
            )
        )

    assert all(results)
    key = cache._generate_cache_key(ROBOT_ID, MESSAGE_TYPE, tr, None, 25)
    assert cache.robot_index[ROBOT_ID] == [key]
    assert cache.type_index[MESSAGE_TYPE] == [key]
    assert cache.get_stats()["total_entries"] == 1
    assert cache.current_memory_bytes == cache.time_index[key].size_bytes


def test_unique_misses_cannot_grow_access_tracking_without_bound():
    cache = TimeIndexedCache(max_access_patterns=8)

    for index in range(100):
        cache.get(
            f"robot-{index}",
            MESSAGE_TYPE,
            _range(index * 2, index * 2 + 1),
            limit=1,
        )

    assert len(cache.access_patterns) == 8


def test_unique_query_shapes_cannot_grow_optimizer_tracking_without_bound():
    cache = TimeIndexedCache(max_access_patterns=8)
    optimizer = ReductStoreStyleQueryOptimizer(cache, max_query_patterns=8)

    for index in range(100):
        result, was_cached = optimizer.optimize_time_range_query(
            f"robot-{index}",
            MESSAGE_TYPE,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            lambda: None,
            limit=1,
        )
        assert result is None
        assert was_cached is False

    assert optimizer.get_optimization_stats()["total_unique_patterns"] == 8
