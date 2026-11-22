from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from query_service import planner, registry, settings
from query_service.routers import query_api


def _import_query_service_main():
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]

    import fastapi.staticfiles as fastapi_staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    original = fastapi_staticfiles.StaticFiles
    fastapi_staticfiles.StaticFiles = _StubStaticFiles
    try:
        return importlib.import_module("query_service.main")
    finally:
        fastapi_staticfiles.StaticFiles = original


NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def _file(
    index: int,
    rows: int = 1,
    source_bytes: int = 0,
) -> registry.ParquetFile:
    return registry.ParquetFile(
        file_path=f"bucket/file-{index}.parquet",
        robot_id="r1",
        message_type="imu",
        schema_version=1,
        schema_hash="a" * 64,
        row_count=rows,
        start_timestamp=NOW,
        end_timestamp=NOW,
        column_names=["ts", "topic", "x", "y", "z"],
        all_null_columns=[],
        file_size_bytes=source_bytes,
    )


def test_explicit_source_range_rejects_reversed_and_overwide(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_RANGE_SECONDS", 60.0)
    with pytest.raises(HTTPException) as reversed_error:
        planner.validate_source_range(NOW, NOW - timedelta(seconds=1))
    assert reversed_error.value.status_code == 422
    assert reversed_error.value.detail["code"] == "source_range_reversed"

    with pytest.raises(HTTPException) as wide_error:
        planner.validate_source_range(NOW, NOW + timedelta(seconds=61))
    assert wide_error.value.detail == {
        "code": "source_range_too_wide",
        "max_seconds": 60.0,
    }
    planner.validate_source_range(NOW, NOW + timedelta(seconds=60))


def test_registry_plan_admission_caps_measured_files_and_rows(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 1)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_ROWS", 10)
    with pytest.raises(HTTPException) as files_error:
        planner.admit_source_plan(
            planner.QueryPlan(files=[_file(1), _file(2)])
        )
    assert files_error.value.detail["code"] == "source_file_limit_exceeded"

    with pytest.raises(HTTPException) as rows_error:
        planner.admit_source_plan(
            planner.QueryPlan(files=[_file(1, rows=11)])
        )
    assert rows_error.value.detail["code"] == "source_row_limit_exceeded"


def test_registry_plan_enforces_recorded_bytes(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 10)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_ROWS", 100)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 10)
    measured = planner.QueryPlan(
        files=[_file(1, source_bytes=6), _file(2, source_bytes=5)],
    )
    assert measured.source_bytes_estimate == 11
    assert measured.describe()["source_bytes_estimate"] == 11
    with pytest.raises(HTTPException) as raised:
        planner.admit_source_plan(measured)
    assert raised.value.detail == {
        "code": "source_byte_limit_exceeded",
        "source_bytes": 11,
        "max_bytes": 10,
    }


def test_explicit_upload_paths_share_file_and_local_byte_admission(monkeypatch, tmp_path):
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")
    paths = [str(first), str(second)]

    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 1)
    with pytest.raises(HTTPException) as files_error:
        planner.admit_explicit_source_paths(paths)
    assert files_error.value.detail["code"] == "source_file_limit_exceeded"

    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 2)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 7)
    with pytest.raises(HTTPException) as bytes_error:
        planner.admit_explicit_source_paths(paths)
    assert bytes_error.value.detail["code"] == "source_byte_limit_exceeded"


def test_explicit_remote_paths_enforce_known_size_subtotal(monkeypatch):
    paths = [
        "s3://lake/known.parquet",
        "s3://lake/unsized.parquet",
    ]
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 10)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 10)
    with pytest.raises(HTTPException) as raised:
        planner.admit_explicit_source_paths(
            paths,
            {"s3://lake/known.parquet": 11},
        )
    assert raised.value.detail == {
        "code": "source_byte_limit_exceeded",
        "source_bytes": 11,
        "max_bytes": 10,
    }


def test_admission_metrics_use_a_fixed_specific_reason_enum(monkeypatch):
    reasons = []

    class Metric:
        def labels(self, **labels):
            reasons.append(labels["reason"])
            return self

        def inc(self):
            pass

    class NoCapacity:
        def acquire(self, **_kwargs):
            return False

    monkeypatch.setattr(planner, "QUERY_ADMISSION_REJECTIONS_TOTAL", Metric())
    monkeypatch.setattr(planner, "_QUERY_SLOTS", NoCapacity())
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(HTTPException):
        planner.acquire_query_capacity()
    with pytest.raises(HTTPException):
        planner.validate_source_range(NOW, NOW - timedelta(seconds=1))

    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 1)
    with pytest.raises(HTTPException):
        planner.admit_source_plan(
            planner.QueryPlan(files=[_file(1), _file(2)])
        )

    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 10)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_ROWS", 1)
    with pytest.raises(HTTPException):
        planner.admit_source_plan(
            planner.QueryPlan(files=[_file(1, rows=2)])
        )

    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_ROWS", 10)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 1)
    with pytest.raises(HTTPException):
        planner.admit_source_plan(
            planner.QueryPlan(files=[_file(1, source_bytes=2)])
        )
    assert reasons == [
        "capacity",
        "source_range",
        "source_files",
        "source_rows",
        "source_bytes",
    ]


