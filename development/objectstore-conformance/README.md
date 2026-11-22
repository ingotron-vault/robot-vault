# Object-store conformance gate

**Reader:** a developer or release engineer who changes the object store (RustFS image, its settings, the bucket
policies) or the code that talks to it.
**Scope:** the core edition. The gate runs the pinned RustFS from `development/objectstore/CONTRACT.md` in its own
disposable Compose project. It never touches a real stack.

The gate is a failing gate. It exits nonzero when any step failed or any required probe or artifact is missing. A
skipped probe counts as missing.

## When to run it

- Before you change the RustFS digest or a store setting in `development/objectstore/CONTRACT.md`. A new digest needs two green runs.
- After a change to a bucket policy in `development/release/objectstore-policies/`.
- After a change to an S3 client setting (`s3api.py`, `s3_gc.py`, the ingest S3 client).

## Run it

Prerequisite: Docker, and the pinned image in the local cache (the first run pulls it).

```bash
make test-objectstore-conformance CONFORMANCE_LABEL=run1     # own Compose project, own teardown
```

A run takes about ten minutes. The exit code is the verdict. `results/<label>/gate.json` lists every problem.
`results/` is not committed; keep an artifact folder next to your report.

## What it checks

| Step | Program | Pass condition |
| --- | --- | --- |
| `keys` | `suite_env.py` | all five service keys and the root key are set. If not, nothing starts. |
| `bootstrap` | `../objectstore/bootstrap.py` | the bucket and the five users exist (the product program, not a copy). |
| `expiry-red`, `expiry-green` | `expiry_test.py` | a 20 s stale-upload expiry aborts an upload (the test fails, as it must); the frozen `360h` keeps it. |
| `store-argv`, `durability-log` | `run.sh` | the root password is in no command line of the store; the log shows mode `strict`. |
| `bootstrap-argv` | `test_bootstrap_argv.py` | no secret in any sampled command line or output; the control that puts a secret in argv is caught. |
| `iam` | `iam_probe.py` | 290 live probes match the reviewed policies: 0 over-permit, 0 under-permit, 0 errors. |
| `conformance` | `conformance.py` | 17 probes of the S3 calls the services make (below). |
| `contract-probe` | `rustfs_contract_probe.py` | 14 checks with the real query-service and GC clients: SigV4 presign, path style, guarded delete. |
| `crash` | `crash_test.py` | 200 acknowledged writes (PUT, completed multipart, parts of open uploads) are identical after `kill -9` and a restart. |

`gate.py` reads the artifacts of all steps and decides. The list of required probes is `REQUIRED_FEATURES` in
`gate.py`; `conformance.py` imports it, so the two cannot drift.

## Rules

1. **Failures propagate.** No command in `run.sh` ends in a pipe or `|| true`. A failed build, bootstrap or restart is a
   failed step. The final exit code is the exit code of `gate.py`.
2. **All five service keys are required.** A data-path probe uses the key that owns the path (ingest on `raw/`, processor
   on `processed/`, query for reads, gc for deletes). The root key only seeds fixtures and does backup and restore.
   There is no fallback to the root key.
3. **Exact status codes.** A failed precondition gives `412`; a denied call gives `403`; a bad checksum gives `400`; a
   missing key gives `404`. Any other status fails the probe.
4. **Denied and failed writes change nothing.** After 16 denied calls and four failed preconditions, the bytes, ETag,
   content type, metadata and last-modified time of the objects are unchanged, and no object was added or removed.

## Proof that the gate can go red

| Case | How to see it | Expected |
| --- | --- | --- |
| a probe fails | `STALE_EXPIRY=20s make test-objectstore-conformance CONFORMANCE_LABEL=red-probe` (the store aborts the upload that the `expiry-green` probe holds) | exit 1, `gate.json` names `step expiry-green: fail rc=1` |
| a service key is missing | `CONFORMANCE_ENV_FILE=./results/no-gc.env make test-objectstore-conformance CONFORMANCE_LABEL=red-key`, where `no-gc.env` is `test.env` without the two `GC_S3_*` lines | exit 1, nothing starts, `suite_env.py` names `GC_S3_ACCESS_KEY` and `GC_S3_SECRET_KEY` |
| offline | `python -m pytest development/objectstore-conformance/test_conformance_gate.py` (needs boto3, requests) | the suite exits nonzero for an unreachable store and for each missing key; `gate.py` is red for a failed probe, a skipped probe, a missing artifact and a failed step |

## Files

| File | Edition | Purpose |
| --- | --- | --- |
| `run.sh`, `compose.yml`, `runner.Dockerfile` | core | the gate and its disposable store and test runner |
| `gate.py`, `suite_env.py`, `conformance.py`, `iam_probe.py`, `expiry_test.py`, `crash_test.py`, `test_bootstrap_argv.py`, `rustfs_contract_probe.py` | core | the probes |
| `test_conformance_gate.py` | core | offline proof that the gate is red when it must be |
| `test.env`, `test-root-password` | core | throwaway credentials for this gate only |
