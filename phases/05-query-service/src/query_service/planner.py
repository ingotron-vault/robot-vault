"""The key-pruning query layer.

Reading the whole `processed/parquet/` tree into an in-memory DuckDB table
and only then applying the caller's robot/topic/time filter would not scale.
The `parquet_files` registry prunes at the *file* level. This
module is where that pruning lives, together with the second half:

  * **file pruning** (from the registry): ask Postgres for the files that
    can possibly match this robot / topic / time window, and hand DuckDB that
    explicit key list;
  * **row pruning**: push the caller's time predicate INTO the
    `read_parquet` materialization, so a one-minute window over a month of
    data materializes a minute of rows instead of a month of them and then
    throwing 99.9% away;
  * **metadata-only answers**: a question the registry already
    knows the answer to -- "what is this robot's time range?" -- is answered
    from `parquet_files.start_timestamp/end_timestamp` without opening a
    single Parquet file.

Everything the endpoints need to read the lake goes through `plan()` and a
lazy Parquet view, so there is exactly one place that decides what gets read.

Behaviour carried over from the registry and deliberately preserved:
  - topic matching is by PREFIX (a parent topic matches its children), which
    is what `registry.list_files` implements;
  - the registry is the only source. An unreadable registered object fails
    the query, and an empty registry answer is an honest empty result.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, cast

import duckdb
from fastapi import HTTPException

from . import duckdb_local
from . import registry as registry_mod
from . import settings
from .middleware import (
    QUERY_ACTIVE,
    QUERY_ADMISSION_REJECTIONS_TOTAL,
    QUERY_TIMEOUTS_TOTAL,
)

logger = logging.getLogger(__name__)

# `date_trunc()`'s unit argument must be a SQL string literal -- DuckDB does
# not accept it as a bind parameter -- so the `bucket` query param can never
# be parameterized like `robot_id`/`topic` are. It must instead be checked
# against a fixed allowlist and rejected before any SQL string is built.
_VALID_BUCKETS = {"second", "minute", "hour", "day"}


class RegistryReadError(RuntimeError):
    """An authoritative registry entry could not be read."""


class QueryExecutionTimeout(RuntimeError):
    """DuckDB was interrupted after the configured request deadline."""


_QUERY_SLOTS = threading.BoundedSemaphore(settings.DUCKDB_QUERY_CONCURRENCY)


class QueryCapacityLease:
    """One process-wide query-capacity permit.

    A lease can be held across work that happens before or after DuckDB is
    opened (upload detail first scans MCAP, then Parquet).  At most one DuckDB
    connection may borrow it at a time.  Closing an enclosing lease while a
    connection is still alive defers the semaphore release until that
    connection closes, so misuse cannot silently make the capacity bound
    advisory.
    """

    def __init__(self, slot_pool: threading.BoundedSemaphore) -> None:
        self._slot_pool = slot_pool
        self._lock = threading.Lock()
        self._connection_borrowed = False
        self._close_requested = False
        self._released = False

    def __enter__(self) -> "QueryCapacityLease":
        with self._lock:
            if self._close_requested or self._released:
                raise RuntimeError("query capacity lease is closed")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        release_now = False
        with self._lock:
            if self._close_requested or self._released:
                return
            self._close_requested = True
            if not self._connection_borrowed:
                self._released = True
                release_now = True
        if release_now:
            self._slot_pool.release()
            QUERY_ACTIVE.dec()

    def _borrow_connection(self) -> None:
        with self._lock:
            if self._close_requested or self._released:
                raise RuntimeError("query capacity lease is closed")
            if self._connection_borrowed:
                raise RuntimeError(
                    "query capacity lease already has an open DuckDB connection"
                )
            self._connection_borrowed = True

    def _return_connection(self) -> None:
        release_now = False
        with self._lock:
            if not self._connection_borrowed:
                raise RuntimeError("query capacity lease has no borrowed connection")
            self._connection_borrowed = False
            if self._close_requested and not self._released:
                self._released = True
                release_now = True
        if release_now:
            self._slot_pool.release()
            QUERY_ACTIVE.dec()


def acquire_query_capacity() -> QueryCapacityLease:
    """Acquire one shared query slot or fail with the service's 503 contract."""
    slot_pool = _QUERY_SLOTS
    if not slot_pool.acquire(
        timeout=settings.DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS
    ):
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="capacity").inc()
        raise HTTPException(
            status_code=503,
            detail={"code": "query_capacity_exceeded"},
        )
    QUERY_ACTIVE.inc()
    return QueryCapacityLease(slot_pool)


