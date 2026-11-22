# Robot upload usage guide

This guide is for operators who mount robot recordings in Docker, configure
the upload agent, and upload files from one or more robots. It also shows how
to retry an interrupted upload.

## Prerequisites
- Docker and Docker Compose
- Repo cloned and dev images built (e.g., `docker compose build robot`)
- Browser UI built from the repo root with `make ui-build` (generated SPA assets are not committed)
- Ingest server reachable (set server host/IP in your config JSON)

If the SPA has not been built, the web root returns `503` with build
instructions; the JSON API remains available. The removed hand-written
JavaScript fallback is not served.

## Configure your host bag directory
1. Set `ROBOT_DATA_HOST_DIR` to the absolute path of your recording directory.
2. Start the robot container. It mounts that directory at `/tmp/robot_data`.

```
export ROBOT_DATA_HOST_DIR=/home/your-user/dev/rosbags
docker compose up -d robot
```

## Folder layout (multi-robot)
Organize per robot ID, optionally by date:
```
/home/your-user/dev/rosbags/
  demo1/
    2025-08-14/
      bag1.mcap
  demo2/
    2025-08-15/
      drive_001.mcap
```

The top-level config `robot_id` is required. It must match the registered
robot and, in release, the uniform resource identifier (URI) subject
alternative name (SAN) on its certificate. The agent never takes the robot id
from the folder path. A config without `robot_id` does not load.

The web API treats `/tmp/robot_data` as a filesystem sandbox. A selected robot
root must resolve below it, and every selected file/subpath must resolve below
that selected root. Traversal and symlinks that point outside the boundary are
rejected; the UI does not fall back to showing sibling robots when a selected
root is empty.

## Config file
Create or edit `configs/vault.remote.json` so it points to your ingest server.
For example:

```json
{
  "robot_id": "replace-with-registered-robot-id",
  "paths": { "robot_root": "/tmp/robot_data", "extension": ".mcap" },
  "server": { "host": "ingest.vault.example.com", "port": 50051 },
  "tls": {
    "enabled": true,
    "mtls": true,
    "ca_path": "/etc/robot-vault/tls/ca.crt",
    "cert_path": "/etc/robot-vault/tls/robot.crt",
    "key_path": "/etc/robot-vault/tls/robot.key"
  },
  "upload_agent": { "chunk_size_bytes": 5242880, "parallel_streams": 2, "bandwidth_mbps": 0, "concurrency": 1, "small_file_threshold_mb": 4 }
}
```

The remote template requires mutual Transport Layer Security (mTLS). Copy the
issuing certificate authority (CA) certificate, this robot's certificate, and
its private key into the paths above. Use the DNS name
present in the ingest server certificate's subject alternative names as
`server.host` (an IP address works only when the certificate contains that IP).
For release ingest, issue the robot leaf with exactly one URI SAN
`spiffe://robot-vault/robots/<percent-encoded-robot-id>` matching the JSON
`robot_id` (for example `URI:spiffe://robot-vault/robots/rover-7`). A CN-only
certificate authenticates to the CA but is rejected for robot authorization.
The config cannot hold an inline token. Put this robot's credential in a file
and set `auth.token_file` to its path, or export the token as
`INGEST_AUTH_TOKEN`. Uploads to a token-protected server without a credential
fail `UNAUTHENTICATED`.

TLS configuration is validated before a channel is opened. `tls.enabled` is the
only TLS switch, and it defaults to `true`. With no `ca_path`, the agent uses
the platform trust store. `tls.mtls: true` requires all three paths
(`ca_path`, `cert_path`, and `key_path`). Client certificate and key paths are
rejected unless mTLS is enabled. Only `configs/vault.local.json` sets
`"tls": {"enabled": false}`, for the local Compose network.

## Enroll the robot identity

Ingest rejects uploads until the configured `robot_id` exists in its registry.
For a local Compose deployment, register it through ingest's authenticated admin
route before the first upload:

