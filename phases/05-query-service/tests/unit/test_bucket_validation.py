"""
Regression test for the bucket param SQL injection.

The `bucket` query param (a time granularity: second|minute|hour|day) is
interpolated directly into `date_trunc('{bucket}', ts_ts)` in both
`/api/timeseries` and `/api/v1/robots/{robot_id}/stats/{message_type}`
(now phases/05-query-service/src/query_service/routers/query_api.py), because DuckDB
requires date_trunc's unit argument to be a SQL literal -- it cannot be
passed as a bind parameter. The DuckDB connection used to run that query
holds S3-write credentials, so before the fix a crafted `bucket` value
(e.g. `minute'), (SELECT ...) --`) could break out of the string literal
and inject arbitrary SQL, including a `COPY ... TO 's3://...'`.

The fix adds `validate_bucket()`, an allowlist check against
`{'second','minute','hour','day'}` that raises `HTTPException(400)` and is
called at the top of both endpoints, before any SQL string is built.

This test:
  1. Exercises `_validate_bucket()` directly for all four valid values and
     for a representative injection payload.
  2. Hits both endpoints through FastAPI's TestClient to prove the guard is
     actually wired in (not just present as an unused helper), and that an
     invalid `bucket` is rejected with 400 before any DuckDB/S3 access --
     no live DB/S3 required, since the endpoints 400 out before touching
     either.
"""
import importlib
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from query_service import planner


def _import_query_service_main():
    """Import query_service.main, working around an environment-only issue.

    main.py mounts a FastAPI StaticFiles app pointing at
    phases/05-query-service/src/ui, which is only populated by the ui-spa
    npm build step baked into the service's Docker image -- it does not
    exist in the plain dev/test image. A bare import therefore raises
    RuntimeError before we ever get to the code we want to test. We stub
    out StaticFiles for the duration of the import only; this has no
    bearing on the bucket-validation logic under test here.
    """
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


qsm = _import_query_service_main()

VALID_BUCKETS = ["second", "minute", "hour", "day"]

INJECTION_PAYLOADS = [
    "minute'), (SELECT 1) --",
    "minute'); COPY (SELECT 1) TO 's3://evil/x.parquet'; --",
    "day' UNION SELECT 1--",
    "not_a_bucket",
    "",
    "MINUTE",  # case must match exactly -- no case-insensitive bypass
    "minute ",  # no whitespace bypass
]


# --- 1. Direct unit tests of the allowlist helper -------------------------

@pytest.mark.parametrize("bucket", VALID_BUCKETS)
def test_validate_bucket_accepts_all_four_valid_values(bucket):
    assert planner.validate_bucket(bucket) == bucket


@pytest.mark.parametrize("bucket", INJECTION_PAYLOADS)
def test_validate_bucket_rejects_invalid_values(bucket):
    with pytest.raises(HTTPException) as exc_info:
        planner.validate_bucket(bucket)
    assert exc_info.value.status_code == 400


# --- 2. End-to-end: the guard is actually wired into both endpoints -------

client = TestClient(qsm.app, raise_server_exceptions=False)

INJECTION_PAYLOAD = "minute'); COPY (SELECT 1) TO 's3://evil/x.parquet'; --"


def test_timeseries_rejects_invalid_bucket_with_400():
    resp = client.get("/api/timeseries", params={"bucket": INJECTION_PAYLOAD})
    assert resp.status_code == 400
    assert "bucket" in resp.json()["detail"]


@pytest.mark.parametrize("bucket", VALID_BUCKETS)
def test_timeseries_valid_bucket_passes_the_guard(bucket):
    # Without live S3 config, the endpoint fails downstream with a
    # RuntimeError (-> 500) once it gets past validation. Asserting 500
    # (not 400) proves the value cleared the bucket allowlist rather than
    # being rejected by it -- exercising the full endpoint without
    # requiring a mocked DuckDB/S3 layer.
    resp = client.get("/api/timeseries", params={"bucket": bucket})
    assert resp.status_code != 400


def test_robot_message_stats_rejects_invalid_bucket_with_400():
    resp = client.get(
        "/api/v1/robots/robotA/stats/imu",
        params={"bucket": INJECTION_PAYLOAD},
    )
    assert resp.status_code == 400
    assert "bucket" in resp.json()["detail"]


@pytest.mark.parametrize("bucket", VALID_BUCKETS)
def test_robot_message_stats_valid_bucket_accepted(bucket):
    # S3 is not configured in this test environment, so the endpoint takes
    # its documented "S3 disabled" short-circuit (`return {"stats": []}`,
    # HTTP 200) -- proving the value passed the bucket guard without
    # needing a mocked DuckDB/S3 layer.
    resp = client.get(
        "/api/v1/robots/robotA/stats/imu",
        params={"bucket": bucket},
    )
    assert resp.status_code == 200
    assert resp.json() == {"stats": []}


def test_robot_message_stats_omitted_bucket_still_works():
    """bucket is optional on this endpoint; omitting it must not raise."""
    resp = client.get("/api/v1/robots/robotA/stats/imu")
    assert resp.status_code == 200
