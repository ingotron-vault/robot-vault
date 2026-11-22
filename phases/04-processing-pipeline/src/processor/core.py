from dataclasses import dataclass
from pathlib import Path
import io
import os
import socket
import threading
import time
import hashlib
import uuid
from typing import Callable, List, Literal, Tuple, Dict, Optional, Union
import pyarrow.parquet as pq
import pyarrow.fs as pafs
import json
from datetime import datetime, timedelta, timezone
from prometheus_client import Counter, Histogram
from .supported_types import ROS2_TYPE_NORMALIZATION, normalize_topic
from .extractors import extract_xyz
from .typed_extractors import extract_typed_rows, family_for_schema
from .cdr_decode import McapRos2Decoder, check_cdr_decoder_available
from .mcap_bounds import McapLimits, TerminalInputError, make_reader
from .claim_fence import (
    PROCESSING_CLAIM_SQL,
    ProcessingClaimLostError,
    canonical_attempt_id,
    lock_processing_claim,
    prune_processing_claims,
)
from . import parquet_sinks
from . import pro_stages
from .parquet_sinks import _HashingSink as _HashingSink
from .parquet_schema import (
    SCHEMA_VERSION,
    TYPED_SCHEMA_VERSION,
    column_names,
    file_metadata,
    parquet_prefix,
    schema_hash,
)
import psycopg


@dataclass
class ProcessorConfig:
    parquet_root: Path
    topics: List[str]


# After this many failed attempts, an upload is marked
# 'dead_letter' in processing_status and list_unprocessed_uploads stops
# re-selecting it -- so one poison upload can no longer halt the processor
# forever, but it also isn't retried indefinitely.
PROCESSOR_MAX_ATTEMPTS = max(
    1, int(os.environ.get("PROCESSOR_MAX_ATTEMPTS", "3"))
)

# A processing claim is a renewable lease, not a permanent flag.  The UUID is
# the fencing token: every completion/failure write must still own the exact
# attempt that selected the upload.  A crashed worker therefore becomes
# recoverable after the lease, while a slow/stale worker cannot acknowledge a
# newer replica's work when it eventually wakes up.
PROCESSOR_LEASE_SECONDS = max(
    30, int(os.environ.get("PROCESSOR_LEASE_SECONDS", "900"))
)
PROCESSOR_BATCH_LIMIT = max(
    1, int(os.environ.get("PROCESSOR_BATCH_LIMIT", "50"))
)
PROCESSOR_WORKER_ID = os.environ.get("PROCESSOR_WORKER_ID") or (
    f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
)

# Read-ahead window for MCAP objects streamed out of S3.
#
# PyArrow's S3 input file issues one HTTP range request per `read()`, and
# mcap's SeekingReader reads record headers a few bytes at a time, so an
# unbuffered handle turns one object into thousands of round trips.
# Measured on a 515 MiB MCAP in a loopback S3 object store (65,536 messages):
#
#   buffer   wall    range requests   bytes fetched vs object
#   none     13.5 s          11,336            -0.2 %
#    64 KiB   3.1 s           1,074            +0.0 %
#     1 MiB   2.4 s             518            +0.2 %
#     4 MiB   1.8 s             132            +0.8 %   <- chosen
#    16 MiB   1.7 s              36            +3.1 %
#
# 4 MiB is the knee: 16 MiB buys ~3 % more speed for 4x the window and
# four times the over-fetch. The over-fetch column is the reason to stop
# there rather than a mechanism argument -- a seek discards the buffer and
# the next small read refills it, while a read larger than the window
# bypasses it entirely, so the useful statement is the measured one: bytes
# fetched over bytes needed grows with the window.
# Peak memory OF THE READ is this window plus one decompressed chunk,
# whatever the object size (measured: growth rate 0.0024 against object size,
# versus 1.0018 when the whole object is read).
#
# Streaming alone does not bound the function's TOTAL peak. If every extracted
# row were accumulated in Python lists (ts/topic/x/y/z, then again per topic)
# and only converted to Arrow after the whole MCAP had been read, peak would
# track MESSAGE COUNT rather than object size. Measured that way, delta over
# baseline:
#
#   207.1 MB object,  12,800 messages ->   8.0 MiB   (657 B/msg)
#    17.5 MB object, 200,000 messages ->  81.2 MiB   (425 B/msg)
#
# i.e. an object 12x smaller cost 10x the memory, and a message-dense
# multi-GB bag could exhaust the container at roughly 400-650 bytes per
# message. So rows are buffered per-writer (one buffer per topic) and flushed as a Parquet
# row group every PARQUET_FLUSH_ROWS rows instead of once at the end -- see
# `_IncrementalParquetSink` below. Peak is bounded by PARQUET_FLUSH_ROWS x
# (distinct topics in the upload) x per-row cost, not by total message count.
MCAP_READ_BUFFER_BYTES = 4 * 1024 * 1024

# Row-group flush threshold for the incremental Parquet writer
# (`_IncrementalParquetSink`). Each writer -- one per topic -- buffers rows in
# Python lists and flushes (writes a row group and clears the buffer) once it
# holds this many rows, instead of only at the end.
#
# The constraint this trades off: bounded peak vs. healthy row groups.
#   - Bounded peak: at the ~425-650 B/row measured above for an
#     all-in-memory buffer, 50_000 rows caps ONE buffer around 20-33 MiB.
#     A single upload's peak is that, times (distinct topics) for the
#     buffers that are simultaneously mid-fill -- a small, upload-shape
#     bound instead of one that grows without limit with total message
#     count. A robot bag's distinct-topic count is bounded by what the
#     robot publishes (tens, typically), not by how long it ran.
#   - Healthy row groups: too small a threshold inflates per-row-group
#     metadata overhead and hurts scan/compression efficiency (Parquet
#     documents ~100K-1M rows or ~64-512 MiB per row group as the useful
#     range). 50_000 rows of this schema's five narrow columns lands
#     comfortably inside that range while still keeping the peak bound
#     above small.
PARQUET_FLUSH_ROWS = 50_000


class DecodeFailureError(Exception):
    """Raised when ~100% of attempted CDR decodes failed for an upload.
    run_once treats this like any other
    per-upload failure: it's recorded in processing_status and retried up
    to PROCESSOR_MAX_ATTEMPTS, instead of silently writing an all-null
    upload and marking it processed.
    """


class ProcessingStageError(RuntimeError):
    """A configured derived stage did not publish a complete generation."""

    def __init__(self, stage: str, detail: object):
        self.stage = str(stage)
        self.detail = detail
        super().__init__(f"{self.stage} stage incomplete: {detail}")


def _set_stage_status(
    stats: Optional[Dict[str, object]],
    stage: str,
    status: str,
    *,
    detail: Optional[object] = None,
) -> None:
    if stats is None:
        return
    stages = stats.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        stats["stages"] = stages
    entry: Dict[str, object] = {"status": status}
    if detail is not None:
        entry["detail"] = detail
    stages[stage] = entry


