"""Hardening: `POST /api/v1/query/sql` requires auth.

Background: the SQL console was once the only guarded query route, leaving raw
telemetry, upload metadata, schema/plan details, S3, and observability public
under OIDC. `main.create_app()` now adds an unconditional dynamic viewer floor
to every data-bearing router. The SPA uses the shared authorized fetch path, so
it no longer requires an anonymous exception for its read calls.

`run_sql` retains its direct viewer dependency as defense in depth, and the
router-level dependency follows the same unconditional pattern: it reacts to
`AUTH_MODE` changing later instead of freezing the condition during app
registration. `require_role` itself is a no-op whenever
`settings.AUTH_MODE != "oidc"` (the default), so a stack with no identity
provider configured is unaffected -- exactly like every other `require_role`
call site in this service.

Ordering is deliberate and is what the last two tests below pin down:
`Depends` resolves before `run_sql`'s body runs, so an unauthenticated caller
gets 401 from `require_role` and never reaches `sqlguard` -- it must not be
able to distinguish "unauthenticated" from "unauthenticated, and also your
SQL was bad." An authenticated caller's rejected SQL still surfaces as
sqlguard's own 400, not a 401 -- auth and statement validation are different
failures, and the gate must not swallow the second one for a caller who
already cleared the first.

Authenticated cases obtain a real RS256 token from `GET /auth/dev/token`.
That pins the same signature-verification path used by a local development
deployment without encoding an alternate test-only authentication path.
"""
import importlib
import sys
from datetime import datetime, timezone

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

from query_service import settings  # noqa: E402

LEGIT_SQL = "SELECT topic, count(*) FROM all_parquet GROUP BY topic"
REJECTED_SQL = "SELECT * FROM duckdb_settings()"  # table function -- sqlguard rejects this


def _dev_token(roles=("viewer",)) -> str:
    response = client.get("/auth/dev/token", params={"roles": ",".join(roles)})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def oidc_dev_mode(monkeypatch):
    """Reproduce the deployed configuration (`configs/query.env`:
    AUTH_MODE=oidc, OIDC_DEV=true) for the duration of one test. Both are
    read live off `settings.*` by `auth.py`, so no module reload is needed.
    """
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", True)


def test_sql_route_401_without_token(oidc_dev_mode):
    resp = client.post("/api/v1/query/sql", json={"sql": LEGIT_SQL})
    assert resp.status_code == 401


def test_sql_route_200_with_token(oidc_dev_mode):
    token = _dev_token()
    resp = client.post(
        "/api/v1/query/sql",
        json={"sql": LEGIT_SQL},
        headers={"Authorization": f"Bearer {token}"},
    )
    # No live S3 in this environment, so execution returns the stable 503
    # backend-unavailable response. The point here is that the request got
    # past authentication; a valid token must not be mistaken for a 401/403.
    assert resp.status_code == 503
    assert resp.json() == {"detail": "query_unavailable"}


