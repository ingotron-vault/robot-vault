# Phase 04 — Processing user guide

This guide explains what happens after an upload completes and where to find
the results. The processor runs in the background and checks for new uploads.

The processor:
- Polls completed uploads and claims each with a renewable, fenced lease.
- Streams MCAP once to produce registered Parquet. Pro adds episode
  boundaries, optional AV1 media and QA reports in the same pass.
- Records failures and bounded attempts so one poison file cannot block the
  queue. Every derived object and registry row is owned by the claim UUID; a
  live, unexpired token is checked in each registry transaction and again in
  the transaction that publishes the successful generation. A worker that
  resumes after lease expiry can leave only an unregistered GC orphan.

Keep recording and uploading as usual. The Docker processor checks for newly
completed uploads every few seconds; you do not need to start each job.

Results go to:
- Parquet under `processed/parquet/`, with authoritative rows in Postgres. Pro
  also writes encoded clips under `processed/media/` and QA detail blobs under
  `qa/reports/`.
  New output paths contain `_attempts/{claim_uuid}`. Do not construct these
  paths yourself; query the registry for the authoritative generation.
- In the development compatibility path without S3 configuration, only Parquet
  falls back to local disk. Pro media and QA are recorded as skipped with reason
  `s3_disabled`; their registries and garbage-collection model require object
  storage. Release uses a scoped processor database role and object-store
  identity.

Core runs Parquet extraction only. Pro adds the episode, media and QA stages,
and the matching workflows in the query service and UI.
