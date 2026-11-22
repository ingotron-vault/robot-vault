# End-to-end test inventory

This list shows every end-to-end (E2E) test file, what it needs to run, and its
edition. Use it to choose the tests for a run and to know which skips are
expected.

All tests here are core tests. They ship in this repository and pass against
the core edition. Pro adds its own end-to-end tests (see the Pro
documentation).

## Core: `tests/e2e/`

| Test file | Needs | Runs with | Skips when |
|---|---|---|---|
| `test_pipeline_e2e.py` | Live dev stack (`make stack-up`) | `make test-e2e` | The stack is not reachable, unless `REQUIRE_LIVE_STACK=1` (then it fails) |
| `test_upload_detail_publication_e2e.py` | Live dev stack | `make test-e2e` | Same as above |
| `test_robot_enrollment_e2e.py` | Docker; builds the dev, ingest, query and processor images and starts its own containers | `make test-robot-enrollment` | `ROBOT_ENROLLMENT_E2E=1` is not set |
| `test_upload_resume_e2e.py` | Docker; a built ingest binary in `INGEST_RESUME_BINARY`; its own Postgres and object store (RustFS) | See the module docstring | `INGEST_RESUME_BINARY` is not set |

`make test-e2e` runs `pytest tests/e2e` in the dev container with
`REQUIRE_LIVE_STACK=1`. The files that need their own environment variable
skip there. A run with `FailOnSkip` must exclude them or list them as allowed
skips.

## Other E2E harnesses

These are not pytest files in `tests/e2e/`, but they are E2E gates:

| Harness | Edition | Make target |
|---|---|---|
| `development/browser-e2e/` with `BROWSER_APPS=query,robot` (core UI, panels, robot UI) | Core | `make test-browser` |
| `development/test_upload_jobs_e2e.py` | Core | `make test-upload-jobs` |
| `development/backup-restore-e2e/` (`make backup` / `make restore` against real Postgres and the RustFS object store; 15 cases) | Core | See [`docs/TESTING.md`](../../docs/TESTING.md#backup-and-restore-round-trip) |