class _ManagedDuckDBConnection:
    """Return a borrowed capacity lease when DuckDB closes."""

    def __init__(
        self,
        connection: duckdb.DuckDBPyConnection,
        capacity_lease: QueryCapacityLease,
        *,
        owns_capacity_lease: bool,
        timeout_seconds: float,
        query_type: str,
    ) -> None:
        self._connection = connection
        self._capacity_lease = capacity_lease
        self._owns_capacity_lease = owns_capacity_lease
        self._timeout_seconds = float(timeout_seconds)
        self._query_type = str(query_type or "unknown")
        self._state_lock = threading.Lock()
        self._closed = False
        self._deadline_expired = False
        self._timer = threading.Timer(self._timeout_seconds, self._expire_deadline)
        self._timer.daemon = True
        self._timer.start()

    @property
    def deadline_expired(self) -> bool:
        with self._state_lock:
            return self._deadline_expired

    def _expire_deadline(self) -> None:
        with self._state_lock:
            if self._closed or self._deadline_expired:
                return
            self._deadline_expired = True
            query_type = self._query_type
        QUERY_TIMEOUTS_TOTAL.labels(query_type=query_type).inc()
        # DuckDB documents interrupt() as callable from another thread. The
        # blocked worker unwinds with InterruptException and the route maps it
        # to the stable 504 contract instead of losing the worker process.
        try:
            self._connection.interrupt()
        except Exception:
            logger.warning("Could not interrupt expired DuckDB query", exc_info=True)

    def _call(self, method: str, *args, **kwargs):
        if self.deadline_expired:
            raise QueryExecutionTimeout(
                f"DuckDB query exceeded {self._timeout_seconds:g} seconds"
            )
        try:
            return getattr(self._connection, method)(*args, **kwargs)
        except Exception as exc:
            if self.deadline_expired:
                raise QueryExecutionTimeout(
                    f"DuckDB query exceeded {self._timeout_seconds:g} seconds"
                ) from exc
            raise

    def execute(self, *args, **kwargs):
        self._call("execute", *args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self._call("executemany", *args, **kwargs)
        return self

    def fetchall(self):
        return self._call("fetchall")

    def fetchone(self):
        return self._call("fetchone")

    def fetchmany(self, *args, **kwargs):
        return self._call("fetchmany", *args, **kwargs)

    def read_parquet(self, *args, **kwargs):
        return self._call("read_parquet", *args, **kwargs)

    def set_query_type(self, query_type: str) -> None:
        """Attach the route label without changing the historical open call.

        Several internal integrations wrap ``open_duckdb`` with the original
        keyword contract. Labelling immediately after opening keeps those
        wrappers compatible while still attributing deadline metrics.
        """
        with self._state_lock:
            self._query_type = str(query_type or "unknown")

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._timer.cancel()
        try:
            self._connection.close()
        finally:
            self._capacity_lease._return_connection()
            if self._owns_capacity_lease:
                self._capacity_lease.close()


def validate_bucket(bucket: str) -> str:
    """Reject any `bucket` value outside the fixed date_trunc allowlist.

    `bucket` is interpolated directly into `date_trunc('{bucket}', ...)`
    because DuckDB requires the unit as a literal. Without this check a
    crafted value could break out of the literal and inject arbitrary SQL
    against a DuckDB connection that holds S3-write credentials (e.g. a
    `COPY ... TO 's3://...'`). Call this before building any SQL that uses
    `bucket`.
    """
    if bucket not in _VALID_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid bucket '{bucket}': must be one of {sorted(_VALID_BUCKETS)}",
        )
    return bucket


