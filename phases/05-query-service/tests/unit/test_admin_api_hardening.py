"""Production boundary tests for the operator ingest-status endpoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from query_service import settings
from query_service.routers import admin_api


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "none")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_api.router)
    return TestClient(app)


@pytest.mark.parametrize("limit", [0, 101, -1, 10_000_000])
def test_ingest_status_rejects_unbounded_limits(limit):
    response = _client().get("/api/admin/ingest/status", params={"limit": limit})
    assert response.status_code == 422


def test_ingest_status_requires_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = _client().get("/api/admin/ingest/status")
    assert response.status_code == 503
    assert response.json() == {"detail": "db_not_configured"}


def test_ingest_status_uses_bounded_database_timeouts(monkeypatch):
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

        def fetchall(self):
            sql = calls[-1][0]
            if "FROM uploads GROUP BY" in sql:
                return [("completed", 2)]
            if "FROM processing_status" in sql:
                return [("processed", 1)]
            return [("upload-1", "robot-1", "run.mcap", "completed", None, None)]

        def fetchone(self):
            return (2,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return Cursor()

    connect_calls = []

    def connect(dsn, **kwargs):
        connect_calls.append((dsn, kwargs))
        return Connection()

    monkeypatch.setenv("DATABASE_URL", "postgres://configured")
    monkeypatch.setattr(admin_api.psycopg, "connect", connect)
    monkeypatch.setattr(admin_api, "audit", lambda *args, **kwargs: None)

    response = _client().get("/api/admin/ingest/status", params={"limit": 25})

    assert response.status_code == 200
    assert response.json()["uploads_counts"] == {"completed": 2}
    assert connect_calls == [
        (
            "postgres://configured",
            {"connect_timeout": 3, "options": "-c statement_timeout=3000"},
        )
    ]
    assert any(params == (25,) for _, params in calls)


def test_ingest_status_sanitizes_database_failures(monkeypatch, caplog):
    sentinel = "postgres://owner:super-secret@db/internal"

    def connect(*args, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setenv("DATABASE_URL", "postgres://configured")
    monkeypatch.setattr(admin_api.psycopg, "connect", connect)

    response = _client().get("/api/admin/ingest/status")

    assert response.status_code == 503
    assert response.json() == {"detail": "database_unavailable"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    assert "RuntimeError" in caplog.text


def test_removed_noop_backfill_is_not_advertised():
    paths = {route.path for route in admin_api.router.routes}
    assert "/api/admin/backfill/uploads" not in paths
