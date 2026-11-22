"""
Regression test for the error-vs-empty rule (distinguish "empty" from "error" in read
endpoints).

Before the fix, several read paths caught ALL exceptions and returned an
empty/None/zero result, so a genuine Postgres/S3 outage (wrong
`DATABASE_URL`, connection refused, bad credentials, DNS failure, etc.) was
indistinguishable from "there is genuinely no data yet". An operator on a
freshly (mis)configured box would see an empty-but-"healthy" UI with no way
to tell the pipeline was broken.

The fix narrows each broad `except Exception` down to only the specific
"not bootstrapped yet" case:
  - `uploads.py:read_index_page` / `get_upload_detail`: only
    `psycopg.errors.UndefinedTable` (the `uploads` table doesn't exist yet
    in a fresh/un-migrated DB) is swallowed into an empty result. Every
    other psycopg error (e.g. `OperationalError` from a connection
    failure) now propagates.
  - `list_robots`: only the "not bootstrapped yet" shape is swallowed into
    `[]`. Every other backend error propagates and surfaces as a 500 through
    FastAPI's default handler. (This endpoint reads the `robots` table, not
    an S3 prefix listing, so the not-bootstrapped shape is
    now Postgres' `UndefinedTable` rather than S3's `NoSuchBucket` -- see the
    section comment below. The invariant is unchanged.)

This test exercises both functions directly (no live Postgres/S3 required)
by monkeypatching the psycopg boundary to raise each kind of error, and
additionally drives `list_robots` through FastAPI's `TestClient` to confirm
a real outage surfaces as an HTTP 500 rather than a `200` with an empty body.

`main.py` is split into modules, so the symbols under test are
imported from the module that now owns each of them (`routers.query_api`,
`planner`, `settings`) rather than all from `main`. Patching them on `main`
would have silently done nothing.
"""
import importlib
import sys

import psycopg
import pytest

from query_service import planner
from query_service import registry as registry_mod
from query_service import uploads as uploads_mod
from query_service.routers import query_api


def _import_query_service_main():
    """Import query_service.main, working around an environment-only issue.

    main.py mounts a FastAPI StaticFiles app pointing at
    phases/05-query-service/src/ui, which is only populated by the ui-spa
    npm build step baked into the service's Docker image -- it does not
    exist in the plain dev/test image. A bare import therefore raises
    RuntimeError before we ever get to the endpoint we want to test. We
    stub out StaticFiles for the duration of the import only; this has no
    bearing on the error-handling logic under test here. (Same workaround
    as tests/unit/test_bucket_validation.py.)
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

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(qsm.app, raise_server_exceptions=False)


# --- read_index_page (uploads.py) ------------------------------------------


def _call_read_index_page():
    return uploads_mod.read_index_page(
        robot_id=None,
        since=None,
        until=None,
        limit=50,
        offset=0,
        order_by="completed_at",
        order_dir="desc",
    )


def test_read_index_page_propagates_genuine_backend_error(monkeypatch):
    """A real Postgres outage (e.g. mis-set DATABASE_URL, connection
    refused) must NOT be silently reported as an empty page."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@nonexistent-host:5432/robotvault")

    def _raise_connect(dsn):
        raise psycopg.OperationalError(
            'connection to server at "nonexistent-host" (127.0.0.1), port 5432 failed: Connection refused'
        )

    monkeypatch.setattr(uploads_mod.psycopg, "connect", _raise_connect)

    with pytest.raises(psycopg.OperationalError):
        _call_read_index_page()


def test_read_index_page_returns_empty_when_not_bootstrapped(monkeypatch):
    """A fresh/un-migrated DB where the `uploads` table doesn't exist yet
    is genuinely "no data" and should still return an empty page, not an
    error."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")

    def _raise_connect(dsn):
        raise psycopg.errors.UndefinedTable('relation "uploads" does not exist')

    monkeypatch.setattr(uploads_mod.psycopg, "connect", _raise_connect)

    rows, total_count = _call_read_index_page()
    assert rows == []
    assert total_count == 0


# --- get_upload_detail (uploads.py) -----------------------------------------


def test_get_upload_detail_propagates_genuine_backend_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@nonexistent-host:5432/robotvault")

    def _raise_connect(dsn):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(uploads_mod.psycopg, "connect", _raise_connect)

    with pytest.raises(psycopg.OperationalError):
        uploads_mod.get_upload_detail("some-upload-id")


def test_get_upload_detail_returns_none_when_not_bootstrapped(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")

    def _raise_connect(dsn):
        raise psycopg.errors.UndefinedTable('relation "uploads" does not exist')

    monkeypatch.setattr(uploads_mod.psycopg, "connect", _raise_connect)

    assert uploads_mod.get_upload_detail("some-upload-id") is None


# --- list_robots (routers/query_api.py) ------------------------------------
#
# `/api/robots` reads the `robots` table, not an S3 prefix listing, so the boundary these tests fake moved from
# boto3 to psycopg. The INVARIANT under test is unchanged and is what matters:
# "not bootstrapped yet" is an empty fleet, any real backend failure is an
# error. Only the "not bootstrapped" shape differs -- it used to be S3's
# `NoSuchBucket`, it is now Postgres' `UndefinedTable`.


def _fake_pg(error: Exception):
    """A psycopg.connect replacement that raises `error` on connect."""
    def _connect(*args, **kwargs):
        raise error
    return _connect


def test_list_robots_returns_empty_when_db_not_migrated(monkeypatch):
    """A database that has not run db/migrations has no `robots` table.
    That is genuinely "no fleet registered yet", not a backend failure."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/robotvault")
    monkeypatch.setattr(
        query_api.psycopg,
        "connect",
        _fake_pg(psycopg.errors.UndefinedTable('relation "robots" does not exist')),
    )

    assert query_api.list_robots() == []

    resp = client.get("/api/robots")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_robots_returns_empty_when_no_database_configured(monkeypatch):
    """No DATABASE_URL at all: nothing to ask, so an empty fleet -- the same
    answer the S3-listing version gave when no bucket was configured."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert query_api.list_robots() == []


def test_list_robots_propagates_genuine_backend_outage(monkeypatch):
    """A real Postgres outage (unreachable host, wrong credentials, ...)
    must NOT be silently reported as "no robots"."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@nonexistent-host:5432/robotvault")
    monkeypatch.setattr(
        query_api.psycopg,
        "connect",
        _fake_pg(psycopg.OperationalError("connection to server failed: Name or service not known")),
    )

    with pytest.raises(psycopg.OperationalError):
        query_api.list_robots()

    resp = client.get("/api/robots")
    assert resp.status_code == 500


# --- robot_time_range (routers/query_api.py) -------------------------------


def test_robot_time_range_returns_none_when_no_database_configured(monkeypatch):
    """No registry configured is "no data", answered from Postgres alone."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _must_not_open_duckdb(*_args, **_kwargs):
        raise AssertionError("the time range is a registry aggregate")

    monkeypatch.setattr(planner, "open_duckdb", _must_not_open_duckdb)
    assert query_api.robot_time_range("robotA") == {"t_min": None, "t_max": None}


def test_robot_time_range_propagates_genuine_backend_error(monkeypatch):
    """A registry outage is a real outage and must not come back looking
    identical to "no data"."""

    def _raise_connect(*_args, **_kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setattr(registry_mod.psycopg, "connect", _raise_connect)

    with pytest.raises(psycopg.OperationalError):
        query_api.robot_time_range("robotA")
