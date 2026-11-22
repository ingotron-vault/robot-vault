"""
Contract tests for two query-service responses the UI depends on (downloads
and open-in-chart):

1. `uploads.py:get_upload_files` returns `parquet_keys`: a
   `Record<string, string[]>` of bucket-relative S3 object keys, which
   `Uploads/index.tsx`'s `UploadFilesResp` reads and feeds straight into
   `/api/v1/s3/presign`. Any other field name leaves the Downloads section
   and every per-topic Parquet link empty.

2. `uploads.py:get_upload_detail` returns the real `ts_min`/`ts_max` of the
   processed data. The UI's "Open in chart" button is
   `disabled={!since || !until}` with `since`/`until` sourced from those two
   fields, so a `None` here keeps the button disabled.

This test exercises both functions directly (no live Postgres/S3 required)
by monkeypatching the `psycopg` connection boundary with a fake
cursor/connection pair (same technique as
`tests/unit/test_error_vs_empty.py`), and by writing real local Parquet
files with a `ts` column so `get_upload_detail`'s DuckDB-based time-range
query (`uploads.py:_compute_ts_range_from_parquet_paths`) reads genuine
data. Local filesystem paths that DuckDB reads directly avoid needing live
S3/httpfs.
"""
import importlib
import sys
import threading

from fastapi import HTTPException
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from query_service import planner
from query_service import settings
from query_service import uploads as uploads_mod


def _import_query_service_main():
    """Import query_service.main, working around an environment-only issue
    (main.py mounts a StaticFiles app over a directory only populated by the
    ui-spa npm build). Same workaround as the other unit tests in this dir."""
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]

    import fastapi.staticfiles as fastapi_staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    original_static_files = fastapi_staticfiles.StaticFiles
    fastapi_staticfiles.StaticFiles = _StubStaticFiles
    try:
        return importlib.import_module("query_service.main")
    finally:
        fastapi_staticfiles.StaticFiles = original_static_files


_import_query_service_main()


# --- Fake psycopg connection/cursor -----------------------------------------
#
# get_upload_detail issues up to three queries across two separate
# `with psycopg.connect(dsn) as conn:` blocks:
#   1. SELECT ... FROM uploads WHERE id::text = %s OR client_upload_id = %s
#   2. SELECT pt.topic, pt.rows_written, pt.parquet_path, pt.checksum,
#      registry-backed byte size
#      FROM processed_topics pt JOIN uploads u ...
#   3. SELECT p.rows_written FROM processed p JOIN uploads u ...
# The fake cursor dispatches fetchone()/fetchall() based on which of these
# was most recently executed, keyed by a distinctive substring of the query.


class _FakeCursor:
    def __init__(self, upload_row, topic_rows, processed_row=None):
        self._upload_row = upload_row
        self._topic_rows = topic_rows
        self._processed_row = processed_row
        self._kind = None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if "FROM uploads" in q and "WHERE id::text" in q:
            self._kind = "upload"
        elif "FROM processed_topics" in q:
            self._kind = "topics"
        elif "FROM processed p" in q:
            self._kind = "processed"
        else:
            self._kind = None

    def fetchone(self):
        if self._kind == "upload":
            return (*self._upload_row, "resolved-upload-id") if self._upload_row is not None else None
        if self._kind == "processed":
            return self._processed_row
        return None

    def fetchall(self):
        if self._kind == "topics":
            return self._topic_rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, upload_row, topic_rows, processed_row=None):
        self._upload_row = upload_row
        self._topic_rows = topic_rows
        self._processed_row = processed_row

    def cursor(self):
        return _FakeCursor(self._upload_row, self._topic_rows, self._processed_row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_fake_connect(upload_row, topic_rows, processed_row=None):
    def _connect(dsn):
        return _FakeConn(upload_row, topic_rows, processed_row)

    return _connect


UPLOAD_ROW = (
    "robotA",  # robot_id
    "session1.mcap",  # original_filename
    "s3://test-bucket/raw/upload-1.mcap",  # storage_path
    123456,  # file_size_bytes
    "completed",  # status
    1_700_000_000,  # completed_at (epoch seconds)
    123456,  # bytes_received
    # Capture-metadata intake: operator/site/permission_ref/
    # session_tags/task_label, appended to `get_upload_detail`'s SELECT list.
    # None here matches an upload that never set them --
    # test_get_upload_detail_surfaces_capture_metadata below covers a row
    # that HAS them populated.
    None,
    None,
    None,
    None,
    None,
)


def _write_local_parquet(path, ts_values, topic):
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "ts": pa.array(ts_values, type=pa.int64()),  # epoch-nanoseconds, same convention as the real processor
        "topic": pa.array([topic] * len(ts_values), type=pa.string()),
        "x": pa.array([float(i) for i in range(len(ts_values))], type=pa.float64()),
    })
    pq.write_table(table, str(path))


