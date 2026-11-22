"""E2E proof for the query-service hardening.

It checks three things:

- DuckDB query connections stay usable and stay confined to the lake.
- API timestamps are UTC with a trailing `Z`, also when Postgres runs in a
  non-UTC time zone.
- Under OIDC, a viewer cannot run PromQL and an operator can.

Run it with Docker only (see docs/run-and-test.md, "Query hardening
check"). It needs:

- the query image (pinned DuckDB and the baked `httpfs` extension);
- a disposable object store (RustFS) reachable at `$S3_ENDPOINT`;
- this service's `src/` on `PYTHONPATH`;
- a disposable Postgres at `$PG_DSN` (any database; the script only runs
  `SELECT` statements);
- no `DATABASE_URL`. The script stands in for the Postgres file registry
  by returning one registry row for the seeded v1 file, so the console and
  the time-series endpoint read through the same explicit-file path that a
  deployed stack uses.

The script seeds the object store, calls the real HTTP endpoints through FastAPI's test
client, opens connections through the real `planner.open_duckdb()`, and runs
probes that must succeed or must be refused. It writes one JSON report to
`$REPORT_PATH` and exits non-zero if any probe has the wrong result.

What can go wrong, and the probe that catches it:

1. `httpfs` is not loaded before file access is disabled: the S3 read probes
   and the two endpoint probes fail.
2. The allowed prefix is wrong (bucket, trailing slash, `v2/` files): the
   glob, exact-file and v2 read probes fail.
3. `allow_persistent_secrets` is set after the secret manager is used:
   `open_duckdb()` raises, so every probe fails.
4. File access stays open: the local-file, other-prefix, other-bucket and
   HTTP probes succeed instead of being refused.
5. Configuration stays unlocked: a statement can switch file access back on.
6. DuckDB's spill directory becomes unusable: the spill probe fails.
7. The parser connection loses `json_serialize_sql`: the console probe fails.
8. A TIMESTAMPTZ read in a `+01:00` session is formatted as `...+01:00Z`,
   or a naive DuckDB timestamp has no `Z`: the two timestamp probes fail.
9. A viewer token reaches Prometheus, or an operator token is refused: the
   two PromQL role probes fail. No Prometheus runs, so an operator request
   that passes the role check ends at `502 prometheus_unavailable`.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import boto3
import duckdb

ENDPOINT = os.environ["S3_ENDPOINT"]
BUCKET = os.environ["S3_PARQUET_BUCKET"]
KEY = os.environ["AWS_ACCESS_KEY_ID"]
SECRET = os.environ["AWS_SECRET_ACCESS_KEY"]
REPORT_PATH = os.environ["REPORT_PATH"]
SECRET_FILE = "/run/secrets/e2e_probe_secret"

LAKE = f"s3://{BUCKET}/processed/parquet"
V1_FILE = f"{LAKE}/robot-e2e/imu/2024/01/01/part-upload-e2e.parquet"
V2_FILE = f"{LAKE}/v2/joint_state/robot-e2e/joint_states/rows-upload-e2e.parquet"
OUTSIDE_PREFIX = f"s3://{BUCKET}/raw/outside.parquet"
OTHER_BUCKET = "s3://e2e-other-bucket/other.parquet"
ROWS = 5


def seed() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=KEY,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
    )
    for bucket in (BUCKET, "e2e-other-bucket"):
        try:
            s3.create_bucket(Bucket=bucket)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass
    # A plain, unrestricted connection writes the fixtures.
    con = duckdb.connect(database=":memory:")
    con.execute(f"SET extension_directory='{os.environ['DUCKDB_EXTENSION_DIRECTORY']}'")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    host = ENDPOINT.split("://", 1)[1]
    con.execute(
        f"CREATE SECRET seed (TYPE S3, KEY_ID '{KEY}', SECRET '{SECRET}', "
        f"REGION 'us-east-1', ENDPOINT '{host}', URL_STYLE 'path', USE_SSL false)"
    )
    rows = (
        "SELECT (1704067200000000000 + range * 1000000000)::BIGINT AS ts, "
        "'imu' AS topic, range * 1.0 AS x, range * 2.0 AS y, range * 3.0 AS z "
        f"FROM range({ROWS})"
    )
    for target in (V1_FILE, V2_FILE, OUTSIDE_PREFIX, OTHER_BUCKET):
        con.execute(f"COPY ({rows}) TO '{target}' (FORMAT parquet)")
    con.close()
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    with open(SECRET_FILE, "w", encoding="utf-8") as handle:
        handle.write("must-not-be-readable\n")


def probe(con, sql: str) -> dict:
    try:
        rows = con.execute(sql).fetchall()
        return {"outcome": "ok", "rows": [list(map(str, r)) for r in rows[:3]]}
    except Exception as exc:  # noqa: BLE001 - the class is the evidence
        return {
            "outcome": "refused",
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
        }


def main() -> int:
    seed()
    sys.modules.pop("query_service", None)
    import fastapi.staticfiles as staticfiles

    class _NoStatic:
        def __init__(self, *args, **kwargs):
            pass

    staticfiles.StaticFiles = _NoStatic
    from fastapi.testclient import TestClient
    from query_service import planner, sqlguard
    from query_service import registry as registry_mod
    from query_service.main import app

    registered = registry_mod.ParquetFile(
        file_path=V1_FILE[len("s3://"):],  # the processor's bucket/key form
        robot_id="robot-e2e",
        message_type="imu",
        schema_version=1,
        schema_hash="0" * 64,
        row_count=ROWS,
        start_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end_timestamp=datetime(2024, 1, 1, 0, 0, ROWS, tzinfo=timezone.utc),
        column_names=["ts", "topic", "x", "y", "z"],
        all_null_columns=[],
        file_size_bytes=4096,
    )

    def list_files(**kwargs):
        if kwargs.get("include_derived"):
            return []
        return [registered]

    registry_mod.list_files = list_files

    client = TestClient(app)
    results: dict = {"must_succeed": {}, "must_be_refused": {}}

    console = client.post(
        "/api/v1/query/sql", json={"sql": "SELECT count(*) AS n FROM all_parquet"}
    )
    results["must_succeed"]["console_count_over_lake"] = {
        "outcome": "ok" if console.status_code == 200 and console.json()["rows"] == [[ROWS]] else "wrong",
        "status": console.status_code,
        "body": console.json(),
    }
    series = client.get(
        "/api/timeseries",
        params={
            "robot_id": "robot-e2e",
            "topic": "imu",
            "since": "2024-01-01T00:00:00Z",
            "until": "2024-01-01T01:00:00Z",
        },
    )
    body = series.json()
    results["must_succeed"]["timeseries_endpoint"] = {
        "outcome": (
            "ok"
            if series.status_code == 200
            and sum(p["n"] for p in body.get("points", [])) == ROWS
            and body["points"][0]["t"] == "2024-01-01T00:00:00Z"
            else "wrong"
        ),
        "status": series.status_code,
        "first_point": (body.get("points") or [None])[0],
    }

    con = planner.open_duckdb()
    try:
        succeed = {
            "s3_read_exact_v1_file": f"SELECT count(*) FROM read_parquet(['{V1_FILE}'])",
            "s3_read_glob": f"SELECT count(*) FROM read_parquet('{LAKE}/**/part-*.parquet')",
            "s3_read_v2_file": f"SELECT count(*) FROM read_parquet(['{V2_FILE}'], union_by_name=true)",
            "spill_to_temp_directory": (
                "SELECT count(*) FROM (SELECT md5(range::VARCHAR) AS h "
                "FROM range(3000000) ORDER BY h DESC)"
            ),
        }
        refuse = {
            "local_read_text_secret": f"SELECT content FROM read_text('{SECRET_FILE}')",
            "local_read_csv_passwd": "SELECT count(*) FROM read_csv('/etc/passwd')",
            "local_glob_secrets": "SELECT * FROM glob('/run/secrets/*')",
            "s3_outside_lake_prefix": f"SELECT count(*) FROM read_parquet('{OUTSIDE_PREFIX}')",
            "s3_other_bucket": f"SELECT count(*) FROM read_parquet('{OTHER_BUCKET}')",
            "http_url": f"SELECT * FROM read_text('{ENDPOINT}/health')",
            "copy_to_local_file": "COPY (SELECT 1) TO '/tmp/e2e-escape.csv'",
            "attach_local_database": "ATTACH '/tmp/e2e-escape.duckdb' AS escape",
            "install_extension": "INSTALL spatial",
            "persistent_secret": "CREATE PERSISTENT SECRET p (TYPE S3, KEY_ID 'a', SECRET 'b')",
            "reenable_external_access": "SET enable_external_access=true",
            "change_allowed_directories": "SET allowed_directories=['/']",
        }
        for name, sql in succeed.items():
            results["must_succeed"][name] = probe(con, sql)
        for name, sql in refuse.items():
            results["must_be_refused"][name] = probe(con, sql)
    finally:
        con.close()

    import psycopg
    from query_service import settings

    with psycopg.connect(os.environ["PG_DSN"], options="-c TimeZone=Europe/Berlin") as pg:
        session_text = pg.execute(
            "SELECT '2024-01-01T00:00:00Z'::timestamptz::text"
        ).fetchone()[0]
    # psycopg converts to the session zone only when the image has tzdata.
    # Parsing the server's own text keeps the +01:00 offset either way.
    instant = datetime.fromisoformat(session_text)
    formatted = settings.iso_utc(instant)
    results["must_succeed"]["postgres_non_utc_session_timestamp"] = {
        "outcome": (
            "ok"
            if instant.utcoffset() is not None
            and instant.utcoffset().total_seconds() == 3600
            and formatted == "2024-01-01T00:00:00Z"
            else "wrong"
        ),
        "postgres_value": instant.isoformat(),
        "api_value": formatted,
    }

    # Role gate on PromQL, through the real OIDC development token path.
    settings.AUTH_MODE = "oidc"
    settings.OIDC_DEV = True
    try:
        for role, expected in (("viewer", 403), ("operator", 502)):
            token = client.get("/auth/dev/token", params={"roles": role}).json()["access_token"]
            response = client.get(
                "/api/obs/promql/range",
                params={"query": "up"},
                headers={"Authorization": f"Bearer {token}"},
            )
            results["must_succeed"][f"promql_as_{role}_gets_{expected}"] = {
                "outcome": "ok" if response.status_code == expected else "wrong",
                "status": response.status_code,
                "body": response.json(),
            }
    finally:
        settings.AUTH_MODE = "none"
        settings.OIDC_DEV = False

    parsed, error = sqlguard._parse_statements("SELECT 1")
    results["must_succeed"]["parser_connection"] = {
        "outcome": "ok" if error is None and parsed and not parsed.get("error") else "wrong",
        "error": error,
    }

    failures = [
        f"must_succeed.{name}" for name, r in results["must_succeed"].items() if r["outcome"] != "ok"
    ] + [
        f"must_be_refused.{name}" for name, r in results["must_be_refused"].items() if r["outcome"] != "refused"
    ]
    report = {
        "check": "query-hardening",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duckdb_version": duckdb.__version__,
        "lake_prefix": planner.lake_root_uri() + "/",
        "passed": not failures,
        "failures": failures,
        "results": results,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({"passed": report["passed"], "failures": failures}))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
