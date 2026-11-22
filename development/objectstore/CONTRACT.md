# Object store contract

Status: frozen on 2026-11-21. The services and tools code against this file. Change it only with a new gate run (`development/objectstore-conformance/run.sh`, section 9) on the new values.

Robot Vault keeps its objects in one S3 bucket on a single-node **RustFS** server.

## 1. Service and image

| Item | Value |
|---|---|
| Compose service | `objectstore` (container host name `objectstore`) |
| Image | `robot-vault-objectstore:<version>` (local tag `robot-vault-objectstore:latest` in development). Built from `deploy/release-images/objectstore.Dockerfile`; the Compose files never pull it. |
| Base | `rustfs/rustfs:1.0.6@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a` |
| Layer on the base | `apk add libcrypto3=3.5.9-r1 libssl3=3.5.9-r1` (exact versions; the base has 3.5.9-r0 with 4 fixable High CVEs). RustFS's Apache-2.0 `LICENSE` at the tag commit (sha256 pinned) in `/usr/share/licenses/rustfs/`, and the labels `org.opencontainers.image.revision`, `.version`, `.source`, `.vendor`. User, entrypoint, command and healthcheck stay those of the base. The `rustfs` binary is static and does not use OpenSSL. |
| Release | RustFS 1.0.6, published 2026-11-15, tag commit `de9009d882644effc2628e777650025c3d8e7c65`, Apache-2.0 |
| Digest kind | The base digest is a multi-arch index (amd64 and arm64). The `-glibc` variant is a different image; do not use it. |
| Data volume | named volume `objectstore_data` mounted at `/data` |
| User | the image user `rustfs` (uid 10001). Do not set `user:`. Only the conformance test compose uses `user: "0"`, because a test runner may bind named volumes to folders owned by the caller. |
| Ports | 9000 (S3 and admin API). The console (9001) is off. Do not publish 9000 on the host in the release stack. |
| Upgrades | A new base digest or a new OpenSSL pin needs the whole gate (`development/objectstore-conformance/run.sh`, twice) on the built image and a new version of this file. |

## 2. Environment of the `objectstore` service (copy exactly)

```yaml
environment:
  RUSTFS_ACCESS_KEY: ${OBJECTSTORE_ROOT_USER:?set OBJECTSTORE_ROOT_USER}
  RUSTFS_SECRET_KEY_FILE: /run/secrets/objectstore_root_password
  RUSTFS_VOLUMES: /data
  RUSTFS_ADDRESS: "0.0.0.0:9000"
  RUSTFS_CONSOLE_ENABLE: "false"
  RUSTFS_DURABILITY_MODE: strict
  RUSTFS_DRIVE_SYNC_ENABLE: "true"
  RUSTFS_NEW_BUCKET_DURABILITY_MODE: strict
  RUSTFS_PUT_RENAME_EARLY_ACK_ENABLE: "false"
  RUSTFS_API_STALE_UPLOADS_EXPIRY: 360h
  RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL: 6h
secrets: [objectstore_root_password]
```

- The root password comes from a file (Compose secret). The image entrypoint reads
  `RUSTFS_SECRET_KEY_FILE`; it refuses the default `rustfsadmin` and empty values. The gate
  checked that the root password is in no process command line in the store container.
- Our env names: `OBJECTSTORE_ROOT_USER` and `OBJECTSTORE_ROOT_PASSWORD` (the secret file's
  content). No `RUSTFS_*` names outside the `objectstore` service.
- Services keep their names: `S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `<SVC>_S3_ACCESS_KEY`,
  `<SVC>_S3_SECRET_KEY`.

## 3. Client settings

| Item | Value |
|---|---|
| Endpoint | `http://objectstore:9000` |
| Addressing | path style, on every client (boto3 `addressing_style=path`, pyarrow `force_virtual_addressing=False`, DuckDB `URL_STYLE 'path'`, Rust SDK `force_path_style(true)`, rclone `force_path_style=true`) |
| Region | `us-east-1` |
| Signatures | SigV4 (`signature_version="s3v4"`) for every client, also for presigned URLs |
| rclone | `type=s3`, `provider=Other` (rclone 1.76.0 has no RustFS provider) |

