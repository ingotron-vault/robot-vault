"""Unit tests for the key-pruning query layer.

Without the planner, every read endpoint pulled the whole matched Parquet set
into an in-memory DuckDB table and only then applied the caller's filters.
`planner` decides what to read first:

  1. **file pruning** -- the `parquet_files` registry answers "which files
     can possibly match this robot/topic/window", and DuckDB gets that
     explicit key list (the registry's mechanism; these tests pin that the
     planner still uses it, including its prefix topic matching);
  2. **row pruning** -- the time window is pushed INTO `read_parquet`, so a
     narrow window over a wide dataset materializes only the rows in it;
  3. **metadata-only answers** -- a robot's time range comes from the
     registry's recorded per-file bounds, with zero object reads.

Everything here runs against local Parquet files; the registry boundary is
faked, so no Postgres or S3 is required.
"""
from datetime import datetime, timedelta, timezone
import threading

import duckdb
from fastapi import HTTPException
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from query_service import planner
from query_service import registry as registry_mod
from query_service import settings
from query_service import uploads as uploads_mod

BASE_TS = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _write(path, n_rows, topic="imu", start=BASE_TS, step_seconds=60):
    """`n_rows` rows one minute apart, `ts` as a real Arrow timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamps = [start + timedelta(seconds=step_seconds * i) for i in range(n_rows)]
    pq.write_table(
        pa.Table.from_arrays(
            [
                pa.array([s.replace(tzinfo=None) for s in stamps], type=pa.timestamp("ns", tz=None)),
                pa.array([topic] * n_rows, type=pa.string()),
                pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
                pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
                pa.array([float(i) for i in range(n_rows)], type=pa.float64()),
            ],
            names=["ts", "topic", "x", "y", "z"],
        ),
        str(path),
        compression="snappy",
    )
    return str(path), stamps


def _entry(file_path, *, topic="imu", robot="robotA", rows=1, start=BASE_TS, end=BASE_TS):
    return registry_mod.ParquetFile(
        file_path=file_path,
        robot_id=robot,
        message_type=topic,
        schema_version=1,
        schema_hash="a" * 64,
        row_count=rows,
        start_timestamp=start,
        end_timestamp=end,
        column_names=["ts", "topic", "x", "y", "z"],
        all_null_columns=[],
        file_size_bytes=1,
    )


def _stub_registry(monkeypatch, result):
    monkeypatch.setattr(registry_mod, "list_files", lambda **kwargs: result)


def test_console_duckdb_connection_has_its_configured_memory_bound(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: False)

    con = planner.open_duckdb(memory_limit_mb=64)
    try:
        assert con.execute(
            "SELECT current_setting('memory_limit')"
        ).fetchone()[0] == "64.0 MiB"
    finally:
        con.close()


def test_ordinary_duckdb_connection_uses_the_service_memory_bound(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: False)

    con = planner.open_duckdb()
    try:
        assert con.execute(
            "SELECT current_setting('memory_limit')"
        ).fetchone()[0] == f"{float(settings.DUCKDB_MEMORY_LIMIT_MB):.1f} MiB"
    finally:
        con.close()


def test_duckdb_query_slots_fail_fast_and_release_on_close(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: False)
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(planner, "_QUERY_SLOTS", threading.BoundedSemaphore(1))

    first = planner.open_duckdb()
    try:
        with pytest.raises(HTTPException) as raised:
            planner.open_duckdb()
        assert raised.value.status_code == 503
        assert raised.value.detail == {"code": "query_capacity_exceeded"}
    finally:
        first.close()

    second = planner.open_duckdb()
    second.close()


def test_upload_detail_duckdb_uses_the_same_memory_and_capacity_limit(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: False)
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(planner, "_QUERY_SLOTS", threading.BoundedSemaphore(1))

    first = planner.open_duckdb()
    try:
        with pytest.raises(HTTPException) as raised:
            uploads_mod._open_duckdb_for_uploads()
        assert raised.value.status_code == 503
        assert raised.value.detail == {"code": "query_capacity_exceeded"}
    finally:
        first.close()

    upload_connection = uploads_mod._open_duckdb_for_uploads()
    try:
        assert upload_connection.execute(
            "SELECT current_setting('memory_limit')"
        ).fetchone()[0] == f"{float(settings.DUCKDB_MEMORY_LIMIT_MB):.1f} MiB"
    finally:
        upload_connection.close()


def test_duckdb_can_borrow_an_outer_capacity_lease_without_reacquiring(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: False)
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(planner, "_QUERY_SLOTS", threading.BoundedSemaphore(1))

    with planner.acquire_query_capacity() as capacity_lease:
        borrowed = planner.open_duckdb(capacity_lease=capacity_lease)
        borrowed.close()

        # Closing a borrowed connection returns it to the lease, but the
        # surrounding detail scan still owns the sole process-wide permit.
        with pytest.raises(HTTPException) as raised:
            planner.open_duckdb()
        assert raised.value.status_code == 503
        assert raised.value.detail == {"code": "query_capacity_exceeded"}

    # The outer scope, not the borrowed connection, releases the permit.
    ordinary = planner.open_duckdb()
    ordinary.close()


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "64"])
def test_console_duckdb_memory_bound_rejects_invalid_values(invalid):
    with pytest.raises(ValueError, match="positive integer"):
        planner.open_duckdb(memory_limit_mb=invalid)


# --- 1. the plan itself -----------------------------------------------------


def test_plan_reports_what_it_will_read(monkeypatch, tmp_path):
    path, _ = _write(tmp_path / "a" / "part-a.parquet", 5)
    _stub_registry(monkeypatch, [_entry(path, rows=5)])

    p = planner.plan("robotA", "imu")

    assert p.file_count == 1
    assert p.row_count_estimate == 5
    assert p.source_bytes_estimate == 1
    assert p.uris == [registry_mod._to_read_uri(path)]
    assert p.describe()["file_count"] == 1


def test_plan_narrows_on_robot_topic_and_window_before_reading(monkeypatch):
    """The filters must reach Postgres, not be applied after the read --
    that is the entire point of the layer."""
    captured = {}

    def _list_files(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(registry_mod, "list_files", _list_files)
    since = BASE_TS
    until = BASE_TS + timedelta(hours=1)

    planner.plan("robotA", "imu", since=since, until=until)

    assert captured["robot_id"] == "robotA"
    assert captured["message_type"] == "imu"
    assert captured["since"] == since
    assert captured["until"] == until


# --- 2. row pruning: the window is pushed into the read ---------------------


def test_time_window_is_pushed_into_read_parquet(monkeypatch, tmp_path):
    """10 rows one minute apart; a 3-minute window must MATERIALIZE 3 rows,
    not materialize 10 and filter afterwards."""
    path, stamps = _write(tmp_path / "a" / "part-a.parquet", 10)
    _stub_registry(monkeypatch, [_entry(path, rows=10)])

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(con, "robotA", topic="imu", since=stamps[2], until=stamps[4])

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 3


def test_pushdown_can_be_disabled_for_callers_that_need_every_row(monkeypatch, tmp_path):
    path, stamps = _write(tmp_path / "a" / "part-a.parquet", 10)
    _stub_registry(monkeypatch, [_entry(path, rows=10)])

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(
        con, "robotA", topic="imu", since=stamps[2], until=stamps[4], pushdown_time=False
    )

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 10


def test_pushdown_produces_the_same_rows_as_filtering_afterwards(monkeypatch, tmp_path):
    """The optimization must be invisible in the results. Same data, same
    window, once pruned at read time and once filtered post-hoc."""
    path, stamps = _write(tmp_path / "a" / "part-a.parquet", 12)
    _stub_registry(monkeypatch, [_entry(path, rows=12)])
    since, until = stamps[3], stamps[9]

    pruned = duckdb.connect(database=":memory:")
    planner.create_t_raw(pruned, "robotA", topic="imu", since=since, until=until)
    pruned_rows = pruned.execute("SELECT ts, x FROM t_raw ORDER BY ts").fetchall()

    unpruned = duckdb.connect(database=":memory:")
    planner.create_t_raw(
        unpruned, "robotA", topic="imu", since=since, until=until, pushdown_time=False
    )
    # Filter post-hoc through the same UTC-nanosecond normalization and
    # bounds. Comparing TIMESTAMP_NS directly with TIMESTAMPTZ would recreate
    # the session-timezone bug this test protects against.
    unpruned_rows = unpruned.execute(
        f"SELECT ts, x FROM t_raw "
        f"WHERE {planner.TS_EXPR} >= make_timestamp_ns(?) "
        f"AND {planner.TS_EXPR} <= make_timestamp_ns(?) ORDER BY ts",
        [settings.datetime_to_epoch_ns(since), settings.datetime_to_epoch_ns(until)],
    ).fetchall()

    assert pruned_rows == unpruned_rows


@pytest.mark.parametrize("session_timezone", ["UTC", "Europe/Berlin"])
def test_native_timestamp_pushdown_does_not_depend_on_duckdb_timezone(
    monkeypatch, tmp_path, session_timezone
):
    """Parquet stores UTC as timezone-naive TIMESTAMP_NS. A TIMESTAMPTZ
    comparison would reinterpret that clock through the DuckDB session zone,
    so exercise UTC and a non-UTC deployment explicitly."""
    path, stamps = _write(
        tmp_path / session_timezone.replace("/", "-") / "part.parquet", 10
    )
    _stub_registry(monkeypatch, [_entry(path, rows=10)])

    con = duckdb.connect(database=":memory:")
    con.execute(f"SET TimeZone='{session_timezone}'")
    planner.create_t_raw(
        con,
        "robotA",
        topic="imu",
        since=stamps[2],
        until=stamps[4],
    )

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 3
    assert con.execute("SELECT min(ts), max(ts) FROM t_raw").fetchone() == (
        stamps[2].replace(tzinfo=None),
        stamps[4].replace(tzinfo=None),
    )


def test_no_window_reads_everything(monkeypatch, tmp_path):
    path, _ = _write(tmp_path / "a" / "part-a.parquet", 6)
    _stub_registry(monkeypatch, [_entry(path, rows=6)])

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(con, "robotA", topic="imu")

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 6
    assert con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE view_name = 't_raw'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 't_raw'"
    ).fetchone()[0] == 0


# --- 3. registry rows stay authoritative -----------------------------------


def test_a_registry_row_naming_a_missing_object_fails_the_query(
    monkeypatch, tmp_path
):
    real, stamps = _write(tmp_path / "real" / "part-a.parquet", 5)
    missing = str(tmp_path / "gone" / "part-z.parquet")
    _stub_registry(monkeypatch, [_entry(real, rows=5), _entry(missing, rows=5)])

    con = duckdb.connect(database=":memory:")
    with pytest.raises(planner.RegistryReadError):
        planner.create_t_raw(
            con,
            "robotA",
            topic="imu",
            since=stamps[0],
            until=stamps[4],
        )


# --- 4. UTC-nanosecond normalization ---------------------------------------


def test_pushdown_expression_normalizes_both_shapes_to_timestamp_ns():
    """Both supported physical schemas must retain nanoseconds and avoid a
    session-zone-sensitive TIMESTAMPTZ coercion."""
    from query_service.routers import query_api

    for fragment in (
        "try_cast(ts AS BIGINT)",
        "make_timestamp_ns",
        "try_cast(ts AS TIMESTAMP_NS)",
    ):
        assert fragment in planner.TS_EXPR
    assert "to_timestamp" not in planner.TS_EXPR

    con = duckdb.connect(database=":memory:")
    con.execute("CREATE TABLE native_ts (ts TIMESTAMP_NS)")
    con.execute(
        "INSERT INTO native_ts VALUES "
        "(TIMESTAMP_NS '2024-01-15 12:00:00.000000123')"
    )
    normalized_type = con.execute(
        f"SELECT typeof({planner.TS_EXPR}) FROM native_ts"
    ).fetchone()[0]
    assert normalized_type == "TIMESTAMP_NS"

    # The endpoint view still accepts the same native/integer source shapes;
    # its connections are pinned to UTC by planner.open_duckdb().
    assert "try_cast(ts AS BIGINT) AS ts_num" in query_api._T_NORM_SQL
    assert "make_timestamp_ns(ts_num)" in query_api._T_SQL
    assert "try_cast(ts AS TIMESTAMP_NS) AS ts_stamp" in query_api._T_NORM_SQL


def test_pushdown_bounds_are_exact_and_treat_naive_values_as_utc():
    """No process-local timezone lookup or floating-point epoch conversion
    is allowed while constructing an inlined predicate."""
    naive = BASE_TS.replace(tzinfo=None)
    offset = BASE_TS.astimezone(timezone(timedelta(hours=5, minutes=30)))
    expected_ns = 1_705_320_000_000_000_000

    assert settings.datetime_to_epoch_ns(naive) == expected_ns
    assert settings.datetime_to_epoch_ns(offset) == expected_ns
    where = planner._pushdown_where(naive, offset)
    assert where.count(f"make_timestamp_ns({expected_ns})") == 2
    assert "to_timestamp" not in where


def test_epoch_nanosecond_timestamps_are_pruned_correctly(monkeypatch, tmp_path):
    """Some producers store `ts` as epoch nanoseconds rather than a real
    TIMESTAMP; the pushdown has to handle both, exactly like the view."""
    path = tmp_path / "ns" / "part-a.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    base_ns = settings.datetime_to_epoch_ns(BASE_TS)
    pq.write_table(
        pa.Table.from_arrays(
            [
                pa.array([base_ns + i * 60 * 10**9 for i in range(10)], type=pa.int64()),
                pa.array(["imu"] * 10, type=pa.string()),
                pa.array([float(i) for i in range(10)], type=pa.float64()),
                pa.array([float(i) for i in range(10)], type=pa.float64()),
                pa.array([float(i) for i in range(10)], type=pa.float64()),
            ],
            names=["ts", "topic", "x", "y", "z"],
        ),
        str(path),
        compression="snappy",
    )
    _stub_registry(monkeypatch, [_entry(str(path), rows=10)])

    con = duckdb.connect(database=":memory:")
    planner.create_t_raw(
        con,
        "robotA",
        topic="imu",
        since=BASE_TS + timedelta(minutes=2),
        until=BASE_TS + timedelta(minutes=4),
    )

    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 3


def test_integer_epoch_nanoseconds_are_not_rounded_through_float_seconds(
    monkeypatch, tmp_path
):
    """The former ``ns / 1e9`` branch rounded adjacent nanoseconds to the
    same floating-point second. An exact point window must retain one row."""
    path = tmp_path / "ns-exact" / "part-a.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    base_ns = settings.datetime_to_epoch_ns(BASE_TS)
    pq.write_table(
        pa.Table.from_arrays(
            [
                pa.array([base_ns - 1, base_ns, base_ns + 1], type=pa.int64()),
                pa.array(["imu"] * 3, type=pa.string()),
                pa.array([0.0, 1.0, 2.0], type=pa.float64()),
                pa.array([0.0, 1.0, 2.0], type=pa.float64()),
                pa.array([0.0, 1.0, 2.0], type=pa.float64()),
            ],
            names=["ts", "topic", "x", "y", "z"],
        ),
        str(path),
        compression="snappy",
    )
    _stub_registry(monkeypatch, [_entry(str(path), rows=3)])

    con = duckdb.connect(database=":memory:")
    con.execute("SET TimeZone='Europe/Berlin'")
    planner.create_t_raw(
        con,
        "robotA",
        topic="imu",
        since=BASE_TS,
        until=BASE_TS,
    )

    assert con.execute("SELECT ts, x FROM t_raw").fetchall() == [(base_ns, 1.0)]
