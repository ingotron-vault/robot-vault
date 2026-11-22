import logging
import os
import threading
import time
import psycopg
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Dict, Iterable, List, Optional, Tuple

import duckdb
from mcap.reader import make_reader
from io import BytesIO
import boto3
from botocore.config import Config as BotoConfig

from . import settings

if TYPE_CHECKING:
    from .planner import QueryCapacityLease

logger = logging.getLogger(__name__)

# The ONE canonical topic-normalization module, shared with the
# processor (which uses it to write `processed_topics.topic`) via the
# `robot-vault-config` package installed into both images.
from robot_vault_config.topics import normalize_topic

# S3 config for reading raw MCAP from object storage
_S3_ENDPOINT = os.environ.get("S3_ENDPOINT")
_S3_REGION = os.environ.get("S3_REGION", "us-east-1")
_S3_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
_S3_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
_S3_SECURE = os.environ.get("S3_SECURE", "false").lower() == "true"
_S3_BUCKET = os.environ.get("S3_PARQUET_BUCKET", os.environ.get("S3_BUCKET", ""))


def _positive_env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


_RAW_DETAIL_MAX_BYTES = _positive_env_int("UPLOAD_DETAIL_MCAP_MAX_BYTES", 64 * 1024 * 1024)
_RAW_DETAIL_MAX_MESSAGES = _positive_env_int("UPLOAD_DETAIL_MCAP_MAX_MESSAGES", 250_000)


class RawDetailUnavailable(Exception):
    """The optional on-demand raw breakdown could not be computed safely."""

def _s3_client_for_uploads():
    if not (_S3_ENDPOINT and _S3_ACCESS_KEY_ID and _S3_SECRET_ACCESS_KEY):
        return None
    cfg = BotoConfig(region_name=_S3_REGION, signature_version="s3v4", s3={"addressing_style": "path"})
    return boto3.client(
        "s3",
        endpoint_url=_S3_ENDPOINT,
        aws_access_key_id=_S3_ACCESS_KEY_ID,
        aws_secret_access_key=_S3_SECRET_ACCESS_KEY,
        use_ssl=_S3_SECURE,
        config=cfg,
    )


def _s3_key_from_path(path: str) -> str:
    """Convert a stored Parquet path into a bucket-relative object key.

    The `/api/v1/s3/presign`, `/api/v1/s3/download` and `/api/v1/s3/list`
    endpoints (s3api.py) operate on bucket-relative keys checked against an
    allowlisted prefix (`processed/parquet/...`), not full URIs -- passing a
    fully-qualified path through 400s on the `invalid_key_prefix` check.

    Two stored forms have to be handled, because they genuinely both occur:
    `s3://bucket/key` and the scheme-less `bucket/key` the processor actually
    writes into
    `processed_topics.parquet_path` and `parquet_files.file_path`
    (`_s3_parquet_root()` returns `f"{bucket}/processed/parquet"`). Stripping
    only the first would make every Downloads link in the uploads UI resolve
    to `robotics-data/processed/parquet/...` and fail with 400.
    """
    if not path:
        return ""
    if path.startswith("s3://"):
        _, _, key = path[len("s3://"):].partition("/")
        return key
    if _S3_BUCKET and path.startswith(f"{_S3_BUCKET}/"):
        return path[len(_S3_BUCKET) + 1:]
    return path


def _duckdb_parquet_path(path: str) -> str:
    """Turn the processor's bucket/key form into a DuckDB S3 URI.

    The registry also contains full S3 URIs and, in local development, real
    filesystem paths. Only the configured bucket's scheme-less form is an S3
    object; leaving local paths alone keeps those deployments working.
    """
    if _S3_BUCKET and path.startswith(f"{_S3_BUCKET}/"):
        return f"s3://{path}"
    return path


def _open_duckdb_for_uploads(
    capacity_lease: Optional["QueryCapacityLease"] = None,
) -> "duckdb.DuckDBPyConnection":
    """Open the same bounded DuckDB connection used by every query endpoint.

    The import stays local because ``query_api`` imports this module while it
    also imports ``planner``. Planner does not import uploads, so resolving the
    helper at call time avoids initialization-order coupling while ensuring
    upload-detail scans cannot bypass the shared concurrency and memory caps.
    """
    from . import planner

    con = planner.open_duckdb(capacity_lease=capacity_lease)
    planner.label_query_connection(con, "uploads_detail")
    return con