# Set when the connection is created. `allow_persistent_secrets` cannot be
# changed after the secret manager is first used, so it must be here.
_CONNECT_HARDENING = {
    # A statement cannot write a secret to, or read one from, the disk.
    "allow_persistent_secrets": False,
    # A statement cannot make DuckDB fetch or load an extension it names.
    # `httpfs` is installed and loaded explicitly in `open_duckdb`.
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
}


def lake_root_uri() -> str:
    """The processed Parquet prefix, as a DuckDB S3 URI with no trailing slash."""
    scope = settings.S3_PREFIX_SCOPE or ""
    return f"s3://{settings.S3_PARQUET_BUCKET}/{scope}processed/parquet"


def restrict_connection(con: duckdb.DuckDBPyConnection) -> None:
    """Limit file access on `con` and lock its configuration.

    Call this after `httpfs` is loaded and the S3 secret exists, and before
    the connection runs any query.

    With S3 configured, the connection can open only objects under the
    processed Parquet prefix (`lake_root_uri()`). It cannot read local files
    (for example `/run/secrets/...` or `/etc/passwd`), other S3 prefixes,
    other buckets, or HTTP URLs. DuckDB's temporary directory stays usable,
    so large queries can still spill to disk.

    `allowed_directories` has no effect while `enable_external_access` is
    true, so the two settings are used together. Both were tested on DuckDB
    1.5.5 against the object store: Parquet reads under the prefix work, and every
    other path is refused with a PermissionException.

    Without S3 the service has no lake to read, so file access is left open.
    Only unit tests read local Parquet files in that mode.

    `lock_configuration` comes last. After it, no statement can change any
    setting, including the S3 credentials or the two settings above.
    """
    if settings.s3_enabled():
        con.execute(
            f"SET allowed_directories=[{sql_literal(lake_root_uri() + '/')}];"
        )
        con.execute("SET enable_external_access=false;")
    con.execute("SET lock_configuration=true;")


def open_duckdb(
    *,
    memory_limit_mb: Optional[int] = None,
    capacity_lease: Optional[QueryCapacityLease] = None,
    timeout_seconds: Optional[float] = None,
    query_type: str = "unknown",
) -> duckdb.DuckDBPyConnection:
    """Open one isolated in-memory DuckDB database.

    Ordinary endpoints use ``DUCKDB_MEMORY_LIMIT_MB``. The SQL console passes
    its separate, explicit cap. A process-wide semaphore bounds the number of
    independent DuckDB memory managers that can run at once.
    """
    if memory_limit_mb is None:
        memory_limit_mb = settings.DUCKDB_MEMORY_LIMIT_MB
    if isinstance(memory_limit_mb, bool) or not isinstance(memory_limit_mb, int):
        raise ValueError("memory_limit_mb must be a positive integer")
    if memory_limit_mb <= 0:
        raise ValueError("memory_limit_mb must be a positive integer")
    if timeout_seconds is None:
        timeout_seconds = settings.DUCKDB_QUERY_TIMEOUT_SECONDS
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    owns_capacity_lease = capacity_lease is None
    if capacity_lease is None:
        capacity_lease = acquire_query_capacity()
    capacity_lease._borrow_connection()
    raw_con: Optional[duckdb.DuckDBPyConnection] = None
    try:
        raw_con = duckdb.connect(
            database=":memory:",
            config={
                "memory_limit": f"{memory_limit_mb}MiB",
                **_CONNECT_HARDENING,
            },
        )
        # Architecture invariant: all timestamps are stored and compared as UTC.
        raw_con.execute("SET TimeZone='UTC';")
        if settings.DUCKDB_TEMP_DIRECTORY:
            raw_con.execute(f"PRAGMA temp_directory='{settings.DUCKDB_TEMP_DIRECTORY}';")
        raw_con.execute(f"PRAGMA threads={settings.DUCKDB_THREADS};")
        if settings.s3_enabled():
            endpoint = settings.S3_ENDPOINT
            access_key_id = settings.S3_ACCESS_KEY_ID
            secret_access_key = settings.S3_SECRET_ACCESS_KEY
            if endpoint is None or access_key_id is None or secret_access_key is None:
                raise RuntimeError("incomplete S3 configuration")
            duckdb_local.install_httpfs(raw_con)
            install_s3_credentials(
                raw_con,
                endpoint=endpoint,
                region=settings.S3_REGION,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
                secure=settings.S3_SECURE,
            )
        restrict_connection(raw_con)
        managed = _ManagedDuckDBConnection(
            raw_con,
            capacity_lease,
            owns_capacity_lease=owns_capacity_lease,
            timeout_seconds=timeout_seconds,
            query_type=query_type,
        )
        raw_con = None
    except Exception:
        try:
            if raw_con is not None:
                raw_con.close()
        finally:
            capacity_lease._return_connection()
            if owns_capacity_lease:
                capacity_lease.close()
        raise
    # The adapter forwards DuckDB's complete connection API and changes only
    # close/context-manager bookkeeping so the process-wide slot is released.
    # Keep that implementation detail at this construction boundary rather
    # than forcing every query helper to accept a second connection type.
    return cast(duckdb.DuckDBPyConnection, managed)