## 4. Health

| Probe | Path | Use |
|---|---|---|
| Liveness | `GET /health` (also `/health/live`) | the process answers |
| Readiness | `GET /health/ready` | storage and IAM are ready; may return 503 |

Compose healthcheck (the image has `/usr/bin/curl`):

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:9000/health/ready"]
  interval: 5s
  timeout: 3s
  retries: 24
  start_period: 5s
```

Other services wait with `depends_on: {objectstore: {condition: service_healthy}}`.

## 5. Durability

`strict` is the RustFS default; we also set it explicitly so that a typo cannot select a weaker
mode. In `strict`, RustFS fsyncs before it answers 200 to PutObject, UploadPart and
CompleteMultipartUpload: the payload (fdatasync of shard and part files), `xl.meta`, rollback
copies, and the parent directories of the commit renames. Sources at tag 1.0.6:

- `docs/operations/durability-modes.md:46-67` (what each mode syncs; "Every acknowledged write
  (PUT, UploadPart, CompleteMultipartUpload ...) is durable on the individual drive before the
  200 OK"), `:88-92` (single node must stay `strict`).
- `crates/ecstore/src/disk/local.rs:1719-1724` (env names, default true), `:1944-1991` (modes;
  `syncs_data_shards`, `syncs_commit_metadata`), `:2000-2017` (an invalid `RUSTFS_DURABILITY_MODE`
  falls back to `RUSTFS_DRIVE_SYNC_ENABLE`; `false` there means no fsync at all), `:2220-2229`
  (per-bucket override; system volumes pinned to strict).
- `crates/ecstore/src/disk/local/commit.rs:827-828`, `:1290-1291` (object commit), and
  `crates/ecstore/src/disk/local.rs:14630-14736` (`rename_part`: part fdatasync, metadata sync,
  directory fsync), `:15415-15449` (`rename_file_durable`).
- `crates/ecstore/src/bucket/durability.rs:67-101` (`RUSTFS_NEW_BUCKET_DURABILITY_MODE`; we seed
  `strict` so a later global change cannot weaken our bucket).
- `crates/ecstore/src/set_disk/core/io_primitives.rs:3538-3540`, `:4420` (early acknowledgement at
  write quorum, default on; with one drive the quorum is the whole set, we turn it off anyway).
- The experimental group-commit switches (`RUSTFS_EXPERIMENTAL_*_GROUP_COMMIT_*`,
  `crates/ecstore/src/disk/os.rs:679-684`) default off. Do not set them.

Proven by the gate: acknowledged writes survive `kill -9` of the store (results in section 9).
Not proven: host power loss or a kernel crash. The kill test cannot show it, because the page
cache survives a process kill. For power loss we rely on the `strict` fsync contract above and on
the disk and file system honouring fsync (no volatile write cache without power-loss protection).

## 6. Stale multipart uploads

RustFS aborts incomplete multipart uploads older than `RUSTFS_API_STALE_UPLOADS_EXPIRY`, checked
every `RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL`
(`crates/ecstore/src/bucket/lifecycle/bucket_lifecycle_ops.rs:186-190`, `:3454-3467`, `:3861-3871`,
`:3974-3990`).

- **The RustFS default is 24 hours.** Ingest keeps a session and its multipart upload for up to
  14 days (`phases/03-ingest-server-rs/src/resumable.rs:11-18`), so the default would destroy
  resumable uploads. We set `360h` (15 days): an upload survives at least 15 days from its
  creation, more than 14 days + 15 minutes.
- An unparsable or zero value silently falls back to 24 hours (`:3454-3459`). Write the value as
  `360h` exactly (humantime syntax).
- No bucket lifecycle rule (`AbortIncompleteMultipartUpload`) may be set; it would shorten the
  expiry (`:3862-3868`).

## 7. Bootstrap one-shot `objectstore-access-ready`

| Item | Value |
|---|---|
| Program | `development/objectstore/bootstrap.py` (Python 3, botocore only; runs in the toolkit image) |
| Command | `["python3", "/release/objectstore_bootstrap.py"]` with the script mounted read-only as a Compose config |
| Policies | `development/release/objectstore-policies/{ingest,processor,exporter,query,gc}.json`, each mounted read-only at `/release/objectstore-policies/<svc>.json` (override the folder with `OBJECTSTORE_POLICY_DIR`) |
| Runs | once, after `objectstore` is healthy; services that use the store wait for `service_completed_successfully` |
| API | RustFS admin REST API `/rustfs/admin/v3` (`add-canned-policy`, `remove-user`, `add-user`, `set-user-or-group-policy`, `user-info`) and S3 `CreateBucket` / `DeleteBucketPolicy`, all SigV4 with the root key. Plain JSON bodies on this path. |
| Environment | `S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `OBJECTSTORE_ROOT_USER`, `OBJECTSTORE_ROOT_PASSWORD`, `<SVC>_S3_ACCESS_KEY`, `<SVC>_S3_SECRET_KEY`. Each secret may instead come from a file named by `<NAME>_FILE` (not both). |
| Secrets | never on a command line and never printed. Proven by `test_bootstrap_argv.py` (section 9). |
| Checks | fixed access keys, secrets at least 16 characters, no line breaks, all secrets different from each other and from the root password, safe bucket name. Exit 1 with a message that names the variable, never its value. |
| Idempotent | re-creates the five policies, removes and re-creates each user, attaches exactly one policy, reads it back (policy, status `enabled`, no groups). Rotation: change a secret and run it again. |
| Bucket | created if missing; any bucket policy is removed (no anonymous access) |