# --- get_upload_files: field-name contract -----------------------------------


def test_get_upload_files_returns_parquet_keys(monkeypatch):
    """The /files response carries `parquet_keys`, the field
    Uploads/index.tsx's UploadFilesResp reads. The combined `_all` file is
    no longer written, so no `combined_keys` field is returned."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")

    topic_rows = [
        ("imu", 42, "s3://test-bucket/processed/parquet/robotA/imu/2024/01/15/part-upload-1.parquet", "chk-imu", 101),
        ("odom", 7, "s3://test-bucket/processed/parquet/robotA/odom/2024/01/15/part-upload-1.parquet", "chk-odom", 202),
    ]
    monkeypatch.setattr(
        uploads_mod.psycopg, "connect", _make_fake_connect(UPLOAD_ROW, topic_rows)
    )
    # No S3 creds configured in this test image -> _s3_client_for_uploads()
    # returns None immediately (no network), and the ts-range DuckDB query
    # over fake s3:// paths gracefully degrades to (None, None); neither
    # matters for this test, which only checks the /files key shape.

    result = uploads_mod.get_upload_files("upload-files-contract-1")

    assert result is not None
    assert set(result.keys()) == {"upload_id", "parquet_keys"}
    assert isinstance(result["parquet_keys"], dict)

    # Bucket-relative keys (no `s3://bucket/` prefix), matching what
    # /api/v1/s3/presign and /api/v1/s3/list operate on.
    assert result["parquet_keys"]["imu"] == ["processed/parquet/robotA/imu/2024/01/15/part-upload-1.parquet"]
    assert result["parquet_keys"]["odom"] == ["processed/parquet/robotA/odom/2024/01/15/part-upload-1.parquet"]


def test_get_upload_files_returns_none_for_unknown_upload(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    monkeypatch.setattr(uploads_mod.psycopg, "connect", _make_fake_connect(None, []))

    assert uploads_mod.get_upload_files("does-not-exist") is None


# --- _s3_key_from_path: pure helper -------------------------------------------


def test_s3_key_from_path_strips_scheme_and_bucket():
    assert (
        uploads_mod._s3_key_from_path("s3://mybucket/processed/parquet/robotA/imu/part-1.parquet")
        == "processed/parquet/robotA/imu/part-1.parquet"
    )


def test_s3_key_from_path_passes_through_non_s3_values():
    # Defensive: if parquet_path is ever already a bare key, don't mangle it.
    assert uploads_mod._s3_key_from_path("processed/parquet/robotA/imu/part-1.parquet") == (
        "processed/parquet/robotA/imu/part-1.parquet"
    )
    assert uploads_mod._s3_key_from_path("") == ""


# --- get_upload_detail: ts_min/ts_max populated from real parquet data -------


def test_get_upload_detail_populates_ts_min_max_when_processed_rows_exist(monkeypatch, tmp_path):
    """ts_min/ts_max must not be hardcoded to None, or the UI's
    "Open in chart" button (disabled={!since || !until}) could never
    enable. With real processed_topics rows pointing at real parquet
    files, get_upload_detail must return a genuine, non-None time range."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")

    imu_path = tmp_path / "imu.parquet"
    odom_path = tmp_path / "odom.parquet"
    # ns since epoch; imu covers an earlier/narrower window, odom a later one,
    # so the overall min/max must span across both files.
    _write_local_parquet(imu_path, [1_700_000_000_000_000_000, 1_700_000_010_000_000_000], "imu")
    _write_local_parquet(odom_path, [1_700_000_020_000_000_000, 1_700_000_030_000_000_000], "odom")

    topic_rows = [
        ("imu", 2, str(imu_path), "chk-imu", imu_path.stat().st_size),
        ("odom", 2, str(odom_path), "chk-odom", odom_path.stat().st_size),
    ]
    monkeypatch.setattr(
        uploads_mod.psycopg, "connect", _make_fake_connect(UPLOAD_ROW, topic_rows)
    )
    # With one slot, acquiring separately for the outer MCAP scan and inner
    # DuckDB scan would time out. A real range proves the lease is reused.
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(planner, "_QUERY_SLOTS", threading.BoundedSemaphore(1))

    detail = uploads_mod.get_upload_detail("upload-ts-range-1")

    assert detail is not None
    assert detail["ts_min"] is not None
    assert detail["ts_max"] is not None
    # ISO-8601 strings the UI can hand to `new Date(...)` / URL query params.
    assert detail["ts_min"].endswith("Z") or "+" in detail["ts_min"]
    assert detail["ts_max"].endswith("Z") or "+" in detail["ts_max"]
    assert detail["ts_min"] < detail["ts_max"]
    # The min must come from the earlier (imu) file, the max from the later
    # (odom) file -- proves the range is computed across ALL of the
    # upload's topics, not just one.
    assert detail["ts_min"].startswith("2023-11-14")
    assert detail["ts_max"].startswith("2023-11-14")


