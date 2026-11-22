"""Read endpoints: the fleet, the processed lake, and the uploads index.

Every Parquet read here goes through the planner, which prunes by robot,
topic, and date before opening an object.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, TypedDict

import duckdb
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import planner
from .. import sqlguard
from .. import registry as registry_mod
from .. import settings
from .. import uploads as uploads_mod
from ..auth import require_role
from ..middleware import (
    queries_total,
    query_duration,
    UPLOADS_DETAIL_DURATION_SECONDS,
    UPLOADS_DETAIL_FILES_BUCKETED,
    UPLOADS_DETAIL_REQUESTS_TOTAL,
    UPLOADS_DETAIL_ROWS_BUCKETED,
    UPLOADS_LIST_REQUESTS_TOTAL,
)
from ..time_index_cache import get_query_optimizer

logger = logging.getLogger(__name__)

router = APIRouter()

# The SQL console is an exploratory UI, not a bulk-export endpoint. Bound the
# result in the DuckDB plan itself (not only after fetchall) so a query without
# LIMIT cannot materialize an arbitrary number of Python rows in the service.
_SQL_CONSOLE_MAX_ROWS = 10_000


def _bounded_count_bucket(
    value: int,
    bounds=(0, 1, 10, 100, 1000, 10000, 100000),
) -> str:
    for bound in bounds:
        if value <= bound:
            return f"<={bound}"
    return f">{bounds[-1]}"

# These failures are produced while binding the already-validated caller SQL
# against the successfully-created all_parquet view.  They are permanent
# statement errors (unknown/ambiguous columns or functions), unlike
# IOException/OutOfMemoryException and setup failures, which remain a stable
# 503 because retrying after backend recovery can succeed.
_CONSOLE_USER_SQL_ERRORS = (
    duckdb.BinderException,
    duckdb.CatalogException,
)


class _ConsoleResultTooLarge(Exception):
    pass


class _ConsoleQueryInvalid(Exception):
    pass


class QueryLimitsResponse(BaseModel):
    max_source_range_seconds: float
    max_source_files: int
    max_source_rows: int
    max_source_bytes: int
    query_timeout_seconds: float
    max_concurrent_queries: int


def _execute_bounded_console_sql(con, sql: str):
    # Round-trip through the same parser used by the read-only gate. Merely
    # trimming a final semicolon leaves `; /* comment */` inside the subquery,
    # and an inline trailing `-- comment` can consume the wrapper itself.
    # Parser serialization removes those delimiters/comments without changing
    # strings containing comment markers or trying to duplicate SQL lexing.
    statement = con.execute(
        "SELECT json_deserialize_sql(json_serialize_sql(?))", [sql]
    ).fetchone()[0]
    bounded = (
        f"SELECT * FROM ({statement}) AS _robot_vault_console_result "
        f"LIMIT {_SQL_CONSOLE_MAX_ROWS + 1}"
    )
    result = con.execute(bounded)
    rows = result.fetchall()
    if len(rows) > _SQL_CONSOLE_MAX_ROWS:
        raise _ConsoleResultTooLarge
    return [description[0] for description in result.description], rows


def _execute_classified_console_sql(con, sql: str):
    """Turn only stable statement-shape failures into a caller error."""
    try:
        return _execute_bounded_console_sql(con, sql)
    except _CONSOLE_USER_SQL_ERRORS as exc:
        raise _ConsoleQueryInvalid from exc

# The two views every read endpoint builds on top of `t_raw`: `ts` normalized
# to a real TIMESTAMP whether the file stored it as one or as epoch
# nanoseconds. Factored out of the five endpoints that each had a verbatim
# copy, and kept in step with `planner.TS_EXPR` (the pushdown filter) so the
# two can never disagree about what `ts` means or lose nanoseconds to floats.
_T_NORM_SQL = """
CREATE VIEW t_norm AS
SELECT *, try_cast(ts AS BIGINT) AS ts_num, try_cast(ts AS TIMESTAMP_NS) AS ts_stamp
FROM t_raw;
"""
_T_SQL = """
CREATE VIEW t AS
SELECT *, CASE
    WHEN ts_num IS NOT NULL THEN make_timestamp_ns(ts_num)
    ELSE ts_stamp
