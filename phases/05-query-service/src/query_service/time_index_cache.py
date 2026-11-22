"""
Time-Indexed Cache Layer inspired by ReductStore principles.

An in-process, in-memory cache in front of the DuckDB-over-Parquet read
path, consulted only by `/api/v1/robots/{robot}/data/{type}` and only when
BOTH `since` and `until` are given.

MEASURED (336 Parquet files / 6.72M rows on a single-box
test lake, loopback, medians of 7 trials; method in
`phases/05-query-service/bench/README.md`):

    range      rows     cold      warm    speedup   first cached request
    1h        3,333   192.5 ms   61.7 ms    3.1x    2nd
    24h      80,000  2539.4 ms 1616.3 ms    1.6x    2nd
    7d      100,000  3421.1 ms 3418.3 ms    1.0x    4th
    30d     100,000  3474.9 ms 3421.3 ms    1.0x    4th

Hit rate for 20 repeats of one 24h window: 0.90 (18/20).

Three things the measurement showed, which the design does not advertise:

1. `_should_cache` admits a range only if a PRE-COMPUTED BLOCK contains it,
   or if the same key has been requested more than twice. The blocks are
   built once in `_initialize_common_blocks` at construction and never
   refreshed. The two wide ranges miss for different reasons, and only one
   of them is subtle:
     - 30d simply exceeds every block -- the widest is `last_week`, so a
       30-day window can never be contained. Nothing surprising.
     - 7d misses by 5.7e-05 SECONDS. `last_week` starts at (cache
       construction - 7d), and a "last 7 days" request computed even
       microseconds earlier starts fractionally BEFORE it, so containment
       fails. Traced live, not inferred.
   Both are therefore served uncached until the third repeat, when the
   access-count branch admits them. The blocks also age with the process,
   so a long-lived worker's "last hour" block drifts steadily into the
   past.
2. The `data_size < 1MB and duration < 3600` fast path never fires for the
   canonical "last hour" query: its duration is exactly 3600, and `<` is
   strict.
3. A cache HIT costs under 0.01 ms for an 80k-row payload (0.0088 ms,
   median of 20 lookups). The warm figures above are dominated by encoding
   the response, not by the lookup -- so the cache's ceiling is set by how
   much JSON the endpoint returns, and no amount of caching moves the 24h
   number much below ~1.6 s while it serializes 80,000 rows. The MISS path
   is not free either: admission conservatively walks the retained Python
   object graph. That work happens outside the cache's index lock so one
   large response cannot serialize unrelated cache hits.

Cache warming / pre-computed range population is not implemented; the
measurement covers only what exists.

Key concepts borrowed from ReductStore:
1. Time-first indexing for sub-second range queries
2. Intelligent cache eviction based on access patterns
3. Pre-computed time blocks for common ranges
4. FIFO quota management for memory efficiency

This cache is per-worker and per-process. It does not survive a restart
and is not shared between workers.
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from collections import defaultdict, OrderedDict
import hashlib
import sys
import threading
from prometheus_client import Counter, Histogram, Gauge

from . import settings


# Metrics
TIME_INDEX_HITS = Counter("time_index_cache_hits_total", "Cache hits by type", ["cache_type"])
TIME_INDEX_MISSES = Counter("time_index_cache_misses_total", "Cache misses by type", ["cache_type"])
TIME_INDEX_EVICTIONS = Counter("time_index_cache_evictions_total", "Cache evictions by reason", ["reason"])
TIME_INDEX_QUERY_DURATION = Histogram(
    "time_index_query_duration_seconds", 
    "Time spent on time-indexed queries",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0)
)
TIME_INDEX_MEMORY_USAGE = Gauge("time_index_cache_memory_bytes", "Memory usage of time index cache")


# Miss/admission tracking is useful only for recently active query shapes. A
# fixed LRU bound prevents authenticated callers from retaining one dict entry
# forever for every unique robot/time range they probe.
_MAX_ACCESS_PATTERNS = 4096
_MAX_QUERY_PATTERNS = 4096

# Account for the CacheEntry/TimeRange objects plus their memberships in the
# time, robot, type, and hot indexes. The deep walk below counts the response
# graph itself; this deliberately generous floor covers allocator/container
# slack that ``sys.getsizeof`` cannot attribute cleanly per entry.
_CACHE_ENTRY_METADATA_BYTES = 4096


def _retained_size_bytes(value: Any) -> int:
    """Conservatively count the Python object graph retained by one entry.

    Query responses are JSON-compatible dict/list/tuple/scalar graphs. Counting
    their encoded JSON bytes underestimates resident memory by several times
    because every Python row, cell, string, and container remains live. Shared
    references are intentionally counted once per occurrence, which avoids a
    second large ``seen`` set during admission and errs toward rejecting data
    rather than exceeding the quota. Query results are acyclic JSON-compatible
    graphs (as required by the response encoder).
    """
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(
            _retained_size_bytes(key) + _retained_size_bytes(item)
            for key, item in value.items()
        )
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(_retained_size_bytes(item) for item in value)
    elif isinstance(value, memoryview):
        # A memoryview's small wrapper does not include the referenced buffer.
        size += value.nbytes
    return size


def _cache_entry_size_bytes(data: Any) -> int:
    return _retained_size_bytes(data) + _CACHE_ENTRY_METADATA_BYTES


@dataclass
class TimeRange:
    """Time range with nanosecond precision for robotics data"""
    start_ns: int
    end_ns: int
    
    @classmethod
    def from_iso(cls, start_iso: str, end_iso: str) -> "TimeRange":
        start_dt = settings.parse_iso_ts(start_iso)
        end_dt = settings.parse_iso_ts(end_iso)
        assert start_dt is not None and end_dt is not None
        return cls(
            start_ns=settings.datetime_to_epoch_ns(start_dt),
            end_ns=settings.datetime_to_epoch_ns(end_dt),
        )
    
    def duration_seconds(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000_000
    
    def overlaps(self, other: "TimeRange") -> bool:
        return not (self.end_ns <= other.start_ns or self.start_ns >= other.end_ns)
    
    def contains(self, other: "TimeRange") -> bool:
        return self.start_ns <= other.start_ns and self.end_ns >= other.end_ns


@dataclass
class CacheEntry:
    """Time-indexed cache entry with access tracking"""
    key: str
    data: Any
    time_range: TimeRange
    robot_id: str
    message_type: str
    created_at: float
    last_accessed: float
    access_count: int
    size_bytes: int
    # Response-shaping filters this entry's data was computed with. Two
    # entries covering the same time range but different columns/limit are
    # NOT interchangeable, so these ride along for containment checks.
    columns: Optional[Tuple[str, ...]] = None
    limit: Optional[int] = None
    # Epoch seconds after which this entry must no longer be served. Default
    # of 0.0 fails closed (already expired) for any entry built without one.
    expires_at: float = 0.0

    def access_frequency(self) -> float:
        """Calculate access frequency per hour"""
        age_hours = max(0.1, (time.time() - self.created_at) / 3600)
        return self.access_count / age_hours


class TimeIndexedCache:
    """
    ReductStore-inspired time-indexed cache for ultra-fast time-range queries.
    
    Features:
    - Time-first indexing for O(log n) range queries
    - Intelligent eviction based on access patterns
    - Pre-computed blocks for common time ranges
    - Memory-efficient FIFO quotas
    """
    
    def __init__(
        self,
        max_memory_mb: int = 512,
        ttl_seconds: float = 30.0,
        max_access_patterns: int = _MAX_ACCESS_PATTERNS,
    ):
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.current_memory_bytes = 0
        # FastAPI executes synchronous query handlers in a shared thread pool.
        # Every index and the memory counter form one compound data structure,
        # so individual dict operations being atomic under CPython is not a
        # sufficient concurrency contract.
        self._lock = threading.RLock()
        # Entries are evicted lazily on lookup once older than this -- new
        # Parquet written after a cache fill becomes visible within one TTL.
        self.ttl_seconds = ttl_seconds

        # Time-indexed storage (sorted by start time)
        self.time_index: OrderedDict[str, CacheEntry] = OrderedDict()
        
        # Fast lookups
        self.robot_index: Dict[str, List[str]] = defaultdict(list)
        self.type_index: Dict[str, List[str]] = defaultdict(list)
        
        # Hot data cache (most frequently accessed)
        self.hot_cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.hot_cache_max_entries = 100
        
        # Access pattern tracking
        self.max_access_patterns = max(1, int(max_access_patterns))
        self.access_patterns: OrderedDict[str, Dict[str, float]] = OrderedDict()
        
        # Pre-computed common ranges (ReductStore-style blocks)
        self.range_blocks: Dict[str, Dict] = {}
        self._initialize_common_blocks()
    
    def _initialize_common_blocks(self):
        """Initialize pre-computed blocks for common time ranges"""
        now = datetime.now(timezone.utc)
        
        # Common ranges robotics teams query
        ranges = {
            "last_hour": (now - timedelta(hours=1), now),
            "last_4_hours": (now - timedelta(hours=4), now),
            "last_24_hours": (now - timedelta(hours=24), now),
            "last_week": (now - timedelta(days=7), now),
            "today": (now.replace(hour=0, minute=0, second=0, microsecond=0), now),
            "yesterday": (
                (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                now.replace(hour=0, minute=0, second=0, microsecond=0)
            ),
        }
        
        for name, (start_dt, end_dt) in ranges.items():
            self.range_blocks[name] = {
                "start_ns": settings.datetime_to_epoch_ns(start_dt),
                "end_ns": settings.datetime_to_epoch_ns(end_dt),
                "name": name
            }
    
    def _generate_cache_key(
        self,
        robot_id: str,
        message_type: str,
        time_range: TimeRange,
        columns: Optional[Tuple[str, ...]] = None,
        limit: Optional[int] = None,
    ) -> str:
        """Generate cache key optimized for time-range access.

        Every response-shaping filter must be part of the key, not just the
        time range: a columns=x,y&limit=10 request and a columns=*&limit=100000
        request cover the same time range but return different bodies, so
        they must never collide on the same cache entry. Column order is
        preserved (not sorted) since it determines the order of values in
        each returned row.
        """
        columns_key = ",".join(columns) if columns else "*"
        limit_key = limit if limit is not None else "-"
        key_data = (
            f"{robot_id}:{message_type}:{time_range.start_ns}:{time_range.end_ns}"
            f":{columns_key}:{limit_key}"
        )
        return hashlib.md5(key_data.encode()).hexdigest()[:16]
    
    def _track_access(self, key: str, duration_seconds: float):
        """Track access patterns for intelligent caching"""
        pattern = self.access_patterns.get(key)
        if pattern is None:
            if len(self.access_patterns) >= self.max_access_patterns:
                self.access_patterns.popitem(last=False)
            pattern = {"count": 0, "last_access": 0, "avg_duration": 0}
            self.access_patterns[key] = pattern
        else:
            self.access_patterns.move_to_end(key)
        pattern["count"] += 1
        pattern["last_access"] = time.time()
        pattern["avg_duration"] = (pattern["avg_duration"] + duration_seconds) / 2
    
    def _should_cache(
        self,
        robot_id: str,
        message_type: str,
        time_range: TimeRange,
        data_size: int,
        columns: Optional[Tuple[str, ...]] = None,
        limit: Optional[int] = None,
    ) -> bool:
        """Decide whether to cache based on ReductStore principles"""
        # Always cache small, recent data
        if data_size < 1024 * 1024 and time_range.duration_seconds() < 3600:  # <1MB, <1 hour
            return True

        # Cache if it's a common time range
        for block_name, block_range in self.range_blocks.items():
            block_tr = TimeRange(block_range["start_ns"], block_range["end_ns"])
            if block_tr.contains(time_range):
                return True

        # Cache based on access frequency
        cache_key = self._generate_cache_key(robot_id, message_type, time_range, columns, limit)
        pattern = self.access_patterns.get(cache_key, {})
        if pattern.get("count", 0) > 2:  # Accessed more than twice
            return True
        
        return False
    
    def _evict_if_needed(self, required_bytes: int):
        """Intelligent eviction based on ReductStore FIFO + access patterns"""
        if self.current_memory_bytes + required_bytes <= self.max_memory_bytes:
            return
        
        # Sort by eviction priority (least valuable first)
        entries = list(self.time_index.values())
        
        def eviction_score(entry: CacheEntry) -> float:
            """Lower score = higher priority for eviction"""
            age_hours = (time.time() - entry.last_accessed) / 3600
            frequency = entry.access_frequency()
            size_penalty = entry.size_bytes / (1024 * 1024)  # MB
            
            # ReductStore principle: evict old, infrequently accessed, large data first
            return frequency / (age_hours + 1) - size_penalty * 0.1
        
        entries.sort(key=eviction_score)
        
        # Evict until we have enough space
        bytes_to_free = self.current_memory_bytes + required_bytes - self.max_memory_bytes
        freed = 0
        
        for entry in entries:
            if freed >= bytes_to_free:
                break
            
            # Remove from all indexes
            self._remove_entry(entry.key)
            freed += entry.size_bytes
            TIME_INDEX_EVICTIONS.labels(reason="memory_pressure").inc()
    
    def _remove_entry(self, key: str):
        """Remove entry from all indexes"""
        if key in self.time_index:
            entry = self.time_index[key]
            self.current_memory_bytes -= entry.size_bytes
            
            # Remove from time index
            del self.time_index[key]
            
            # Remove from robot index
            if entry.robot_id in self.robot_index:
                if key in self.robot_index[entry.robot_id]:
                    self.robot_index[entry.robot_id].remove(key)
                if not self.robot_index[entry.robot_id]:
                    del self.robot_index[entry.robot_id]
            
            # Remove from type index
            if entry.message_type in self.type_index:
                if key in self.type_index[entry.message_type]:
                    self.type_index[entry.message_type].remove(key)
                if not self.type_index[entry.message_type]:
                    del self.type_index[entry.message_type]
            
            # Remove from hot cache
            if key in self.hot_cache:
                del self.hot_cache[key]
            TIME_INDEX_MEMORY_USAGE.set(self.current_memory_bytes)

    def _is_expired(self, entry: CacheEntry) -> bool:
        """True once an entry's TTL has elapsed. Expired entries must never
        be served -- this is what keeps newly-written Parquet visible within
        one TTL instead of being masked by a stale cache fill forever."""
        return time.time() >= entry.expires_at

    def get(
        self,
        robot_id: str,
        message_type: str,
        time_range: TimeRange,
        columns: Optional[Tuple[str, ...]] = None,
        limit: Optional[int] = None,
    ) -> Optional[Any]:
        """Get data for time range with sub-millisecond performance"""
        start_time = time.perf_counter()
        cache_key = self._generate_cache_key(
            robot_id, message_type, time_range, columns, limit
        )

        try:
            with self._lock:
                # Check hot cache first (ReductStore-style speed)
                if cache_key in self.hot_cache:
                    entry = self.hot_cache[cache_key]
                    if self._is_expired(entry):
                        self._remove_entry(cache_key)
                        TIME_INDEX_EVICTIONS.labels(reason="ttl_expired").inc()
                    else:
                        entry.last_accessed = time.time()
                        entry.access_count += 1

                        # Move to end (LRU)
                        self.hot_cache.move_to_end(cache_key)

                        TIME_INDEX_HITS.labels(cache_type="hot").inc()
                        return entry.data

                # Check main time index
                if cache_key in self.time_index:
                    entry = self.time_index[cache_key]
                    if self._is_expired(entry):
                        self._remove_entry(cache_key)
                        TIME_INDEX_EVICTIONS.labels(reason="ttl_expired").inc()
                    else:
                        entry.last_accessed = time.time()
                        entry.access_count += 1

                        # Promote to hot cache if frequently accessed
                        if entry.access_frequency() > 1.0:  # >1 access per hour
                            self._promote_to_hot(cache_key, entry)

                        TIME_INDEX_HITS.labels(cache_type="time_index").inc()
                        return entry.data

                # Snapshot immutable entry references under the index lock.
                # Potentially large row filtering happens below without
                # blocking exact hits or insertions for unrelated keys.
                overlapping = self._find_overlapping_ranges(
                    robot_id, message_type, time_range, columns, limit
                )

            for entry in overlapping:
                merged_data = self._merge_overlapping_data([entry], time_range)
                if merged_data is not None:
                    # Do not serve a snapshot invalidated/replaced while it was
                    # being filtered outside the lock.
                    with self._lock:
                        current = self.time_index.get(entry.key)
                        if current is entry and not self._is_expired(entry):
                            TIME_INDEX_HITS.labels(cache_type="range_merge").inc()
                            return merged_data

            TIME_INDEX_MISSES.labels(cache_type="not_found").inc()
            return None

        finally:
            duration = time.perf_counter() - start_time
            TIME_INDEX_QUERY_DURATION.observe(duration)
            with self._lock:
                self._track_access(cache_key, duration)
    
    def put(
        self,
        robot_id: str,
        message_type: str,
        time_range: TimeRange,
        data: Any,
        columns: Optional[Tuple[str, ...]] = None,
        limit: Optional[int] = None,
    ) -> bool:
        # Deep sizing is CPU-heavy for large row sets. It reads only the
        # request-local result object, so keep it outside the global index lock.
        data_size = _cache_entry_size_bytes(data)
        cache_key = self._generate_cache_key(
            robot_id, message_type, time_range, columns, limit
        )
        with self._lock:
            return self._put_unlocked(
                robot_id,
                message_type,
                time_range,
                data,
                columns,
                limit,
                data_size=data_size,
                cache_key=cache_key,
            )

    def _put_unlocked(
        self,
        robot_id: str,
        message_type: str,
        time_range: TimeRange,
        data: Any,
        columns: Optional[Tuple[str, ...]] = None,
        limit: Optional[int] = None,
        *,
        data_size: int,
        cache_key: str,
    ) -> bool:
        """Store data with intelligent caching decisions"""
        # Decision: should we cache this?
        if not self._should_cache(robot_id, message_type, time_range, data_size, columns, limit):
            return False

        # One entry can never fit, even after evicting everything. Refuse it
        # instead of exceeding the configured quota.
        if data_size > self.max_memory_bytes:
            return False

        # Replacement is a replacement, not another index membership and
        # another copy in the memory counter.
        if cache_key in self.time_index:
            self._remove_entry(cache_key)

        # Evict if needed
        self._evict_if_needed(data_size)

        # Create cache entry
        now = time.time()
        entry = CacheEntry(
            key=cache_key,
            data=data,
            time_range=time_range,
            robot_id=robot_id,
            message_type=message_type,
            created_at=now,
            last_accessed=now,
            access_count=1,
            size_bytes=data_size,
            columns=tuple(columns) if columns else None,
            limit=limit,
            expires_at=now + self.ttl_seconds,
        )

        # Add to indexes
        self.time_index[cache_key] = entry
        self.robot_index[robot_id].append(cache_key)
        self.type_index[message_type].append(cache_key)
        self.current_memory_bytes += data_size
        
        # Update metrics
        TIME_INDEX_MEMORY_USAGE.set(self.current_memory_bytes)
        
        return True
    
    def _promote_to_hot(self, key: str, entry: CacheEntry):
        """Promote frequently accessed data to hot cache"""
        # Evict LRU from hot cache if needed
        if len(self.hot_cache) >= self.hot_cache_max_entries:
            lru_key, _ = self.hot_cache.popitem(last=False)
            TIME_INDEX_EVICTIONS.labels(reason="hot_cache_lru").inc()
        
        self.hot_cache[key] = entry
    
    def _find_overlapping_ranges(
        self,
        robot_id: str,
        message_type: str,
        target_range: TimeRange,
        columns: Optional[Tuple[str, ...]] = None,
        limit: Optional[int] = None,
    ) -> List[CacheEntry]:
        """Find cached, unexpired entries with the SAME response shape
        (columns/limit) whose time range overlaps the target range. An entry
        computed with different columns/limit is never a candidate -- its
        data isn't shaped like what the caller asked for."""
        overlapping = []
        columns_norm = tuple(columns) if columns else None

        # Check robot-specific entries
        for key in self.robot_index.get(robot_id, []):
            entry = self.time_index.get(key)
            if not entry or entry.message_type != message_type:
                continue
            if entry.columns != columns_norm or entry.limit != limit:
                continue
            if self._is_expired(entry):
                continue
            if entry.time_range.overlaps(target_range):
                overlapping.append(entry)

        return overlapping

    def _merge_overlapping_data(self, entries: List[CacheEntry], target_range: TimeRange) -> Optional[Any]:
        """Serve a cached entry for the target range ONLY when it EXACTLY
        contains the request (entry.start <= target.start and entry.end >=
        target.end), returning rows filtered down to the requested window.

        There is no fuzzy/partial match here: an entry that merely overlaps
        without full containment is skipped, so the caller falls through to
        a genuine cache miss (compute fresh) rather than risk serving a
        window that under- or over-shoots what was asked for.
        """
        for entry in entries:
            if not entry.time_range.contains(target_range):
                continue

            if (
                entry.time_range.start_ns == target_range.start_ns
                and entry.time_range.end_ns == target_range.end_ns
            ):
                return entry.data

            # SQL populates these entries with LIMIT but no completeness
            # marker. If the result reached that limit, rows outside the first
            # limited slice may exist anywhere in the wider time range. Such an
            # entry is safe for its exact key but cannot prove a contained
            # sub-range is complete.
            if entry.limit is not None:
                rows = entry.data.get("rows") if isinstance(entry.data, dict) else None
                if not isinstance(rows, list) or len(rows) >= entry.limit:
                    continue

            filtered = self._filter_rows_to_range(entry.data, target_range)
            if filtered is not None:
                return filtered

        return None

    def _filter_rows_to_range(self, data: Any, target_range: TimeRange) -> Optional[Any]:
        """Filter a {"columns": [...], "rows": [...]} payload down to rows
        whose timestamp falls inside target_range.

        Returns None -- meaning "treat as a cache miss", never an empty hit
        -- if the payload isn't a shape we can safely filter (e.g. the
        caller's `columns` selection excluded the timestamp column). We
        never guess at row membership.
        """
        if not isinstance(data, dict):
            return None
        columns = data.get("columns")
        rows = data.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return None

        ts_idx = None
        for candidate in ("ts", "ts_num", "ts_ts"):
            if candidate in columns:
                ts_idx = columns.index(candidate)
                break
        if ts_idx is None:
            return None

        filtered_rows = []
        for row in rows:
            ns = self._row_timestamp_ns(row[ts_idx])
            if ns is None:
                return None
            if target_range.start_ns <= ns <= target_range.end_ns:
                filtered_rows.append(row)

        return {"columns": columns, "rows": filtered_rows}

    @staticmethod
    def _row_timestamp_ns(value: Any) -> Optional[int]:
        """Best-effort conversion of a row's timestamp cell to epoch ns."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, datetime):
            return settings.datetime_to_epoch_ns(value)
        return None

    def invalidate_robot(self, robot_id: str):
        """Invalidate all cache entries for a robot"""
        with self._lock:
            keys_to_remove = self.robot_index.get(robot_id, []).copy()
            for key in keys_to_remove:
                self._remove_entry(key)
            TIME_INDEX_EVICTIONS.labels(reason="robot_invalidation").inc(len(keys_to_remove))
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics for monitoring"""
        with self._lock:
            hot_count = len(self.hot_cache)
            total_count = len(self.time_index)

            return {
                "memory_usage_mb": self.current_memory_bytes / (1024 * 1024),
                "memory_limit_mb": self.max_memory_bytes / (1024 * 1024),
                "total_entries": total_count,
                "hot_entries": hot_count,
                "robots_cached": len(self.robot_index),
                "message_types_cached": len(self.type_index),
                "enabled": True,
            }


class ReductStoreStyleQueryOptimizer:
    """
    Query optimizer that applies ReductStore principles to existing DuckDB queries.

    Measured speedup is 3.1x on a 1h window and 1.0x on 7d/30d -- see the
    module docstring for the full table and why.
    """
    
    def __init__(
        self,
        cache: TimeIndexedCache,
        max_query_patterns: int = _MAX_QUERY_PATTERNS,
    ):
        self.cache = cache
        self.max_query_patterns = max(1, int(max_query_patterns))
        self.query_patterns: OrderedDict[str, int] = OrderedDict()
        self._query_patterns_lock = threading.Lock()

    def _track_query_pattern(self, pattern_key: str) -> None:
        with self._query_patterns_lock:
            if pattern_key in self.query_patterns:
                self.query_patterns[pattern_key] += 1
                self.query_patterns.move_to_end(pattern_key)
                return
            if len(self.query_patterns) >= self.max_query_patterns:
                self.query_patterns.popitem(last=False)
            self.query_patterns[pattern_key] = 1
    
    def optimize_time_range_query(
        self,
        robot_id: str,
        message_type: str,
        start_time: str,
        end_time: str,
        fallback_query_func: callable,
        columns: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> Tuple[Any, bool]:
        """
        Optimize time-range query with ReductStore-inspired caching.

        `columns`/`limit` MUST be the caller's actual response-shaping
        filters -- they are part of the cache key, so a columns=x,y&limit=10
        request can never be served the body computed for columns=*&limit=100000.

        Returns: (result, was_cached)
        """
        time_range = TimeRange.from_iso(start_time, end_time)
        columns_key = tuple(columns) if columns else None

        # Track query patterns
        pattern_key = f"{robot_id}:{message_type}:{time_range.duration_seconds():.0f}s"
        self._track_query_pattern(pattern_key)

        # Try cache first
        cached_result = self.cache.get(robot_id, message_type, time_range, columns_key, limit)
        if cached_result is not None:
            return cached_result, True

        # Execute fallback query
        result = fallback_query_func()

        # Cache result if beneficial
        if result is not None:
            self.cache.put(robot_id, message_type, time_range, result, columns_key, limit)

        return result, False
    
    def get_optimization_stats(self) -> Dict[str, Any]:
        """Get optimization statistics"""
        with self._query_patterns_lock:
            # Find most common query patterns from one consistent snapshot.
            top_patterns = sorted(
                self.query_patterns.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:10]
            total_unique_patterns = len(self.query_patterns)
        
        cache_stats = self.cache.get_stats()
        
        return {
            "cache": cache_stats,
            "top_query_patterns": [
                {"pattern": pattern, "count": count} 
                for pattern, count in top_patterns
            ],
            "total_unique_patterns": total_unique_patterns,
            "reductstore_inspired": True,
            # Deliberately no speedup or hit-rate figures here. The measured
            # speedups came from one benchmark on one lake on one box; served
            # from a live endpoint they would read as this deployment's
            # current performance. They live in this module's docstring, where
            # their conditions travel with them. This endpoint reports only
            # what this process has actually observed.
            "benchmark": "see phases/05-query-service/bench/README.md",
        }


# Global instance for the query service
_global_time_index_cache: Optional[TimeIndexedCache] = None
_global_query_optimizer: Optional[ReductStoreStyleQueryOptimizer] = None


def initialize_time_index_cache(max_memory_mb: int = 512):
    """Initialize global time index cache"""
    global _global_time_index_cache, _global_query_optimizer
    
    _global_time_index_cache = TimeIndexedCache(max_memory_mb=max_memory_mb)
    _global_query_optimizer = ReductStoreStyleQueryOptimizer(_global_time_index_cache)


def get_time_index_cache() -> Optional[TimeIndexedCache]:
    """Get global time index cache instance"""
    return _global_time_index_cache


def get_query_optimizer() -> Optional[ReductStoreStyleQueryOptimizer]:
    """Get global query optimizer instance"""
    return _global_query_optimizer