def list_unprocessed_uploads(
    limit: int = 50, *, exclude_upload_ids: Optional[List[str]] = None
) -> List[Tuple[object, ...]]:
    """Atomically claim completed uploads that still need processing.

    Reads Postgres via `_get_pg_conn_str()` and nothing else.

    Selection and the conditional `processing_status` transition happen in one
    statement; older-generation registry rows are then removed with fresh
    READ COMMITTED snapshots before the same transaction commits. Concurrent
    inserts/updates serialize on that table's unique
    `upload_id`; the `ON CONFLICT ... WHERE` predicate lets only a pending,
    failed, or expired lease transition to `processing`. This avoids taking an
    unnecessary row lock on `uploads` (and avoids granting the worker UPDATE on
    immutable intake metadata). A fresh UUID fencing token is returned with
    every row and is required by every registry and terminal write. Claim-time
    pruning is the authority handoff: there is no committed interval in which
    the new token is current while the predecessor's registry remains live.

    Only source-to-ingest SHA256-verified uploads are eligible. Also excludes
    uploads whose processing_status has reached 'dead_letter' or whose attempt
    count reached PROCESSOR_MAX_ATTEMPTS, so a poison upload is no longer
    re-selected forever.
    Expired final-attempt claims are marked dead_letter in the same statement,
    including when a killed worker could not record its own failure.

    Raises on any Postgres error instead of swallowing it into an empty
    list. An empty list would be indistinguishable from "nothing to
    do" and would hide a real query failure.
    """
    dsn = _get_pg_conn_str()
    if not dsn:
        # No database connection configured -- legitimately nothing to do.
        return []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                PROCESSING_CLAIM_SQL,
                (
                    PROCESSOR_MAX_ATTEMPTS,
                    list(exclude_upload_ids or []),
                    max(1, int(limit)),
                    PROCESSOR_WORKER_ID,
                    PROCESSOR_LEASE_SECONDS,
                    PROCESSOR_MAX_ATTEMPTS,
                ),
            )
            rows = list(cur.fetchall())
            prune_processing_claims(cur, (row[3] for row in rows))
            conn.commit()
            return [tuple(row) for row in rows]


# Prometheus metrics
PROCESS_DURATION = Histogram(
    "processor_duration_seconds",
    "Time spent processing a single upload",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)
PROCESSED_UPLOADS = Counter(
    "processor_processed_uploads_total",
    "Count of processed uploads",
)
PROCESSED_EMPTY_UPLOADS = Counter(
    "processor_processed_empty_uploads_total",
    "Uploads processed successfully but with zero supported/extracted rows "
    "(status='completed_empty' -- distinct from a failure so a future "
    "backfill can find these once the type catalog grows)",
)
PROCESSED_ROWS = Counter(
    "processor_processed_rows_total",
    "Number of rows written by the processor",
)
PROCESSOR_FAILURES = Counter(
    "processor_failures_total",
    "Processor failures by reason",
    ["reason"],
)
MESSAGES_READ = Counter(
    "processor_messages_read_total",
    "Total MCAP messages read from source files, before any filtering",
)
MESSAGES_SKIPPED_BY_SCHEMA = Counter(
    "processor_messages_skipped_by_schema_total",
    "Messages skipped (topic allowlist or unsupported schema), labeled by schema",
    ["schema"],
)
TYPED_ROWS_WRITTEN = Counter(
    "processor_typed_rows_total",
    "Rows written to the v2 typed-family Parquet files, labeled by family "
    "(these are ADDITIONAL to processor_processed_rows_total, "
    "which stays the v1 message count)",
    ["family"],
)
def _candidate_parts(
    candidate: Tuple[object, ...],
) -> Tuple[str, str, str, str, int]:
    """Split one claim row: upload, robot, storage path, attempt id, attempt number."""
    if len(candidate) != 5 or not candidate[3]:
        raise ValueError(f"invalid processor claim row: {candidate!r}")
    return (
        str(candidate[0]),
        str(candidate[1]),
        str(candidate[2]),
        str(candidate[3]),
        int(str(candidate[4])),
    )


class _ClaimHeartbeat:
    """Renew one claim in the background while MCAP/FFmpeg work blocks."""

    def __init__(self, upload_id: str, attempt_id: str):
        self.upload_id = upload_id
        self.attempt_id = attempt_id
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self):
        if _get_pg_conn_str():
            self._thread = threading.Thread(
                target=self._run,
                name=f"processor-lease-{self.upload_id[:12]}",
                daemon=True,
            )
            self._thread.start()
        return self

    def _run(self) -> None:
        interval = max(1.0, PROCESSOR_LEASE_SECONDS / 3.0)
        while not self._stop.wait(interval):
            try:
                owned = _pg_renew_claim(self.upload_id, str(self.attempt_id))
            except Exception as exc:
                print(
                    f"ERROR: failed to renew processing claim for "
                    f"upload {self.upload_id}: {type(exc).__name__}: {exc}"
                )
                self._lost.set()
                return
            if not owned:
                self._lost.set()
                return

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise ProcessingClaimLostError(
                f"processing claim for upload {self.upload_id} was lost"
            )

    def __exit__(self, *_exc) -> Literal[False]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return False


def _mark_terminal_stage(stats: Dict[str, object], error: Exception) -> None:
    stages = stats.setdefault("stages", {})
    if not isinstance(stages, dict):
        stages = {}
        stats["stages"] = stages
    stage = error.stage if isinstance(error, ProcessingStageError) else "pipeline"
    current = stages.get(stage)
    if not isinstance(current, dict):
        current = {"error": f"{type(error).__name__}: {error}"}
        stages[stage] = current
    current["status"] = "terminal_failure"


def _process_candidates(
    cfg: ProcessorConfig, candidates: List[Tuple[object, ...]]
) -> int:
    processed = 0
    for candidate in candidates:
        upload_id, robot_id, storage_path, attempt_id, attempt_number = (
            _candidate_parts(candidate)
        )
        t0 = time.time()
        stats: Dict[str, object] = {}
        try:
            with _ClaimHeartbeat(upload_id, attempt_id) as heartbeat:
                # A recovered attempt starts from an empty derived registry.
                # Object bytes are intentionally left for the reachability
                # GC: deleting them here would race an S3 reader and object
                # stores cannot join this transaction.
                _pg_prepare_claim(upload_id, attempt_id)
                rows_written = _extract_minimal_topics_to_parquet(
                    cfg,
                    upload_id,
                    robot_id,
                    storage_path,
                    attempt_id=attempt_id,
                    ensure_owned=heartbeat.ensure_owned,
                    stats=stats,
                )
                status = "completed" if rows_written > 0 else "completed_empty"

                heartbeat.ensure_owned()
                # Fenced completion publishes `processed` and the success
                # status in one Postgres transaction. A stale worker gets
                # zero rows from the ownership check and cannot ack.
                _pg_complete_claim(
                    upload_id,
                    attempt_id,
                    status=status,
                    rows_written=rows_written,
                    metadata=stats,
                )

                PROCESSED_UPLOADS.inc()
                PROCESSED_ROWS.inc(max(0, int(rows_written)))
                if status == "completed_empty":
                    PROCESSED_EMPTY_UPLOADS.inc()
                processed += 1
        except Exception as error:
            PROCESSOR_FAILURES.labels(reason=type(error).__name__).inc()
            # Input that breaks a hard limit fails the same way on every
            # retry, so it is dead-lettered at once instead of being re-run
            # until PROCESSOR_MAX_ATTEMPTS.
            terminal = isinstance(error, TerminalInputError)
            if terminal or attempt_number >= PROCESSOR_MAX_ATTEMPTS:
                _mark_terminal_stage(stats, error)
            try:
                _pg_record_failure(
                    upload_id,
                    f"{type(error).__name__}: {error}",
                    metadata=stats or None,
                    attempt_id=attempt_id,
                    terminal=terminal,
                )
            except Exception as record_err:
                # A lost token is expected when a stale worker is fenced. Any
                # other bookkeeping failure is equally non-successful and is
                # left for lease recovery; neither may abandon the batch.
                print(
                    f"ERROR: failed to record processing failure for "
                    f"upload {upload_id}: {record_err}"
                )
        finally:
            PROCESS_DURATION.observe(max(0.0, time.time() - t0))
    return processed


