"""Regression coverage for Explore's typed JointState time series."""
from datetime import datetime, timedelta, timezone
import threading

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from query_service import planner
from query_service import registry as registry_mod
from query_service import settings
from query_service.routers import query_api


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)


def _entry(path, *, message_type, columns, all_null, rows, version=1):
    return registry_mod.ParquetFile(
        file_path=str(path),
        robot_id="example-manipulator-01",
        message_type=message_type,
        schema_version=version,
        schema_hash="a" * 64,
        row_count=rows,
        start_timestamp=BASE.replace(tzinfo=timezone.utc),
        end_timestamp=(BASE + timedelta(seconds=1)).replace(tzinfo=timezone.utc),
        column_names=columns,
        all_null_columns=all_null,
        file_size_bytes=1,
    )


def _write_v1(path, *, numeric=False):
    points = [BASE, BASE + timedelta(seconds=1)]
    pq.write_table(
        pa.table({
            "ts": pa.array(points, type=pa.timestamp("ns")),
            "topic": ["joint_state" if not numeric else "twist"] * 2,
            "x": [None, None] if not numeric else [0.1, 0.2],
            "y": [None, None] if not numeric else [0.0, 0.0],
            "z": [None, None] if not numeric else [0.0, 0.0],
        }),
        path,
    )


def _write_joint_state(path):
    timestamps = [BASE, BASE, BASE + timedelta(seconds=1), BASE + timedelta(seconds=1)]
    pq.write_table(
        pa.table({
            "ts": pa.array(timestamps, type=pa.timestamp("ns")),
            "topic": ["joint_state"] * 4,
            "joint_name": ["elbow", "shoulder", "elbow", "shoulder"],
            "position": [1.0, 2.0, 3.0, 4.0],
            "velocity": pa.array([None] * 4, type=pa.float64()),
            "effort": [10.0, 20.0, 30.0, 40.0],
        }),
        path,
    )


def _configure_local_query(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: True)
    monkeypatch.setattr(settings, "cache_get", lambda _key: None)
    monkeypatch.setattr(settings, "cache_set", lambda _key, _value: None)
    monkeypatch.setattr(planner, "open_duckdb", lambda: duckdb.connect(database=":memory:"))


def test_timeseries_adds_one_series_per_joint_and_populated_field(monkeypatch, tmp_path):
    v1_path = tmp_path / "v1.parquet"
    typed_path = tmp_path / "joint-state.parquet"
    _write_v1(v1_path)
    _write_joint_state(typed_path)
    v1 = _entry(
        v1_path,
        message_type="joint_state",
        columns=["ts", "topic", "x", "y", "z"],
        all_null=["x", "y", "z"],
        rows=2,
    )
    typed = _entry(
        typed_path,
        message_type="_v2/joint_state/joint_state",
        columns=["ts", "topic", "joint_name", "position", "velocity", "effort"],
        all_null=["velocity"],
        rows=4,
        version=2,
    )
    calls = []

    def list_files(**kwargs):
        calls.append(kwargs)
        if kwargs.get("message_type") == "joint_state":
            return [v1]
        if kwargs.get("message_type") == "_v2/joint_state/joint_state":
            assert kwargs.get("include_derived") is True
            return [typed]
        raise AssertionError(f"unexpected registry query: {kwargs}")

    monkeypatch.setattr(registry_mod, "list_files", list_files)
    _configure_local_query(monkeypatch)

    response = query_api.timeseries(
        "example-manipulator-01", "joint_state", None, None, "second"
    )

    assert response["points"] == [
        {"t": "2026-01-01T00:00:00Z", "x": None, "y": None, "z": None, "n": 1},
        {"t": "2026-01-01T00:00:01Z", "x": None, "y": None, "z": None, "n": 1},
    ]
    assert response["columns"] == ["x", "y", "z"]
    assert response["all_null_columns"] == ["x", "y", "z"]
    assert response["series_source"] == "typed_joint_state"
    assert [series["name"] for series in response["series"]] == [
        "elbow · effort",
        "elbow · position",
        "shoulder · effort",
        "shoulder · position",
    ]
    elbow_position = next(
        series for series in response["series"] if series["name"] == "elbow · position"
    )
    assert elbow_position == {
        "name": "elbow · position",
        "field": "position",
        "dimensions": {"joint_name": "elbow"},
        "points": [
            {"t": "2026-01-01T00:00:00Z", "value": 1.0, "n": 1},
            {"t": "2026-01-01T00:00:01Z", "value": 3.0, "n": 1},
        ],
    }
    assert len(calls) == 2


def test_joint_state_derivative_reuses_one_bounded_connection(monkeypatch, tmp_path):
    v1_path = tmp_path / "bounded-v1.parquet"
    typed_path = tmp_path / "bounded-joint-state.parquet"
    _write_v1(v1_path)
    _write_joint_state(typed_path)
    v1 = _entry(
        v1_path,
        message_type="joint_state",
        columns=["ts", "topic", "x", "y", "z"],
        all_null=["x", "y", "z"],
        rows=2,
    )
    typed = _entry(
        typed_path,
        message_type="_v2/joint_state/joint_state",
        columns=["ts", "topic", "joint_name", "position", "velocity", "effort"],
        all_null=["velocity"],
        rows=4,
        version=2,
    )

    def list_files(**kwargs):
        if kwargs.get("message_type") == "joint_state":
            return [v1]
        if kwargs.get("message_type") == "_v2/joint_state/joint_state":
            return [typed]
        raise AssertionError(f"unexpected registry query: {kwargs}")

    monkeypatch.setattr(registry_mod, "list_files", list_files)
    monkeypatch.setattr(settings, "cache_get", lambda _key: None)
    monkeypatch.setattr(settings, "cache_set", lambda _key, _value: None)
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(planner, "_QUERY_SLOTS", threading.BoundedSemaphore(1))

    # The endpoint requires lake mode, while this local-file test must keep
    # planner connection setup from installing S3 credentials/httpfs.
    monkeypatch.setattr(settings, "s3_enabled", lambda: True)
    real_open = planner.open_duckdb
    open_calls = 0

    def open_local_bounded():
        nonlocal open_calls
        open_calls += 1
        s3_enabled = settings.s3_enabled
        monkeypatch.setattr(settings, "s3_enabled", lambda: False)
        try:
            return real_open()
        finally:
            monkeypatch.setattr(settings, "s3_enabled", s3_enabled)

    monkeypatch.setattr(planner, "open_duckdb", open_local_bounded)

    response = query_api.timeseries(
        "example-manipulator-01", "joint_state", None, None, "second"
    )

    assert response["series_source"] == "typed_joint_state"
    assert open_calls == 1


def test_timeseries_keeps_scalar_response_shape_and_skips_derivative(monkeypatch, tmp_path):
    v1_path = tmp_path / "twist.parquet"
    _write_v1(v1_path, numeric=True)
    scalar = _entry(
        v1_path,
        message_type="twist",
        columns=["ts", "topic", "x", "y", "z"],
        all_null=[],
        rows=2,
    )

    def list_files(**kwargs):
        assert kwargs.get("message_type") == "twist", "scalar topics must not probe derivatives"
        return [scalar]

    monkeypatch.setattr(registry_mod, "list_files", list_files)
    _configure_local_query(monkeypatch)

    response = query_api.timeseries(
        "example-manipulator-01", "twist", None, None, "second"
    )

    assert set(response) == {"points", "columns", "all_null_columns"}
    assert response["points"][0]["x"] == 0.1
    assert response["columns"] == ["x", "y", "z"]