def label_query_connection(con: duckdb.DuckDBPyConnection, query_type: str) -> None:
    """Set a deadline metric label when ``con`` is our managed connection."""
    setter = getattr(con, "set_query_type", None)
    if callable(setter):
        setter(query_type)


def validate_source_range(
    since: Optional[datetime],
    until: Optional[datetime],
) -> None:
    """Reject reversed or over-wide explicit source windows before planning.

    Omitted bounds retain the existing interactive behaviour. Once a caller
    supplies both instants, the window becomes an admission claim and must fit
    the configured maximum. This check runs before Postgres planning, S3, and
    DuckDB capacity acquisition.
    """
    if since is None or until is None:
        return
    width_seconds = (until - since).total_seconds()
    if width_seconds < 0:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_range").inc()
        raise HTTPException(
            status_code=422,
            detail={"code": "source_range_reversed"},
        )
    if width_seconds > settings.QUERY_MAX_SOURCE_RANGE_SECONDS:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_range").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_range_too_wide",
                "max_seconds": settings.QUERY_MAX_SOURCE_RANGE_SECONDS,
            },
        )


def admit_source_plan(query_plan: "QueryPlan") -> None:
    """Bound the source set before DuckDB opens Parquet objects.

    The registry records the file count, row count and byte size of every
    planned file, so a plan can admit an omitted or one-sided time window
    when the actual source set is small.
    """
    validate_source_range(query_plan.since, query_plan.until)
    file_count = query_plan.file_count
    row_count = query_plan.row_count_estimate
    source_bytes = query_plan.source_bytes_estimate
    if file_count > settings.QUERY_MAX_SOURCE_FILES:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_files").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_file_limit_exceeded",
                "file_count": file_count,
                "max_files": settings.QUERY_MAX_SOURCE_FILES,
            },
        )
    if row_count > settings.QUERY_MAX_SOURCE_ROWS:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_rows").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_row_limit_exceeded",
                "row_count_estimate": row_count,
                "max_rows": settings.QUERY_MAX_SOURCE_ROWS,
            },
        )
    if source_bytes > settings.QUERY_MAX_SOURCE_BYTES:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_bytes").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_byte_limit_exceeded",
                "source_bytes": source_bytes,
                "max_bytes": settings.QUERY_MAX_SOURCE_BYTES,
            },
        )


