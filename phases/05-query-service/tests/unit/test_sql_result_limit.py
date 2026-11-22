import duckdb
import pytest

from query_service.routers import query_api
from query_service import sqlguard


def test_console_result_limit_is_applied_inside_the_query_plan():
    con = duckdb.connect(":memory:")
    columns, rows = query_api._execute_bounded_console_sql(
        con, "SELECT range AS n FROM range(10000);"
    )
    assert columns == ["n"]
    assert len(rows) == 10_000


def test_console_refuses_one_row_over_the_limit():
    con = duckdb.connect(":memory:")
    with pytest.raises(query_api._ConsoleResultTooLarge):
        query_api._execute_bounded_console_sql(con, "SELECT * FROM range(10001)")


def test_console_wrapper_preserves_ctes_and_semicolons_inside_literals():
    con = duckdb.connect(":memory:")
    columns, rows = query_api._execute_bounded_console_sql(
        con, "WITH x AS (SELECT ';' AS value) SELECT value FROM x;"
    )
    assert columns == ["value"]
    assert rows == [(";",)]


@pytest.mark.parametrize("suffix", [
    " -- trailing comment",
    " /* trailing comment */",
    "; -- trailing comment",
    "; /* trailing comment */",
    "; /* outer /* nested */ comment */ -- final comment\n",
])
def test_console_wrapper_preserves_valid_commented_queries(suffix):
    sql = "WITH x AS (SELECT '; -- /* café */' AS value) SELECT value FROM x" + suffix
    assert sqlguard.validate_read_only_sql(sql) is None
    with duckdb.connect(":memory:") as con:
        columns, rows = query_api._execute_bounded_console_sql(con, sql)
    assert columns == ["value"]
    assert rows == [("; -- /* café */",)]


@pytest.mark.parametrize("sql", [
    "SELECT 1; /* comment */ SELECT 2",
    "SELECT 1; -- comment\n DROP TABLE all_parquet",
    "SELECT current_setting('s3_secret_access_key'); -- comment",
    "SELECT * FROM read_parquet('/tmp/private.parquet'); /* comment */",
])
def test_commented_queries_still_obey_console_policy(sql):
    assert sqlguard.validate_read_only_sql(sql) is not None
