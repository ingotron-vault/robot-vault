"""Unit tests for `GET /api/v1/metrics/overview` (Home's KPI tiles).

Covers, with no live Postgres anywhere (every test monkeypatches
`obs.psycopg.connect`, same pattern as `test_fleet_registry.py`):

  - happy path: the four KPIs come back as plain ints, one query per key.
  - no `DATABASE_URL` -> all four null (matches `fleet.py::robots_list`'s
    "cannot confirm either way, do not fabricate" rule).
  - `UndefinedTable` on `psycopg.connect` itself (an un-migrated DB) -> all
    four null.
  - `UndefinedTable` on ONE query only -- the shape a core edition takes,
    which has no `episodes`/`qa_reports` tables at all -- nulls just that
    KPI and still answers the other three. This is the one behaviour that
    does NOT mirror `fleet.py` (which has a single query, so it cannot
    partially fail): each KPI here is independent, so one missing table must
    not blank out the rest of the row.

Grafana/Prometheus are gone from this endpoint entirely; nothing here talks
to either.
"""
from __future__ import annotations

import psycopg
import pytest

from query_service.routers import obs

DSN = "postgresql://user:pass@localhost:5432/robotvault"


class _FakeCursor:
    def __init__(self, values, raise_for=None):
        self.values = values
        self.raise_for = raise_for or {}
        self._last_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql
        for needle, err in self.raise_for.items():
            if needle in sql:
                raise err

    def fetchone(self):
        flat = self._last_sql
        if "count(*) FROM uploads WHERE created_at" in flat:
            return (self.values.get("uploads_last_hour", 0),)
        if "sum(file_size_bytes)" in flat:
            return (self.values.get("storage_bytes_total", 0),)
        if "FROM episodes e" in flat:
            return (self.values.get("episodes_awaiting_qa", 0),)
        if "FROM export_status" in flat:
            return (self.values.get("exports_completed_24h", 0),)
        raise AssertionError(f"unhandled SQL in fake cursor: {flat}")


class _FakeConn:
    def __init__(self, values, raise_for=None):
        self.values = values
        self.raise_for = raise_for
        self.rolled_back = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.values, self.raise_for)

    def rollback(self):
        self.rolled_back += 1


def _connect(monkeypatch, values=None, raise_for=None, connect_error=None):
    monkeypatch.setenv("DATABASE_URL", DSN)
    captured = {}

    def _fake_connect(dsn, **kwargs):
        # This KPI endpoint runs on a threadpool worker and is polled per open
        # tab, so it must bound both the connect and every statement (mirroring
        # admin_api.py / main.py). Capture the kwargs so a test can assert the
        # bounds are actually passed and cannot be silently dropped later.
        captured["dsn"] = dsn
        captured.update(kwargs)
        if connect_error is not None:
            raise connect_error
        assert dsn == DSN
        return _FakeConn(values or {}, raise_for)

    monkeypatch.setattr(obs.psycopg, "connect", _fake_connect)
    return captured


# --- happy path --------------------------------------------------------------


def test_metrics_overview_happy_path_returns_four_int_kpis(monkeypatch):
    captured = _connect(
        monkeypatch,
        values={
            "uploads_last_hour": 3,
            "storage_bytes_total": 123456789,
            "episodes_awaiting_qa": 7,
            "exports_completed_24h": 2,
        },
    )

    result = obs.metrics_overview()

    # The connect must be bounded on both axes -- otherwise a blackholed
    # Postgres pins a threadpool worker per poll and starves every sync route.
    assert captured.get("connect_timeout") == 3
    assert "statement_timeout" in str(captured.get("options", ""))

    assert result == {
        "kpis": {
            "uploads_last_hour": 3,
            "storage_bytes_total": 123456789,
            "episodes_awaiting_qa": 7,
            "exports_completed_24h": 2,
        }
    }
    assert all(isinstance(v, int) for v in result["kpis"].values())


def test_metrics_overview_coalesces_a_null_sum_to_zero_not_none(monkeypatch):
    """`storage_bytes_total` wraps its sum in COALESCE(..., 0), so an empty
    `uploads` table reads as 0 bytes stored, not "unknown"."""
    _connect(monkeypatch, values={"storage_bytes_total": 0})

    result = obs.metrics_overview()

    assert result["kpis"]["storage_bytes_total"] == 0


# --- no database configured ---------------------------------------------------


def test_metrics_overview_without_database_url_is_all_null(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = obs.metrics_overview()

    assert result == {
        "kpis": {
            "uploads_last_hour": None,
            "storage_bytes_total": None,
            "episodes_awaiting_qa": None,
            "exports_completed_24h": None,
        }
    }


# --- UndefinedTable -----------------------------------------------------------


def test_metrics_overview_undefined_table_on_connect_is_all_null(monkeypatch):
    """An un-migrated DB has none of these tables -- "no data", not a 500."""
    _connect(monkeypatch, connect_error=psycopg.errors.UndefinedTable("no such relation"))

    result = obs.metrics_overview()

    assert result == {
        "kpis": {
            "uploads_last_hour": None,
            "storage_bytes_total": None,
            "episodes_awaiting_qa": None,
            "exports_completed_24h": None,
        }
    }


def test_metrics_overview_undefined_table_on_one_query_nulls_only_that_kpi(monkeypatch):
    """The shape a `core` edition takes: `uploads` exists, but the pro-only
    `episodes`/`qa_reports` tables do not. `episodes_awaiting_qa` must go
    null on its own without blanking out the other three KPIs."""
    _connect(
        monkeypatch,
        values={
            "uploads_last_hour": 5,
            "storage_bytes_total": 999,
            "exports_completed_24h": 1,
        },
        raise_for={"FROM episodes e": psycopg.errors.UndefinedTable('relation "episodes" does not exist')},
    )

    result = obs.metrics_overview()

    assert result["kpis"] == {
        "uploads_last_hour": 5,
        "storage_bytes_total": 999,
        "episodes_awaiting_qa": None,
        "exports_completed_24h": 1,
    }


def test_metrics_overview_other_psycopg_errors_propagate(monkeypatch):
    """Only UndefinedTable reads as "no data" (the error-vs-empty rule, same as
    `fleet.py`). A refused connection, bad auth, etc. must not be swallowed
    into a silent null -- that would be indistinguishable from a real outage
    reading as a healthy empty deployment."""
    _connect(monkeypatch, connect_error=psycopg.OperationalError("could not connect"))

    with pytest.raises(psycopg.OperationalError):
        obs.metrics_overview()
