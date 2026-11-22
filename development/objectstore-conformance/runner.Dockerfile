# Test runner for the object-store gate: the S3 client libraries and versions the services
# use (boto3/botocore as in the toolkit lock), plus what the conformance suite imports.
FROM rclone/rclone:1.76.0@sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466 AS rclone
FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
RUN pip install --no-cache-dir boto3==1.43.64 botocore==1.43.64 pyarrow==25.0.0 duckdb==1.5.5 \
        "psycopg[binary]" prometheus_client pyyaml requests pytest \
 && python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')" \
 && chmod -R a+rX /root
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
# The runner runs as the caller's uid; HOME stays /root so DuckDB finds httpfs.
ENV HOME=/root PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo
