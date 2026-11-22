# Phase 01 — Robot storage manager technical guide

This guide is for developers working on local recording storage. The
command-line interface (CLI) scans recordings, maintains per-file manifests,
computes content hashes, and plans or applies retention. Stable file identity
and manifest state support Phase 02's idempotent upload retries.

## Goals
- Deterministic local directory layout per robot and day.
- Authoritative manifest per file with SHA256, metadata, and status.
- Retention planning by age and capacity with dry-run by default.
- Clear CLI with idempotent operations and safe defaults.

## Non-Goals (Phase 01)
- Networking, uploads, and server coordination (Phase 02+).
- Robot heartbeats, background schedulers and upload windows (not implemented).

## Directory Layout
Default layout on robots:
```
/robot_data/{robot_id}/{YYYY-MM-DD}/*.mcap
```
Examples:
```
/robot_data/rover-7/2025-08-14/drive_001.mcap
/robot_data/rover-7/2025-08-14/drive_002.mcap
```

## Manifest
One manifest JSON per file, stored beside the data file with suffix `.manifest.json`:
```
/robot_data/rover-7/2025-08-14/drive_001.mcap.manifest.json
```
The writer also maintains a small `.manifest.json.lock` advisory-lock sidecar.
It is coordination metadata, is excluded from scans/history, and may remain
after retention removes the data/manifest pair.

### Manifest fields
- `path` (string): absolute path to the data file
- `file_size_bytes` (int): size at last scan
- `device_id` / `inode` / `mtime_ns` / `ctime_ns` (int): identity snapshot used
  to invalidate stale upload state when a file is replaced or rewritten
- `sha256_hash` (string|null): full-file SHA256 hex; computed on demand
- `created_at` (RFC3339): file creation time (or first-seen time)
- `recorded_at` (RFC3339|null): when the data was recorded (if known)
- `status` (enum): `pending|uploading|completed|failed`
- `upload_id` (string|null): deterministic ID for idempotent upload retries
  (Phase 02)
- `manifest_revision` (int): monotonically increasing revision used to reject
  stale concurrent writers
- `labels` (object<string,string>|null): optional metadata
- `operator` / `site` / `permission_ref` / `session_tags` / `task_label`
  (string|null each): operator-declared session context from the shared
  config's `capture` section. The first manifest creation copies these values.
  Later `scan` and `hash` runs keep them, even if `capture` changes in
  `vault.json`. This keeps retry metadata stable, as the upload agent does with
  `chunk_size_bytes`. Older manifests, or manifests created
  without `--config`, have `null` in all five fields. The upload agent's
  `_metadata_from_manifest` in `robot_upload_agent/transport.py` copies them
  into `UploadMetadata` fields 11–15 in `proto/upload.proto`. Ingest stores them
  in the matching `uploads` columns defined by
  `db/migrations/001_baseline.sql`.

### Example
```json
{
  "path": "/robot_data/rover-7/2025-08-14/drive_001.mcap",
  "file_size_bytes": 10485760,
  "device_id": 2049,
  "inode": 812345,
  "mtime_ns": 1755141731000000000,
  "ctime_ns": 1755141731000000000,
  "sha256_hash": "cb1e2d...",
  "created_at": "2025-08-14T03:22:11Z",
  "recorded_at": "2025-08-14T03:20:00Z",
  "status": "pending",
  "upload_id": null,
  "manifest_revision": 1,
  "labels": {"mission": "alpha"},
  "operator": "alice",
  "site": "lab-1",
  "permission_ref": "consent-42",
  "session_tags": "demo,indoor",
  "task_label": "pick_and_place"
}
```

## Hashing
- Algorithm: SHA256, streaming in configurable chunks (default 8 MiB) to avoid memory spikes.
- Invariants:
  - Hashing opens a regular file without following a final symlink and checks
    size, timestamps, device, and inode before and after the read. Mutation or
    atomic replacement fails the operation; no digest is saved.
  - `rsm hash` skips files that are still being written (`is_file_stable`):
    files newer than the configured stability window (5 seconds by default),
    and `.mcap` files that do not end with a complete MCAP footer (a Footer
    record followed by the closing magic `\x89MCAP0\r\n`).
    `--min-stable-age-seconds 0` turns off only the window; the footer check
    always applies.
  - If content identity changes after scan, mark manifest `status` back to
    `pending` and clear the hash and upload ID.