END AS ts_ts
FROM t_norm;
"""


def _create_ts_views(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_T_NORM_SQL)
    con.execute(_T_SQL)


def _close_duckdb(con) -> None:
    if con is None:
        return
    try:
        con.close()
    except Exception as exc:
        logger.warning(
            "Could not close request DuckDB connection (%s)",
            type(exc).__name__,
        )


_JOINT_STATE_DERIVED_PREFIX = "_v2/joint_state/"
_JOINT_STATE_VALUE_COLUMNS = ("position", "velocity", "effort")


class _JointStateSeries(TypedDict):
    name: str
    field: str
    dimensions: Dict[str, str]
    points: List[Dict[str, object]]


def _joint_state_series(
    con: duckdb.DuckDBPyConnection,
    robot_id: Optional[str],
    topic: str,
    since_dt,
    until_dt,
    bucket: str,
) -> List[_JointStateSeries]:
    """Build chart series from the typed, long-form JointState derivative.

    The fixed ``x/y/z`` columns of a topic file cannot hold a JointState
    array. The processor writes JointState data as a typed v2 file instead,
    with one row per joint, registered under ``_v2/joint_state/<topic>``.
    Ordinary lake queries exclude derivatives to avoid double counting, so
    the time-series endpoint opts in here when the topic files give no
    numeric value.

    Only a JointState topic with a typed file gains ``series``. Scalar
    topics keep their response shape.
    """
    normalized_topic = topic.strip("/")
    if not normalized_topic:
        return []
    derived_topic = f"{_JOINT_STATE_DERIVED_PREFIX}{normalized_topic}"
    files = registry_mod.list_files(
        robot_id=robot_id,
        message_type=derived_topic,
        since=since_dt,
        until=until_dt,
        include_derived=True,
        limit=settings.QUERY_MAX_SOURCE_FILES + 1,
    )
    if not files:
        return []

    available = set(registry_mod.union_column_names(files))
    if "joint_name" not in available:
        return []
    value_columns = [c for c in _JOINT_STATE_VALUE_COLUMNS if c in available]
    if not value_columns:
        return []

    # Reuse the request's existing bounded connection. Opening another one
    # here deadlocks/fails at query concurrency=1 (and at the default once four
    # outer time-series requests are active), silently dropping typed data.
    query_plan = planner.QueryPlan(
        robot_id=robot_id,
        topic=derived_topic,
        since=since_dt,
        until=until_dt,
        files=files,
        uris=registry_mod.read_uris(files),
    )
    planner.create_lazy_parquet_view(
        con,
        query_plan,
        view="_joint_state_raw",
        replace=True,
    )

    where = ["(topic = ? OR topic LIKE ? ESCAPE '\\')"]
    params: List[object] = [normalized_topic, planner.topic_like_prefix(normalized_topic)]
    if since_dt:
        where.append("ts_ts >= make_timestamp_ns(?)")
        params.append(settings.datetime_to_epoch_ns(since_dt))
    if until_dt:
        where.append("ts_ts <= make_timestamp_ns(?)")
        params.append(settings.datetime_to_epoch_ns(until_dt))
    where_clause = " WHERE " + " AND ".join(where)
    aggregates = ", ".join(
        f"avg({planner.quote_identifier(c)}) AS {planner.quote_identifier(c)}"
        for c in value_columns
    )
    rows = con.execute(
        f"""
        SELECT date_trunc('{bucket}', ts_ts) AS bucket,
               joint_name, {aggregates}, count(*) AS n
        FROM (
            SELECT *, {planner.TS_EXPR} AS ts_ts
            FROM _joint_state_raw
        ) AS typed {where_clause}
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        params,
    ).fetchall()

    by_name: Dict[str, _JointStateSeries] = {}
    for row in rows:
        timestamp = settings.iso_utc(row[0])
        joint_name = str(row[1])
        count = int(row[len(value_columns) + 2])
        for index, field in enumerate(value_columns):
            value = row[index + 2]
            if value is None:
                continue
            name = f"{joint_name} · {field}"
            series = by_name.setdefault(
                name,
                {
                    "name": name,
                    "field": field,
                    "dimensions": {"joint_name": joint_name},
                    "points": [],
                },
            )
            series["points"].append({"t": timestamp, "value": value, "n": count})
    return [by_name[name] for name in sorted(by_name)]


# --- Fleet ------------------------------------------------------------------


