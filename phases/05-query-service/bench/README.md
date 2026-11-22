# Query-service benchmarks

## `bench_time_index_cache.py` — the time-index cache

Measures `/api/v1/robots/{robot}/data/{type}` cold vs warm, which is the only
endpoint that consults `time_index_cache` — and only when BOTH `since` and
`until` are supplied.

Needs a live object store and Postgres and a lake built by `generate_lake.py`. The
recorded run used 336 Parquet files / 6.72M rows (3 robots x 4 topics x 7
days), single-box loopback, in-process TestClient — so the figures are
server-side work, excluding network, and are **not** a deployed-stack API
latency.

```bash
make debug-up
export S3_ENDPOINT=http://127.0.0.1:9002 AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
export S3_BUCKET=robotics-data S3_PARQUET_BUCKET=robotics-data
export DATABASE_URL=postgres://robotvault:robotvault@127.0.0.1:5432/robotvault

python3 generate_lake.py --days 7 --robots 3 --files-per-day 4 --rows-per-file 20000
python3 bench_time_index_cache.py --trials 7 --out time-index-cache-$(date +%F).json
```

The example uses the loopback-only debug overlay because default Compose does
not publish the object store or PostgreSQL. A separately managed test deployment may use
different endpoints; never point this generator at production state.

Results and the three findings that came with them are in
`time_index_cache.py`'s module docstring.

**The lake generator writes to whatever `S3_BUCKET` names and inserts into
`robots`, `uploads`, `processing_status`, `processed` and `parquet_files`.**
Each file gets its own published upload, so the query service shows it. Point
the generator at a throwaway bucket and database, not at a live deployment.
