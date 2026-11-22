"""Unit tests for the `robots` fleet registry.

Two things are pinned here.

**`/api/robots` reads Postgres.** Listing S3 prefixes under
`processed/parquet/` would make the fleet a side-effect of which directories
happen to exist: a robot that had uploaded but not been processed yet would
be invisible, a robot with no data at all could not be listed, and a stray
prefix would be indistinguishable from a real machine. It reads the `robots`
table.

**The schema keeps the fleet FK safe.** `uploads.robot_id` references
`robots(id)` with comparable types, and deleting a robot never cascades to
its uploads. This is asserted against `db/migrations/001_baseline.sql`
itself, because this suite has no database.
"""
import re
from pathlib import Path

import pytest

from query_service import registry as registry_mod
from query_service.routers import query_api

REPO_ROOT = Path(__file__).resolve().parents[4]
MIGRATION = REPO_ROOT / "db" / "migrations" / "001_baseline.sql"


# --- /api/robots reads the registry ----------------------------------------


class _Cur:
    def __init__(self, rows, recorder):
        self._rows = rows
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._recorder.append(" ".join(sql.split()))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows, recorder):
        self._rows = rows
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cur(self._rows, self._recorder)


def _fake_connect(monkeypatch, rows):
    recorded = []
    monkeypatch.setattr(
        query_api.psycopg, "connect", lambda dsn: _Conn(rows, recorded)
    )
    monkeypatch.setattr(registry_mod, "registry_dsn", lambda: "postgres://fake/fake")
    return recorded


def test_list_robots_queries_the_robots_table(monkeypatch):
    recorded = _fake_connect(monkeypatch, [("demo",), ("rover-7",)])

    assert query_api.list_robots() == ["demo", "rover-7"]

    assert any("FROM robots" in sql for sql in recorded), recorded
    assert not any("parquet" in sql.lower() for sql in recorded), (
        "the fleet must come from the registry, not from what happens to be "
        f"in the processed lake: {recorded}"
    )


def test_list_robots_returns_a_bare_list_of_ids(monkeypatch):
    """The SPA types this endpoint as `string[]` in four places
    (Home, Uploads x2, Explore), so the response shape is a contract."""
    _fake_connect(monkeypatch, [("demo",)])

    result = query_api.list_robots()

    assert isinstance(result, list)
    assert all(isinstance(r, str) for r in result)


def test_list_robots_lists_a_robot_that_has_never_uploaded(monkeypatch):
    """A robot is in the fleet because it was provisioned, not because it
    produced data."""
    _fake_connect(monkeypatch, [("brand-new-robot",)])
    assert query_api.list_robots() == ["brand-new-robot"]


# --- schema ----------------------------------------------------------------


@pytest.fixture(scope="module")
def migration_sql():
    assert MIGRATION.is_file(), f"missing {MIGRATION}"
    return MIGRATION.read_text()


def _statement(sql: str, start_marker: str) -> str:
    statement = sql[sql.index(start_marker):]
    return statement[: statement.index(";")]


def test_robots_id_type_matches_uploads_robot_id(migration_sql):
    """A Postgres FK needs comparable types, and a wider column here would
    let an operator register an id no upload could ever reference
    (`uploads.robot_id` is VARCHAR(50))."""
    robots = _statement(migration_sql, "CREATE TABLE robots")
    uploads = _statement(migration_sql, "CREATE TABLE uploads")
    assert re.search(r"\bid\s+VARCHAR\(50\)\s+PRIMARY KEY", robots), robots
    assert re.search(r"\brobot_id\s+VARCHAR\(50\)\s+NOT NULL", uploads), uploads


def test_deleting_a_robot_does_not_cascade_to_its_uploads(migration_sql):
    """Every other FK in this schema cascades FROM uploads, because those
    rows are derived from an upload. This one points the other way: an
    `uploads` row is the system of record for bytes really stored in S3, and
    removing a fleet-registry entry must never silently delete it."""
    uploads = _statement(migration_sql, "CREATE TABLE uploads")
    fk = uploads[uploads.index("CONSTRAINT uploads_robot_id_fkey"):]
    fk = fk[: fk.index(",")]
    assert "REFERENCES robots(id) ON UPDATE CASCADE" in fk
    assert "ON DELETE CASCADE" not in fk, fk
