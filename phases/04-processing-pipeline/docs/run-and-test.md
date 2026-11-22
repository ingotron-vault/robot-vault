# Phase 04 — Run & Test

## Bring up the processor
Run from the repo root:
```
make stack-up
```
This supported wrapper builds the images and SPAs, generates protobuf stubs and
local secrets, initializes the bucket, starts the complete development stack,
and waits for readiness. If those prerequisites are already complete and you
only need the reduced service set, `docker compose up -d processor query`
starts their declared dependency closure. The processor itself does not consume
from ingest or a broker directly; it polls Postgres for uploads with
`status = 'completed'` (see `docs/technical.md`).

## Smoke test (end-to-end)
```
# Upload a file (see ../../../docs/TESTING.md at the repo root for details)
# After the upload reaches status=completed, check for processed Parquet.
```
The processor container's own logs (`docker compose logs -f processor`) print a
line such as `{'processed_candidates': 1}` once per poll interval, which
confirms that the poll found a candidate.

To list the resulting Parquet files, run rclone from the toolkit image
(`make build-toolkit`) on the Compose network. The root password goes in by
variable name, never on the command line:
```
export RCLONE_CONFIG_RV_SECRET_ACCESS_KEY="$(cat configs/objectstore-root-password.dev)"
docker run --rm --network "$(docker inspect robot-vault-query \
    --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')" \
  -e RCLONE_CONFIG_RV_SECRET_ACCESS_KEY \
  -e RCLONE_CONFIG_RV_TYPE=s3 -e RCLONE_CONFIG_RV_PROVIDER=Other \
  -e RCLONE_CONFIG_RV_ENDPOINT=http://objectstore:9000 -e RCLONE_CONFIG_RV_FORCE_PATH_STYLE=true \
  -e RCLONE_CONFIG_RV_ACCESS_KEY_ID=robot-vault-dev-root \
  --entrypoint rclone robot-vault-toolkit:latest ls rv:robotics-data/processed/parquet | head -n 20
```

## Notes
- S3-first: the processor writes to the object store (S3 API) when `S3_BUCKET`, `S3_ENDPOINT`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` are all set (see `configs/processor.env`, wired into the `processor` service by `docker-compose.yml`). Without them, the development compatibility path writes only Parquet to local `uploads/processed/parquet`; media and QA record `s3_disabled` skips because they have no local registry/object layout.
- Discovery is poll-based: the processor atomically leases completed, unprocessed uploads on an interval (`--interval`; the CLI default is 10s, and the dev Compose service passes `--interval 5`), with one fenced claim at a time per replica and stale-lease recovery; there is no broker. Registry rows and object paths carry that claim UUID.
- The processor handles a fixed catalog of ~63 supported ROS 2 message types (`docs/supported_ros2_topics.yaml`); unsupported schemas are skipped, not errored. Coverage is expanded incrementally without breaking existing Parquet schemas.
- A single bad/corrupt MCAP no longer blocks the rest of the batch: failures are isolated per upload and recorded in Postgres (`processing_status`), with retries up to `PROCESSOR_MAX_ATTEMPTS` (default 3) before an upload is marked `dead_letter`.
- An MCAP or camera data that breaks a memory or disk limit is marked `dead_letter` on the first attempt, with the limit name in `processing_status.error_message`. The limits and their defaults are in `docs/technical.md` → Config (`PROCESSOR_MCAP_MAX_*`, `MEDIA_MAX_*`).
- `SEGMENTATION_ENABLED=false` is the core-edition gate: Parquet extraction continues, while episode persistence and the dependent media/QA stages are explicitly skipped.