def run_once(cfg: ProcessorConfig) -> int:
    # Log clearly, once, if the dynamic ros2msg/CDR decoder is unavailable.
    # CDR records still count as failures below, so this never downgrades a
    # malformed runtime into silently-null observations.
    check_cdr_decoder_available()

    # Resolve (and thereby validate) the edition and the Pro stage config ONCE,
    # before any candidate is claimed. Left inside the per-upload path, a
    # config typo would raise on every candidate in turn, and each raise is
    # caught by _process_candidates' failure handler, which increments that
    # upload's attempt count -- so the whole backlog would silently
    # dead-letter. Failing here means a bad config can never consume a claim.
    stages = pro_stages.load()
    if stages is not None:
        stages.validate_config()

    # The MCAP memory limits are validated here for the same reason.
    McapLimits.from_env()

    if not _get_pg_conn_str():
        # Without Postgres there is nothing to claim.
        return 0

    # Claim only the upload about to run. Claiming 50 up front and processing
    # them sequentially would hoard work from healthy replicas and let the
    # tail of the batch expire before its turn.
    processed = 0
    attempted_upload_ids: List[str] = []
    for _ in range(PROCESSOR_BATCH_LIMIT):
        candidates = list_unprocessed_uploads(
            limit=1, exclude_upload_ids=list(attempted_upload_ids)
        )
        if not candidates:
            break
        attempted_upload_ids.append(str(candidates[0][0]))
        processed += _process_candidates(cfg, candidates)
    return processed


# Preserve core's public/test seams. In particular, tests patch
# `core._HashingSink` to force a short output write; every writer still uses
# that patched factory through this explicit adapter.
def _open_hashed_parquet_writer(
    path: str, schema, *, filesystem: Optional[pafs.S3FileSystem],
) -> Tuple[pq.ParquetWriter, _HashingSink]:
    return parquet_sinks._open_hashed_parquet_writer(
        path, schema, filesystem=filesystem, sink_factory=_HashingSink,
    )


class _IncrementalParquetSink(parquet_sinks._IncrementalParquetSink):
    def __init__(
        self, path: str, *, filesystem: Optional[pafs.S3FileSystem],
        metadata: Dict[bytes, bytes],
    ) -> None:
        super().__init__(
            path, filesystem=filesystem, metadata=metadata,
            flush_rows=PARQUET_FLUSH_ROWS,
            writer_factory=_open_hashed_parquet_writer,
        )


class _TypedParquetSink(parquet_sinks._TypedParquetSink):
    def __init__(
        self, path: str, *, family: str,
        filesystem: Optional[pafs.S3FileSystem], metadata: Dict[bytes, bytes],
        version: int = TYPED_SCHEMA_VERSION,
    ) -> None:
        super().__init__(
            path, family=family, filesystem=filesystem, metadata=metadata,
            version=version, flush_rows=PARQUET_FLUSH_ROWS,
            writer_factory=_open_hashed_parquet_writer,
        )


_required_time_bounds = parquet_sinks._required_time_bounds
_required_sha256 = parquet_sinks._required_sha256


def _sanitize_path_segment(value: str) -> str:
    """Turn a normalized topic into ONE path segment.

    Normalized topics are hierarchical (`imu/linear_accel`, `gps/fix`), and
    v1 spends that hierarchy on directories. The v2 layout already spends
    its depth on `v{N}/{family}/{robot}/`, so the topic is flattened into a
    single segment instead -- keeping "one directory, one column set, one
    topic" true and the depth fixed no matter how deep a topic name goes.
    """
    flattened = (value or "").strip("/").replace("/", "__")
    return flattened or "_unknown"


def _attempt_parquet_path(
    root: str | Path,
    robot_id: str,
    attempt_id: str,
    *segments: str,
) -> str:
    """Build a generation-owned path below a Parquet schema root.

    ``_attempts`` is reserved, so generations never share a path. Readers
    find a generation only through its fenced registry row.
    """

    base = str(root).rstrip("/")
    suffix = "/".join(str(segment).strip("/") for segment in segments)
    return (
        f"{base}/{robot_id}/_attempts/{canonical_attempt_id(attempt_id)}/"
        f"{suffix}"
    )