## Retention Policy
`min_retain_days` and `max_total_gib` resolve with precedence CLI flag (`--min-retain-days`/`--max-total-gib`) > shared config (`retention.min_retain_days`/`retention.max_total_gib`) > built-in default (7 days / 200 GiB).

Two mechanisms, applied in this order:
1) Age-based: keep all files newer than `min_retain_days`.
2) Capacity-based: if total exceeds `max_total_gib`, remove files in two tiers until under limit:
   - Tier 1: delete only verified `completed` files, oldest first. A deletion-
     eligible completion must have a regular on-disk sidecar, a 64-character
     SHA256, a non-empty upload ID, and content identity matching the data file.
   - Tier 2: if still over limit and `--allow-unuploaded` was passed, delete oldest files among `pending` and `failed`

Rules:
- Never delete files with `status` in `{uploading}`.
- A `completed` status without integrity/upload evidence is protected and
  reported under `unverified_completed`; status text alone is not enough to
  destroy the robot's only copy.
- `pending`/`failed` files are never-uploaded and are protected by default: they are never included in the deletion plan unless the operator explicitly passes `--allow-unuploaded`. Either way, the plan reports them under `never_uploaded_at_risk` so operators can see what unverified data would be affected.
- Files inside the minimum-age window and files with multiple hard links are
  protected and reported separately. Hard links are not counted as safely
  freed bytes because unlinking one name may release no disk space.
- Plans report `projected_total_bytes`, `capacity_shortfall_bytes`,
  `capacity_target_met`, and whether the capacity measurement was complete.
  An executed apply returns non-zero when the requested capacity target cannot
  be met, even if every safe planned deletion succeeded.
- Each plan pins data and manifest identities. Apply reacquires the manifest
  writer lock and rechecks those identities immediately before descriptor-
  relative unlinking. A changed file, manifest, or parent-directory symlink
  swap fails closed rather than deleting the replacement/off-root target.
- Dry-run by default; require `--execute` to perform deletions.

## Configuration
Shared config file (JSON at this phase) centralizes paths, retention, upload agent, and server settings.

Top-level keys:
- `robot_id` (required): this robot's registered fleet identity. A config
  without it does not load. The robot id is never taken from the file path.
- `paths`: `{ robot_root, extension }`
- `retention`: `{ min_retain_days, max_total_gib }`
- `upload_agent`: `{ concurrency, parallel_streams, bandwidth_mbps, chunk_size_bytes, max_retries, base_backoff_seconds, small_file_threshold_mb, compute_chunk_hashes, min_stable_age_seconds }` — see `phases/02-robot-upload-agent/docs/technical.md` for bounds.
- `server`: `{ host, port }`
- `tls` (optional): `{ enabled, mtls, ca_path, cert_path, key_path }`.
  `enabled` is the only TLS switch and defaults to `true`. mTLS requires all
  three files.
- `auth` (optional): `{ token_file }`, the path to this robot's credential
  file. The config cannot hold an inline token.
- `capture` (optional, capture metadata): `{ operator, site, permission_ref, session_tags, task_label }`, all optional strings. Set once per deployment/session — there is no per-run CLI flag for these, deliberately: an operator/site/permission reference doesn't change file-to-file the way retention or chunk size might. If the section is absent, or a key is omitted, every manifest field it feeds defaults to `null`. See "Manifest fields" above for how these values reach a manifest, and `proto/upload.proto`'s `UploadMetadata` fields 11-15 for how they reach the ingest server.

Example:
```json
{
  "robot_id": "replace-with-registered-robot-id",
  "paths": { "robot_root": "/robot_data", "extension": ".mcap" },
  "retention": { "min_retain_days": 14, "max_total_gib": 200 },
  "upload_agent": { "concurrency": 2 },
  "server": { "host": "upload.local", "port": 50051 },
  "tls": {
    "enabled": true,
    "mtls": true,
    "ca_path": "/etc/robot-vault/tls/ca.crt",
    "cert_path": "/etc/robot-vault/tls/robot.crt",
    "key_path": "/etc/robot-vault/tls/robot.key"
  },
  "capture": {
    "operator": "alice",
    "site": "lab-1",
    "permission_ref": "consent-42",
    "session_tags": "demo,indoor",
    "task_label": "pick_and_place"
  }
}
```