def test_sql_route_403_with_insufficient_role(oidc_dev_mode):
    """A token that authenticates but carries no role meeting the "viewer"
    threshold is a distinct failure from no token at all."""
    token = _dev_token(roles=())
    resp = client.post(
        "/api/v1/query/sql",
        json={"sql": LEGIT_SQL},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_sqlguard_400_survives_with_valid_auth(oidc_dev_mode):
    """An authenticated caller's bad SQL still gets sqlguard's own 400, not a
    401 -- the auth gate must not mask statement-validation failures for a
    caller who already cleared it."""
    token = _dev_token()
    resp = client.post(
        "/api/v1/query/sql",
        json={"sql": REJECTED_SQL},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "sql rejected" in resp.json().get("detail", "")


def test_unauthenticated_bad_sql_is_401_not_400(oidc_dev_mode):
    """The ordering half of the above: an unauthenticated caller gets 401
    regardless of whether its SQL would also have been rejected -- it must
    not learn that from the response."""
    resp = client.post("/api/v1/query/sql", json={"sql": REJECTED_SQL})
    assert resp.status_code == 401


def test_all_data_routers_reject_anonymous_oidc_requests(oidc_dev_mode):
    """Representative routes pin the app-factory viewer floor for every
    data-bearing router, including the former registration-time conditionals.
    The Pro repository tests the Pro routes."""
    requests = (
        ("GET", "/api/robots", None),
        ("GET", "/api/v1/uploads/list", None),
        ("GET", "/api/v1/query/limits", None),
        ("GET", "/api/v1/s3/list", None),
        ("GET", "/api/v1/robots/list", None),
        ("GET", "/api/obs/health", None),
        ("GET", "/api/v1/metrics/overview", None),
        ("GET", "/api/admin/ingest/status", None),
        ("GET", "/api/tenant", None),
        ("GET", "/api/selfcheck", None),
    )
    for method, path, body in requests:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, (method, path, response.text)


def test_query_limits_are_available_to_an_authenticated_viewer(oidc_dev_mode):
    token = _dev_token(("viewer",))
    response = client.get(
        "/api/v1/query/limits",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "max_source_range_seconds": settings.QUERY_MAX_SOURCE_RANGE_SECONDS,
        "max_source_files": settings.QUERY_MAX_SOURCE_FILES,
        "max_source_rows": settings.QUERY_MAX_SOURCE_ROWS,
        "max_source_bytes": settings.QUERY_MAX_SOURCE_BYTES,
        "query_timeout_seconds": settings.DUCKDB_QUERY_TIMEOUT_SECONDS,
        "max_concurrent_queries": settings.DUCKDB_QUERY_CONCURRENCY,
    }


def test_login_bootstrap_and_process_probes_remain_public(oidc_dev_mode):
    for path in ("/", "/healthz", "/readyz", "/metrics", "/api/v1/edition", "/api/v1/auth/config"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)


def test_selfcheck_requires_operator_role(oidc_dev_mode):
    viewer = _dev_token(("viewer",))
    assert client.get(
        "/api/selfcheck",
        headers={"Authorization": f"Bearer {viewer}"},
    ).status_code == 403

    operator = _dev_token(("operator",))
    assert client.get(
        "/api/selfcheck",
        headers={"Authorization": f"Bearer {operator}"},
    ).status_code == 200


def test_metrics_scrape_token_is_constant_contract_not_oidc(monkeypatch):
    monkeypatch.setattr(settings, "METRICS_AUTH_TOKEN", "m" * 32)

    missing = client.get("/metrics")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert client.get("/metrics", headers={"authorization": "Bearer wrong"}).status_code == 401
    accepted = client.get("/metrics", headers={"authorization": f"Bearer {'m' * 32}"})
    assert accepted.status_code == 200
    assert "text/plain" in accepted.headers["content-type"]


def test_default_auth_mode_leaves_the_route_open():
    """Without the oidc_dev_mode fixture, AUTH_MODE is whatever the test
    process's environment left it as (the `dev` Docker service sets none, so
    this is "none" in practice) -- require_role no-ops, matching every
    other call site, and the route stays reachable exactly as before this
    change."""
    resp = client.post("/api/v1/query/sql", json={"sql": LEGIT_SQL})
    assert resp.status_code != 401


def test_sql_execution_failure_does_not_expose_backend_details(monkeypatch, caplog):
    from query_service.routers import query_api

    sentinel = "postgres://owner:super-secret@db/private/object.parquet"
    monkeypatch.setattr(settings, "s3_enabled", lambda: True)

    def fail_open(**kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(query_api.planner, "open_duckdb", fail_open)
    response = client.post("/api/v1/query/sql", json={"sql": LEGIT_SQL})

    assert response.status_code == 503
    assert response.json() == {"detail": "query_unavailable"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"sql": ""},
        {"sql": "   "},
        {"sql": 1},
        {"sql": None},
        {"sql": ["SELECT 1"]},
    ],
)
def test_sql_rejects_missing_empty_and_non_string_statements(body):
    response = client.post("/api/v1/query/sql", json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_sql"}


def test_sql_console_always_closes_its_duckdb_connection(monkeypatch):
    from query_service.routers import query_api

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(settings, "s3_enabled", lambda: True)
    monkeypatch.setattr(
        query_api.planner,
        "open_duckdb",
        lambda **kwargs: connection,
    )
    monkeypatch.setattr(
        query_api.planner,
        "create_all_parquet_view",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        query_api,
        "_execute_bounded_console_sql",
        lambda *args: (["value"], [[1]]),
    )

    response = client.post("/api/v1/query/sql", json={"sql": LEGIT_SQL})

    assert response.status_code == 200
    assert connection.closed is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT missing_column FROM all_parquet",
        "SELECT no_such_function(topic) FROM all_parquet",
    ],
)
def test_permanent_console_statement_errors_are_stable_400(monkeypatch, sql):
    import duckdb
    from query_service.routers import query_api

    monkeypatch.setattr(settings, "s3_enabled", lambda: True)

    def open_local(**kwargs):
        assert kwargs == {
            "memory_limit_mb": settings.SQL_CONSOLE_MEMORY_LIMIT_MB,
        }
        return duckdb.connect(database=":memory:")

    def create_local_relation(con):
        con.execute("CREATE TABLE all_parquet (topic VARCHAR)")

    monkeypatch.setattr(query_api.planner, "open_duckdb", open_local)
    monkeypatch.setattr(
        query_api.planner,
        "create_all_parquet_view",
        create_local_relation,
    )

    response = client.post("/api/v1/query/sql", json={"sql": sql})

    assert response.status_code == 400
    assert response.json() == {"detail": "query_invalid"}