def admit_explicit_source_paths(
    paths: List[str],
    known_sizes_by_path: Optional[dict[str, int]] = None,
) -> None:
    """Bound a caller that already has an explicit Parquet object list.

    Upload detail uses its own per-upload registry query rather than a
    ``QueryPlan``. Object count is always knowable; local/dev bytes are
    measured before DuckDB opens a file. Callers pass the remote object sizes
    they got from the registry.
    """
    distinct_paths = sorted({str(path) for path in paths if path})
    if len(distinct_paths) > settings.QUERY_MAX_SOURCE_FILES:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_files").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_file_limit_exceeded",
                "file_count": len(distinct_paths),
                "max_files": settings.QUERY_MAX_SOURCE_FILES,
            },
        )
    known_sizes_by_path = known_sizes_by_path or {}
    source_bytes = 0
    for path in distinct_paths:
        size: Optional[int]
        if "://" not in path:
            try:
                size = os.path.getsize(path)
            except OSError:
                # Let the existing registry-read error contract report a
                # missing file. Continue enforcing every other known size.
                size = None
        else:
            size = known_sizes_by_path.get(path)
        if size is not None:
            # Registry/object sizes are non-negative by contract. Clamp a
            # corrupt negative value so it can never reduce the safe subtotal.
            source_bytes += max(0, int(size))
        if source_bytes > settings.QUERY_MAX_SOURCE_BYTES:
            break
    if source_bytes > settings.QUERY_MAX_SOURCE_BYTES:
        QUERY_ADMISSION_REJECTIONS_TOTAL.labels(reason="source_bytes").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_byte_limit_exceeded",
                "source_bytes": source_bytes,
                "max_bytes": settings.QUERY_MAX_SOURCE_BYTES,
            },
        )


def quote_identifier(name: str) -> str:
    """Quote `name` as a SQL identifier, escaping embedded double quotes.

    Identifiers cannot be passed as bind parameters, so anywhere a
    name reaches SQL it has to be quoted rather than interpolated bare. The
    names this is applied to are registry-derived today, not caller input --
    quoting them is insurance against that changing, and it cannot break a
    legitimate column name.
    """
    return '"' + str(name).replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    """Quote `value` as a SQL string literal, escaping embedded quotes.

    These values are server-derived (env), not caller input, so this is
    hygiene rather than a fix for a live hole -- but a secret containing an
    apostrophe would otherwise produce a syntax error or, worse, change the
    statement's meaning.
    """
    return "'" + str(value).replace("'", "''") + "'"