## 8. The five service keys

| Service | Access key (fixed) | Policy name | Policy file |
|---|---|---|---|
| ingest | `robot-vault-ingest` | `robot-vault-ingest` | `objectstore-policies/ingest.json` |
| processor | `robot-vault-processor` | `robot-vault-processor` | `objectstore-policies/processor.json` |
| exporter | `robot-vault-exporter` | `robot-vault-exporter` | `objectstore-policies/exporter.json` |
| query | `robot-vault-query` | `robot-vault-query` | `objectstore-policies/query.json` |
| gc | `robot-vault-gc` | `robot-vault-gc` | `objectstore-policies/gc.json` |

The policy JSON is the reviewed policy set. RustFS evaluates it exactly as the
static evaluator does (290 live probes, 0 over-permit, 0 under-permit). The root key is for the
bootstrap and for backup/restore only, never for a service.

## 9. Gate

`run.sh` runs against the image digest and settings above:

```bash
make test-objectstore-conformance CONFORMANCE_LABEL=run1
```

| Step | Program | Pass condition |
|---|---|---|
| expiry red | `expiry_test.py`, 20 s expiry, 5 s sweep, 45 s hold | the upload is aborted (the test fails, as it must) |
| expiry green | `expiry_test.py`, 360h expiry, 5 s sweep, 45 s hold | the upload and its part survive |
| store argv, durability log | `run.sh` | root password in no command line; log shows mode `strict` |
| bootstrap argv | `test_bootstrap_argv.py` | control caught; two bootstrap runs, no secret in any sampled `/proc/*/cmdline` or output; a short secret is rejected |
| IAM | `iam_probe.py` | 290 probes, 0 over, 0 under, 0 errors |
| conformance | `conformance.py` | 17 features pass, metrics n/a |
| crash | `crash_test.py` | 200 acknowledged writes (110 PUT, 60 completed MPU, 30 parts of open MPUs) identical after `kill -9` and restart |

Result on 2026-11-21, on the first-party image `robot-vault-objectstore` (the base above plus `libcrypto3`/`libssl3`
3.5.9-r1): two runs of `development/objectstore-conformance/run.sh`, both exit 0, `gate.json` status `pass`, teardown
verified. All 11 steps passed in both runs: keys, bootstrap, expiry red and green, store argv, durability log, bootstrap
argv, IAM (290 probes, 0 over, 0 under, 0 errors), conformance (17 pass, metrics n/a), contract probe, crash (200 of 200
acknowledged writes intact, no torn object). A container image vulnerability scan found 0 blockers and 0 fixable High or
Critical findings.