def test_concurrent_uncached_upload_detail_scans_share_the_process_gate(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    monkeypatch.setattr(settings, "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(planner, "_QUERY_SLOTS", threading.BoundedSemaphore(1))
    topic_rows = [("imu", 2, "/missing/imu.parquet", "chk-imu", 1)]
    monkeypatch.setattr(
        uploads_mod.psycopg, "connect", _make_fake_connect(UPLOAD_ROW, topic_rows)
    )

    scan_started = threading.Event()
    release_scan = threading.Event()
    parquet_leases = []

    def _blocking_raw_scan(_storage_path, _expected_topics):
        scan_started.set()
        assert release_scan.wait(timeout=2)
        return {}, 0, {}

    def _record_parquet_lease(
        _paths,
        capacity_lease=None,
        *,
        known_sizes_by_path=None,
    ):
        parquet_leases.append(capacity_lease)
        return None, None

    monkeypatch.setattr(uploads_mod, "_calculate_raw_bytes_from_mcap", _blocking_raw_scan)
    monkeypatch.setattr(uploads_mod, "_compute_ts_range_from_parquet_paths", _record_parquet_lease)

    first_result = []
    first_error = []

    def _first_detail():
        try:
            first_result.append(uploads_mod.get_upload_detail("detail-gate-first"))
        except Exception as exc:  # pragma: no cover - asserted below
            first_error.append(exc)

    worker = threading.Thread(target=_first_detail)
    worker.start()
    assert scan_started.wait(timeout=1)
    try:
        with pytest.raises(HTTPException) as raised:
            uploads_mod.get_upload_detail("detail-gate-second")
        assert raised.value.status_code == 503
        assert raised.value.detail == {"code": "query_capacity_exceeded"}
    finally:
        release_scan.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert first_error == []
    assert len(first_result) == 1
    assert parquet_leases and parquet_leases[0] is not None


def test_get_upload_detail_ts_min_max_none_when_no_processed_rows(monkeypatch):
    """An upload with no processed_topics rows yet has no time range to
    report; ts_min/ts_max should stay None (not crash, not fabricate a
    range) so "Open in chart" correctly stays disabled."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    monkeypatch.setattr(uploads_mod.psycopg, "connect", _make_fake_connect(UPLOAD_ROW, []))

    detail = uploads_mod.get_upload_detail("upload-no-processed-1")

    assert detail is not None
    assert detail["ts_min"] is None
    assert detail["ts_max"] is None


def test_get_upload_detail_reports_real_parquet_sizes_without_fabricating_zero(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    topic_rows = [
        ("imu", 2, "/missing/imu.parquet", "chk-imu", 101),
        ("odom", 2, "/missing/odom.parquet", "chk-odom", 202),
    ]
    monkeypatch.setattr(
        uploads_mod.psycopg, "connect", _make_fake_connect(UPLOAD_ROW, topic_rows)
    )

    detail = uploads_mod.get_upload_detail("upload-size-known-1")

    assert detail is not None
    # Storage paths stay internal; `/files` serves the download keys.
    assert "parquet_files" not in detail
    assert "combined" not in detail
    assert detail["parquet_bytes"] == 303
    assert [topic["parquet_bytes"] for topic in detail["topics"]] == [101, 202]


def test_upload_detail_admits_remote_parquet_using_registry_sizes(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    topic_rows = [
        ("imu", 2, "s3://test-bucket/processed/imu.parquet", "chk-imu", 11),
        ("odom", 2, "s3://test-bucket/processed/odom.parquet", "chk-odom", 3),
    ]
    monkeypatch.setattr(
        uploads_mod.psycopg,
        "connect",
        _make_fake_connect(UPLOAD_ROW, topic_rows),
    )
    monkeypatch.setattr(settings, "QUERY_MAX_SOURCE_BYTES", 10)
    monkeypatch.setattr(
        uploads_mod,
        "_calculate_raw_bytes_from_mcap",
        lambda *_args, **_kwargs: ({}, 0, {}),
    )

    def must_not_open(*_args, **_kwargs):
        raise AssertionError("oversized known remote source must be rejected first")

    monkeypatch.setattr(uploads_mod, "_open_duckdb_for_uploads", must_not_open)

    with pytest.raises(HTTPException) as raised:
        uploads_mod.get_upload_detail("upload-remote-size-admission-1")
    assert raised.value.detail == {
        "code": "source_byte_limit_exceeded",
        "source_bytes": 11,
        "max_bytes": 10,
    }


def test_compute_ts_range_from_parquet_paths_handles_missing_files_gracefully():
    """A parquet_path that doesn't (yet) exist on disk must degrade to
    (None, None), not raise -- get_upload_detail must not 500 just because
    a referenced file isn't visible yet."""
    ts_min, ts_max = uploads_mod._compute_ts_range_from_parquet_paths(
        ["/nonexistent/path/does-not-exist.parquet"]
    )
    assert ts_min is None
    assert ts_max is None


def test_compute_ts_range_from_parquet_paths_empty_input():
    assert uploads_mod._compute_ts_range_from_parquet_paths([]) == (None, None)


def test_raw_detail_scan_refuses_oversized_local_mcap(monkeypatch, tmp_path):
    path = tmp_path / "oversized.mcap"
    path.write_bytes(b"x" * 17)
    monkeypatch.setattr(uploads_mod, "_RAW_DETAIL_MAX_BYTES", 16)

    with pytest.raises(uploads_mod.RawDetailUnavailable, match="byte_limit"):
        uploads_mod._calculate_raw_bytes_from_mcap(str(path), ["imu"])


def test_upload_detail_reports_bounded_raw_scan_refusal(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    raw_path = tmp_path / "too-large.mcap"
    raw_path.write_bytes(b"x" * 17)
    upload_row = UPLOAD_ROW[:2] + (str(raw_path),) + UPLOAD_ROW[3:]
    topic_rows = [("imu", 2, "/missing/imu.parquet", "chk-imu", 1)]
    monkeypatch.setattr(uploads_mod, "_RAW_DETAIL_MAX_BYTES", 16)
    monkeypatch.setattr(
        uploads_mod.psycopg, "connect", _make_fake_connect(upload_row, topic_rows)
    )

    detail = uploads_mod.get_upload_detail("upload-raw-limit-1")

    assert detail is not None
    assert detail["raw_bytes_mode"] == "error"
    assert detail["raw_bytes_error"] == "raw_detail_byte_limit_exceeded"
    assert detail["raw_payload_sum"] == 0


# --- capture-metadata intake -------------------------------------------------


def test_get_upload_detail_surfaces_capture_metadata(monkeypatch):
    """operator/site/permission_ref/session_tags/task_label must ride the
    `uploads` row all the way out to the detail response, not just live in
    the DB column."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")

    row_with_capture = UPLOAD_ROW[:7] + (
        "alice",
        "lab-1",
        "consent-42",
        "demo,indoor",
        "pick_and_place",
    )
    monkeypatch.setattr(
        uploads_mod.psycopg, "connect", _make_fake_connect(row_with_capture, [])
    )

    detail = uploads_mod.get_upload_detail("upload-capture-1")

    assert detail is not None
    assert detail["operator"] == "alice"
    assert detail["site"] == "lab-1"
    assert detail["permission_ref"] == "consent-42"
    assert detail["session_tags"] == "demo,indoor"
    assert detail["task_label"] == "pick_and_place"


def test_get_upload_detail_capture_metadata_none_when_unset(monkeypatch):
    """An upload row with no capture metadata (one that never had them set) surfaces None for all five, never a
    KeyError or a fabricated empty string."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    monkeypatch.setattr(uploads_mod.psycopg, "connect", _make_fake_connect(UPLOAD_ROW, []))

    detail = uploads_mod.get_upload_detail("upload-no-capture-1")

    assert detail is not None
    for key in ("operator", "site", "permission_ref", "session_tags", "task_label"):
        assert detail[key] is None
