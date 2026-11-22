"""The query service's S3 credentials must not be readable by a
query running on the connection that uses them.

Background. `configs/query.env` gave the query service the object store ROOT keys.
Those were installed with `SET s3_access_key_id=... / SET
s3_secret_access_key=...`, which stores them in session settings, where any
statement on that connection could read them back:

    SELECT current_setting('s3_secret_access_key')

`POST /api/v1/query/sql` runs caller-supplied SQL on exactly such a
connection, and `sqlguard` only inspected relations -- so that statement was
accepted and returned the root secret in the HTTP response body. Credentials
now live in a DuckDB SECRET, which `current_setting` cannot see.

The first test executes nothing and therefore runs everywhere, including on a
box with no DuckDB extensions and no network: it asserts the *statements*
never put the secret in session settings. The rest execute against a real
in-memory DuckDB (`CREATE SECRET` needs no httpfs and no network).
"""
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from query_service import planner  # noqa: E402

SECRET = "SUPER-SECRET-ROOT-KEY"
KEY_ID = "ROOT-KEY-ID"


class _RecordingConnection:
    """Records SQL instead of running it."""

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(sql)
        return self


def _install(con):
    planner.install_s3_credentials(
        con,
        endpoint="http://objectstore:9000",
        region="us-east-1",
        access_key_id=KEY_ID,
        secret_access_key=SECRET,
        secure=False,
    )


@pytest.fixture
def deployed_like_env(monkeypatch):
    """Reproduce the configuration the service actually runs in.

    This fixture is the difference between a test that passes and a test that
    means something. DuckDB's httpfs extension seeds `s3_access_key_id` /
    `s3_secret_access_key` from `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
    when it loads, and those are exactly the variables this service takes its
    credentials from (`configs/query.env` sets them in the container). The
    first version of these tests neither loaded httpfs nor set those
    variables, so they passed against a connection that had nothing to leak —
    while the deployed one still handed out the root secret.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)


def _connection_like_production():
    """A connection built the way `planner.open_duckdb` builds one: httpfs
    loaded BEFORE the credentials are installed."""
    con = duckdb.connect(database=":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    return con


def test_credentials_are_never_put_into_session_settings():
    """The always-runs guard: no execution, no extensions, no network."""
    con = _RecordingConnection()
    _install(con)

    joined = "\n".join(con.statements)
    lowered = joined.lower()
    assert "create" in lowered and "secret" in lowered, (
        "credentials must be installed as a DuckDB SECRET"
    )

    # The property is about the VALUE, not the setting name: the helper does
    # issue `SET s3_secret_access_key=''`, deliberately, to clear what httpfs
    # seeded from the environment. What must never happen is a SET that
    # carries the credential itself.
    for line in joined.splitlines():
        if line.strip().lower().startswith("set "):
            assert SECRET not in line and KEY_ID not in line, (
                f"this statement puts a credential where current_setting() can "
                f"read it: {line.strip()}"
            )

    # ...and the blanking must actually be issued, or httpfs's environment
    # seeding leaves the credential readable in the deployed configuration.
    for setting in ("s3_secret_access_key", "s3_access_key_id"):
        assert f"SET {setting}=''" in joined, (
            f"{setting} is not cleared; httpfs seeds it from AWS_* in the "
            f"environment, so the SECRET alone does not close the leak"
        )


def test_secret_is_not_readable_through_current_setting(deployed_like_env):
    """The real regression test: httpfs loaded and AWS_* present in the
    environment, i.e. the configuration the container runs in."""
    con = _connection_like_production()
    _install(con)

    for name in ("s3_secret_access_key", "s3_access_key_id"):
        value = con.execute(f"SELECT current_setting('{name}')").fetchone()[0]
        assert value not in (SECRET, KEY_ID), (
            f"current_setting('{name}') returned the live credential"
        )


def test_httpfs_really_does_seed_the_settings_from_the_environment(deployed_like_env):
    """Pins the behaviour the fix exists for, so nobody deletes the blanking
    as redundant. Without it, loading httpfs alone puts the credentials where
    any query can read them."""
    con = _connection_like_production()
    leaked = con.execute("SELECT current_setting('s3_secret_access_key')").fetchone()[0]
    assert leaked == SECRET, (
        "httpfs no longer seeds s3_secret_access_key from AWS_SECRET_ACCESS_KEY; "
        "if that is really true for this DuckDB version, the blanking in "
        "install_s3_credentials is belt-and-braces rather than the fix, and this "
        "test should be updated deliberately rather than deleted"
    )


def test_settings_are_blanked_not_merely_shadowed_by_the_secret(deployed_like_env):
    """Creating the SECRET alone is NOT enough -- that was the first version
    of this fix, and it left the deployed configuration leaking."""
    con = _connection_like_production()
    con.execute(
        "CREATE OR REPLACE SECRET s3_lake (TYPE S3, KEY_ID 'x', SECRET 'y', "
        "REGION 'us-east-1', ENDPOINT 'objectstore:9000', URL_STYLE 'path', USE_SSL false)"
    )
    still_there = con.execute("SELECT current_setting('s3_secret_access_key')").fetchone()[0]
    assert still_there == SECRET, "precondition: the SECRET alone does not clear the setting"

    _install(con)
    assert con.execute("SELECT current_setting('s3_secret_access_key')").fetchone()[0] != SECRET


def test_secret_value_is_redacted_in_the_secrets_listing():
    """`duckdb_secrets()` is a table function, so sqlguard rejects it on the
    console path -- but it should not hand over the secret even where it is
    reachable.

    Note what this does and does not claim: the SECRET is redacted, the
    KEY_ID is not. That is why the console's rejection of table functions is
    load-bearing here rather than incidental.
    """
    con = duckdb.connect(database=":memory:")
    _install(con)

    listing = str(con.execute("SELECT * FROM duckdb_secrets()").fetchall())
    assert SECRET not in listing
    assert "redacted" in listing


def test_the_console_gate_rejects_the_secrets_listing():
    """Since duckdb_secrets() does expose the key id, the guard that keeps it
    off the console path is part of this fix, not a coincidence."""
    from query_service import sqlguard

    assert sqlguard.validate_read_only_sql("SELECT * FROM duckdb_secrets()") is not None


def test_quote_identifier_escapes_embedded_quotes():
    assert planner.quote_identifier("x") == '"x"'
    assert planner.quote_identifier('a"b') == '"a""b"'
    # The shape that would otherwise close the identifier early and append SQL.
    injected = planner.quote_identifier('x" , (SELECT 1) AS "y')
    assert injected.startswith('"') and injected.endswith('"')
    assert injected.count('"') % 2 == 0


def test_sql_literal_escapes_embedded_quotes():
    assert planner.sql_literal("abc") == "'abc'"
    assert planner.sql_literal("it's") == "'it''s'"


def test_quoted_identifier_round_trips_through_duckdb():
    """The positive half: quoting must not break a legitimate column name."""
    con = duckdb.connect(database=":memory:")
    con.execute(f"CREATE TABLE t ({planner.quote_identifier('odd name')} DOUBLE)")
    con.execute("INSERT INTO t VALUES (1.5)")
    assert con.execute(
        f"SELECT avg({planner.quote_identifier('odd name')}) FROM t"
    ).fetchone()[0] == 1.5