def _extract_minimal_topics_to_parquet(
    cfg: ProcessorConfig,
    upload_id: str,
    robot_id: str,
    mcap_path_or_url: str | Path,
    *,
    attempt_id: str,
    ensure_owned: Optional[Callable[[], None]] = None,
    stats: Optional[Dict[str, object]] = None,
) -> int:
    """Extract minimal fields and write Parquet under the attempt's paths:
    - Per-topic: processed/parquet/{robot}/_attempts/{attempt}/{topic}/YYYY/MM/DD/rows-{upload}.parquet

    ...plus, for the message types that have a typed extractor, a second set
    of files in the v2 schema families:
    - Typed: processed/parquet/v2/{family}/{robot}/_attempts/{attempt}/{topic}/rows-{upload}.parquet

    Every path is owned by the processing attempt (`attempt_id`, the claim's
    fencing token). There is no combined `_all` copy: every row is in exactly
    one per-topic file.

    The typed files are additive. Every message still produces exactly one
    v1 row, and `rows_written`
    (the return value, `processed.rows_written`, and the empty/non-empty
    status decision) still counts v1 rows only -- a JointState message is
    one v1 row whether it carries 6 joints or 12. The typed files are
    registered in `parquet_files` with `schema_version=2`, which is what
    makes them reachable for GC (`tools/s3_gc.py` derives reachability from
    that table and is layout-agnostic by design) and findable for readers.

    The v1 layout hangs off `parquet_prefix()`, which is a function of the
    schema version: v1 keeps the unversioned root,
    v2+ get an explicit `v{N}/` segment. Every file carries its schema
    version + hash in its Parquet key-value metadata and is registered in
    the `parquet_files` table, so readers resolve paths through the
    registry rather than reconstructing this layout by string template.

    Raises FileNotFoundError if a local `storage_path` doesn't exist, and
    DecodeFailureError if ~100% of attempted CDR decodes failed. Neither is
    swallowed into `return 0`, which run_once would then mark processed
    forever.

    `stats`, if given a dict, is populated with messages_read,
    messages_skipped_by_schema, and decode attempt/failure counts so the
    caller can persist per-upload completeness bookkeeping. Returns
    rows_written (int), including 0 for a genuinely empty/
    all-unsupported-schema MCAP -- the caller distinguishes that
    ("completed_empty") from a failure using this return value.
    """
    generation = canonical_attempt_id(attempt_id)
    ownership_check = ensure_owned or (lambda: None)
    out_root = Path(parquet_prefix(str(cfg.parquet_root), SCHEMA_VERSION))
    use_s3 = _s3_enabled()
    s3fs = _s3_filesystem() if use_s3 else None
    if not use_s3:
        out_root.mkdir(parents=True, exist_ok=True)

    # One incremental sink per topic, created lazily on each destination's
    # first row (not eagerly here), so an upload with zero supported/extracted
    # rows creates no files at all. `opened_sinks` is the
    # cleanup list: if reading/decoding raises partway through a large
    # upload, some sinks may already have flushed real row groups to disk
    # or S3 (rows are written before the read finishes). Closing them on the way out
    # finalizes those files' footers -- required so the underlying
    # `pq.ParquetWriter` releases its file handle (and, on S3, so any
    # in-flight multipart upload is completed rather than abandoned) -- but
    # leaves an orphaned, unregistered object at that generation-specific
    # path. Nothing references it (the registry/dual-write calls below never
    # run on this path), and the age-bounded reachability GC collects it.
    per_topic_sinks: Dict[str, _IncrementalParquetSink] = {}
    # One v2 sink per (family, normalized topic). Multiple source channels
    # can share a bucket, so every typed row also retains its raw_topic.
    # Keep normalized topic/registry keys for existing query
    # discovery. Same cleanup list as v1 finalizes files on mid-read errors.
    typed_sinks: Dict[Tuple[str, str], _TypedParquetSink] = {}
    opened_sinks: List[Union[_IncrementalParquetSink, _TypedParquetSink]] = []

    def _dest_path_topic(topic: str, ts0_ns: int) -> str:
        # Partition date (UTC) comes from the topic's FIRST row, evaluated
        # when the sink is created.
        ts0_sec = ts0_ns // 1_000_000_000
        dt = datetime.fromtimestamp(ts0_sec, tz=timezone.utc)
        yyyy, mm, dd = f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"
        path = _attempt_parquet_path(
            _s3_parquet_root() if use_s3 else out_root,
            robot_id,
            generation,
            topic,
            yyyy,
            mm,
            dd,
            f"rows-{upload_id}.parquet",
        )
        if not use_s3:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return path

    def _dest_path_typed(family: str, topic: str) -> str:
        """`{root}/v2/{family}/{robot}/_attempts/{attempt}/{topic}/rows-{upload_id}.parquet`.

        No date partitioning: one upload writes one file per (family,
        topic), and the registry carries its `[start, end]` range, so the
        YYYY/MM/DD segments v1 uses to make a blind glob prunable buy
        nothing here -- the only reader of these files is the registry-
        driven one.

        Readers find these files through the `parquet_files` registry, never
        by globbing the layout.
        """
        segment = _sanitize_path_segment(topic)
        if use_s3:
            return _attempt_parquet_path(
                _s3_parquet_root(TYPED_SCHEMA_VERSION, family=family),
                robot_id,
                generation,
                segment,
                f"rows-{upload_id}.parquet",
            )
        typed_root = Path(
            parquet_prefix(str(cfg.parquet_root), TYPED_SCHEMA_VERSION, family=family)
        )
        path = _attempt_parquet_path(
            typed_root,
            robot_id,
            generation,
            segment,
            f"rows-{upload_id}.parquet",
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return path

    # Per-upload completeness counters, populated into `stats`
    # (and Prometheus) below, plus decode_stats, which flags/fails an
    # upload whose CDR-encoded messages are ~100% undecodable.
    messages_read = 0
    messages_skipped_by_schema: Dict[str, int] = {}
    decode_stats: Dict[str, int] = {"attempted": 0, "failed": 0}

    mcap_limits = McapLimits.from_env()

    # The Pro rider (episode segmentation, the media spool and QA) rides along
    # on this one pass instead of re-reading the MCAP afterwards; see
    # processor/pro_stages.py. It is None in edition core and when
    # segmentation is disabled, and core then records the disabled-path
    # stage statuses itself.
    stages = pro_stages.load()
    rider = None
    if stages is not None:
        rider = stages.begin_upload(
            pro_stages.StageContext(
                upload_id=upload_id,
                robot_id=robot_id,
                attempt_id=attempt_id,
                dsn=_get_pg_conn_str(),
                filesystem=s3fs,
                bucket=os.environ.get("S3_BUCKET"),
                stats=stats,
                ensure_owned=ownership_check,
                set_stage_status=_set_stage_status,
                stage_error=ProcessingStageError,
            )
        )

    # Process MCAP from local disk or S3 - keep file handle open during processing
    def process_mcap_data(file_handle):
        """Process MCAP data from an open file handle"""
        nonlocal messages_read
        # Bounded reader: chunk, message, definition and buffer sizes are
        # checked before memory is allocated (see processor/mcap_bounds.py).
        reader = make_reader(file_handle, limits=mcap_limits)
        # Schema IDs are local to this MCAP. Keep the factory and its generated
        # decoder cache local too so an ID reused by a later upload cannot be
        # decoded with a previous upload's ros2msg definition.
        cdr_decoder = McapRos2Decoder()
        supported_type_set = set(ROS2_TYPE_NORMALIZATION.keys())
        for schema, channel, message in reader.iter_messages():
            ownership_check()
            messages_read += 1
            raw_topic = channel.topic
            schema_name = schema.name if schema else ""
            # Normalization comes from `robot_vault_config.topics`
            # so the query service calls the SAME function when it needs to
            # match the `topic` values written below (for example Imu is
            # written as "imu/linear_accel", not a flat "imu").
            topic = normalize_topic(schema_name, raw_topic)
            ts_ns = int(message.log_time)

            # The rider's segmenter is fed BEFORE the allowlist/supported-type
            # filters below, on purpose: an episode boundary is a property of
            # the recording, not of which message types this build extracts.
            if rider is not None:
                rider.observe_raw(
                    log_time_ns=ts_ns,
                    raw_topic=raw_topic,
                    topic=topic,
                    schema_name=schema_name,
                    message_encoding=channel.message_encoding,
                    data=message.data,
                    cdr_decoder=cdr_decoder,
                    schema=schema,
                )

            # Allowlist behavior: if --topics provided, filter by normalized topic prefix
            if cfg.topics:
                if not any(topic.startswith(t) for t in cfg.topics):
                    messages_skipped_by_schema[schema_name or "unknown"] = (
                        messages_skipped_by_schema.get(schema_name or "unknown", 0) + 1
                    )
                    continue
            else:
                # Default behavior: only process messages with known/supported ROS 2 types
                if schema_name not in supported_type_set:
                    messages_skipped_by_schema[schema_name or "unknown"] = (
                        messages_skipped_by_schema.get(schema_name or "unknown", 0) + 1
                    )
                    continue

            # Extract x,y,z fields for charting
            x, y, z = extract_xyz(
                schema_name, raw_topic, channel.message_encoding, message.data,
                decode_stats=decode_stats,
                cdr_decoder=cdr_decoder,
                schema=schema,
            )

            # Incremental sinks instead of per-topic Python-list
            # accumulation -- each `add()` may flush a row
            # group and clear its buffer, so peak memory is bounded by
            # PARQUET_FLUSH_ROWS regardless of how many messages remain.
            topic_sink = per_topic_sinks.get(topic)
            if topic_sink is None:
                topic_sink = _IncrementalParquetSink(
                    _dest_path_topic(topic, ts_ns),
                    filesystem=s3fs,
                    metadata=file_metadata(
                        SCHEMA_VERSION, robot_id=robot_id, upload_id=upload_id,
                        topic=topic, kind="topic",
                    ),
                )
                per_topic_sinks[topic] = topic_sink
                opened_sinks.append(topic_sink)
            topic_sink.add(ts_ns, topic, x, y, z)

            # Typed families, strictly in addition to the v1
            # write above -- nothing here can change what they wrote. Most
            # message types have no typed extractor and cost one dict
            # lookup; the six that do produce one row per record inside
            # the message (per joint, per transform) rather than per
            # message.
            family = family_for_schema(schema_name)
            typed_rows = None
            if family is not None:
                typed_rows = extract_typed_rows(
                    schema_name, raw_topic, channel.message_encoding, message.data,
                    cdr_decoder=cdr_decoder, schema=schema,
                )
                if typed_rows:
                    typed_sink = typed_sinks.get((family, topic))
                    if typed_sink is None:
                        typed_sink = _TypedParquetSink(
                            _dest_path_typed(family, topic),
                            family=family,
                            filesystem=s3fs,
                            metadata=file_metadata(
                                TYPED_SCHEMA_VERSION, family=family,
                                robot_id=robot_id, upload_id=upload_id,
                                topic=topic, kind="typed",
                            ),
                        )
                        typed_sinks[(family, topic)] = typed_sink
                        opened_sinks.append(typed_sink)
                    for typed_row in typed_rows:
                        typed_sink.add(
                            ts_ns, topic, {**typed_row, "raw_topic": raw_topic}
                        )

            # The rider's media spool and QA collector are fed here, AFTER the
            # filters, like the Parquet writes: an mp4 or a QA report is a
            # derived output, so `--topics imu` scopes it too.
            if rider is not None:
                rider.observe_extracted(
                    log_time_ns=ts_ns,
                    raw_topic=raw_topic,
                    topic=topic,
                    schema_name=schema_name,
                    message_encoding=channel.message_encoding,
                    data=message.data,
                    cdr_decoder=cdr_decoder,
                    schema=schema,
                    xyz=(x, y, z),
                    family=family,
                    typed_rows=typed_rows,
                )

    # Everything from the read to the Pro stages runs inside one
    # try/finally so the rider's spool directory is removed on EVERY exit -- the two
    # early returns, the decode-failure raise, an S3 error mid-read. A spool
    # that outlives its upload is a disk leak that grows with traffic and is
    # invisible until the volume fills, which is precisely the class of bug a
    # `finally` exists for.
    try:
        try:
            # Open MCAP and process with proper file handle management
            if isinstance(mcap_path_or_url, Path):
                with mcap_path_or_url.open("rb") as f:
                    process_mcap_data(f)
            else:
                path_str = str(mcap_path_or_url)
                if path_str.startswith("s3://"):
                    # Parse s3://bucket/key and include bucket in the path for PyArrow
                    from urllib.parse import urlparse
                    parsed = urlparse(path_str)
                    bucket = parsed.netloc
                    key = parsed.path.lstrip("/")
                    fs_read = _s3_filesystem()
                    # PyArrow S3FileSystem expects bucket/key format
                    full_s3_path = f"{bucket}/{key}"
                    # Stream the object; do NOT read it whole.
                    #
                    # Reading it whole (`data = f.read()` into a BytesIO) makes
                    # peak RSS track object size byte for byte (measured growth
                    # rate 1.0018), so one 2 GB bag could OOM-kill the processor.
                    # `process_mcap_data()` consumes the reader fully while the
                    # handle is open, so no read-all is needed. The local-disk
                    # branch below streams the same way.
                    with fs_read.open_input_file(full_s3_path) as raw:
                        with io.BufferedReader(raw, buffer_size=MCAP_READ_BUFFER_BYTES) as f:
                            process_mcap_data(f)
                else:
                    p = Path(path_str)
                    if not p.exists():
                        # Do not `return 0`: run_once would mark that processed
                        # forever, and a missing object would be indistinguishable from a real,
                        # completed, empty upload. Raise so run_once records a
                        # failed attempt and retries instead.
                        raise FileNotFoundError(f"storage_path does not exist: {path_str}")
                    with p.open("rb") as f:
                        process_mcap_data(f)

            # If every attempted CDR decode failed,
            # flag/fail this upload instead of silently writing all-null rows.
            # Every attempted CDR decode is fail-closed, including a missing
            # embedded schema or dynamic-decoder dependency. The failure metric
            # and this ratio gate make a malformed MCAP/runtime visible rather
            # than publishing an all-null observation set.
            if (
                decode_stats["attempted"] > 0
                and decode_stats["failed"] == decode_stats["attempted"]
            ):
                raise DecodeFailureError(
                    f"upload {upload_id}: all {decode_stats['attempted']} "
                    f"CDR-encoded messages failed to decode"
                )
        except Exception:
            for sink in opened_sinks:
                try:
                    sink.close()
                except Exception:
                    pass  # best-effort: don't mask the original exception with a cleanup one
            raise

        MESSAGES_READ.inc(messages_read)
        for schema_name, count in messages_skipped_by_schema.items():
            MESSAGES_SKIPPED_BY_SCHEMA.labels(schema=schema_name).inc(count)

        if stats is not None:
            stats["messages_read"] = messages_read
            stats["messages_skipped_by_schema"] = messages_skipped_by_schema
            stats["decode_attempted"] = decode_stats["attempted"]
            stats["decode_failed"] = decode_stats["failed"]
            # Without a rider this upload yields no episodes; the rider
            # overwrites it with its own count in finish_read().
            stats["episodes"] = 0
        # Only after a clean read: a partially-read MCAP's final "episode" is
        # an artifact of the failure, and the upload is retried whole.
        if rider is not None:
            rider.finish_read()

        if not per_topic_sinks:
            # A genuinely empty MCAP, or one whose messages were all
            # unsupported-schema / filtered out. This is a real success (the
            # file was read fine), not a failure -- the caller (run_once)
            # records it as status='completed_empty' rather than a plain
            # 'completed', so it's distinguishable from a real failure and a
            # future backfill can find it once the type catalog grows. No sink
            # is created, so no file is written.
            #
            # The rider still records its (empty) episode list here, so a
            # reprocess that finds nothing clears a previous attempt's rows.
            if rider is not None:
                rider.publish_empty()
            if stats is not None:
                stats["rows_written"] = 0
            _set_stage_status(stats, "parquet", "completed_empty")
            _set_stage_status(
                stats,
                "segmentation",
                "completed" if rider is not None else "skipped",
                detail=None if rider is not None else "disabled",
            )
            _set_stage_status(stats, "media", "not_applicable", detail="no_rows")
            _set_stage_status(stats, "qa", "not_applicable", detail="no_rows")
            return 0

        # Each sink's close() flushes its remainder as the final row group and
        # finalizes the file. The column list and Arrow types come from
        # parquet_schema, and every file is stamped with its
        # schema version + hash so it stays self-describing even outside the
        # registry (see `_IncrementalParquetSink.__init__`).
        total_rows = 0
        # Close + register each per-topic partition.
        for topic, topic_sink in per_topic_sinks.items():
            # Idempotency handled by Postgres constraints
            topic_sink.close()
            ownership_check()
            total_rows += topic_sink.row_count
            topic_ts_min, topic_ts_max = _required_time_bounds(topic_sink)
            _pg_register_parquet_file(
                upload_id=upload_id,
                robot_id=robot_id,
                message_type=topic,
                kind="topic",
                file_path=topic_sink.path,
                row_count=topic_sink.row_count,
                file_size_bytes=_written_file_size(topic_sink.path, s3fs),
                sha256=_required_sha256(topic_sink),
                ts_min_ns=topic_ts_min,
                ts_max_ns=topic_ts_max,
                all_null_cols=topic_sink.all_null_columns,
                attempt_id=attempt_id,
            )
            # Simple checksum for the idempotency registry (ts + count + sums),
            # accumulated incrementally in the sink rather than recomputed from
            # a full column list that no longer exists post-flush -- see
            # `_IncrementalParquetSink.add()` for why this reproduces the exact
            # same float64 sums a post-hoc `sum(...)` over the full column gives.
            h = hashlib.sha256()
            h.update(str(topic_sink.row_count).encode())
            h.update(str(topic_sink.ts_first).encode())
            h.update(str(topic_sink.ts_last).encode())
            for s in (topic_sink.sum_x, topic_sink.sum_y, topic_sink.sum_z):
                h.update(str(s).encode())
            checksum = h.hexdigest()
            _pg_dualwrite_processed_topic(
                upload_id,
                topic,
                topic_sink.row_count,
                checksum,
                topic_sink.path,
                attempt_id=attempt_id,
            )

        # Close + register the v2 typed files, after every v1 write, so the v1
        # path (and the order of its Postgres statements) does not depend on
        # the typed families.
        #
        # They are registered with `kind='topic'` -- the schema's CHECK
        # allows only 'topic' -- but under a RESERVED message_type
        # namespace, `_v2/{family}/{topic}`. That is what keeps them out of v1
        # reads: `query_service/registry.py:list_files` selects on
        # `message_type = %s OR message_type LIKE '{topic}/%'`, and no
        # normalized topic bucket starts with `_`, so a query for `joint_state` cannot match
        # `_v2/joint_state/joint_state` and pull rows of a different shape,
        # for the same messages, into a v1 aggregate. `schema_version` and
        # `column_names` tell a reader that asks for them exactly what the
        # file holds.
        typed_rows_by_family: Dict[str, int] = {}
        for (family, topic), typed_sink in typed_sinks.items():
            typed_sink.close()
            ownership_check()
            typed_ts_min, typed_ts_max = _required_time_bounds(typed_sink)
            _pg_register_parquet_file(
                upload_id=upload_id,
                robot_id=robot_id,
                message_type=f"_v{TYPED_SCHEMA_VERSION}/{family}/{topic}",
                kind="topic",
                file_path=typed_sink.path,
                row_count=typed_sink.row_count,
                file_size_bytes=_written_file_size(typed_sink.path, s3fs),
                sha256=_required_sha256(typed_sink),
                ts_min_ns=typed_ts_min,
                ts_max_ns=typed_ts_max,
                all_null_cols=typed_sink.all_null_columns,
                version=TYPED_SCHEMA_VERSION,
                family=family,
                attempt_id=attempt_id,
            )
            TYPED_ROWS_WRITTEN.labels(family=family).inc(typed_sink.row_count)
            typed_rows_by_family[family] = (
                typed_rows_by_family.get(family, 0) + typed_sink.row_count
            )

        if stats is not None:
            stats["rows_written"] = total_rows
            stats["typed_rows_written"] = typed_rows_by_family
        _set_stage_status(stats, "parquet", "completed")

        if rider is None:
            _set_stage_status(
                stats, "segmentation", "skipped", detail="disabled"
            )
            _set_stage_status(
                stats, "media", "skipped", detail="segmentation_disabled"
            )
            _set_stage_status(
                stats, "qa", "skipped", detail="segmentation_disabled"
            )
            return total_rows

        # Last, after every Parquet file this upload produced is registered:
        # episodes, then media, then QA (which reads the spool before the
        # `finally` below removes it).
        rider.publish()
        return total_rows
    finally:
        if rider is not None:
            rider.cleanup()


def _s3_enabled() -> bool:
    return bool(os.environ.get("S3_BUCKET") and os.environ.get("S3_ENDPOINT") and os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))


