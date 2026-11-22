"""Generate a synthetic processed lake for the query-service benchmarks.

The demo seed writes about five rows, which is far too small to say anything
about query latency or a cache. This writes a lake with the shape the query
service actually reads: many per-topic Parquet partitions across several
robots, topics and days, registered in `parquet_files` the way the processor
registers them.

Mirrors what the processor writes (`parquet_schema.py`): columns
ts/topic/x/y/z, at
`processed/parquet/{robot}/_attempts/{attempt}/{topic}/{YYYY}/{MM}/{DD}/rows-{upload_id}.parquet`.

The query service shows a file only when its upload is published. So each
file gets its own completed `uploads` row, a completed `processing_status`
row, and a `processed` row whose `attempt_id` equals the file's
`attempt_id`. `sha256` is the digest of the bytes written to S3.

Usage:
    S3_ENDPOINT=... AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \
    S3_BUCKET=... DATABASE_URL=... \
    python3 generate_lake.py [--days 7] [--robots 3] [--rows-per-file 20000]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

TOPICS = ["imu/linear_accel", "imu/angular_vel", "odom", "gps"]

SCHEMA = pa.schema([
    pa.field("ts", pa.timestamp("ns")),
    pa.field("topic", pa.string()),
    pa.field("x", pa.float64()),
    pa.field("y", pa.float64()),
    pa.field("z", pa.float64()),
])

# Same digest as `parquet_schema.schema_hash(1)`: SHA-256 of the
# `name:type` pairs, in schema order.
SCHEMA_HASH = hashlib.sha256(
    "\n".join(f"{field.name}:{field.type}" for field in SCHEMA).encode("utf-8")
).hexdigest()


def register_published_file(
    cur,
    *,
    bucket: str,
    robot_id: str,
    topic: str,
    upload_id: str,
    attempt_id: str,
    key: str,
    sha256: str,
    size: int,
    rows: int,
    start: datetime,
    end: datetime,
) -> None:
    """Insert the rows that make one Parquet file visible to the reader."""
    cur.execute(
        """
        INSERT INTO uploads
            (id, client_upload_id, robot_id, original_filename, storage_path,
             file_size_bytes, status, upload_completed_at, bytes_received)
        VALUES (%s, %s, %s, %s, %s, %s, 'completed', NOW(), %s)
        """,
        (
            upload_id,
            upload_id,
            robot_id,
            f"bench-{upload_id}.mcap",
            f"s3://{bucket}/raw/bench/{upload_id}.mcap",
            size,
            size,
        ),
    )
    cur.execute(
        """
        INSERT INTO processing_status
            (upload_id, status, started_at, completed_at, attempts, attempt_id)
        VALUES (%s, 'completed', NOW(), NOW(), 1, %s)
        """,
        (upload_id, attempt_id),
    )
    cur.execute(
        """
        INSERT INTO processed (upload_id, completed_at, rows_written, attempt_id)
        VALUES (%s, NOW(), %s, %s)
        """,
        (upload_id, rows, attempt_id),
    )
    cur.execute(
        """
        INSERT INTO parquet_files
            (file_path, robot_id, message_type, kind, upload_id,
             start_timestamp, end_timestamp, row_count, file_size_bytes,
             schema_version, schema_hash, column_names, all_null_columns,
             sha256, attempt_id)
        VALUES (%s, %s, %s, 'topic', %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
        """,
        (
            key,
            robot_id,
            topic,
            upload_id,
            start,
            end,
            rows,
            size,
            SCHEMA_HASH,
            [field.name for field in SCHEMA],
            [],
            sha256,
            attempt_id,
        ),
    )


def s3_filesystem() -> pafs.S3FileSystem:
    endpoint = os.environ["S3_ENDPOINT"].replace("http://", "").replace("https://", "")
    return pafs.S3FileSystem(
        access_key=os.environ["AWS_ACCESS_KEY_ID"],
        secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        endpoint_override=endpoint,
        scheme="http",
        region=os.environ.get("S3_REGION", "us-east-1"),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--robots", type=int, default=3)
    ap.add_argument("--files-per-day", type=int, default=4)
    ap.add_argument("--rows-per-file", type=int, default=20000)
    args = ap.parse_args()

    bucket = os.environ["S3_BUCKET"]
    fs = s3_filesystem()
    dsn = os.environ["DATABASE_URL"]

    # End "now" so the cache's pre-computed common blocks (last_hour,
    # last_24_hours, last_week — see time_index_cache) actually contain the
    # ranges a benchmark asks for. A lake dated in the past would measure the
    # uncached path and call it a cache result.
    now = datetime.now(timezone.utc).replace(microsecond=0)

    conn = psycopg.connect(dsn)
    conn.autocommit = True
    files_written = 0
    rows_written = 0

    with conn.cursor() as cur:
        for r in range(args.robots):
            robot_id = f"bench-robot-{r+1}"
            # `robots` is keyed on `id`, not `robot_id`.
            cur.execute(
                "INSERT INTO robots (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (robot_id, robot_id),
            )

            for day in range(args.days):
                day_start = (now - timedelta(days=day)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                for topic in TOPICS:
                    for f in range(args.files_per_day):
                        upload_id = str(uuid.uuid4())
                        attempt_id = str(uuid.uuid4())
                        # Spread each file's rows over a slice of the day.
                        base = day_start + timedelta(hours=f * (24 / args.files_per_day))
                        step_ns = int(
                            timedelta(hours=24 / args.files_per_day).total_seconds()
                            * 1e9
                            / max(1, args.rows_per_file)
                        )
                        base_ns = int(base.timestamp() * 1e9)
                        ts_values = [base_ns + i * step_ns for i in range(args.rows_per_file)]
                        table = pa.table(
                            {
                                "ts": pa.array(ts_values, type=pa.int64()).cast(
                                    pa.timestamp("ns")
                                ),
                                "topic": pa.array([topic] * args.rows_per_file),
                                "x": pa.array(
                                    [float(i % 100) for i in range(args.rows_per_file)]
                                ),
                                "y": pa.array(
                                    [float(i % 7) for i in range(args.rows_per_file)]
                                ),
                                "z": pa.array(
                                    [9.8] * args.rows_per_file
                                ),
                            },
                            schema=SCHEMA,
                        )
                        key = (
                            f"{bucket}/processed/parquet/{robot_id}/_attempts/"
                            f"{attempt_id}/{topic}/"
                            f"{base.year:04d}/{base.month:02d}/{base.day:02d}/"
                            f"rows-{upload_id}.parquet"
                        )
                        # Serialize once, so the registered digest and size
                        # describe exactly the bytes in S3.
                        sink = pa.BufferOutputStream()
                        pq.write_table(table, sink, compression="snappy")
                        payload = sink.getvalue().to_pybytes()
                        with fs.open_output_stream(key) as out:
                            out.write(payload)

                        # One transaction per file: the file is visible
                        # only once all of its rows exist.
                        with conn.transaction():
                            register_published_file(
                                cur,
                                bucket=bucket,
                                robot_id=robot_id,
                                topic=topic,
                                upload_id=upload_id,
                                attempt_id=attempt_id,
                                key=key,
                                sha256=hashlib.sha256(payload).hexdigest(),
                                size=len(payload),
                                rows=args.rows_per_file,
                                start=datetime.fromtimestamp(
                                    ts_values[0] / 1e9, tz=timezone.utc
                                ),
                                end=datetime.fromtimestamp(
                                    ts_values[-1] / 1e9, tz=timezone.utc
                                ),
                            )
                        files_written += 1
                        rows_written += args.rows_per_file
                print(
                    f"  {robot_id} day-{day}: {files_written} files, "
                    f"{rows_written:,} rows so far",
                    flush=True,
                )

    conn.close()
    print(f"lake: {files_written} parquet files, {rows_written:,} rows")


if __name__ == "__main__":
    main()
