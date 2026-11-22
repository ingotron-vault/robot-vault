"""Tests for `sqlguard`, the SQL console's gate.

A guard that can silently not run is not a guard: the relation allowlist and
the expression checks (`current_setting` below) each need their own tests.

Both halves matter: a test that only checks
rejection passes just as happily on an endpoint that rejects everything.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from query_service import sqlguard  # noqa: E402


# --- session-secret expressions must be rejected -------------------------

@pytest.mark.parametrize("sql", [
    "SELECT current_setting('s3_secret_access_key')",
    "SELECT current_setting('s3_access_key_id')",
    "select CURRENT_SETTING('s3_secret_access_key')",
    "SELECT a FROM all_parquet WHERE current_setting('s3_secret_access_key') = 'x'",
    "SELECT (SELECT current_setting('s3_secret_access_key')) AS leaked",
    "WITH c AS (SELECT current_setting('s3_secret_access_key') AS s) SELECT * FROM c",
    "SELECT getvariable('anything')",
    # `getenv` reads the *process* environment, which is exactly where
    # configs/query.env places AWS_SECRET_ACCESS_KEY. The pinned DuckDB does not
    # register it today, but the pin is a range (>=1.5,<2); denylisting it keeps
    # the door shut if an in-range rebuild ever exposes it.
    "SELECT getenv('AWS_SECRET_ACCESS_KEY')",
    "SELECT a FROM all_parquet WHERE getenv('AWS_SECRET_ACCESS_KEY') = 'x'",
])
def test_session_configuration_reads_are_rejected(sql):
    """`SET lock_configuration=true` blocks *writing* settings, not reading
    them, and the relation walk does not look at expressions -- so a
    session secret must be rejected explicitly."""
    reason = sqlguard.validate_read_only_sql(sql)
    assert reason is not None, f"{sql!r} was accepted by the guard"
    assert "configuration" in reason


def test_a_path_like_relation_carried_on_a_non_base_table_node_is_rejected():
    """Defence in depth for the AST walk. A replacement-scan target can be
    carried as a bare `table_name` string on a node the walk does not model as a
    BASE_TABLE (e.g. DuckDB's SHOW_REF for SUMMARIZE/DESCRIBE/SHOW). Only
    BASE_TABLE reaches `_check_base_table`, so without the general `table_name`
    screen such a path would slip past the file-path guard. Exercised directly
    because on the pinned DuckDB a real `SUMMARIZE 's3://...'` already surfaces
    the path as a BASE_TABLE; this pins the branch that guards future shapes."""
    node = {"type": "SHOW_REF", "table_name": "s3://other-bucket/secrets.parquet"}
    reason = sqlguard._walk_relations(node, set())
    assert reason is not None and "file path" in reason


def test_a_benign_table_name_on_a_non_base_table_node_is_accepted():
    """The general screen must not reject synthetic show-refs that carry no
    path separators (DuckDB emits e.g. `__show_tables_expanded`), so legitimate
    analytics SQL keeps working."""
    node = {"type": "SHOW_REF", "table_name": "__show_tables_expanded"}
    assert sqlguard._walk_relations(node, set()) is None


def test_denied_function_rejected_wherever_it_appears_in_the_tree():
    """Nested deep enough that a shallow check would miss it."""
    sql = (
        "WITH a AS (SELECT 1 AS n), "
        "b AS (SELECT n, current_setting('s3_secret_access_key') AS s FROM a) "
        "SELECT * FROM b JOIN all_parquet ON true"
    )
    assert sqlguard.validate_read_only_sql(sql) is not None


# --- relation allowlist -------------------------------------------------

@pytest.mark.parametrize("sql,fragment", [
    ("SELECT * FROM duckdb_settings()", "table function"),
    ("SELECT * FROM read_parquet('s3://other-bucket/x.parquet')", "table function"),
    ("SELECT * FROM 's3://other-bucket/x.parquet'", "file path"),
    ("SELECT * FROM '/etc/passwd'", "not allowed"),
    ("SELECT * FROM secrets", "not allowed"),
    ("SELECT * FROM main.all_parquet", "qualified relation"),
])
def test_relation_allowlist_rejections(sql, fragment):
    reason = sqlguard.validate_read_only_sql(sql)
    assert reason is not None, f"{sql!r} was accepted by the guard"
    assert fragment in reason


@pytest.mark.parametrize("sql", [
    "COPY (SELECT 1) TO 's3://robotics-data/pwned.csv'",
    "INSERT INTO all_parquet VALUES (1)",
    "DELETE FROM all_parquet",
    "SET s3_secret_access_key='mine'",
    "PRAGMA database_list",
    "ATTACH 's3://other/db.duckdb'",
    "CREATE TABLE t AS SELECT 1",
    "SELECT 1; SELECT 2",
    "",
    "   ",
])
def test_non_select_statements_are_rejected(sql):
    assert sqlguard.validate_read_only_sql(sql) is not None


def test_cte_scope_is_definition_order_aware():
    """A CTE body may not reference a LATER sibling. Over-approximating this
    would let an attacker name a later sibling after a malicious path, so the
    validator would allow a FROM that DuckDB resolves as a replacement scan."""
    reason = sqlguard.validate_read_only_sql(
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1) SELECT * FROM a"
    )
    assert reason is not None
    assert "'b'" in reason


# --- the half that stops this from being over-tightening ------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM all_parquet",
    "SELECT * FROM all_parquet LIMIT 100",
    "SELECT topic, count(*) FROM all_parquet GROUP BY topic ORDER BY 2 DESC",
    "SELECT date_trunc('day', ts) AS d, avg(x) FROM all_parquet GROUP BY 1",
    "SELECT avg(x) OVER (PARTITION BY topic ORDER BY ts) FROM all_parquet",
    "SELECT try_cast(x AS BIGINT) FROM all_parquet WHERE topic LIKE 'imu%'",
    "WITH recent AS (SELECT * FROM all_parquet WHERE ts > now() - INTERVAL 1 DAY)"
    " SELECT topic, count(*) FROM recent GROUP BY topic",
    "WITH a AS (SELECT 1 AS n), b AS (SELECT n FROM a) SELECT * FROM b",
    "SELECT * FROM all_parquet UNION ALL SELECT * FROM all_parquet",
    "SELECT t.topic FROM all_parquet AS t WHERE t.x IS NOT NULL",
    "SELECT (SELECT max(ts) FROM all_parquet) AS latest",
    "SELECT strftime(ts, '%Y-%m') AS m, sum(x) FROM all_parquet GROUP BY 1",
])
def test_legitimate_console_queries_are_accepted(sql):
    """The console exists to run arbitrary read-only analytics SQL. A guard
    that rejected these would be 'safe' and useless, which is why the guard is
    a denylist of introspection functions rather than an allowlist of
    permitted ones."""
    assert sqlguard.validate_read_only_sql(sql) is None, (
        f"{sql!r} is a legitimate console query but the guard rejected it"
    )


def test_validator_fails_closed_on_unparseable_input():
    assert sqlguard.validate_read_only_sql("SELECT ((((") is not None