def _s3_filesystem() -> pafs.S3FileSystem:
    endpoint = os.environ.get("S3_ENDPOINT", "")
    region = os.environ.get("S3_REGION", "us-east-1")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    use_ssl = os.environ.get("S3_SECURE", "false").lower() == "true"
    endpoint_clean = endpoint.replace("http://", "").replace("https://", "")
    return pafs.S3FileSystem(
        access_key=access_key,
        secret_key=secret_key,
        endpoint_override=endpoint_clean,
        scheme="https" if use_ssl else "http",
        region=region,
        # Path style (development/objectstore/CONTRACT.md section 3).
        force_virtual_addressing=False,
    )


def _s3_parquet_root(
    version: int = SCHEMA_VERSION, *, family: Optional[str] = None
) -> str:
    """Parquet root for `version`, in PyArrow's bucket/key format.

    The prefix is derived from the schema version by
    `parquet_schema.parquet_prefix()` -- v1 stays at the
    unversioned root (moving it would strand live objects outside
    `tools/s3_gc.py`'s reachability template), v2+ get a `v{N}/` segment.
    A v2 `family` adds its own segment under that.
    """
    bucket = os.environ.get("S3_BUCKET")
    return parquet_prefix(f"{bucket}/processed/parquet", version, family=family)


def _written_file_size(path: str, s3fs: Optional[pafs.S3FileSystem]) -> int:
    """Byte size of a Parquet file the processor has just finished writing.

    Used for `parquet_files.file_size_bytes`. `s3fs` is the filesystem the
    file was written through, or None for the local-disk path.
    """
    if s3fs is not None:
        return int(s3fs.get_file_info(path).size)
    return Path(path).stat().st_size