## CLI Overview
Package name: `robot_storage_manager` (CLI: `rsm`).

Commands:
- `rsm scan --root /robot_data/ROBOT [--config /path/vault.json] [--ext .mcap]` — discover files, create/update manifests.
- `rsm hash --root /robot_data/ROBOT [--config /path/vault.json] [--missing-only] [--min-stable-age-seconds N]` — compute SHA256 and update manifests.
- `rsm list --root /robot_data/ROBOT [--status pending|uploading|completed|failed]` — list files and metadata.
- `rsm inspect PATH` — show a single manifest.
- `rsm retention plan --root /robot_data/ROBOT [--config /path/vault.json] [--min-retain-days N] [--max-total-gib G] [--allow-unuploaded]` — print plan; CLI flags override the shared config, which overrides the built-in defaults (7 days / 200 GiB).
- `rsm retention apply --root ... [--config /path/vault.json] [--min-retain-days N] [--max-total-gib G] [--allow-unuploaded] [--execute]` — apply plan (dry-run unless `--execute`).

Commands are idempotent. A destructive retention apply is safe to retry, but
may return non-zero when a concurrent change invalidates its plan or protected
data prevents reaching the capacity target.

## Failure Modes and Handling
- Partial files (growing): `rsm hash` applies `is_file_stable()` automatically
  (default 5s); files inside the window are reported and deferred. Hashing also
  detects mutation during the read.
- Corrupt manifests: input is capped at 1 MiB and parsed as strict JSON
  (duplicate keys and non-finite numbers are rejected). The original is
  preserved, bounded, as `<manifest>.corrupt`, and a fresh `pending` manifest
  is regenerated. Permission/I/O failures do not trigger regeneration.
- Concurrent writers: atomic replacement prevents torn JSON; an advisory
  sidecar lock plus `manifest_revision` prevents lost updates. A stale writer
  raises instead of downgrading newer upload state, while a confirmed
  completion remains sticky for identical content.
- Persistence: the temp file and containing directory are both fsynced so the
  atomic replacement survives a crash/power-loss boundary on POSIX filesystems.
- Incomplete scans: CLI commands request strict scanning. A traversal/I/O
  failure exits non-zero instead of applying retention from a partial file set
  and falsely reporting that the capacity measurement was complete.
- Time skew: base age calculations on file mtime; allow `--now` override for deterministic tests.

## Data Integrity
- Use canonical absolute paths in manifests; operational loads reject final
  data/manifest symlinks and non-regular files. CLI manifest reads and writes
  are anchored to the selected capture root by directory-descriptor traversal,
  so swapping an intermediate directory after discovery cannot redirect them.
- Stable hashing with chunked reads; verify after upload in Phase 02.

## Docker/dev usage
- See `run-and-test.md` for the current sample-data and CLI workflow; Compose
  mounts robot capture data at `/tmp/robot_data`.
- Use `configs/vault.local.json` for development and
  `configs/vault.remote.json` for a secure remote deployment. The older
  `configs/vault.json` is a compatibility template with platform-trust TLS but
  no explicit robot identity, client certificate, or CA path; do not use it as
  a release security template.

## Acceptance Criteria
- `scan` creates manifests for discovered files, idempotent.
- `hash` computes correct SHA256 for deterministic fixtures.
- `retention plan` respects `min_retain_days`, orders by age, and produces stable dry-run results.
- Unit tests cover manifest round-trip, hashing, and retention selection logic.

## Interfaces to Phase 02
- `upload_id` is reserved for the upload agent to fill (deterministic from
  path, size, SHA-256, and file modification time).
- `status` lifecycle transitions will be managed by the agent; this phase initializes as `pending`.

## Observability (local)
- CLI prints structured output; logs to stderr on errors with exit codes (>0 on failure).