def install_s3_credentials(
    con: duckdb.DuckDBPyConnection,
    *,
    endpoint: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    secure: bool,
) -> None:
    """Give `con` S3 access via a DuckDB SECRET rather than session settings.

    `SET s3_access_key_id=...` / `SET s3_secret_access_key=...` would put the
    credentials somewhere any query on the connection could read them back:

        SELECT current_setting('s3_secret_access_key')

    The SQL console (`POST /api/v1/query/sql`) executes caller-supplied SQL on
    exactly this kind of connection, and `sqlguard` also rejects such
    expressions. `SET lock_configuration=true` does not help on its own -- it
    blocks writing settings, not reading them.

    A SECRET is not readable through `current_setting`. `duckdb_secrets()`
    reports the SECRET value as `redacted` (though not the key id) and is a
    table function, which sqlguard rejects. S3 Parquet reads are unaffected:
    same rows, same code path.

    CREATING THE SECRET IS NOT SUFFICIENT ON ITS OWN, which is the whole
    reason the settings are explicitly blanked below. DuckDB's httpfs
    extension seeds `s3_access_key_id` / `s3_secret_access_key` from the
    `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables when
    it loads, and those are the variables this service reads its own
    credentials from (`settings.S3_SECRET_ACCESS_KEY`), set in the container
    by `configs/query.env`. So in the deployed configuration
    `current_setting('s3_secret_access_key')` would return the secret
    whether or not a SECRET existed. Blanking the two settings after the
    SECRET is in place closes it: the SECRET still serves every s3:// read,
    and `restrict_connection`'s `lock_configuration` then prevents a query
    putting anything back.

    Kept as one helper so that the connection setup here and in `uploads.py`
    cannot diverge.
    """
    endpoint_clean = endpoint.replace("http://", "").replace("https://", "")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET s3_lake (
            TYPE S3,
            KEY_ID {sql_literal(access_key_id)},
            SECRET {sql_literal(secret_access_key)},
            REGION {sql_literal(region)},
            ENDPOINT {sql_literal(endpoint_clean)},
            URL_STYLE 'path',
            USE_SSL {'true' if secure else 'false'}
        );
        """
    )
    # See the docstring: httpfs seeds these from AWS_* in the environment, so
    # they must be cleared explicitly or the credential stays readable.
    con.execute("SET s3_access_key_id='';")
    con.execute("SET s3_secret_access_key='';")


# The normalized-timestamp expression. The processor writes `ts` as Arrow's
# timezone-naive TIMESTAMP_NS, whose clock value is UTC by contract. Older or
# external producers can instead store epoch nanoseconds as an integer.
# Normalize both shapes to TIMESTAMP_NS rather than mixing TIMESTAMP with
# TIMESTAMPTZ: DuckDB resolves such a mixed comparison through the session
# timezone, which shifts the native Parquet timestamps on non-UTC hosts.
# `make_timestamp_ns` also preserves all integer nanoseconds (a `/1e9`
# conversion would pass through a floating-point epoch-seconds value).
TS_EXPR = (
    "CASE WHEN try_cast(ts AS BIGINT) IS NOT NULL "
    "THEN make_timestamp_ns(try_cast(ts AS BIGINT)) "
    "ELSE try_cast(ts AS TIMESTAMP_NS) END"
)

@dataclass
class QueryPlan:
    """What a query will read, decided before anything is opened.

    `files` is the registry's authoritative answer, possibly empty.
    """

    robot_id: Optional[str] = None
    topic: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    files: List[registry_mod.ParquetFile] = field(default_factory=list)
    uris: List[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def row_count_estimate(self) -> int:
        """Rows the registry says live in the planned files."""
        return sum(f.row_count for f in self.files)

    @property
    def source_bytes_estimate(self) -> int:
        """Recorded byte total of the planned files."""
        return sum(max(0, int(f.file_size_bytes)) for f in self.files)

    def describe(self) -> dict[str, object]:
        """JSON-safe summary, for `/api/v1/query/plan`."""
        return {
            "robot_id": self.robot_id,
            "topic": self.topic,
            "since": settings.iso_utc(self.since),
            "until": settings.iso_utc(self.until),
            "file_count": self.file_count,
            "row_count_estimate": self.row_count_estimate,
            "source_bytes_estimate": self.source_bytes_estimate,
            "files": [f.file_path for f in self.files][:100],
        }


def plan(
    robot_id: Optional[str] = None,
    topic: Optional[str] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> QueryPlan:
    """Decide which Parquet files this query needs, without opening any.

    Pruning happens here, in Postgres, against `parquet_files`: robot id and
    topic (by prefix) narrow the file set, and the timestamp range drops files
    whose recorded `[start, end]` cannot overlap the window.
    """
    files = registry_mod.list_files(
        robot_id=robot_id,
        message_type=topic,
        since=since,
        until=until,
        limit=settings.QUERY_MAX_SOURCE_FILES + 1,
    )
    return QueryPlan(
        robot_id=robot_id,
        topic=topic,
        since=since,
        until=until,
        files=files,
        uris=registry_mod.read_uris(files),
    )


def _pushdown_where(since: Optional[datetime], until: Optional[datetime]) -> str:
    """The row-level time predicate to push into `read_parquet`.

    Safe to apply because every caller that passes a window already applies
    the same UTC-instant predicate to the materialized relation afterwards.
    Rows whose `ts` does not parse compare NULL (false) in both paths. Doing
    it here just stops them being materialized first. The endpoint connection
    remains pinned to UTC for its display/bucketing operations; this lower
    layer does not rely on that mutable session setting for correctness.

    Bounds are inlined as exact epoch nanoseconds rather than bound
    parameters: DuckDB cannot mix a prepared parameter list with the
    file-list parameter in the same `read_parquet` call. They are integers
    derived from a parsed datetime, never caller-controlled text. Both the
    normalized column and `make_timestamp_ns(...)` bounds are TIMESTAMP_NS,
    so neither Python's host timezone nor DuckDB's session timezone can
    change the comparison.
    """
    clauses = []
    if since is not None:
        clauses.append(f"{TS_EXPR} >= make_timestamp_ns({settings.datetime_to_epoch_ns(since)})")
    if until is not None:
        clauses.append(f"{TS_EXPR} <= make_timestamp_ns({settings.datetime_to_epoch_ns(until)})")
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def create_lazy_parquet_view(
    con: duckdb.DuckDBPyConnection,
    query_plan: QueryPlan,
    *,
    view: str,
    replace: bool = False,
) -> List[registry_mod.ParquetFile]:
    """Expose a plan as a lazy DuckDB view without copying its rows.

    Ordinary endpoints and the SQL console both use this path so setup does not
    copy a whole time range before the final filter, aggregation, or limit runs.

    DuckDB cannot prepare ``CREATE VIEW ... read_parquet(?)``.  Its relation
    API accepts the same bound Python path/list values safely and registers a
    genuinely lazy view, avoiding path interpolation. It keeps two
    contracts: the registry file list is read with ``union_by_name=true``,
    and a plan that names no files gives a correctly-typed empty result.
    """
    admit_source_plan(query_plan)
    files = query_plan.files
    if not query_plan.uris:
        columns = ", ".join(
            f"CAST(NULL AS {registry_mod.DEFAULT_COLUMN_TYPES.get(name, 'DOUBLE')}) "
            f"AS {quote_identifier(name)}"
            for name in registry_mod.union_column_names(files)
        )
        replace_sql = " OR REPLACE" if replace else ""
        con.execute(
            f"CREATE{replace_sql} TEMP VIEW {quote_identifier(view)} AS "
            f"SELECT {columns} WHERE false;"
        )
        return files

    try:
        relation = con.read_parquet(query_plan.uris, union_by_name=True)
        where = _pushdown_where(query_plan.since, query_plan.until)
        if where:
            relation = relation.filter(where.removeprefix(" WHERE "))
        relation.create_view(view, replace=replace)
    except duckdb.IOException as exc:
        if bool(getattr(con, "deadline_expired", False)):
            raise QueryExecutionTimeout(
                "DuckDB query exceeded its execution deadline"
            ) from exc
        logger.error("authoritative Parquet registry read failed (%s)", type(exc).__name__)
        raise RegistryReadError(
            "an authoritative Parquet registry object is unavailable"
        ) from exc
    except Exception as exc:
        if bool(getattr(con, "deadline_expired", False)):
            raise QueryExecutionTimeout(
                "DuckDB query exceeded its execution deadline"
            ) from exc
        raise
    return files


def create_all_parquet_view(
    con: duckdb.DuckDBPyConnection,
) -> List[registry_mod.ParquetFile]:
    """Create the SQL console's lazy, registry-backed ``all_parquet`` view."""
    return create_lazy_parquet_view(con, plan(None, None), view="all_parquet")


def create_t_raw(
    con: duckdb.DuckDBPyConnection,
    robot_id: Optional[str] = None,
    topic: Optional[str] = None,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    table: str = "t_raw",
    pushdown_time: bool = True,
) -> List[registry_mod.ParquetFile]:
    """Build the planned lazy ``t_raw`` relation used by ordinary endpoints."""
    query_plan = plan(robot_id, topic, since=since, until=until)
    if not pushdown_time:
        query_plan.since = None
        query_plan.until = None
    return create_lazy_parquet_view(con, query_plan, view=table)


def topic_like_prefix(topic: str) -> str:
    """LIKE pattern matching every descendant of `topic`.

    Mirrors `registry._like_escape`, so a row filter selects exactly what the
    registry's file filter selected. Topics are hierarchical (`imu` is a parent
    of `imu/linear_accel`); an endpoint that prunes files by prefix and then
    filters rows by equality loads the right files and discards every row.
    """
    escaped = (topic or "").rstrip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/%"


def value_columns(files: List[registry_mod.ParquetFile]) -> List[str]:
    """Columns to aggregate over, from the registry's record of the files.

    No file gives the v1 value columns (x/y/z), which type the empty view.
    """
    return registry_mod.value_column_names(files)