def _ns_to_datetime(ts_ns: int) -> datetime:
    """Convert an MCAP nanosecond log time to a UTC datetime.

    Split into whole seconds + microseconds rather than dividing by 1e9:
    a nanosecond epoch stamp needs ~19 significant digits, which float64
    cannot hold, so the naive division silently loses sub-second precision
    in the timestamp range this pipeline actually sees. Postgres TIMESTAMPTZ
    stores microseconds, so that is the resolution kept here.
    """
    ts_ns = int(ts_ns)
    seconds, remainder_ns = divmod(ts_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        microseconds=remainder_ns // 1000
    )


# ========== Postgres dual-write helpers (optional) ==========
def _get_pg_conn_str() -> Optional[str]:
    return os.environ.get("DATABASE_URL")


def _pg_renew_claim(upload_id: str, attempt_id: str) -> bool:
    """Extend an unexpired lease iff this worker still owns its token."""
    dsn = _get_pg_conn_str()
    if not dsn:
        return False
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE processing_status
                SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE upload_id = (
                    SELECT id FROM uploads WHERE client_upload_id=%s LIMIT 1
                )
                  AND status = 'processing'
                  AND attempt_id = %s::uuid
                  AND lease_expires_at > NOW()
                RETURNING upload_id
                """,
                (PROCESSOR_LEASE_SECONDS, upload_id, attempt_id),
            )
            owned = cur.fetchone() is not None
            conn.commit()
    return owned


def _pg_lock_claim(cur, upload_id: str, attempt_id: str, *, unexpired: bool) -> int:
    """Compatibility wrapper around the shared media/QA fencing primitive."""

    return lock_processing_claim(
        cur, upload_id, attempt_id, unexpired=unexpired
    )


def _pg_delete_attempt_registry(
    cur,
    upload_id: str,
    attempt_id: str,
    *,
    other_attempts: bool,
) -> None:
    """Remove only the selected generations' DB-visible output.

    The corresponding object bytes are deliberately not deleted here. They
    remain unreferenced and are collected by the registry-derived GC; deleting
    S3 bytes inline would create a non-transactional race with readers.

    Claim handoff removes rows whose token differs from the new owner. Failure
    cleanup removes rows whose token equals the failing owner. In neither mode
    can a stale caller delete its replacement's generation.
    """
    attempt_predicate = (
        "<> %(attempt_id)s::uuid"
        if other_attempts
        else "= %(attempt_id)s::uuid"
    )
    upload_subquery = (
        "SELECT id FROM uploads WHERE client_upload_id=%(upload_id)s LIMIT 1"
    )
    params = {"upload_id": upload_id, "attempt_id": attempt_id}
    cur.execute(
        f"""
        DELETE FROM qa_reports AS old
        WHERE old.attempt_id {attempt_predicate}
          AND (
            old.upload_id = ({upload_subquery})
            OR old.episode_id IN (
               SELECT episode_id FROM episodes
               WHERE upload_id = ({upload_subquery})
            )
          )
        """,
        params,
    )
    # Cascades remove episode-scoped media and any remaining episode QA rows.
    cur.execute(
        f"""
        DELETE FROM episodes
        WHERE upload_id = ({upload_subquery})
          AND attempt_id {attempt_predicate}
        """,
        params,
    )
    cur.execute(
        f"""
        DELETE FROM parquet_files
        WHERE upload_id = ({upload_subquery})
          AND attempt_id {attempt_predicate}
        """,
        params,
    )
    cur.execute(
        f"""
        DELETE FROM processed_topics
        WHERE upload_id = ({upload_subquery})
          AND attempt_id {attempt_predicate}
        """,
        params,
    )


def _pg_prepare_claim(upload_id: str, attempt_id: str) -> None:
    """Fence and clear any partial registry left by a crashed predecessor."""
    dsn = _get_pg_conn_str()
    if not dsn:
        return
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            _pg_lock_claim(cur, upload_id, attempt_id, unexpired=True)
            _pg_delete_attempt_registry(
                cur, upload_id, attempt_id, other_attempts=True
            )
            conn.commit()


def _pg_complete_claim(
    upload_id: str,
    attempt_id: str,
    *,
    status: str,
    rows_written: int,
    metadata: Optional[Dict[str, object]],
) -> None:
    """Atomically publish success for the still-owned, unexpired attempt."""
    if status not in {"completed", "completed_empty"}:
        raise ValueError(f"invalid processor success status: {status!r}")
    dsn = _get_pg_conn_str()
    if not dsn:
        return
    metadata_json = json.dumps(metadata) if metadata is not None else None
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            _pg_lock_claim(cur, upload_id, attempt_id, unexpired=True)
            # Re-run the handoff under the same lock that publishes the
            # authoritative marker. This is idempotent after claim-time
            # pruning and closes recovery paths that bypassed claim SQL.
            _pg_delete_attempt_registry(
                cur, upload_id, attempt_id, other_attempts=True
            )
            cur.execute(
                """
                INSERT INTO processed
                    (upload_id, completed_at, rows_written, attempt_id)
                VALUES (
                    (SELECT id FROM uploads WHERE client_upload_id=%s LIMIT 1),
                    NOW(), %s, %s::uuid
                )
                ON CONFLICT (upload_id) DO UPDATE SET
                    completed_at = EXCLUDED.completed_at,
                    rows_written = EXCLUDED.rows_written,
                    attempt_id = EXCLUDED.attempt_id
                """,
                (upload_id, int(rows_written), attempt_id),
            )
            cur.execute(
                """
                UPDATE processing_status
                SET status = %s,
                    completed_at = NOW(),
                    error_message = NULL,
                    metadata = %s::jsonb,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE upload_id = (
                    SELECT id FROM uploads WHERE client_upload_id=%s LIMIT 1
                )
                  AND status = 'processing'
                  AND attempt_id = %s::uuid
                RETURNING upload_id
                """,
                (status, metadata_json, upload_id, attempt_id),
            )
            if cur.fetchone() is None:
                raise ProcessingClaimLostError(
                    f"processing claim for upload {upload_id} changed before commit"
                )
            conn.commit()


def _pg_dualwrite_processed_topic(
    upload_id: str,
    topic: str,
    rows_written: int,
    checksum: str,
    parquet_path: str,
    *,
    attempt_id: str,
) -> None:
    """Insert/update the `processed_topics` row for this upload/topic.

    Raises on any Postgres error: a swallowed failure here is exactly the
    kind of silent loss the processor must never allow. run_once records the failed
    attempt instead of counting the upload processed.
    """
    dsn = _get_pg_conn_str()
    if not dsn:
        return
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            _pg_lock_claim(cur, upload_id, attempt_id, unexpired=True)
            cur.execute(
                """
                INSERT INTO processed_topics
                    (upload_id, topic, rows_written, checksum, parquet_path,
                     attempt_id)
                VALUES (
                    (SELECT id FROM uploads
                     WHERE client_upload_id=%s LIMIT 1),
                    %s, %s, %s, %s, %s::uuid
                )
                ON CONFLICT (upload_id, topic) DO UPDATE SET
                    rows_written = EXCLUDED.rows_written,
                    checksum = EXCLUDED.checksum,
                    parquet_path = EXCLUDED.parquet_path,
                    attempt_id = EXCLUDED.attempt_id
                """,
                (
                    upload_id,
                    topic,
                    int(rows_written),
                    checksum,
                    parquet_path,
                    attempt_id,
                ),
            )
            conn.commit()


def _pg_register_parquet_file(
    *,
    upload_id: str,
    robot_id: str,
    message_type: str,
    kind: str,
    file_path: str,
    row_count: int,
    file_size_bytes: int,
    ts_min_ns: int,
    ts_max_ns: int,
    all_null_cols: List[str],
    version: int = SCHEMA_VERSION,
    family: Optional[str] = None,
    sha256: str,
    attempt_id: str,
) -> None:
    """Register one written Parquet file in `parquet_files`.

    `parquet_files` is the description of the lake. Every file the
    processor writes -- each per-topic partition and each typed file, both
    kind='topic' -- lands here with its path,
    robot, topic, timestamp range, row count, schema version/hash and the
    columns that are entirely NULL in it, which is what lets the query
    service resolve read paths from Postgres instead of globbing.

    Keyed on `file_path` (UNIQUE), so a repeat inside one attempt updates in
    place. A replacement attempt uses a disjoint path and claim-time handoff
    prunes the predecessor's row without overwriting its object bytes.

    `family` selects one of the v2 typed schemas for the
    recorded `schema_hash`/`column_names`, so a typed file's registry row
    describes the shape it actually has instead of v1's x/y/z. Defaulting
    to None keeps every v1 call site identical.

    `sha256` is the CONTENT digest, teed out of the write
    itself by `_HashingSink` -- not to be confused with `schema_hash`, which
    describes the file's column layout and says nothing about its bytes. It
    is required; every call site passes the closed sink's digest.

    Raises on any Postgres error, exactly like the `processed_topics` write
    above: run_once treats a failed
    bookkeeping write as a failed attempt and retries, rather than marking
    an upload processed whose files nothing can find.
    """
    dsn = _get_pg_conn_str()
    if not dsn:
        return
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            _pg_lock_claim(cur, upload_id, attempt_id, unexpired=True)
            cur.execute(
                """
                INSERT INTO parquet_files
                    (file_path, robot_id, message_type, kind, upload_id,
                     start_timestamp, end_timestamp, row_count,
                     file_size_bytes, schema_version, schema_hash,
                     column_names, all_null_columns, sha256, attempt_id)
                VALUES (
                    %s, %s, %s, %s,
                    (SELECT id FROM uploads
                     WHERE client_upload_id=%s LIMIT 1),
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid
                )
                ON CONFLICT (file_path) DO UPDATE SET
                    robot_id = EXCLUDED.robot_id,
                    message_type = EXCLUDED.message_type,
                    kind = EXCLUDED.kind,
                    upload_id = EXCLUDED.upload_id,
                    start_timestamp = EXCLUDED.start_timestamp,
                    end_timestamp = EXCLUDED.end_timestamp,
                    row_count = EXCLUDED.row_count,
                    file_size_bytes = EXCLUDED.file_size_bytes,
                    schema_version = EXCLUDED.schema_version,
                    schema_hash = EXCLUDED.schema_hash,
                    column_names = EXCLUDED.column_names,
                    all_null_columns = EXCLUDED.all_null_columns,
                    sha256 = EXCLUDED.sha256,
                    attempt_id = EXCLUDED.attempt_id
                WHERE parquet_files.attempt_id = EXCLUDED.attempt_id
                RETURNING id
                """,
                (
                    file_path,
                    robot_id,
                    message_type,
                    kind,
                    upload_id,
                    _ns_to_datetime(ts_min_ns),
                    _ns_to_datetime(ts_max_ns),
                    int(row_count),
                    int(file_size_bytes),
                    int(version),
                    schema_hash(version, family),
                    column_names(version, family),
                    list(all_null_cols),
                    sha256,
                    attempt_id,
                ),
            )
            if cur.fetchone() is None:
                raise ProcessingClaimLostError(
                    f"processing attempt {attempt_id} cannot register {file_path}"
                )
            conn.commit()


def _pg_record_failure(
    upload_id: str,
    error_message: str,
    metadata: Optional[Dict[str, object]] = None,
    *,
    attempt_id: str,
    terminal: bool = False,
) -> int:
    """Record failure only for the attempt that still owns the claim.

    Attempts increment their counter at claim time, not here; the attempt
    UUID fences this transition and the same transaction removes any
    partially published registry rows.

    Marks the row 'dead_letter' once attempts reach PROCESSOR_MAX_ATTEMPTS so
    the claim query stops re-selecting it.
    ``terminal=True`` marks it 'dead_letter' at once: the input itself is
    unprocessable (for example an MCAP above the memory limits).

    Returns the attempts count after this call (0 if no DATABASE_URL is
    configured). Raises on any Postgres error -- callers must not swallow
    this silently.
    """
    dsn = _get_pg_conn_str()
    if not dsn:
        return 0

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # An expired owner may clean itself up if nobody reclaimed it
            # yet; once a newer claim changes attempt_id this lock returns
            # no row and the stale worker is fenced from both cleanup and
            # state mutation.
            attempts = _pg_lock_claim(
                cur, upload_id, attempt_id, unexpired=False
            )
            cleanup_error: Optional[str] = None
            cur.execute("SAVEPOINT processor_registry_cleanup")
            try:
                _pg_delete_attempt_registry(
                    cur, upload_id, attempt_id, other_attempts=False
                )
            except Exception as exc:
                # Cleanup is important, but it must not strand the row in
                # `processing` forever (for example, an external dataset
                # may have acquired a RESTRICT FK to a prematurely visible
                # episode). Roll back only cleanup, record the exact
                # boundary, and still make the attempt failed/terminal.
                cur.execute("ROLLBACK TO SAVEPOINT processor_registry_cleanup")
                cleanup_error = f"{type(exc).__name__}: {exc}"
            finally:
                cur.execute("RELEASE SAVEPOINT processor_registry_cleanup")

            failure_metadata = dict(metadata or {})
            if cleanup_error is not None:
                failure_metadata["registry_cleanup_failed"] = cleanup_error
            metadata_json = (
                json.dumps(failure_metadata) if failure_metadata else None
            )
            next_status = (
                "dead_letter"
                if terminal or attempts >= PROCESSOR_MAX_ATTEMPTS
                else "failed"
            )
            cur.execute(
                """
                UPDATE processing_status
                SET status = %s,
                    completed_at = NOW(),
                    error_message = %s,
                    metadata = %s::jsonb,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE upload_id = (
                    SELECT id FROM uploads
                    WHERE client_upload_id=%s LIMIT 1
                )
                  AND status = 'processing'
                  AND attempt_id = %s::uuid
                RETURNING attempts
                """,
                (
                    next_status,
                    error_message,
                    metadata_json,
                    upload_id,
                    attempt_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise ProcessingClaimLostError(
                    f"processing claim for upload {upload_id} changed before failure commit"
                )
            conn.commit()
    return attempts