def _compute_ts_range_from_parquet_paths(
    paths: List[str],
    capacity_lease: Optional["QueryCapacityLease"] = None,
    *,
    known_sizes_by_path: Optional[Dict[str, int]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Compute (ts_min, ts_max) across the given parquet file paths.

    `ts` may be stored either as an epoch-nanosecond BIGINT or a native
    TIMESTAMP (same normalization as `main.py:robot_message_stats`). Used
    to populate `get_upload_detail`'s `ts_min`/`ts_max` from the upload's
    own `processed_topics.parquet_path` files, since Postgres has no
    per-message timestamp column to query directly.

    Best-effort: returns (None, None) on any failure (no paths, files not
    yet visible, S3 not configured, etc.) rather than raising, so a
    transient read issue degrades to "Open in chart" staying disabled
    instead of breaking the whole upload-detail response.
    """
    clean_paths = [_duckdb_parquet_path(p) for p in paths if p]
    if not clean_paths:
        return None, None
    from . import planner

    normalized_sizes = {
        _duckdb_parquet_path(path): size
        for path, size in (known_sizes_by_path or {}).items()
    }
    planner.admit_explicit_source_paths(clean_paths, normalized_sizes)
    con = None
    try:
        con = _open_duckdb_for_uploads(capacity_lease)
        con.execute(
            """
            SELECT min(ts_ts), max(ts_ts) FROM (
                SELECT CASE
                    WHEN try_cast(ts AS BIGINT) IS NOT NULL THEN make_timestamp_ns(try_cast(ts AS BIGINT))
                    ELSE try_cast(ts AS TIMESTAMP_NS)
                END AS ts_ts
                FROM read_parquet(?)
            )
            """,
            [clean_paths],
        )
        row = con.fetchone()
        if not row or row[0] is None or row[1] is None:
            return None, None
        return settings.iso_utc(row[0]), settings.iso_utc(row[1])
    except Exception as exc:
        logger.warning(
            "Upload-detail timestamp scan failed (%s)",
            type(exc).__name__,
        )
        return None, None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception as exc:
                logger.warning(
                    "Could not close upload-detail DuckDB connection (%s)",
                    type(exc).__name__,
                )

def _scan_mcap_raw_bytes(
    stream: BinaryIO,
    expected_topics: Iterable[str],
) -> Tuple[Dict[str, int], int, Dict[str, Dict[str, int]]]:
    raw_bytes_by_topic: Dict[str, int] = {}
    total_raw_payload_sum = 0
    subtopics_by_topic: Dict[str, Dict[str, int]] = {}
    expected = set(expected_topics)
    reader = make_reader(stream)
    for index, (schema, channel, message) in enumerate(reader.iter_messages(), start=1):
        if index > _RAW_DETAIL_MAX_MESSAGES:
            raise RawDetailUnavailable("raw_detail_message_limit_exceeded")
        schema_name = schema.name if schema else ""
        raw_topic = channel.topic
        normalized_topic = normalize_topic(schema_name, raw_topic)
        if normalized_topic not in expected:
            continue
        message_size = len(message.data) if message.data else 0
        raw_bytes_by_topic[normalized_topic] = (
            raw_bytes_by_topic.get(normalized_topic, 0) + message_size
        )
        total_raw_payload_sum += message_size
        sources = subtopics_by_topic.setdefault(normalized_topic, {})
        sources[raw_topic] = sources.get(raw_topic, 0) + message_size
    return raw_bytes_by_topic, total_raw_payload_sum, subtopics_by_topic


def _calculate_raw_bytes_from_mcap(
    storage_path: str,
    expected_topics: Iterable[str],
) -> Tuple[Dict[str, int], int, Dict[str, Dict[str, int]]]:
    """
    Calculate raw bytes per topic by reading MCAP file with subtopic breakdown.
    Returns (raw_bytes_by_topic, total_raw_payload_sum, subtopics_by_topic)
    """
    # Normalization comes from the shared `robot_vault_config.topics`
    # module -- the SAME function the processor uses to write these topics.
    # A private copy of the map would drift (for example Imu is written as
    # "imu/linear_accel"/"imu/angular_vel", NavSatFix as "gps/fix") and would
    # never match `expected_topics` (which comes from `processed_topics`,
    # i.e. the processor's spelling).

    from urllib.parse import urlparse

    bucket = ""
    key = ""
    if storage_path.startswith("s3://"):
        parsed = urlparse(storage_path)
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
    elif _S3_BUCKET and storage_path.startswith(f"{_S3_BUCKET}/"):
        bucket, key = _S3_BUCKET, storage_path[len(_S3_BUCKET) + 1 :]

    if bucket and key:
        s3 = _s3_client_for_uploads()
        if not s3:
            raise RawDetailUnavailable("raw_detail_s3_not_configured")
        try:
            response = s3.get_object(Bucket=bucket, Key=key)
            size = response.get("ContentLength")
            if size is not None and int(size) > _RAW_DETAIL_MAX_BYTES:
                response["Body"].close()
                raise RawDetailUnavailable("raw_detail_byte_limit_exceeded")
            body = response["Body"]
            try:
                data = body.read(_RAW_DETAIL_MAX_BYTES + 1)
            finally:
                body.close()
        except RawDetailUnavailable:
            raise
        except Exception as exc:
            raise RawDetailUnavailable("raw_detail_s3_read_failed") from exc
        if len(data) > _RAW_DETAIL_MAX_BYTES:
            raise RawDetailUnavailable("raw_detail_byte_limit_exceeded")
        try:
            return _scan_mcap_raw_bytes(BytesIO(data), expected_topics)
        except RawDetailUnavailable:
            raise
        except Exception as exc:
            raise RawDetailUnavailable("raw_detail_parse_failed") from exc

    mcap_path = Path(storage_path)
    if not mcap_path.is_file():
        raise RawDetailUnavailable("raw_detail_file_not_found")
    if mcap_path.stat().st_size > _RAW_DETAIL_MAX_BYTES:
        raise RawDetailUnavailable("raw_detail_byte_limit_exceeded")
    try:
        with mcap_path.open("rb") as stream:
            return _scan_mcap_raw_bytes(stream, expected_topics)
    except RawDetailUnavailable:
        raise
    except Exception as exc:
        raise RawDetailUnavailable("raw_detail_parse_failed") from exc

# Configuration
DUCKDB_THREADS = int(os.environ.get("DUCKDB_THREADS", "2"))
DUCKDB_TEMP_DIRECTORY = os.environ.get("DUCKDB_TEMP_DIRECTORY")

DATABASE_URL = os.environ.get("DATABASE_URL")

def _parse_iso_ts(value: Optional[str]) -> Optional[datetime]:
    return settings.parse_iso_ts(value)

RECORDING_EVIDENCE_FIELDS = (
    "integrity_verified", "integrity_verification_basis", "upload_error", "upload_updated_at",
    "processing_status", "processing_error", "processing_attempts", "processing_started_at",
    "processing_completed_at", "processing_updated_at", "processing_lease_expires_at",
)


def read_recording_evidence(cur, upload_ids: List[str]) -> Dict[str, dict]:
    """Read registered verification and processing facts in one bounded batch.

    These fields describe database evidence, not a live object-store existence
    check. Neither this service nor the upload registry knows the robot's current
    local copy or whether its retention policy permits deletion.
    """
    if not upload_ids:
        return {}
    cur.execute(
        """
        SELECT u.id::text,
               u.integrity_verified, u.integrity_verification_basis,
               u.error_message, u.updated_at,
               ps.status, ps.error_message, ps.attempts, ps.started_at,
               ps.completed_at, ps.updated_at, ps.lease_expires_at
        FROM uploads u
        LEFT JOIN processing_status ps ON ps.upload_id = u.id
        WHERE u.id::text = ANY(%s)
        """,
        (upload_ids,),
    )
    result = {}
    timestamps = {"upload_updated_at", "processing_started_at", "processing_completed_at",
                  "processing_updated_at", "processing_lease_expires_at"}
    for row in cur.fetchall():
        evidence = {
            name: settings.iso_utc(value) if name in timestamps else value
            for name, value in zip(RECORDING_EVIDENCE_FIELDS, row[1:])
        }
        result[str(row[0])] = evidence
    return result


@dataclass
class UploadRow:
    upload_id: str
    robot_id: str
    original_filename: str
    storage_path: str
    file_size_bytes: int
    status: str
    completed_at: int
    bytes_received: int
    # Capture-metadata intake: operator-declared session
    # context, NULL in Postgres when the operator did not set it.
    operator: Optional[str] = None
    site: Optional[str] = None
    permission_ref: Optional[str] = None
    session_tags: Optional[str] = None
    task_label: Optional[str] = None
    recording_evidence: dict = field(default_factory=dict)

def read_index_page(
    robot_id: Optional[str],
    since: Optional[str],
    until: Optional[str],
    limit: int,
    offset: int,
    order_by: str,
    order_dir: str,
    include_in_progress: bool = False,
    q: Optional[str] = None,
    status: Optional[str] = None,
) -> Tuple[List[UploadRow], int]:
    """Returns a tuple of (rows, total_count) using validated sorting and pagination."""
    
    # Postgres-only mode
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return [], 0

    # Whitelist mapping for safe ORDER BY
    order_map = {
        "completed_at": "completed_at",
        "file_size_bytes": "file_size",
        "original_filename": "filename",
        "robot_id": "robot_id",
    }
    column = order_map.get(order_by)
    if column is None:
        raise ValueError(f"invalid_order_by: {order_by}")
    direction = (order_dir or "desc").lower()
    if direction not in ("asc", "desc"):
        raise ValueError(f"invalid_order_dir: {order_dir}")

    where: List[str] = []
    statuses: List[str] = []
    if status:
        statuses = [s.strip() for s in str(status).split(',') if s.strip()]
    if statuses:
        placeholders = ",".join(["?"] * len(statuses))
        where.append(f"status IN ({placeholders})")
    else:
        if include_in_progress:
            where.append("status IN ('completed','uploading')")
        else:
            where.append("status = 'completed'")
    params: List[object] = []
    if statuses:
        params.extend(statuses)
    if robot_id:
        where.append("robot_id = ?")
        params.append(robot_id)
    since_dt = _parse_iso_ts(since)
    until_dt = _parse_iso_ts(until)
    # These predicates must name REAL columns, not the SELECT list's aliases.
    # `completed_at` and `filename` are aliases (see base_select below);
    # Postgres does not resolve aliases in WHERE, and `sql_count` has no SELECT
    # list at all -- so a request carrying since/until/q would 500 with
    # UndefinedColumn.
    if since_dt is not None:
        where.append("upload_completed_at >= ?")
        params.append(since_dt)
    if until_dt is not None:
        where.append("upload_completed_at <= ?")
        params.append(until_dt)
    if q:
        where.append("(original_filename LIKE ? OR storage_path LIKE ?)")
        like = f"%{q}%"
        params.append(like)
        params.append(like)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    # Build SQL with deterministic tiebreakers
    order_sql = f" ORDER BY {column} {direction.upper()}, upload_id {'DESC' if direction=='desc' else 'ASC'}"
    # Capture-metadata intake: operator/site/permission_ref/
    # session_tags/task_label ride along at the end of the SELECT list so the
    # positional `r[N]` unpacking below stays a simple append, not a reindex
    # of every existing column.
    base_select = (
        "SELECT id::text as upload_id, robot_id, original_filename as filename, storage_path, file_size_bytes as file_size, status, EXTRACT(epoch FROM upload_completed_at)::int as completed_at, bytes_received, "
        "operator, site, permission_ref, session_tags, task_label FROM uploads"
    )
    sql_rows = base_select + where_clause + order_sql + " LIMIT ? OFFSET ?"
    sql_count = "SELECT COUNT(*) FROM uploads" + where_clause

    rows: List[UploadRow] = []
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_count.replace('?', '%s'), params)
                total_count = int(cur.fetchone()[0])
                params_rows = list(params)
                params_rows.extend([int(limit), int(offset)])
                cur.execute(sql_rows.replace('?', '%s'), params_rows)
                for r in cur.fetchall():
                    rows.append(
                        UploadRow(
                            upload_id=str(r[0]),
                            robot_id=str(r[1]),
                            original_filename=str(r[2]),
                            storage_path=str(r[3]),
                            file_size_bytes=int(r[4]),
                            status=str(r[5]),
                            completed_at=int(r[6] or 0),
                            bytes_received=int(r[7] or 0),
                            operator=r[8],
                            site=r[9],
                            permission_ref=r[10],
                            session_tags=r[11],
                            task_label=r[12],
                        )
                    )
                evidence = read_recording_evidence(cur, [row.upload_id for row in rows])
                for row in rows:
                    row.recording_evidence = evidence.get(row.upload_id, {})
        return rows, total_count
    except psycopg.errors.UndefinedTable:
        # `uploads` table not created yet (fresh/un-migrated DB) -- genuinely
        # no data, not a backend failure. Any other error (connection
        # refused, auth failure, DNS, bad DSN, etc.) is a real outage and
        # must propagate so callers can surface it as a 5xx instead of a
        # silently "empty but healthy" page.
        return [], 0

class TTLCache:
    def __init__(self, maxsize: int = 100, ttl_seconds: int = 60):
        self._data: OrderedDict[str, Tuple[float, object]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[object]:
        now = time.monotonic()
        with self._lock:
            if key in self._data:
                expires_at, value = self._data.pop(key)
                if expires_at >= now:
                    # refresh LRU order
                    self._data[key] = (expires_at, value)
                    return value
        return None

    def set(self, key: str, value: object) -> None:
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            if key in self._data:
                self._data.pop(key)
            self._data[key] = (expires_at, value)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

_detail_cache = TTLCache(maxsize=100, ttl_seconds=60)

def get_upload_detail(upload_id: str) -> Optional[dict]:
    """Get upload detail with processed topics from Postgres."""
    loaded = _load_upload_detail(upload_id)
    return loaded[0] if loaded is not None else None


def _load_upload_detail(upload_id: str) -> Optional[Tuple[dict, Dict[str, str]]]:
    """Return `(detail response, Parquet path by topic)`, or None if unknown.

    The paths stay out of the detail response. Only `get_upload_files` uses
    them, to build the bucket-relative download keys.
    """
    # Get basic upload info from Postgres
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    upload_info = None
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT robot_id, original_filename, storage_path, file_size_bytes, status,
                           EXTRACT(epoch FROM upload_completed_at)::int, bytes_received,
                           operator, site, permission_ref, session_tags, task_label, id::text AS resolved_upload_id
                    FROM uploads
                    WHERE id::text = %s OR client_upload_id = %s
                    LIMIT 1
                    """,
                    (upload_id, upload_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                upload_info = {
                    "upload_id": upload_id,
                    "robot_id": str(row[0]),
                    "original_filename": str(row[1]),
                    "storage_path": str(row[2]),
                    "file_size_bytes": int(row[3] or 0),
                    "status": str(row[4] or "unknown"),
                    "completed_at": int(row[5] or 0),
                    "raw_bytes": int(row[3] or 0),
                    "bytes_received": int(row[6] or 0),
                    # Capture-metadata intake: operator-declared session
                    # context; None in the DB (and here) for an upload that
                    # never had them set.
                    "operator": row[7],
                    "site": row[8],
                    "permission_ref": row[9],
                    "session_tags": row[10],
                    "task_label": row[11],
                }
                upload_info.update(dict.fromkeys(RECORDING_EVIDENCE_FIELDS))
                upload_info.update(read_recording_evidence(cur, [str(row[12])]).get(str(row[12]), {}))
    except psycopg.errors.UndefinedTable:
        # `uploads` table not created yet (fresh/un-migrated DB) -- treat
        # like "not found" rather than masking a real backend failure.
        # Any other error (connection refused, auth failure, DNS, bad DSN,
        # etc.) is a real outage and must propagate so the caller surfaces
        # a 5xx instead of a silent 404.
        return None
    
    if not upload_info:
        return None

    # Processing publishes its terminal success status and derived registry
    # rows in one transaction. Change the expensive cache only when a new
    # generation is published; heartbeats and failed retries can reuse the
    # same artifacts while the fresh lifecycle evidence is overlaid below.
    # In particular, a pre-processing empty response must never survive the
    # transition to completed Parquet.
    published_generation = (
        (
            upload_info["processing_status"],
            upload_info["processing_attempts"],
            upload_info["processing_completed_at"],
        )
        if upload_info["processing_status"] in {"completed", "completed_empty"}
        else None
    )
    cache_version = (
        upload_info["status"],
        upload_info["completed_at"],
        published_generation,
    )
    cache_key = f"{upload_id}|enhanced|{cache_version!r}"
    cached = _detail_cache.get(cache_key)
    if cached is not None:
        # No new artifact generation was published. Refresh lifecycle fields
        # without rescanning S3/MCAP.
        cached_detail, cached_paths = cached
        return {**cached_detail, **upload_info}, cached_paths

    # Get processed topics from Postgres if available
    topics_data = {}
    rows_total = 0
    files_total = 1
    
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        try:
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    # Get processed topics for this upload
                    cur.execute(
                        """
                        SELECT pt.topic, pt.rows_written, pt.parquet_path, pt.checksum,
                               pf.file_size_bytes AS parquet_bytes
                        FROM processed_topics pt
                        JOIN uploads u ON u.id = pt.upload_id
                        JOIN processed published
                          ON published.upload_id = pt.upload_id
                         AND published.attempt_id = pt.attempt_id
                        JOIN parquet_files pf
                          ON pf.upload_id = pt.upload_id
                         AND pf.file_path = pt.parquet_path
                         AND pf.attempt_id = pt.attempt_id
                        WHERE (u.id::text = %s OR u.client_upload_id = %s)
                        ORDER BY pt.topic
                        """,
                        (upload_id, upload_id),
                    )
                    
                    topics = cur.fetchall()
                    if topics:
                        for topic_row in topics:
                            topic = str(topic_row[0])
                            rows = int(topic_row[1] or 0)
                            parquet_path = str(topic_row[2] or "")
                            parquet_bytes = int(topic_row[4])
                            
                            topics_data[topic] = {
                                "rows": rows,
                                "parquet_path": parquet_path,
                                "topic": topic,
                                "parquet_bytes": parquet_bytes,
                            }
                            rows_total += rows
                        
                        files_total = len(topics)
                    
                    # The upload-level `processed` row gives a total when no
                    # per-topic rows exist.
                    cur.execute(
                        """
                        SELECT p.rows_written 
                        FROM processed p 
                        JOIN uploads u ON u.id = p.upload_id 
                        WHERE u.id::text = %s OR u.client_upload_id = %s
                        """,
                        (upload_id, upload_id),
                    )
                    
                    processed_row = cur.fetchone()
                    if processed_row and not topics_data:
                        # If no topics but there's a processed record, show summary
                        rows_total = int(processed_row[0] or 0)
                        
        except psycopg.errors.UndefinedTable:
            # A partially migrated development database can still show the
            # basic upload row. Genuine connectivity/query errors propagate;
            # they must not masquerade as an empty, healthy detail response.
            pass
    
    # Hold the same process-wide permit across both potentially memory-heavy
    # scans. The lease is lent to DuckDB below, so opening the Parquet reader
    # does not acquire a second permit and deadlock at concurrency=1.
    from . import planner

    capacity_lease = planner.acquire_query_capacity() if topics_data else None

    # Calculate raw bytes per topic by reading MCAP file with subtopic breakdown
    raw_bytes_by_topic = {}
    total_raw_payload_sum = 0
    subtopics_by_topic = {}
    raw_bytes_error = None
    raw_scan_complete = False
    
    try:
        if topics_data and upload_info["storage_path"]:
            try:
                raw_bytes_by_topic, total_raw_payload_sum, subtopics_by_topic = _calculate_raw_bytes_from_mcap(
                    upload_info["storage_path"], topics_data.keys()
                )
                raw_scan_complete = True
            except RawDetailUnavailable as exc:
                raw_bytes_error = str(exc)

        # Populate the real time range from the upload's own parquet files
        # (Postgres has no per-message ts column to query directly -- see
        # _compute_ts_range_from_parquet_paths). Best-effort: falls back to
        # (None, None) if there's no processed data yet or the read fails, in
        # which case the UI's "Open in chart" button stays disabled.
        ts_min = None
        ts_max = None
        if topics_data:
            parquet_paths = [info.get("parquet_path", "") for info in topics_data.values()]
            known_sizes_by_path: Dict[str, int] = {}
            for info in topics_data.values():
                path = str(info.get("parquet_path", "") or "")
                if path:
                    known_sizes_by_path[path] = int(info["parquet_bytes"])
            ts_min, ts_max = _compute_ts_range_from_parquet_paths(
                parquet_paths,
                capacity_lease,
                known_sizes_by_path=known_sizes_by_path,
            )
    finally:
        if capacity_lease is not None:
            capacity_lease.close()
    
    # Build topics array in format expected by UI with subtopic breakdown
    topics_array = []
    for topic_name, topic_info in topics_data.items():
        raw_bytes = raw_bytes_by_topic.get(topic_name, None)
        
        # Build sources array (original ROS topics) for expandable UI
        sources = []
        subtopic_data = subtopics_by_topic.get(topic_name, {})
        for original_topic, topic_bytes in subtopic_data.items():
            sources.append({
                "name": original_topic,
                "raw_bytes": topic_bytes
            })
        
        topics_array.append({
            "topic": topic_name,
            "rows": topic_info["rows"],
            "parquet_bytes": topic_info["parquet_bytes"],
            "raw_bytes": raw_bytes,  # Calculated from MCAP
            "sources": sources  # Original ROS topics with individual sizes
        })
    
    # Build enhanced detail response in UI-compatible format
    parquet_bytes_total = (
        sum(info["parquet_bytes"] for info in topics_data.values())
        if topics_data
        else None
    )

    detail = {
        **upload_info,
        "rows_total": rows_total,
        "files_total": files_total,
        "topics": topics_array,  # UI expects array of topic objects, not strings
        "ts_min": ts_min,  # UI expects these fields
        "ts_max": ts_max,
        "raw_payload_sum": total_raw_payload_sum,  # Total raw bytes across all topics
        # None when the upload has no published topic files.
        "parquet_bytes": parquet_bytes_total,
        "raw_bytes_mode": "mcap" if raw_scan_complete else (
            "error" if raw_bytes_error else "unavailable"
        ),
        "raw_bytes_error": raw_bytes_error,
        "time_range": {
            "start": upload_info["completed_at"], 
            "end": upload_info["completed_at"]
        },
        # ReductStore-inspired enhancements
        "reductstore_enhanced": True,
        "labels": {
            "source": "postgres_only", 
            "enhanced": True,
            "topics_count": len(topics_data),
            "raw_bytes_calculated": total_raw_payload_sum > 0
        },
        "priority": "normal",
        "performance_optimized": True,
    }
    
    parquet_paths = {
        topic: str(info.get("parquet_path") or "")
        for topic, info in topics_data.items()
    }
    _detail_cache.set(cache_key, (detail, parquet_paths))
    return detail, parquet_paths

def get_upload_files(upload_id: str) -> Optional[dict]:
    """List the upload's per-topic Parquet files as download keys.

    `parquet_keys` maps each topic to bucket-relative S3 object keys (not
    `s3://` URIs). `Uploads/index.tsx` passes them to `/api/v1/s3/presign`,
    which accepts only keys under the `processed/parquet/` prefix
    (`s3api.py:ALLOWED_PREFIXES`).
    """
    loaded = _load_upload_detail(upload_id)
    if loaded is None:
        return None
    _detail, parquet_paths = loaded

    parquet_keys: Dict[str, List[str]] = {}
    for topic, path in parquet_paths.items():
        key = _s3_key_from_path(path)
        if key:
            parquet_keys[topic] = [key]

    return {
        "upload_id": upload_id,
        "parquet_keys": parquet_keys,
    }