def test_deferred_missing_registry_object_returns_redacted_503(
    monkeypatch, tmp_path
):
    """A registered object that disappears before execution fails the query."""
    import duckdb
    from query_service import planner, registry as registry_mod
    from query_service.routers import query_api

    registered = tmp_path / "registered" / "part-old.parquet"
    registered.parent.mkdir()
    writer = duckdb.connect(database=":memory:")
    try:
        writer.execute(
            "COPY (SELECT range AS n FROM range(2)) TO ? (FORMAT PARQUET)",
            [str(registered)],
        )
    finally:
        writer.close()

    entry = registry_mod.ParquetFile(
        file_path=str(registered),
        robot_id="robotA",
        message_type="imu",
        schema_version=1,
        schema_hash="a" * 64,
        row_count=2,
        start_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        column_names=["n"],
        all_null_columns=[],
        file_size_bytes=registered.stat().st_size,
    )
    query_plan = planner.QueryPlan(
        files=[entry],
        uris=[str(registered)],
    )

    monkeypatch.setattr(settings, "s3_enabled", lambda: True)
    monkeypatch.setattr(planner, "plan", lambda *args, **kwargs: query_plan)

    def open_local(**kwargs):
        assert kwargs == {
            "memory_limit_mb": settings.SQL_CONSOLE_MEMORY_LIMIT_MB,
        }
        return duckdb.connect(
            database=":memory:",
            config={
                "memory_limit": f"{settings.SQL_CONSOLE_MEMORY_LIMIT_MB}MiB",
            },
        )

    monkeypatch.setattr(planner, "open_duckdb", open_local)
    execute = query_api._execute_bounded_console_sql
    first_attempt = True

    def remove_registered_then_execute(con, sql):
        nonlocal first_attempt
        if first_attempt:
            first_attempt = False
            registered.unlink()
        return execute(con, sql)

    monkeypatch.setattr(
        query_api,
        "_execute_bounded_console_sql",
        remove_registered_then_execute,
    )

    response = client.post(
        "/api/v1/query/sql",
        json={"sql": "SELECT count(*) AS n FROM all_parquet"},
    )

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "query_unavailable"}
    assert str(registered) not in response.text
    assert first_attempt is False


def test_console_execution_io_failure_remains_a_redacted_503(monkeypatch):
    import duckdb
    from query_service.routers import query_api

    class Connection:
        def close(self):
            pass

    sentinel = "s3://private-bucket/sensitive-object.parquet"
    monkeypatch.setattr(settings, "s3_enabled", lambda: True)
    monkeypatch.setattr(
        query_api.planner,
        "open_duckdb",
        lambda **kwargs: Connection(),
    )
    monkeypatch.setattr(
        query_api.planner,
        "create_all_parquet_view",
        lambda con: [],
    )

    def fail_execute(con, sql):
        raise duckdb.IOException(sentinel)

    monkeypatch.setattr(query_api, "_execute_bounded_console_sql", fail_execute)

    response = client.post("/api/v1/query/sql", json={"sql": "SELECT 1"})

    assert response.status_code == 503
    assert response.json() == {"detail": "query_unavailable"}
    assert sentinel not in response.text
