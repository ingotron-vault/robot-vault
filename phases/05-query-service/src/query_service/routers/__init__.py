"""HTTP routers for the query service.

`main.py` was a single 1863-line module holding settings, auth, middleware,
the DuckDB read path and every endpoint. It is now an assembly point; the
endpoints live here, grouped by what they talk to:

  - `query_api`  -- the lake (DuckDB over Parquet) and the uploads index
  - `admin_api`  -- operator ingest status from Postgres.
                    Deliberately NOT garbage collection: tools/s3_gc.py is the
                    only thing in the system that deletes S3 objects.
  - `robots_admin` -- admin-gated allowlist proxy to ingest's private
                      provisioning API; it is not a query-database writer.
  - `obs`        -- bounded Prometheus queries, obs-profile health check, and
                    Home's Postgres-backed KPI tiles
"""
