"""Unit tests for reading through the `parquet_files` registry.

The query service answers "which files exist" and "what is in them" from
the registry the processor populates at write time, not from a glob over
`s3://{bucket}/processed/parquet/` or a literal column list.

What these tests pin down:

  1. the registry's file list is what DuckDB reads, and only the published
     generation of each upload is visible;
  2. "no match" is a real empty result, not an error -- an outage must never
     look like "no data";
  3. a file with a *newer* schema (an extra column) can be read alongside v1
     files without breaking the v1 reader. That is the whole point of
     versioning the schema: adding a field must not break readers.

No live S3 or Postgres: the registry boundary is faked and the file paths
point at local Parquet files DuckDB reads straight off disk.

The read path lives in `planner`; this file tests it there.
"""
from datetime import datetime, timezone

import duckdb
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from query_service import planner
from query_service import registry as registry_mod
from query_service import settings


# --- helpers ----------------------------------------------------------------

def _write_v1(path, n_rows, topic, start_ns=1_700_000_000_000_000_000, x_null=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_arrays(
        [
            pa.array([start_ns + i for i in range(n_rows)], type=pa.timestamp("ns", tz=None)),
            pa.array([topic] * n_rows, type=pa.string()),
            pa.array([None] * n_rows if x_null else [float(i) for i in range(n_rows)], type=pa.float64()),
            pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
            pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
        ],
        names=["ts", "topic", "x", "y", "z"],
    )
    pq.write_table(table, str(path), compression="snappy")
    return str(path)


def _write_v2(path, n_rows, topic, start_ns=1_700_000_000_000_000_000):
    """A hypothetical v2 file: v1's columns plus one new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_arrays(
        [
            pa.array([start_ns + i for i in range(n_rows)], type=pa.timestamp("ns", tz=None)),
            pa.array([topic] * n_rows, type=pa.string()),
            pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
            pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
            pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
            pa.array([float(i) * 10 for i in range(n_rows)], type=pa.float64()),
        ],
        names=["ts", "topic", "x", "y", "z", "w"],
    )
    pq.write_table(table, str(path), compression="snappy")
    return str(path)


def _entry(file_path, *, topic="imu", robot="robotA", version=1, columns=None,
           all_null=None, rows=1):
    return registry_mod.ParquetFile(
        file_path=file_path,
        robot_id=robot,
        message_type=topic,
        schema_version=version,
        schema_hash="a" * 64,
        row_count=rows,
        start_timestamp=datetime(2023, 11, 14, tzinfo=timezone.utc),
        end_timestamp=datetime(2023, 11, 14, tzinfo=timezone.utc),
        column_names=columns if columns is not None else ["ts", "topic", "x", "y", "z"],
        all_null_columns=all_null if all_null is not None else [],
        file_size_bytes=1,
    )


def _stub_registry(monkeypatch, result):
    """Make `registry.list_files` answer with `result` for any query."""
    monkeypatch.setattr(registry_mod, "list_files", lambda **kwargs: result)


# --- 1. the registry's list is what gets read -------------------------------

def test_registry_file_list_is_what_duckdb_reads(monkeypatch, tmp_path):
    reachable = _write_v1(tmp_path / "wanted" / "part-a.parquet", 7, "imu")
    _write_v1(tmp_path / "unwanted" / "part-b.parquet", 99, "imu")

    _stub_registry(monkeypatch, [_entry(reachable, rows=7)])

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(con, "robotA", topic="imu")

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 7, (
        "the file the registry did not list must not be read"
    )


def test_create_t_raw_asks_the_registry_for_a_bounded_file_list(monkeypatch, tmp_path):
    """The planner asks for one file more than the admission cap, so an
    oversized plan is rejected without fetching every row."""
    captured = {}

    def _list_files(**kwargs):
        captured.update(kwargs)
        return [_entry(_write_v1(tmp_path / "t" / "part-a.parquet", 5, "imu"), rows=5)]

    monkeypatch.setattr(registry_mod, "list_files", _list_files)

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(con, "robotA")

    assert captured["limit"] == settings.QUERY_MAX_SOURCE_FILES + 1
    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 5


def test_unscoped_registry_query_excludes_typed_derivative_namespace(monkeypatch):
    """A no-topic query must not read v1 messages and their v2 expansion."""
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append((" ".join(sql.split()), list(params or [])))

        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda dsn: _Conn())
    assert registry_mod.list_files(dsn="postgres://fake/fake") == []

    select, params = next(item for item in captured if item[0].startswith("SELECT file_path"))
    assert "message_type NOT LIKE %s" in select
    assert r"\_v%/%" in params


def test_registry_query_applies_caller_limit_before_fetchall(monkeypatch):
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append((" ".join(sql.split()), list(params or [])))

        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda _dsn: _Conn())
    assert registry_mod.list_files(dsn="postgres://fake/fake", limit=17) == []
    select, params = next(item for item in captured if item[0].startswith("SELECT file_path"))
    assert select.endswith("ORDER BY file_path LIMIT %s")
    assert params[-1] == 17


def test_message_types_uses_bounded_distinct_metadata_sql(monkeypatch):
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            captured.append((self.sql, list(params or [])))

        def fetchall(self):
            return [("imu",), ("odom",)]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda _dsn: _Conn())
    assert registry_mod.list_message_types(
        dsn="postgres://fake/fake",
        robot_id="robotA",
        max_results=7,
    ) == ["imu", "odom"]

    sql, params = captured[0]
    assert sql.startswith("SELECT DISTINCT file.message_type")
    assert "SELECT file_path" not in sql
    assert sql.endswith("ORDER BY file.message_type LIMIT %s")
    assert params[-1] == 8


def test_schema_and_time_range_are_constant_cardinality_registry_aggregates(
    monkeypatch,
):
    captured = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, tzinfo=timezone.utc)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            captured.append((self.sql, list(params or [])))

        def fetchone(self):
            if self.sql.startswith("SELECT COUNT(*), COALESCE(SUM"):
                # Deliberately far above every source admission default: this
                # is metadata, so no Parquet scan limit applies.
                return (20_000, 50_000_000, start, end)
            if self.sql.startswith("SELECT MIN(file.start_timestamp)"):
                return (start, end)
            raise AssertionError(self.sql)

        def fetchall(self):
            if self.sql.startswith("SELECT DISTINCT file.schema_version"):
                return [(1,), (2,)]
            if self.sql.startswith("SELECT DISTINCT file.schema_hash"):
                return [("aaa",), ("bbb",)]
            if "file.column_names" in self.sql:
                return [("ts",), ("topic",), ("x",), ("temperature",)]
            if "file.all_null_columns" in self.sql:
                return [("temperature",)]
            raise AssertionError(self.sql)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda _dsn: _Conn())
    summary = registry_mod.describe_schema(
        dsn="postgres://fake/fake",
        robot_id="robotA",
        max_values=7,
    )
    assert summary == registry_mod.RegistrySchemaSummary(
        schema_versions=[1, 2],
        schema_hashes=["aaa", "bbb"],
        columns=["ts", "topic", "x", "temperature"],
        all_null_columns=["temperature"],
        file_count=20_000,
        row_count=50_000_000,
        ts_min=start,
        ts_max=end,
    )
    time_range = registry_mod.metadata_time_range(
        dsn="postgres://fake/fake",
        robot_id="robotA",
    )
    assert time_range == registry_mod.RegistryTimeRange(start, end)

    aggregate_sql = next(sql for sql, _params in captured if "SUM(file.row_count)" in sql)
    assert "COUNT(*)" in aggregate_sql
    assert "ORDER BY file.file_path" not in aggregate_sql
    bounded_queries = [
        (sql, params) for sql, params in captured if sql.endswith("LIMIT %s")
    ]
    assert len(bounded_queries) == 4
    assert all(params[-1] == 8 for _sql, params in bounded_queries)


def test_registry_maps_the_recorded_file_size(monkeypatch):
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append(" ".join(sql.split()))

        def fetchall(self):
            return [
                (
                    "bucket/file.parquet",
                    "robotA",
                    "imu",
                    1,
                    "a" * 64,
                    10,
                    datetime(2024, 1, 1, tzinfo=timezone.utc),
                    datetime(2024, 1, 2, tzinfo=timezone.utc),
                    ["ts", "topic"],
                    [],
                    1234,
                )
            ]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda _dsn: _Conn())
    files = registry_mod.list_files(dsn="postgres://fake/fake")
    assert files[0].file_size_bytes == 1234
    assert "file_size_bytes" in captured[0]


def test_attempt_owned_files_are_visible_only_through_published_generation(
    monkeypatch,
):
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append(" ".join(sql.split()))

        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda _dsn: _Conn())
    assert registry_mod.list_files(dsn="postgres://fake/fake") == []

    select = next(sql for sql in captured if sql.startswith("SELECT file_path"))
    assert "JOIN processed AS published" in select
    assert "LEFT JOIN" not in select
    assert "file.attempt_id = published.attempt_id" in select
    assert "IS NULL" not in select


def test_derived_registry_reader_must_opt_in_explicitly(monkeypatch):
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append((" ".join(sql.split()), list(params or [])))

        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda dsn: _Conn())
    registry_mod.list_files(dsn="postgres://fake/fake", include_derived=True)
    select, params = next(item for item in captured if item[0].startswith("SELECT file_path"))
    assert "message_type NOT LIKE %s" not in select
    assert r"\_v%/%" not in params


# --- 2. empty vs error ------------------------------------------------------

def test_populated_registry_with_no_match_is_an_honest_empty_result(monkeypatch):
    """Not an exception: a typed, empty table so the caller's SQL still
    runs and returns zero rows."""
    _stub_registry(monkeypatch, [])

    con = duckdb.connect(database=":memory:")
    files = planner.create_t_raw(con, "robot-with-no-data")

    assert files == []
    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 0
    cols = [r[0] for r in con.execute("DESCRIBE t_raw").fetchall()]
    assert cols == ["ts", "topic", "x", "y", "z"], (
        "the empty table must carry the v1 schema so downstream views compile"
    )


def test_registry_returns_empty_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert registry_mod.list_files(dsn=None) == []


def test_registry_returns_empty_when_table_is_missing(monkeypatch):
    """A fresh, un-migrated database is genuinely "not bootstrapped" -- the
    same narrow carve-out uploads.py makes for the `uploads` table."""

    def _raise_connect(dsn):
        raise psycopg.errors.UndefinedTable('relation "parquet_files" does not exist')

    monkeypatch.setattr(registry_mod.psycopg, "connect", _raise_connect)
    assert registry_mod.list_files(dsn="postgres://fake/fake") == []


def test_registry_propagates_a_genuine_backend_outage(monkeypatch):
    """A real Postgres outage must not be reported as "no data"."""

    def _raise_connect(dsn):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(registry_mod.psycopg, "connect", _raise_connect)
    with pytest.raises(psycopg.OperationalError):
        registry_mod.list_files(dsn="postgres://fake/fake")


# --- 3. schema evolution ----------------------------------------------------

def test_a_newer_schema_version_reads_alongside_v1_without_breaking_it(monkeypatch, tmp_path):
    """The extensibility claim, checked end to end: a v2 file with an extra
    column is readable in the same query as v1 files, the v1 columns keep
    their values, and the new column comes back NULL for v1 rows instead of
    failing the read."""
    v1_path = _write_v1(tmp_path / "v1" / "part-a.parquet", 3, "imu")
    v2_path = _write_v2(tmp_path / "v2" / "part-b.parquet", 2, "imu")

    _stub_registry(
        monkeypatch,
        [
            _entry(v1_path, rows=3, version=1),
            _entry(v2_path, rows=2, version=2, columns=["ts", "topic", "x", "y", "z", "w"]),
        ],
    )

    con = duckdb.connect(database=":memory:")
    files = planner.create_t_raw(con, "robotA", topic="imu")

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 5
    assert con.execute("SELECT count(*) FROM t_raw WHERE w IS NULL").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM t_raw WHERE x IS NOT NULL").fetchone()[0] == 5

    # And the aggregate column list follows the registry, so the new field
    # is queryable without editing an array literal in this service.
    assert planner.value_columns(files) == ["x", "y", "z", "w"]


def test_all_null_columns_are_only_claimed_when_every_file_agrees(tmp_path):
    """A topic whose extractor returns (None, None, None) is genuinely
    all-NULL and should be reported as such -- but only if every matched
    file says so."""
    all_null = [_entry("a", all_null=["x", "y", "z"]), _entry("b", all_null=["x", "y", "z"])]
    assert registry_mod.all_null_column_names(all_null) == ["x", "y", "z"]

    mixed = [_entry("a", all_null=["x", "y", "z"]), _entry("b", all_null=[])]
    assert registry_mod.all_null_column_names(mixed) == []


def test_sql_console_relation_is_actually_creatable(monkeypatch, tmp_path):
    """The SQL console's `all_parquet` relation must exist and be readable.

    `CREATE TEMP VIEW all_parquet AS SELECT * FROM read_parquet(?)` is
    rejected outright by DuckDB ("Unexpected prepared parameter. This type of
    statement can't be prepared!"), so the relation would never be created
    and every console query would fail. Pin the working form down so it
    cannot become an unpreparable statement.
    """
    path = _write_v1(tmp_path / "c" / "part-a.parquet", 3, "imu")
    _stub_registry(monkeypatch, [_entry(path, rows=3)])

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(con, None, None, table="all_parquet")

    assert con.execute("SELECT count(*) FROM all_parquet").fetchone()[0] == 3


def test_sql_console_view_does_not_materialize_parquet_rows(monkeypatch, tmp_path):
    """Creating ``all_parquet`` must stay lazy for unrelated console SQL.

    Removing the source after view creation makes a later view scan fail,
    proving DuckDB did not copy the rows into an in-memory table.  ``SELECT
    1`` still succeeds because it never touches the view at all.
    """
    source = tmp_path / "lazy" / "part-a.parquet"
    _write_v1(source, 100_000, "imu")
    _stub_registry(monkeypatch, [_entry(str(source), rows=100_000)])

    con = duckdb.connect(database=":memory:")
    planner.create_all_parquet_view(con)

    assert con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'all_parquet'"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE view_name = 'all_parquet'"
    ).fetchone()[0] == 1

    source.unlink()
    assert con.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(duckdb.IOException):
        con.execute("SELECT count(*) FROM all_parquet").fetchone()


def test_sql_console_lazy_view_fails_on_a_stale_registry_key(
    monkeypatch, tmp_path
):
    """A stale registry key fails the query. Nothing else is read in its place."""
    _write_v1(
        tmp_path / "robotA" / "imu" / "part-live.parquet", 4, "imu"
    )
    missing = str(tmp_path / "robotA" / "imu" / "part-gone.parquet")
    _stub_registry(monkeypatch, [_entry(missing)])

    con = duckdb.connect(database=":memory:")
    with pytest.raises(planner.RegistryReadError):
        planner.create_all_parquet_view(con)


def test_naive_query_bounds_are_treated_as_utc():
    """Parsing itself establishes the UTC contract before any Python,
    Postgres, or DuckDB timezone conversion can reinterpret the value."""
    naive = datetime(2026, 7, 24, 12, 0, 0)
    assert registry_mod._as_utc(naive) == datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert settings.parse_iso_ts("2026-07-24T12:00:00") == datetime(
        2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc
    )
    assert settings.parse_iso_ts("2026-07-24T14:00:00+02:00") == datetime(
        2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc
    )

    aware = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert registry_mod._as_utc(aware) is aware
    assert registry_mod._as_utc(None) is None

    with pytest.raises(Exception) as exc_info:
        settings.parse_iso_ts("definitely-not-a-timestamp")
    assert getattr(exc_info.value, "status_code", None) == 400


def test_ts_range_pruning_uses_the_recorded_bounds(monkeypatch):
    """A file is kept when its recorded [start, end] overlaps the window."""
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            captured.append(" ".join(sql.split()))

        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(registry_mod.psycopg, "connect", lambda dsn: _Conn())
    registry_mod.list_files(
        dsn="postgres://fake/fake",
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    select = next(s for s in captured if s.startswith("SELECT file_path"))
    assert "end_timestamp >= %s" in select
    assert "start_timestamp <= %s" in select
    assert "IS NULL" not in select


def test_stored_paths_become_duckdb_readable_uris():
    """The processor stores S3 paths in PyArrow's scheme-less `bucket/key`
    form (same as processed_topics.parquet_path); DuckDB httpfs needs the
    `s3://` scheme. Local absolute paths pass through untouched."""
    assert registry_mod._to_read_uri("robotics-data/processed/parquet/r/t/p.parquet") == (
        "s3://robotics-data/processed/parquet/r/t/p.parquet"
    )
    assert registry_mod._to_read_uri("s3://bucket/key.parquet") == "s3://bucket/key.parquet"
    assert registry_mod._to_read_uri("/app/uploads/x.parquet") == "/app/uploads/x.parquet"