```bash
ROBOT_ID=rover-7
docker compose exec -T -e ROBOT_ID="$ROBOT_ID" ingest sh -c '
  curl -fsS -X POST -G \
    -H "authorization: Bearer $INGEST_AUTH_TOKEN" \
    --data-urlencode "robot_id=$ROBOT_ID" \
    --data-urlencode "name=$ROBOT_ID" \
    http://localhost:8080/admin/robots
'
```

Registration is idempotent: repeating the request succeeds without replacing an
existing name or metadata. For a release deployment, run the equivalent request
from an authenticated host or container that can reach the private admin
listener. The complete route contract is in the
[ingest runbook](../../03-ingest-server-rs/docs/run-and-test.md#check-admin).

`chunk_size_bytes` must be in `[5 MiB, 8 MiB]`. The floor is the S3
multipart minimum part size — the server rejects any smaller non-final chunk with
`chunk_too_small_for_s3_part` — and the ceiling is the ingest server's streaming limit. The
larger service-wide decoder allowance is reserved for `UploadSmall`, whose separate file
threshold remains capped at 48 MiB. Default `5242880` (5 MiB); max file size is
`10000 x chunk_size_bytes`, so the default allows about 48.8 GiB / 52.4 GB and the 8 MiB
streaming maximum allows about 78.1 GiB / 83.9 GB. A config with an out-of-range value does not
load. Measured throughput sensitivity is in `docs/technical.md` — on a
server that pipelines S3 parts, 5 MiB is faster than the larger measured arms for a single
large file.

`upload_agent.parallel_streams` (default `2`, ceiling `4`) is how many **whole files** upload at
once, each on its own gRPC channel. It helps a *backlog drain* (measured 1.16x on top of the 1.98x
from `--all`); it does nothing for a single large file, which is one stream by definition. The
effective count is also bounded by the server's advertised per-robot limit — `rua status` shows
`effective_parallel_streams`.

`upload_agent.bandwidth_mbps` (default `0` = unlimited) is a queue-wide upload payload
allowance in binary Mibit/s: `1` means 131,072 payload bytes/s, and `50` means
6.25 MiB/s. All workers in one `UploadQueue` share it; retries and small-file RPCs
consume it too, while already saved resume parts do not. The limiter begins with one
second of allowance. Because gRPC sends a whole payload message at once, an individual
message can arrive as a burst larger than that allowance (at most 48 MiB for
`UploadSmall`, 8 MiB for a streaming chunk); the excess is paced *before* dispatch.
The budget lives for one `UploadQueue` (one CLI invocation or web job). Other
queues, including separately launched agent processes, have independent budgets.
Transport compression is not shipped; the measured rationale is in `docs/technical.md`.

The agent uploads a recording only when it is finished. It uses two checks:

- The file's mtime must be at least `upload_agent.min_stable_age_seconds` old (default `5`).
- A `.mcap` file must end with a complete MCAP footer: a Footer record followed by the closing magic bytes `\x89MCAP0\r\n`. A recorder writes these last, so a file without them is still being written. This check is always on.

A file that fails a check is not failed. Scans report it under `skipped` and try it again on the next pass. An explicit upload of it comes back as `{"status": "unstable"}`. Set `min_stable_age_seconds` to `0` only if a completed file is renamed into place; the footer check still applies.

## Upload with one command

Use the Makefile target to scan, hash, and upload. It skips completed files and
resumes saved parts when the server supports it.

Single robot:
```
make robot-upload ROBOT_ID=demo CFG=configs/vault.remote.json
```
All robots:
```
make robot-upload ROBOT_ID=all CFG=configs/vault.remote.json
```

The target runs these steps in order:

1. `rsm scan` creates or refreshes manifests for `.mcap` files.
2. `rsm hash` refreshes SHA-256 hashes so uploads do not use stale values. The
   agent also computes a missing hash on its first upload attempt, so this step
   refreshes existing hashes rather than enabling integrity checking.
3. `rua upload` retries pending or failed files and skips completed ones. Large
   file retries reuse saved parts. An expired session requires
   the whole file again.

To drain a whole backlog in **one** process instead of one CLI invocation per file, use
`rua upload --all` (optionally `--max-files N`). Measured on 8 x 100 MiB: 9.51 s per-file-CLI vs
4.17 s for `--all` with `parallel_streams=2` — 2.28x, most of it from amortising the ~0.5 s Python
start eight times over. `rua upload` without `--all` still uploads exactly one file.

`rua upload` returns exit status `0` for an uploaded file, an empty queue, or files
deliberately deferred by the stability gate. It returns `1` when any attempted file
has a preparation, authentication, or upload failure, including a batch with other
successful files. JSON output retains `uploaded`, `skipped`, and per-file `errors`.

A transient failure (`UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`, `ABORTED`,
`INTERNAL`) is now retried in-process with full-jitter backoff before the file is marked `failed`,
governed by `max_retries` / `base_backoff_seconds` and the connection strategy's retry multiplier.
Deterministic failures — `sha256_mismatch`, `UNAUTHENTICATED`, an unregistered `robot_id` — are
**not** retried, because re-sending the file would fail identically.

Tip: Add a shell alias for convenience:
```
alias vault-upload='make -C /path/to/robot-vault robot-upload ROBOT_ID=all CFG=configs/vault.remote.json'
```

## Test resume (optional)

To exercise resume with a large file, run the commands below in order. The
agent computes SHA-256 on its first upload attempt if the manifest lacks it.
You do not need a separate `rsm hash` step.
```
# Stage a single file under a temp folder and scan only (no hash)
docker exec -it robot-vault-robot bash -lc '\
  STAGE=/tmp/robot_data/demo/_stage_resume; \
  rm -rf "$STAGE" && mkdir -p "$STAGE" && cp /tmp/robot_data/demo/big.mcap "$STAGE"/ && \
  find "$STAGE" -type f -name "*.manifest.json" -delete && \
  rsm scan --root "$STAGE" --config configs/vault.remote.json'

# Start and interrupt after 5s
docker exec -it robot-vault-robot bash -lc '\
  STAGE=/tmp/robot_data/demo/_stage_resume && timeout 5s rua upload --root "$STAGE" --config configs/vault.remote.json || true'

# Resume
docker exec -it robot-vault-robot bash -lc '\
  STAGE=/tmp/robot_data/demo/_stage_resume && rua upload --root "$STAGE" --config configs/vault.remote.json'
```

## Troubleshooting
- `sha256_mismatch`: Recompute hashes without `--missing-only` before upload if file bytes changed: `rsm hash --root ... --config ...`
- No files found: Verify `ROBOT_DATA_HOST_DIR` mount and your folder layout; container sees data at `/tmp/robot_data`
- Server unreachable: confirm config `server.host` and `port`, and network/firewall. Note the vault publishes no data-plane ports by default — the ingest host must expose `50051` deliberately; see README "Network boundaries"
- `UNAUTHENTICATED`: the server has `INGEST_AUTH_TOKEN` set and the agent sent no/the wrong token — set `auth.token_file` in the config or export `INGEST_AUTH_TOKEN`
- TLS/mTLS config error: `tls.enabled` is the only TLS switch; `server.tls` is not a valid key. mTLS requires readable, non-empty CA, client certificate, and client key files; a cert/key pair is never partially accepted. Also verify `server.host` matches the server certificate SAN.

## References
- Phase 01 CLI (`rsm`): `phases/01-robot-storage/docs/run-and-test.md`
- Phase 02 CLI (`rua`): `phases/02-robot-upload-agent/docs/run-and-test.md`
- Ingest server: `phases/03-ingest-server-rs/docs/run-and-test.md`
