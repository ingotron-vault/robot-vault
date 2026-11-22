# Upload-job HTTP regression gate

Use `make test-upload-jobs` when changing upload-job selection, transfer, or
reporting. This end-to-end (E2E) gate checks the job result through the real
HTTP application while controlling only the remote gRPC service.

The test covers these failures:

- A second job selects a recording another job is uploading. The queue skips
  it, and the HTTP job reports completion with a file still pending.
- Reconciliation accidentally changes the first job's manifest or starts a
  duplicate transfer.
- A server rejection loses its explanation, or an already completed recording
  transfers again when selected in a later job.
- An unexpected error after one successful transfer overwrites its success.
- A recording is replaced after an earlier transfer, and reconciliation
  mistakes a stale completed sidecar for completion of the replacement bytes.
- Failure during reporting leaves a running job or its active queue registered.

The observable contract is the real HTTP job response: every selected file in
a finished job has a terminal outcome. Another job's active transfer produces
an explanatory failure for this attempt; its durable recording state remains
uploading until that transfer finishes. Completed recordings remain completed.

`test_upload_jobs_e2e.py` runs the ordinary FastAPI application, config loader,
queue, manifests and gRPC transport. Only the remote gRPC service is controlled
to hold and reject responses. It hashes received bytes independently. Existing
unit tests inject queues and cannot reproduce the production skip between
queue selection and HTTP reporting. No production-only test switch is needed.

Run `make test-upload-jobs`. It uses a private temporary robot-data mount and
saves JSON plus the HTTP server log in `tmp/upload-jobs-e2e/`. It fails if the
contract fails. It does not verify the complete ingest/storage pipeline.