def test_deadline_interrupts_and_close_is_idempotent():
    class Raw:
        def __init__(self):
            self.interrupts = 0
            self.closes = 0

        def interrupt(self):
            self.interrupts += 1

        def close(self):
            self.closes += 1

    class Lease:
        def __init__(self):
            self.returns = 0
            self.closes = 0

        def _return_connection(self):
            self.returns += 1

        def close(self):
            self.closes += 1

    raw = Raw()
    lease = Lease()
    con = planner._ManagedDuckDBConnection(
        raw,
        lease,
        owns_capacity_lease=True,
        timeout_seconds=0.01,
        query_type="test",
    )
    time.sleep(0.03)
    assert raw.interrupts == 1
    with pytest.raises(planner.QueryExecutionTimeout):
        con.execute("select 1")
    con.close()
    con.close()
    assert (raw.closes, lease.returns, lease.closes) == (1, 1, 1)


def test_capacity_lease_releases_exactly_once_when_close_precedes_connection_return():
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    lease = planner.QueryCapacityLease(slots)
    lease._borrow_connection()
    lease.close()
    assert not slots.acquire(blocking=False)
    lease._return_connection()
    assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)


def test_disable_prom_metrics_supports_active_gauge_decrement():
    env = dict(os.environ)
    env["DISABLE_PROM_METRICS"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from query_service.middleware import QUERY_ACTIVE; "
                "QUERY_ACTIVE.inc(); QUERY_ACTIVE.dec()"
            ),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_global_timeout_handler_is_the_stable_504_contract():
    main = _import_query_service_main()
    app = main.create_app()
    handler = app.exception_handlers[planner.QueryExecutionTimeout]
    response = asyncio.run(handler(None, planner.QueryExecutionTimeout("expired")))
    assert response.status_code == 504
    assert response.body == b'{"detail":{"code":"query_timeout"}}'


def test_query_limits_returns_the_active_typed_admission_contract(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_RANGE_SECONDS", 123.0)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 45)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_ROWS", 678)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 9012)
    monkeypatch.setattr(settings, "DUCKDB_QUERY_TIMEOUT_SECONDS", 34.0)
    monkeypatch.setattr(settings, "DUCKDB_QUERY_CONCURRENCY", 5)

    response = query_api.query_limits(_user=None)
    assert isinstance(response, query_api.QueryLimitsResponse)
    assert response.model_dump() == {
        "max_source_range_seconds": 123.0,
        "max_source_files": 45,
        "max_source_rows": 678,
        "max_source_bytes": 9012,
        "query_timeout_seconds": 34.0,
        "max_concurrent_queries": 5,
    }


def test_registry_metadata_endpoints_do_not_apply_parquet_scan_admission(monkeypatch):
    monkeypatch.setattr(settings, "s3_enabled", lambda: True)
    monkeypatch.setattr(settings, "cache_get", lambda _key: None)
    monkeypatch.setattr(settings, "cache_set", lambda *_args: None)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_FILES", 1)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_ROWS", 1)
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 1)

    def must_not_admit(*_args, **_kwargs):
        raise AssertionError("metadata-only lookup must not apply Parquet admission")

    monkeypatch.setattr(planner, "admit_source_plan", must_not_admit)
    monkeypatch.setattr(
        registry,
        "list_message_types",
        lambda **_kwargs: ["imu", "odom"],
    )
    monkeypatch.setattr(
        registry,
        "describe_schema",
        lambda **_kwargs: registry.RegistrySchemaSummary(
            schema_versions=[1],
            schema_hashes=["abc"],
            columns=["ts", "topic", "x"],
            all_null_columns=[],
            file_count=20_000,
            row_count=50_000_000,
            ts_min=NOW,
            ts_max=NOW + timedelta(days=365),
        ),
    )
    monkeypatch.setattr(
        registry,
        "metadata_time_range",
        lambda **_kwargs: registry.RegistryTimeRange(
            t_min=NOW,
            t_max=NOW + timedelta(days=365),
        ),
    )
    monkeypatch.setattr(
        planner,
        "plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata must not enumerate source files")
        ),
    )

    assert query_api.list_message_types("r1") == ["imu", "odom"]
    schema = query_api.describe_schema("r1", "imu")
    assert schema["file_count"] == 20_000
    assert schema["row_count"] == 50_000_000
    # API timestamps are UTC with a trailing `Z` (settings.iso_utc).
    assert schema["ts_max"] == "2027-08-30T00:00:00Z"
    assert query_api.robot_time_range("r1") == {
        "t_min": "2026-08-30T00:00:00Z",
        "t_max": "2027-08-30T00:00:00Z",
    }
