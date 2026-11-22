# Example data

Use this guide to build, seed, inspect, and remove Robot Vault's public
four-recording example bundle. It is for operators preparing an
installation before real data exists. The examples exercise normal upload
and processing. Pro adds segmentation, privacy handling, quality assurance
(QA), dataset cuts, and export. Find the bundle inventory, source licenses, transformations,
and per-file SHA-256 digests in [`examples/`](../examples/README.md).

## Build the bundle

The TUM source bag is about 1.4 GB. Keep the source cache on roomy storage:

```bash
EXAMPLES_SOURCE_DIR=/roomy/cache/example-data make examples
```

The result is four MCAPs plus JSON sidecars, datacards, and
`examples/manifest.json`. `make examples` downloads the canonical TUM Pioneer
bag and a digest-pinned ALOHA Parquet shard when they are absent. Use
`python -m example_data --help` for explicit paths and air-gapped operation.

## Seed a development deployment

From the repository root, start the development stack, then seed it:

```bash
make stack-up
make seed-examples
```

The command validates every bundle digest, enrolls two marked example robot
identities through ingest's authenticated `/admin/robots` endpoint, and runs
the normal `rsm scan` -> `rsm hash` -> `rua upload` path. It never inserts an
upload, episode, or QA row directly.

It creates these identities:

| Robot ID | Recordings |
| --- | --- |
| `example-wheeled-01` | `wheeled-nominal`, `imperfect-run`, `pii-bearing` |
| `example-manipulator-01` | `manipulator-pickplace` |

Local seed state is written under `.example-seed/`. Existing files with the
expected digest are reused; altered files are rejected rather than replaced.
The upload agent's durable manifests and ingest's upload identity make a
second run a no-op instead of four duplicates. The final command output and
`.example-seed/last-seed-summary.json` record copied/reused and uploaded/skipped
outcomes.

## Seed an mTLS release deployment

Run from an operator host that has `rsm`, `rua`, Docker access to the private
ingest admin container, and one acceptance-style robot config per identity:

```bash
make seed-examples \
  SEED_EXAMPLES_RUNNER=python3 \
  SEED_EXAMPLES_ARGS='--bundle examples \
    --state-dir /var/lib/robot-vault/example-seed \
    --config example-wheeled-01=/secure/robots/example-wheeled-01.json \
    --config example-manipulator-01=/secure/robots/example-manipulator-01.json \
    --admin-container <ingest-container>'
```

The enrollment bearer remains inside the ingest container. Robot private
keys, CA material, and tokens are deployment secrets and are never copied into
this repository. The generated configs are mode `0600`.

## What to observe

Wait for all four uploads and their processing attempts to reach `completed`.
With Pro, the imperfect bag is acceptable only after the real QA reports show
at least one DEFECT and one RISK finding; do not manufacture QA rows if the
injected signals do not trip the current checks.

For `pii-bearing`, inspect the processor's recorded backend and status. With
Pro PII scrubbing and a ready detector it should earn `pii_status=scrubbed`;
without a ready detector it remains `pii_status=raw`. Raw media must never be
relabelled as scrubbed.

## Remove the examples

For a disposable development stack, the supported complete removal is a full
reset:

```bash
rm -rf .example-seed                 # local example-seeder state only
make stack-reset CONFIRM=1           # permanently destroys ALL dev-stack data
```

`stack-reset` is intentionally destructive and is not appropriate for a
shared or release deployment. Robot Vault does not expose an API for
surgical deletion of an uploaded robot's provenance. On a shared deployment,
take the documented Postgres and object-store backup first (with Pro, an
operator also deletes datasets containing the example episodes), then delete
uploads for only
the two exact robot IDs and finally their robot rows in one reviewed database
transaction. Leave the resulting unreferenced objects to the repository's
single `tools/s3_gc.py` owner: preview with `GC_DRY_RUN=true`, then separately
approve a real sweep. See [`docs/OPERATIONS.md`](OPERATIONS.md) for backup and
restore, and
[`docs/CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md#gc-service-mark-and-sweep--configsgcyaml--env-overrides)
for GC controls. This limitation is deliberate: the seeder does not gain a
second object deleter or a hidden database back door for convenience.
