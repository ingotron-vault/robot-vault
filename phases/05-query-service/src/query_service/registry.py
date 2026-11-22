"""Reader-side view of the `parquet_files` registry.

The processor registers every file it writes in `parquet_files`: path,
robot, topic, timestamp range, row count, byte size, schema version/hash,
column names, and the columns that are entirely NULL. The registry is the
only source of truth for which files exist and what they contain. The
query service never lists the lake.

This module resolves a query's file list and column set. The endpoints
build their SQL from what it returns.

Error-vs-empty contract
-----------------------
No DATABASE_URL, or a database that has not run the migrations yet
(`UndefinedTable`), gives an empty result. `/api/robots` makes the same
choice. Real backend failures (connection refused, auth errors) propagate,
because an outage must never look the same as "no data".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

import psycopg

# The v1 column list. An empty result set is typed with it, so downstream SQL
# still compiles against zero rows. Every registered file carries its real
# column list in `parquet_files.column_names`.
# Source of truth: phases/04-processing-pipeline/src/processor/parquet_schema.py
DEFAULT_COLUMN_NAMES = ["ts", "topic", "x", "y", "z"]
STRUCTURAL_COLUMNS = ("ts", "topic")

# Metadata endpoints must not enumerate every source file merely to answer a
# schema or topic question.  Their SQL returns at most this many distinct
# values (plus one sentinel row used to produce a stable error) while aggregate
# counts and timestamp bounds remain constant-cardinality.
METADATA_VALUE_LIMIT = 10_000

# DuckDB types for DEFAULT_COLUMN_NAMES, used to declare an empty `t_raw`
# when the registry matched nothing (see main.py `_create_t_raw`).
DEFAULT_COLUMN_TYPES = {
    "ts": "TIMESTAMP",
    "topic": "VARCHAR",
    "x": "DOUBLE",
    "y": "DOUBLE",
    "z": "DOUBLE",
}


@dataclass(frozen=True)
class ParquetFile:
    """One row of `parquet_files`."""

    file_path: str
    robot_id: str
    message_type: str
    schema_version: int
    schema_hash: str
    row_count: int
    start_timestamp: datetime
    end_timestamp: datetime
    column_names: List[str]
    all_null_columns: List[str]
    file_size_bytes: int


@dataclass(frozen=True)
class RegistrySchemaSummary:
    """Constant-size registry aggregates plus bounded distinct metadata."""

    schema_versions: List[int]
    schema_hashes: List[str]
    columns: List[str]
    all_null_columns: List[str]
    file_count: int
    row_count: int
    ts_min: Optional[datetime]
    ts_max: Optional[datetime]


@dataclass(frozen=True)
class RegistryTimeRange:
    """The earliest start and latest end over the matching files."""

    t_min: Optional[datetime]
    t_max: Optional[datetime]


class MetadataResultLimitExceeded(ValueError):
    """A bounded metadata query found more distinct values than the API cap."""

    def __init__(self, field: str, max_results: int):
        super().__init__(f"{field} exceeds metadata result limit {max_results}")
        self.field = field
        self.max_results = max_results


def registry_dsn() -> Optional[str]:
    return os.environ.get("DATABASE_URL")


def _to_read_uri(file_path: str) -> str:
    """Turn a stored registry path into something DuckDB can read.

    The processor stores S3 paths in PyArrow's scheme-less `bucket/key`
    form (the same format `processed_topics.parquet_path` uses -- see
    `tools/s3_gc.py:normalize_object_key`), while DuckDB's httpfs wants an
    `s3://` URI. Absolute local paths (the processor's non-S3 mode) and
    paths that already carry a scheme are passed through untouched.
    """
    if file_path.startswith("s3://") or file_path.startswith("/"):
        return file_path
    return f"s3://{file_path}"


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Treat a naive query bound as UTC.

    `main.py:_parse_iso_ts` returns a naive datetime for an ISO string with
    no offset, and the rest of the read path already treats those as UTC
    (`to_timestamp(dt.timestamp())` against a UTC-clocked container). The
    registry's bounds are `TIMESTAMPTZ`, so a naive value would otherwise be
    resolved against the *database server's* timezone -- which could prune
    away a file that genuinely falls inside the window.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so a topic name is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _visibility_where(
    *,
    robot_id: Optional[str] = None,
    message_type: Optional[str] = None,
    include_derived: bool = False,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> tuple[List[str], List[object]]:
    """The authoritative publication/filter predicate shared by all reads."""

    # A file is published only when its attempt_id equals
    # processed.attempt_id for its upload. The caller inner-joins
    # `processed AS published`, so an unpublished attempt never leaks.
    where = ["file.attempt_id = published.attempt_id"]
    params: List[object] = []
    if not include_derived:
        where.append("message_type NOT LIKE %s ESCAPE '\\'")
        params.append(r"\_v%/%")
    if robot_id:
        where.append("robot_id = %s")
        params.append(robot_id)
    if message_type:
        where.append(
            "(message_type = %s OR message_type LIKE %s ESCAPE '\\')"
        )
        params.append(message_type)
        params.append(_like_escape(message_type.rstrip("/")) + "/%")
    if since is not None:
        where.append("end_timestamp >= %s")
        params.append(_as_utc(since))
    if until is not None:
        where.append("start_timestamp <= %s")
        params.append(_as_utc(until))
    return where, params


def list_files(
    *,
    robot_id: Optional[str] = None,
    message_type: Optional[str] = None,
    include_derived: bool = False,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    dsn: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[ParquetFile]:
    """Published files matching a query.

    Typed-family derivatives are excluded from ordinary queries. The
    processor registers v2 derivatives under the reserved
    ``_vN/<family>/<topic>`` message namespace. Without the
    namespace filter, an unscoped registry query reads both each v1 message
    and its expanded typed derivative, producing incorrect counts and a
    mixed-schema SQL console. A future derived-data API can opt in explicitly
    with ``include_derived=True``.

    `message_type` matches the topic ITSELF OR ANY CHILD of it. Topics form
    a hierarchy -- `_normalize_topic` puts `imu`, `imu/linear_accel` and
    `imu/angular_vel` in the same lake -- and the `{base}/{robot}/{topic}/**`
    glob this replaced matched every descendant. An equality match would
    silently return zero rows for `topic=imu`, which is a parent of the only
    topics the seed actually writes.

    `since`/`until` prune on the recorded timestamp range.
    """
    dsn = dsn or registry_dsn()
    if not dsn:
        return []

    # `processed.attempt_id` is the publication pointer. Attempt-owned rows
    # are invisible until the transaction that marks processing complete
    # installs it. A stale generation never leaks through this reader, even
    # if cleanup has not run yet.
    where, params = _visibility_where(
        robot_id=robot_id,
        message_type=message_type,
        include_derived=include_derived,
        since=since,
        until=until,
    )

    sql = (
        "SELECT file_path, robot_id, message_type, schema_version, schema_hash, "
        "row_count, start_timestamp, end_timestamp, column_names, all_null_columns, "
        "file_size_bytes "
        "FROM parquet_files AS file "
        "JOIN processed AS published ON published.upload_id = file.upload_id "
        "WHERE " + " AND ".join(where) + " ORDER BY file_path"
    )
    if limit is not None:
        if int(limit) <= 0:
            raise ValueError("limit must be a positive integer")
        sql += " LIMIT %s"
        params.append(int(limit))

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        # Fresh database that hasn't run db/migrations yet. Genuinely "not
        # bootstrapped", not an outage -- same narrow carve-out
        # `uploads.py` makes for the `uploads` table. Every
        # other psycopg error propagates.
        return []

    return [
        ParquetFile(
            file_path=row[0],
            robot_id=row[1],
            message_type=row[2],
            schema_version=int(row[3]),
            schema_hash=str(row[4]),
            row_count=int(row[5]),
            start_timestamp=row[6],
            end_timestamp=row[7],
            column_names=list(row[8]),
            all_null_columns=list(row[9]),
            file_size_bytes=int(row[10]),
        )
        for row in rows
    ]


def list_message_types(
    *,
    robot_id: Optional[str] = None,
    dsn: Optional[str] = None,
    max_results: int = METADATA_VALUE_LIMIT,
) -> List[str]:
    """Distinct published per-topic message types in the registry.

    This replaces a `SELECT DISTINCT topic FROM read_parquet(<glob>)` that
    had to open and scan every Parquet file in the lake to answer a
    question Postgres already knows.
    """
    dsn = dsn or registry_dsn()
    if not dsn:
        return []
    if int(max_results) <= 0:
        raise ValueError("max_results must be a positive integer")

    where, params = _visibility_where(robot_id=robot_id)
    sql = (
        "SELECT DISTINCT file.message_type "
        "FROM parquet_files AS file "
        "JOIN processed AS published ON published.upload_id = file.upload_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY file.message_type LIMIT %s"
    )
    params.append(int(max_results) + 1)
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except psycopg.errors.UndefinedTable:
        return []

    if len(rows) > max_results:
        raise MetadataResultLimitExceeded("message_types", max_results)
    return [str(row[0]) for row in rows]


def describe_schema(
    *,
    robot_id: Optional[str] = None,
    message_type: Optional[str] = None,
    dsn: Optional[str] = None,
    max_values: int = METADATA_VALUE_LIMIT,
) -> RegistrySchemaSummary:
    """Describe matching files without fetching one Python row per file.

    File/row counts and timestamp bounds are a single aggregate row. Distinct
    schema values and columns are bounded by ``max_values`` so corrupt or
    adversarial registry metadata cannot create an unbounded API response.
    No matching file gives an empty summary typed with the v1 columns.
    """

    if int(max_values) <= 0:
        raise ValueError("max_values must be a positive integer")

    where, params = _visibility_where(
        robot_id=robot_id,
        message_type=message_type,
    )
    joins = (
        "FROM parquet_files AS file "
        "JOIN processed AS published ON published.upload_id = file.upload_id "
        "WHERE " + " AND ".join(where)
    )
    aggregate_sql = (
        "SELECT COUNT(*), COALESCE(SUM(file.row_count), 0), "
        "MIN(file.start_timestamp), MAX(file.end_timestamp) "
        + joins
    )
    schema_versions_sql = (
        "SELECT DISTINCT file.schema_version " + joins
        + " ORDER BY file.schema_version LIMIT %s"
    )
    schema_hashes_sql = (
        "SELECT DISTINCT file.schema_hash " + joins
        + " ORDER BY file.schema_hash LIMIT %s"
    )
    matched_columns = (
        "WITH matched AS ("
        "SELECT file.file_path, file.column_names " + joins + ") "
    )
    columns_sql = (
        matched_columns
        + "SELECT columns.column_name FROM matched "
        "CROSS JOIN LATERAL unnest(matched.column_names) "
        "WITH ORDINALITY AS columns(column_name, ordinal_position) "
        "GROUP BY columns.column_name "
        "ORDER BY CASE columns.column_name WHEN 'ts' THEN 0 WHEN 'topic' THEN 1 ELSE 2 END, "
        "MIN(matched.file_path), MIN(columns.ordinal_position), columns.column_name "
        "LIMIT %s"
    )
    matched_nulls = (
        "WITH matched AS ("
        "SELECT file.all_null_columns " + joins + ") "
    )
    all_null_sql = (
        matched_nulls
        + "SELECT DISTINCT candidate.column_name FROM matched AS seed "
        "CROSS JOIN LATERAL unnest(seed.all_null_columns) AS candidate(column_name) "
        "WHERE NOT EXISTS (SELECT 1 FROM matched AS other "
        "WHERE NOT (candidate.column_name = ANY(other.all_null_columns))) "
        "ORDER BY candidate.column_name LIMIT %s"
    )

    empty = RegistrySchemaSummary(
        schema_versions=[],
        schema_hashes=[],
        columns=list(DEFAULT_COLUMN_NAMES),
        all_null_columns=[],
        file_count=0,
        row_count=0,
        ts_min=None,
        ts_max=None,
    )
    dsn = dsn or registry_dsn()
    if not dsn:
        return empty

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(aggregate_sql, params)
                aggregate = cur.fetchone()
                file_count = int(aggregate[0] or 0)
                if file_count == 0:
                    return empty

                cur.execute(schema_versions_sql, [*params, int(max_values) + 1])
                schema_versions = [int(row[0]) for row in cur.fetchall()]
                if len(schema_versions) > max_values:
                    raise MetadataResultLimitExceeded("schema_versions", max_values)

                cur.execute(schema_hashes_sql, [*params, int(max_values) + 1])
                schema_hashes = [str(row[0]) for row in cur.fetchall()]
                if len(schema_hashes) > max_values:
                    raise MetadataResultLimitExceeded("schema_hashes", max_values)

                cur.execute(columns_sql, [*params, int(max_values) + 1])
                columns = [str(row[0]) for row in cur.fetchall()]
                if len(columns) > max_values:
                    raise MetadataResultLimitExceeded("columns", max_values)

                cur.execute(all_null_sql, [*params, int(max_values) + 1])
                all_null_set = {str(row[0]) for row in cur.fetchall()}
                if len(all_null_set) > max_values:
                    raise MetadataResultLimitExceeded(
                        "all_null_columns", max_values
                    )
    except psycopg.errors.UndefinedTable:
        return empty

    value_columns = [name for name in columns if name not in STRUCTURAL_COLUMNS]
    return RegistrySchemaSummary(
        schema_versions=schema_versions,
        schema_hashes=schema_hashes,
        columns=columns,
        all_null_columns=[name for name in value_columns if name in all_null_set],
        file_count=file_count,
        row_count=int(aggregate[1] or 0),
        ts_min=aggregate[2],
        ts_max=aggregate[3],
    )


def metadata_time_range(
    *,
    robot_id: Optional[str] = None,
    dsn: Optional[str] = None,
) -> RegistryTimeRange:
    """Aggregate a robot's recorded range without enumerating its files.

    Every registered file records its range, so this never opens Parquet.
    No matching file gives ``t_min = t_max = None``.
    """

    dsn = dsn or registry_dsn()
    if not dsn:
        return RegistryTimeRange(t_min=None, t_max=None)
    where, params = _visibility_where(robot_id=robot_id)
    sql = (
        "SELECT MIN(file.start_timestamp), MAX(file.end_timestamp) "
        "FROM parquet_files AS file "
        "JOIN processed AS published ON published.upload_id = file.upload_id "
        "WHERE " + " AND ".join(where)
    )
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
    except psycopg.errors.UndefinedTable:
        return RegistryTimeRange(t_min=None, t_max=None)
    if row is None:
        return RegistryTimeRange(t_min=None, t_max=None)
    return RegistryTimeRange(t_min=row[0], t_max=row[1])


def read_uris(files: Sequence[ParquetFile]) -> List[str]:
    """DuckDB-readable URIs for `files`, de-duplicated, in stable order."""
    seen = set()
    uris = []
    for f in files:
        uri = _to_read_uri(f.file_path)
        if uri in seen:
            continue
        seen.add(uri)
        uris.append(uri)
    return uris


def union_column_names(files: Sequence[ParquetFile]) -> List[str]:
    """Every column present across `files`, structural columns first.

    Files of different schema versions can legitimately appear in one
    result set (that is the point of versioning), so this is a union, not
    an intersection -- and `main.py` reads them with `union_by_name=true`
    so a column missing from an older file comes back NULL instead of
    failing the read.
    """
    ordered: List[str] = []
    for f in files:
        for name in f.column_names:
            if name not in ordered:
                ordered.append(name)
    if not ordered:
        ordered = list(DEFAULT_COLUMN_NAMES)
    structural = [c for c in ordered if c in STRUCTURAL_COLUMNS]
    return structural + [c for c in ordered if c not in STRUCTURAL_COLUMNS]


def value_column_names(files: Sequence[ParquetFile]) -> List[str]:
    """Non-structural columns across `files` -- what aggregates run over."""
    return [c for c in union_column_names(files) if c not in STRUCTURAL_COLUMNS]


def all_null_column_names(files: Sequence[ParquetFile]) -> List[str]:
    """Value columns that are 100% NULL in *every* matched file.

    Most of the 67 allowlisted ROS 2 types have no field extractor yet
    (`extractors.extract_xyz` returns (None, None, None)), so plenty of
    topics produce real rows whose x/y/z are all NULL. Surfacing that lets
    a caller tell "this topic has no numeric payload yet" from "the
    aggregate happened to be null".
    """
    if not files:
        return []
    candidates: Optional[set] = None
    for f in files:
        nulls = set(f.all_null_columns)
        candidates = nulls if candidates is None else (candidates & nulls)
        if not candidates:
            return []
    return [c for c in value_column_names(files) if candidates and c in candidates]