@router.get("/api/robots")
def list_robots() -> List[str]:
    """The fleet, from the `robots` registry.

    Listing S3 prefixes under `processed/parquet/` would make "the fleet"
    whatever directories happen to exist: a robot that had uploaded but not
    been processed yet would be invisible, a robot with no data at all could
    not be listed, and a stray prefix would be indistinguishable from a real
    machine. The baseline schema makes `robots` the declared list, and ingest
    refuses uploads from ids that are not in it, so this is both the
    authoritative answer and a cheap one.

    Returns a bare list of ids -- the SPA types this
    endpoint as `string[]` in four places.

    Error-vs-empty: a database that has not run db/migrations yet
    has no `robots` table, which is genuinely "not bootstrapped" -> []. Any
    other Postgres failure is a real outage and propagates, because an outage
    must never be indistinguishable from an empty fleet.
    """
    dsn = registry_mod.registry_dsn()
    if not dsn:
        return []
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM robots ORDER BY id")
                return [str(r[0]) for r in cur.fetchall()]
    except psycopg.errors.UndefinedTable:
        return []


# --- Uploads ----------------------------------------------------------------


@router.get("/api/uploads/summary")
def uploads_summary(
    robot_id: Optional[str] = None,
    since: Optional[str] = Query(None, description="ISO8601 timestamp"),
    until: Optional[str] = Query(None, description="ISO8601 timestamp"),
):
    # Cache
    cache_key = f"uploads_summary:{robot_id}:{since}:{until}"
    cached = settings.cache_get(cache_key)
    if cached is not None:
        return cached

    # S3-only mode
    if not settings.s3_enabled():
        raise RuntimeError("S3 configuration required - local filesystem mode removed")
    since_dt = settings.parse_iso_ts(since)
    until_dt = settings.parse_iso_ts(until)
    planner.validate_source_range(since_dt, until_dt)
    start = time.perf_counter()
    con = None
    try:
        con = planner.open_duckdb()
        planner.label_query_connection(con, "uploads_summary")
        planner.create_t_raw(con, robot_id, since=since_dt, until=until_dt)
        _create_ts_views(con)
        where = []
        if since_dt:
            where.append(f"ts_ts >= make_timestamp_ns({settings.datetime_to_epoch_ns(since_dt)})")
        if until_dt:
            where.append(f"ts_ts <= make_timestamp_ns({settings.datetime_to_epoch_ns(until_dt)})")
        where_clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT date_trunc('day', ts_ts) AS day, count(*) AS n
            FROM t {where_clause}
            GROUP BY 1
            ORDER BY 1
        """
        rows = con.execute(sql).fetchall()
        queries_total.labels("uploads_summary", "success").inc()
        data = {"series": [{"day": settings.iso_utc(r[0]), "count": int(r[1])} for r in rows]}
        settings.cache_set(cache_key, data)
        return data
    except Exception:
        queries_total.labels("uploads_summary", "error").inc()
        raise
    finally:
        _close_duckdb(con)
        query_duration.labels("uploads_summary").observe(time.perf_counter() - start)


@router.get("/api/v1/uploads/list")
def uploads_list(
    robot_id: Optional[str] = None,
    since: Optional[str] = Query(None, description="ISO8601 timestamp"),
    until: Optional[str] = Query(None, description="ISO8601 timestamp"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order_by: str = Query("completed_at", description="completed_at|file_size_bytes|original_filename|robot_id"),
    order_dir: str = Query("desc", description="asc|desc"),
    include_in_progress: bool = Query(False, description="Include uploads with status=uploading"),
    q: Optional[str] = Query(None, description="search filename or storage path contains"),
    status: Optional[str] = Query(None, description="optional comma-separated list of statuses to include, e.g. 'completed,uploading'"),
):
    try:
        rows, total_count = uploads_mod.read_index_page(
            robot_id=robot_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir,
            include_in_progress=include_in_progress,
            q=(q.strip() if isinstance(q, str) else None),
            status=(status.strip() if isinstance(status, str) else None),
        )
        returned = len(rows)
        has_prev = offset > 0
        has_next = (offset + returned) < total_count
        next_offset = (offset + limit) if has_next else None
        prev_offset = (max(0, offset - limit)) if has_prev else None
        UPLOADS_LIST_REQUESTS_TOTAL.labels("success").inc()
        return {
            "items": [
                {
                    "upload_id": r.upload_id,
                    "robot_id": r.robot_id,
                    "original_filename": r.original_filename,
                    "storage_path": r.storage_path,
                    "file_size_bytes": r.file_size_bytes,
                    "status": r.status,
                    "completed_at": r.completed_at,
                    "bytes_received": getattr(r, "bytes_received", 0),
                    "operator": getattr(r, "operator", None),
                    "site": getattr(r, "site", None),
                    "permission_ref": getattr(r, "permission_ref", None),
                    "session_tags": getattr(r, "session_tags", None),
                    "task_label": getattr(r, "task_label", None),
                    **dict.fromkeys(uploads_mod.RECORDING_EVIDENCE_FIELDS),
                    **getattr(r, "recording_evidence", {}),
                }
                for r in rows
            ],
            "page": {
                "limit": int(limit),
                "offset": int(offset),
                "returned": int(returned),
                "total_count": int(total_count),
                "has_prev": bool(has_prev),
                "has_next": bool(has_next),
                "next_offset": next_offset,
                "prev_offset": prev_offset,
                "order_by": order_by,
                "order_dir": order_dir,
            },
        }
    except ValueError as ve:
        UPLOADS_LIST_REQUESTS_TOTAL.labels("error").inc()
        return PlainTextResponse(str(ve), status_code=400)
    except Exception as e:
        # Gracefully handle empty/missing index table as empty page
        if "no such table" in str(e).lower():
            UPLOADS_LIST_REQUESTS_TOTAL.labels("success").inc()
            return {
                "items": [],
                "page": {
                    "limit": int(limit),
                    "offset": int(offset),
                    "returned": 0,
                    "total_count": 0,
                    "has_prev": offset > 0,
                    "has_next": False,
                    "next_offset": None,
                    "prev_offset": 0 if offset > 0 else None,
                    "order_by": order_by,
                    "order_dir": order_dir,
                },
            }
        UPLOADS_LIST_REQUESTS_TOTAL.labels("error").inc()
        raise


@router.get("/api/v1/uploads/{upload_id}/detail")
def upload_detail(upload_id: str):
    start = time.perf_counter()
    try:
        detail = uploads_mod.get_upload_detail(upload_id)
        if detail is None:
            UPLOADS_DETAIL_REQUESTS_TOTAL.labels("not_found").inc()
            return PlainTextResponse("Not Found", status_code=404)
        UPLOADS_DETAIL_REQUESTS_TOTAL.labels("success").inc()
        # Bounded metrics
        try:
            rows_total = int(detail.get("rows_total", 0))
            files_total = int(detail.get("files_total", 0))

            UPLOADS_DETAIL_ROWS_BUCKETED.labels(
                bucket=_bounded_count_bucket(rows_total)
            ).inc()
            UPLOADS_DETAIL_FILES_BUCKETED.labels(
                bucket=_bounded_count_bucket(
                    files_total, bounds=(0, 1, 5, 10, 20, 50, 100)
                )
            ).inc()
        except Exception:
            pass
        return detail
    except Exception:
        UPLOADS_DETAIL_REQUESTS_TOTAL.labels("error").inc()
        raise
    finally:
        UPLOADS_DETAIL_DURATION_SECONDS.observe(time.perf_counter() - start)


@router.get("/api/v1/uploads/{upload_id}/files")
def upload_files(upload_id: str):
    data = uploads_mod.get_upload_files(upload_id)
    if data is None:
        return PlainTextResponse("Not Found", status_code=404)
    return data


# --- The lake ---------------------------------------------------------------


@router.get("/api/timeseries")
def timeseries(
    robot_id: Optional[str] = None,
    topic: str = Query("imu/linear_accel"),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    bucket: str = Query("minute", description="duckdb date_trunc unit: second|minute|hour|day"),
):
    bucket = planner.validate_bucket(bucket)
    cache_key = f"timeseries:{robot_id}:{topic}:{since}:{until}:{bucket}"
    cached = settings.cache_get(cache_key)
    if cached is not None:
        return cached

    # S3-only mode
    if not settings.s3_enabled():
        raise RuntimeError("S3 configuration required - local filesystem mode removed")
    since_dt = settings.parse_iso_ts(since)
    until_dt = settings.parse_iso_ts(until)
    planner.validate_source_range(since_dt, until_dt)
    start = time.perf_counter()
    con = None
    try:
        con = planner.open_duckdb()
        planner.label_query_connection(con, "timeseries")
        # robot_id=None means "every robot for this topic"; the registry
        # narrows on both, so the two cases collapse into one call.
        files = planner.create_t_raw(con, robot_id, topic=topic, since=since_dt, until=until_dt)
        _create_ts_views(con)
        where = []
        params: List[object] = []
        if since_dt:
            where.append("ts_ts >= make_timestamp_ns(?)")
            params.append(settings.datetime_to_epoch_ns(since_dt))
        if until_dt:
            where.append("ts_ts <= make_timestamp_ns(?)")
            params.append(settings.datetime_to_epoch_ns(until_dt))
        where_clause = (" WHERE " + " AND ".join(where)) if where else ""
        # The aggregated columns come from the registry's record
        # of what these files actually contain, not from a hardcoded
        # `avg(x), avg(y), avg(z)`. Column names are registry-managed
        # identifiers (written by the processor from `parquet_schema`),
        # never user input, so interpolating them here introduces no
        # injection surface -- unlike `bucket`, which is user-supplied and
        # is validated against _VALID_BUCKETS above because date_trunc's
        # unit argument cannot be passed as a bind parameter.
        # Quote via the shared helper, which also escapes: a column name
        # containing a double quote must not close the identifier early. The
        # names are registry-derived, so this is insurance.
        value_cols = planner.value_columns(files)
        agg_expr = ", ".join(
            f"avg({planner.quote_identifier(c)}) AS {planner.quote_identifier(c)}"
            for c in value_cols
        )
        sql = f"""
            SELECT date_trunc('{bucket}', ts_ts) AS bucket,
                   {agg_expr}, count(*) AS n
            FROM t {where_clause}
            GROUP BY 1
            ORDER BY 1
        """
        rows = con.execute(sql, params).fetchall()
        queries_total.labels("timeseries", "success").inc()
        points = []
        for r in rows:
            point: Dict[str, Any] = {"t": settings.iso_utc(r[0])}
            point.update({c: r[i + 1] for i, c in enumerate(value_cols)})
            point["n"] = int(r[len(value_cols) + 1])
            points.append(point)
        data = {
            "points": points,
            "columns": value_cols,
            # Columns that are 100% NULL in every matched file: most of the
            # allowlisted ROS 2 types still have no field extractor, so a
            # topic can have plenty of rows and no numeric payload at all.
            # Saying so beats letting a caller read a null average as zero.
            "all_null_columns": registry_mod.all_null_column_names(files),
        }
        # JointState does not fit the x/y/z columns. Its values are in the
        # processor's typed v2 file, one row per joint. Read that file when the
        # topic files give no numeric value, including when they give no rows
        # at all. A scalar topic always has numeric values, so it never
        # enters this branch and its response shape does not change.
        has_numeric_value = any(
            point.get(column) is not None
            for point in points
            for column in value_cols
        )
        if not has_numeric_value:
            try:
                series = _joint_state_series(
                    con,
                    robot_id,
                    topic,
                    since_dt,
                    until_dt,
                    bucket,
                )
            except Exception:
                # A stale typed registry row must not turn the still-valid
                # count response into a 500.
                logger.warning(
                    "unable to build typed JointState series for robot=%r topic=%r",
                    robot_id,
                    topic,
                    exc_info=True,
                )
            else:
                if series:
                    data["series"] = series
                    data["series_source"] = "typed_joint_state"
        settings.cache_set(cache_key, data)
        return data
    except Exception:
        queries_total.labels("timeseries", "error").inc()
        raise
    finally:
        _close_duckdb(con)
        query_duration.labels("timeseries").observe(time.perf_counter() - start)


@router.get("/api/v1/message-types")
def list_message_types(robot_id: Optional[str] = None) -> List[str]:
    cache_key = f"message_types:{robot_id}"
    cached = settings.cache_get(cache_key)
    if isinstance(cached, list) and all(isinstance(item, str) for item in cached):
        return cached

    start = time.perf_counter()
    try:
        # Answer straight from the `parquet_files` registry. No
        # Parquet file is opened.
        data = registry_mod.list_message_types(robot_id=robot_id)
        queries_total.labels("message_types", "success").inc()
        settings.cache_set(cache_key, data)
        return data
    except registry_mod.MetadataResultLimitExceeded as exc:
        queries_total.labels("message_types", "error").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "metadata_result_limit_exceeded",
                "field": exc.field,
                "max_results": exc.max_results,
            },
        ) from exc
    except Exception:
        queries_total.labels("message_types", "error").inc()
        raise
    finally:
        query_duration.labels("message_types").observe(time.perf_counter() - start)


@router.get("/api/v1/schema")
def describe_schema(robot_id: Optional[str] = None, topic: Optional[str] = None) -> Dict[str, object]:
    """Describe what the registry says the matching Parquet files contain.

    The point of a versioned schema is that a reader can find
    out what shape the data is in *before* querying it. This exposes the
    `parquet_files` answer directly -- schema versions/hashes in play, the
    union of column names, which of them are entirely NULL, file and row
    counts, and the covered timestamp range. No matching file gives zero
    counts, null bounds and the v1 column list.
    """
    try:
        summary = registry_mod.describe_schema(
            robot_id=robot_id,
            message_type=topic,
        )
    except registry_mod.MetadataResultLimitExceeded as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "metadata_result_limit_exceeded",
                "field": exc.field,
                "max_results": exc.max_results,
            },
        ) from exc
    return {
        "schema_versions": summary.schema_versions,
        "schema_hashes": summary.schema_hashes,
        "columns": summary.columns,
        "all_null_columns": summary.all_null_columns,
        "file_count": summary.file_count,
        "row_count": summary.row_count,
        "ts_min": settings.iso_utc(summary.ts_min),
        "ts_max": settings.iso_utc(summary.ts_max),
    }


@router.get("/api/v1/query/plan")
def describe_plan(
    robot_id: Optional[str] = None,
    topic: Optional[str] = None,
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
) -> Dict[str, object]:
    """What a read with these filters would actually open.

    The pruning layer decides this before touching S3, so exposing it costs
    one Postgres query and makes "why is this query slow / why is it empty"
    answerable without reading logs: `file_count` says how much the filters
    actually pruned, and `files` lists the keys.
    """
    return planner.plan(
        robot_id,
        topic,
        since=settings.parse_iso_ts(since),
        until=settings.parse_iso_ts(until),
    ).describe()


@router.get("/api/v1/query/limits", response_model=QueryLimitsResponse)
def query_limits(
    _user=Depends(require_role("viewer")),
) -> QueryLimitsResponse:
    """Active read-only admission limits for clients planning a query."""
    return QueryLimitsResponse(
        max_source_range_seconds=settings.QUERY_MAX_SOURCE_RANGE_SECONDS,
        max_source_files=settings.QUERY_MAX_SOURCE_FILES,
        max_source_rows=settings.QUERY_MAX_SOURCE_ROWS,
        max_source_bytes=settings.QUERY_MAX_SOURCE_BYTES,
        query_timeout_seconds=settings.DUCKDB_QUERY_TIMEOUT_SECONDS,
        max_concurrent_queries=settings.DUCKDB_QUERY_CONCURRENCY,
    )


@router.post("/api/v1/query/sql")
def run_sql(
    request: Dict[str, object], _user=Depends(require_role("viewer"))
) -> Dict[str, object]:
    # Auth gate. `main.create_app()` puts the same dynamic viewer floor on
    # every data-bearing router, so this explicit dependency is defense in
    # depth rather than the sole gate. It stays because this endpoint runs
    # caller-supplied SQL on a connection holding live S3 credentials:
    # unauthenticated ad-hoc SQL over the whole lake is a policy hole, not a
    # feature, and this route must not rely on router wiring elsewhere to
    # stay closed.
    #
    # Ordering is deliberate: `Depends` resolves before this function body
    # runs, so an unauthenticated (or under-privileged) caller gets 401/403
    # from `require_role` and never reaches `sqlguard` -- it must not be able
    # to learn whether its SQL would have been accepted. An authenticated
    # caller's rejected SQL still surfaces as sqlguard's own 400 below, not a
    # 401 -- auth and statement validation are different failures and must
    # stay distinguishable to a caller who has already cleared the gate.
    # `require_role` itself is a no-op when `settings.AUTH_MODE != "oidc"`
    # (the default), so this changes nothing for a stack with no identity
    # provider configured -- consistent with every other `require_role` call
    # site in this service (see fleet.py, admin_api.py).
    sql = request.get("sql") if isinstance(request, dict) else None
    if (
        not isinstance(sql, str)
        or not sql.strip()
        or len(sql) > sqlguard._MAX_SQL_LEN
    ):
        raise HTTPException(status_code=400, detail="invalid_sql")
    # Gate BEFORE any execution. This connection carries httpfs and live S3
    # credentials, so an unvalidated statement is a write path into the bucket
    # (`COPY ... TO 's3://...'`). See sqlguard for why this parses with DuckDB's
    # own parser instead of matching keywords.
    try:
        reason = sqlguard.validate_read_only_sql(sql)
    except Exception:
        # Fail CLOSED: an unexpected validator error must become a clean 400,
        # never a 500 that skips the gate.
        reason = "sql could not be validated"
    if reason is not None:
        raise HTTPException(status_code=400, detail=f"sql rejected: {reason}")
    start = time.perf_counter()
    con = None
    try:
        # S3 required
        if not settings.s3_enabled():
            raise RuntimeError("S3 configuration required - local filesystem mode removed")
        con = planner.open_duckdb(
            memory_limit_mb=settings.SQL_CONSOLE_MEMORY_LIMIT_MB,
        )
        planner.label_query_connection(con, "sql")
        # `all_parquet` is the console's entry point into the lake.
        # Back it with the registry's file list so it sees
        # exactly what the registry says exists. Keep
        # it lazy: creating the relation must not copy the entire lake before
        # the caller's statement has even been executed.
        # `open_duckdb` already limited this connection to the processed
        # Parquet prefix and locked its configuration (see
        # `planner.restrict_connection`), so a statement that slipped the gate
        # still cannot read local files or change the S3 credentials.
        planner.create_all_parquet_view(con)
        cols, rows = _execute_classified_console_sql(con, sql)
        queries_total.labels("sql", "success").inc()
        return {"columns": cols, "rows": rows}
    except _ConsoleResultTooLarge:
        queries_total.labels("sql", "error").inc()
        raise HTTPException(
            status_code=422,
            detail={
                "code": "result_limit_exceeded",
                "max_rows": _SQL_CONSOLE_MAX_ROWS,
            },
        )
    except _ConsoleQueryInvalid:
        queries_total.labels("sql", "error").inc()
        raise HTTPException(status_code=400, detail="query_invalid")
    except planner.QueryExecutionTimeout as exc:
        queries_total.labels("sql", "timeout").inc()
        raise HTTPException(
            status_code=504,
            detail={"code": "query_timeout"},
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        queries_total.labels("sql", "error").inc()
        logger.error("SQL console execution failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="query_unavailable") from exc
    finally:
        _close_duckdb(con)
        query_duration.labels("sql").observe(time.perf_counter() - start)


@router.get("/api/v1/robots/{robot_id}/data/{message_type:path}")
def robot_message_data(
    robot_id: str,
    message_type: str,
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    columns: Optional[str] = Query(None, description="comma-separated column list"),
    limit: int = Query(1000, ge=1, le=100000),
):
    """Time-range read, fronted by the in-process time-index cache.

    Measured: 3.1x on a 1h window, 1.6x on 24h, 1.0x on 7d/30d --
    the wider ranges are not admitted to the cache until the third
    identical request. See `time_index_cache`'s module docstring. The cache
    is consulted ONLY when both `since` and `until` are supplied.
    """
    # S3-only mode
    if not settings.s3_enabled():
        raise RuntimeError("S3 configuration required - local filesystem mode removed")

    since_dt = settings.parse_iso_ts(since)
    until_dt = settings.parse_iso_ts(until)
    planner.validate_source_range(since_dt, until_dt)
    selected_cols = [c.strip() for c in columns.split(",")] if columns else None

    # ReductStore-inspired optimization: try time-indexed cache first
    optimizer = get_query_optimizer()
    if optimizer and since and until:
        def fallback_query():
            return _execute_duckdb_time_range_query(robot_id, message_type, since_dt, until_dt, selected_cols, limit)

        result, was_cached = optimizer.optimize_time_range_query(
            robot_id, message_type, since, until, fallback_query,
            columns=selected_cols, limit=limit,
        )

        if was_cached:
            queries_total.labels("robot_data", "cached").inc()
        else:
            queries_total.labels("robot_data", "computed").inc()

        return result

    # Fallback to standard DuckDB query
    return _execute_duckdb_time_range_query(robot_id, message_type, since_dt, until_dt, selected_cols, limit)


def _execute_duckdb_time_range_query(
    robot_id: str,
    message_type: str,
    since_dt,
    until_dt,
    selected_cols: Optional[List[str]],
    limit: int,
):
    """Execute DuckDB query for time range data"""
    start = time.perf_counter()
    con = None
    try:
        con = planner.open_duckdb()
        planner.label_query_connection(con, "robot_data")
        files = planner.create_t_raw(con, robot_id, topic=message_type, since=since_dt, until=until_dt)
        _create_ts_views(con)
        # Match the topic itself OR any descendant, the same way the planner
        # selected the files. A request for `imu` loads imu/linear_accel's
        # files, so an exact row filter here would discard every one of them
        # and report zero for every parent topic.
        where = ["(topic = ? OR topic LIKE ? ESCAPE '\\')"]
        params: List[object] = [message_type, planner.topic_like_prefix(message_type)]
        if since_dt:
            where.append("ts_ts >= make_timestamp_ns(?)")
            params.append(settings.datetime_to_epoch_ns(since_dt))
        if until_dt:
            where.append("ts_ts <= make_timestamp_ns(?)")
            params.append(settings.datetime_to_epoch_ns(until_dt))
        where_clause = " WHERE " + " AND ".join(where)
        # `columns` is a caller-supplied query param interpolated into the
        # SELECT list, so it must be validated against what actually exists --
        # otherwise `?columns=*+FROM+t--` (or a subquery) is injected straight
        # into the statement, on a connection holding live S3 credentials.
        # Identifiers cannot be bound as parameters, so an allowlist is the fix.
        available = set(registry_mod.union_column_names(files)) | set(registry_mod.DEFAULT_COLUMN_NAMES)
        if selected_cols:
            unknown = [c for c in selected_cols if c not in available]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown column(s): {', '.join(sorted(unknown))}",
                )
        col_expr = (
            ", ".join(planner.quote_identifier(c) for c in selected_cols)
            if selected_cols
            else "*"
        )
        sql = f"SELECT {col_expr} FROM t {where_clause} LIMIT {int(limit)}"
        cur = con.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        queries_total.labels("robot_data", "success").inc()
        return {"columns": cols, "rows": rows}
    except Exception:
        queries_total.labels("robot_data", "error").inc()
        raise
    finally:
        _close_duckdb(con)
        query_duration.labels("robot_data").observe(time.perf_counter() - start)


@router.get("/api/v1/robots/{robot_id}/stats/{message_type:path}")
def robot_message_stats(
    robot_id: str,
    message_type: str,
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    bucket: Optional[str] = Query(None, description="optional date_trunc bucket: second|minute|hour|day"),
):
    if bucket is not None:
        bucket = planner.validate_bucket(bucket)
    # S3-only mode
    if not settings.s3_enabled():
        return {"stats": []}
    since_dt = settings.parse_iso_ts(since)
    until_dt = settings.parse_iso_ts(until)
    planner.validate_source_range(since_dt, until_dt)
    start = time.perf_counter()
    con = None
    try:
        con = planner.open_duckdb()
        planner.label_query_connection(con, "robot_stats")
        planner.create_t_raw(con, robot_id, topic=message_type, since=since_dt, until=until_dt)
        _create_ts_views(con)
        # Match the topic itself OR any descendant, the same way the planner
        # selected the files. A request for `imu` loads imu/linear_accel's
        # files, so an exact row filter here would discard every one of them
        # and report zero for every parent topic.
        where = ["(topic = ? OR topic LIKE ? ESCAPE '\\')"]
        params: List[object] = [message_type, planner.topic_like_prefix(message_type)]
        if since_dt:
            where.append("ts_ts >= make_timestamp_ns(?)")
            params.append(settings.datetime_to_epoch_ns(since_dt))
        if until_dt:
            where.append("ts_ts <= make_timestamp_ns(?)")
            params.append(settings.datetime_to_epoch_ns(until_dt))
        where_clause = " WHERE " + " AND ".join(where)
        if bucket:
            sql = f"""
                SELECT date_trunc('{bucket}', ts_ts) AS bucket,
                       count(*) AS n,
                       min(ts_ts) AS t_min,
                       max(ts_ts) AS t_max
                FROM t {where_clause}
                GROUP BY 1
                ORDER BY 1
            """
        else:
            sql = f"""
                SELECT count(*) AS n,
                       min(ts_ts) AS t_min,
                       max(ts_ts) AS t_max
                FROM t {where_clause}
            """
        cur = con.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        queries_total.labels("robot_stats", "success").inc()
        return {"columns": cols, "rows": rows}
    except Exception:
        queries_total.labels("robot_stats", "error").inc()
        raise
    finally:
        _close_duckdb(con)
        query_duration.labels("robot_stats").observe(time.perf_counter() - start)


@router.get("/api/v1/robots/{robot_id}/time-range")
def robot_time_range(robot_id: str):
    # The registry records each file's [start, end] at write time,
    # so this endpoint is a Postgres aggregate. It never opens Parquet.
    time_range = registry_mod.metadata_time_range(robot_id=robot_id)
    return {
        "t_min": settings.iso_utc(time_range.t_min),
        "t_max": settings.iso_utc(time_range.t_max),
    }


@router.get("/api/v1/cache/stats")
def cache_stats():
    """Get ReductStore-inspired cache performance statistics"""
    optimizer = get_query_optimizer()
    if optimizer:
        return optimizer.get_optimization_stats()
    return {"cache": {"enabled": False}, "message": "Time index cache not initialized"}
