# Phase 02 — Robot Upload Agent (Technical)

This phase implements the operator-triggered robot upload agent. CLI and web UI
actions scan Phase 01 manifests and send stable files to the Phase 03 ingest
server with bounded file concurrency, integrity verification and adaptive
multipart resume and retry. It does not run a background scheduler or SSID gate.

## Goals
- Manual CLI and web actions for pending/failed backlog uploads.
- Idempotent retries with deterministic `upload_id` and reuse of
  durable server multipart parts. The agent always requests resume version 1.
- Bounded concurrency and bandwidth throttling per robot.
- **Smart Batching**: connection-quality detection, size-aware ordering with
  an optional critical-priority hint, and file-level upload concurrency. See
  "Smart Batching" below.
- **Adaptive retry**: transient gRPC failures are retried in-process with full-jitter backoff.
- **Measured transport policy**: application/gRPC compression was evaluated and
  deliberately not shipped; recorders should compress MCAP chunks at source.

## Non-Goals (Phase 02)
- Server-side ingest hardening (Phase 03).
- Heartbeats, background scheduling and fleet orchestration (not implemented).

## Inputs/Outputs
- Input: Manifests from Phase 01 (`status` initially `pending`).
- Output: Updated manifests (`pending`/`failed` → `uploading` →
  `completed`/`failed`), logs, and retry state. Status is set to `uploading`
  before the file is streamed, so retention logic protects it while the
  transfer is in flight; it is restored to `failed` if the upload raises.

## State Machine

HTTP jobs describe an individual attempt. A finished job reports every
selected file as completed or failed. If the queue skips a recording another
job is already uploading, this attempt reports `failed` with an `in_progress`
error and retry guidance; it does not rewrite the other job's manifest. Final
reporting reads unresolved files' manifests so a successful earlier transfer
survives an unexpected error later in the queue. The stored path and recording
identity must match before an old completed sidecar confirms an unresolved
result. Replacing the file requires a new upload. `make test-upload-jobs`
protects these outcomes through the ordinary HTTP and gRPC boundaries.

- Idle → operator trigger → queue stable files → upload/retry → update manifest
  → done.
- Retry policy: a transient failure is retried **in process**, with full-jitter exponential backoff, before the manifest is marked `failed`. `max_retries`, `base_backoff_seconds` and the connection strategy's `retry_multiplier` all feed that loop (see "Adaptive retry" below). A file that exhausts its retries is marked `failed` and picked up again by a later CLI trigger or the UI's retry-failed action. Manifest state is persisted to disk, so pending/failed files survive a process restart.

## Configuration & Connectivity Detection

CLI and web uploads use the validated shared `vault.json` schema. Python
integrations should use `UploadAgentConfig.from_vault(load_config(path))`,
with `load_config` imported from `robot_vault_config.model`, or pass
`vault_config_path=path` to `UploadQueue`.

- Shared config file (JSON for now) defines:
  - `robot_id` (required; the agent never derives it from the file path)
  - `paths.robot_root`, `paths.extension`
  - `upload_agent` (concurrency, bandwidth, chunk size, retries, backoff,
    small-file threshold, `min_stable_age_seconds`)
  - `server` (host, port)
  - `tls` (`enabled` default `true`, `mtls`, `ca_path`, `cert_path`, `key_path`)
  - `auth` (optional: `token_file`, the path to this robot's credential file)
- TLS settings are validated atomically before credential files or a channel
  are opened:
  - `tls.enabled` is the only TLS switch. Omitting the `tls` section means
    TLS on with the platform trust store. `"tls": {"enabled": false}` is for
    local development only. `server.tls` is not a valid key.
  - `tls.mtls: true` requires non-empty `ca_path`, `cert_path`, and `key_path`.
    The client certificate/key are atomic and are rejected when mTLS is false.
    A custom `ca_path` by itself remains valid for one-way private-CA TLS.
  - gRPC verifies `server.host` against the server certificate SAN. The client
    does not override the authority name, so the configured public DNS name (or
    certificate-covered IP address) must be used.
  - Release ingest also authorizes the robot identity in the authenticated
    client leaf. It must contain exactly one URI SAN
    `spiffe://robot-vault/robots/<percent-encoded-robot-id>` matching the
    top-level configured `robot_id`; a CN alone is not an authorization
    identity. Development ingest does not require this SAN contract.
- Trigger uploads with CLI `rua upload --root ... --config /path/vault.json` or
  the robot web UI. For local client/server smoke runs (whose Compose graph
  also starts PostgreSQL, object-store and bootstrap dependencies), use
  `configs/vault.local.json`.

## Transport (gRPC)
- gRPC client implemented via `GrpcUploadTransport`.
- The transport repeats the critical structural checks at its byte-level API:
  TLS bytes on a plaintext channel, cert/key XOR, empty material, and a client
  identity without an explicit CA all fail before gRPC creates a channel.
- Authentication: every RPC carries `authorization: Bearer <token>`, where the token comes from the file named by `auth.token_file` if set, else the `INGEST_AUTH_TOKEN` environment variable (which `docker-compose.yml` sets on the `robot`/`seed`/`dev` services). With neither set, no credential is sent — fine against a server that has no token configured; against one that does, the RPC fails `UNAUTHENTICATED`.
- Methods used:
  - `UploadSmall` for files ≤ `upload_agent.small_file_threshold_mb` (default 4 MiB,
    capped at 48 MiB — the whole file rides in one gRPC message and both ends
    cap a message at 50 MiB; a config with a value outside `[0, 48]` does not
    load)
  - `Upload` (client streaming) for larger files, chunked by `upload_agent.chunk_size_bytes`
- Deterministic `upload_id` computed from `{robot_id, path, size, sha256, mtime}`.
  The `robot_id` keeps byte-identical files at the same path on two robots
  from sharing an id (the second robot would otherwise get a permanent
  `ALREADY_EXISTS`). The
  ingest server reserves `(upload_id, robot_id, filename, size, sha256)` before
  object storage; an exact retry is idempotent and conflicting reuse fails
  before it can overwrite bytes.
- Integrity: `ensure_sha256_hash` computes and persists the file's SHA256 before every upload whenever the manifest doesn't already carry one, so `expected_sha256` sent to the server is never empty. End-to-end integrity checking is never skipped, even for files that never went through a manual `rsm hash` step.
- Success → set manifest `status=completed`; failure → `status=failed`
  (retryable in process and on a later trigger).

## Stub Generation (Python)
- Generate stubs from `proto/upload.proto` into the Phase 2 package:
```
cd /path/to/robot-vault
python -m grpc_tools.protoc -I proto \
  --python_out=phases/02-robot-upload-agent/src \
  --grpc_python_out=phases/02-robot-upload-agent/src \
  proto/upload.proto
```

## Concurrency & Throttling

- **`upload_agent.concurrency`** — how many pending files one `run_many` call *takes on*.
- **`upload_agent.parallel_streams`** — how many of them are *in flight at once*. Default 2,
  hard ceiling 4.
- **`upload_agent.bandwidth_mbps`** — shared payload allowance for one queue's unary and
  streaming workers, in binary Mibit/s (1 = 131,072 bytes/s). **`0` (unlimited) is the
  default.** It starts with one second of allowance and charges attempted retransmission
  bytes; resumed parts already on the server consume none. Since a gRPC payload message is
  sent whole, the instantaneous burst can include one message of up to 48 MiB for unary
  uploads or 8 MiB for a stream part. Each CLI invocation and web job creates its own queue
  and allowance; this is not a host-wide limit. `core.py` builds a paced generator and
  `transport.upload_stream` sends exactly what it is given (below), so the value takes effect.
  A default of `50` would cap every robot at 6.25 MiB/s against a measured 193 MiB/s
  single-stream baseline, so the default is an explicit "no cap". The value is not scaled per
  connection quality: "configure 50 Mbit/s, get 30" is not a rate limit anyone asked for.

### `transport.upload_stream` consumes the chunks it is handed

The transport sends the `chunks` iterable it is given and does not rebuild its own generator
from the file. If it did, `bandwidth_mbps` would pace nothing, `compute_chunk_hashes` would
never put a digest on the wire (so the server's `chunk_hash_mismatch` check would never run),
and every byte of every file would be read twice. Per-chunk digests are computed in the
transport, on the bytes it actually sends, not passed through a mutated `manifest["chunk_hash"]`.

### File-level upload concurrency

- **The unit of concurrency is a whole file.** Never parts of one file: the server validates
  the pinned part plan and an exclusive upload lease, so splitting one object across streams
  would be rejected.
- **One independent gRPC channel per worker.** A gRPC channel opens exactly one TCP connection
  per subchannel, so N concurrent RPCs on a shared channel are N HTTP/2 streams over one
  connection, sharing one congestion-control state and one loss-recovery timeline. On a
  link-limited robot that is worth approximately nothing. Independent connections are the
  lever; more streams on one connection are not.
- **Effective count** = `min(config.parallel_streams, strategy.parallel_streams,
  server-advertised per-robot limit, 4, number of files)`. The server's limit is read from
  `HealthCheck`'s `checks` map (`per_robot_concurrency`); when the server advertises nothing
  the client falls back to the documented `ASSUMED_SERVER_PER_ROBOT_STREAMS = 2`, mirroring
  `PER_ROBOT_CONCURRENCY` in `configs/ingest.env`. Over-requesting does not fail — it blocks
  in the server's `acquire_per_robot` while the extra worker holds an idle channel and burns
  its call deadline. `rua status` reports `effective_parallel_streams`.
- **The benefit is U2 (draining a backlog of N files), not U1 (one big file).** A single file
  is one stream by definition.
- `rua upload --all` drains every pending file in one process. `rua upload` without it still
  uploads exactly one file, as before.

### Adaptive retry

`retry.py`. Retry is application-level and hand-rolled, not a gRPC `retryPolicy`: a call
becomes *committed* — no further transport-level retry — as soon as the client sees any
response header or the outgoing stream exceeds the client's retry buffer, which for a
multi-megabyte MCAP happens almost immediately. A service-config policy on `Upload` would
buffer bytes and then silently stop applying.

Every attempt probes `ResumeUpload` before opening a fresh `Upload` RPC. Version 1
responses bind the server's saved parts to a session token; the client reads and sends only
missing parts. A server that does not return resume version 1 is not supported.

| Retried | Not retried |
|---|---|
| `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`, `ABORTED`, `INTERNAL` (capped at 2) | `INVALID_ARGUMENT` (incl. `sha256_mismatch`, `chunk_hash_mismatch`, `chunk_too_small`), `UNAUTHENTICATED`, `PERMISSION_DENIED`, `FAILED_PRECONDITION` (unregistered robot), `NOT_FOUND`, `UNIMPLEMENTED`, `CANCELLED`, and any exception carrying no gRPC status |

`DEADLINE_EXCEEDED` is ambiguous in general, so each retry re-probes durable parts and
binds the new stream to its session token. The server upload lease serializes attempts,
and immutable metadata validation protects completed receipt replay. `INTERNAL` is retried because `main.rs`
deliberately returns it when the completed-row write fails so that the client replays.
The non-retried codes are deterministic: the second attempt fails identically after re-sending
the whole file.

Backoff is **full jitter**, `uniform(0, min(60s, base_backoff_seconds * retry_multiplier ** attempt))`,
hand-rolled because gRPC's A6 proposal (±20 % around nominal) and Microsoft's gRPC retry
documentation (uniform over `[0, backoff)`) describe different distributions. Full jitter is
what actually de-synchronises a fleet that all lost the same server in the same instant.
`RESOURCE_EXHAUSTED` — the server's own backpressure — gets a 4x delay penalty.

Retries save bandwidth by skipping parts the server confirms are durable. This does not
increase physical link speed. Backoff and `max_retries` still limit repeated failures.
The byte budget charges bytes actually yielded to failed attempts, including full sends after
an expired session, and stops at `3 x file_size`. A failed resume probe or a small missing suffix
therefore does not consume a whole file's budget. Callers without byte measurements retain
conservative whole-file accounting.

### Transport compression — measured, then NOT shipped

There is no compression in the upload path, and no `compression` setting anywhere. That is a
measured decision, not an omission, and the measurement is why.

Measured (1 MiB slices, gzip level 6):

| MCAP internal compression | payload | gzip ratio | gzip MiB/s | break-even link |
|---|---|---|---|---|
| none | JSON | 8.13x | 66.7 | 468 Mbit/s |
| none | binary | 1.33x | 35.7 | 70 Mbit/s |
| zstd | JSON | 1.36x | 27.4 | 58 Mbit/s |
| zstd | binary | 1.04x | 39.6 | 13 Mbit/s |
| lz4 | JSON | 2.42x | 21.4 | 100 Mbit/s |
| lz4 | binary | 1.10x | 26.8 | 19 Mbit/s |

"Break-even link" is the speed at which seconds saved on the wire equal seconds burned
compressing; above it, compression makes the upload *slower*. So compression pays only on MCAP
that is **not already internally compressed**, and only on a **slow link** — at LAN speed it
cost up to 2x, because compress and send serialise. End to end it is worth ~24.6% fewer bytes
on an internally-uncompressed bag at 20 Mbit/s, and nothing at all otherwise.

An automatic (unconfigurable) version is not worth shipping, for two reasons:

1. **Real recordings are already compressed.** ROS 2 rosbag2 defaults to zstd chunk
   compression, and this repo's own fixtures (`development/seed.sh`,
   `tests/e2e/..._write_test_mcap`) produce **zstd**, not `NONE` — verified by reading
   `ChunkIndex.compression` back out of a generated file. So the case where compression helps
   is the uncommon one.
2. **It could never fire.** A gate needs a link-speed estimate, which comes from
   `bandwidth_mbps` (0 — meaning unlimited — in all five shipped configs) or from the
   throughput of a previously completed upload in the same process. `rua upload` is a fresh
   process per invocation, so the estimate is always absent and the gate always declines.
   That would put dead code on the data path.

If a fleet is known to record uncompressed bags over a slow link, the honest fix is to enable
zstd **in the recorder**, where it is cheap, once, and does not have to be re-derived per
upload. Note also that compressing `UploadChunk.data` at the application level is not an
option: the server hashes and part-uploads it verbatim, so that would break the end-to-end
sha256 and the 5 MiB part floor.

Nothing in the upload path changes stored bytes, so no 90 % storage / 80 % network saving is
claimed; those figures came from a different (record-packing) workload.

### Connection Quality Detection

- **Measured path, not interface folklore.** Every refresh sends five pings to
  the configured ingest host and parses their average RTT and packet-loss
  percentage. A public `8.8.8.8` ping or an `iwconfig` substring heuristic
  would not describe the route carrying the upload.
- **Observed throughput.** A long-running agent also feeds the most recent
  successful transmitted-bytes/duration measurement into the next 30-second
  refresh. It is not a configured link rate. Short-lived CLI invocations have
  no prior throughput sample and classify on RTT/loss alone.
- **Deterministic thresholds.** Throughput `<1/<10/<50/>=50` Mbit/s maps to
  Poor/Normal/Good/Excellent; RTT `>=300/>=150/>=50/<50` ms and loss
  `>=3/>=1/>0/0` percent map in the same order. The worst available signal
  wins. If no signal can be measured, the label is `NORMAL` and the
  reported measurement source is `unavailable`.
- This classification is deterministic and exercised against synthetic
  netem-shaped measurements. The strategy table itself is still unvalidated
  by a completed throughput matrix, so no claim that smart batching improves
  poor-link throughput is published.

### Connection strategy table

| Quality | Parallel streams | Retry multiplier |
|---------|------------------|------------------|
| Poor | 4 | 3.0x |
| Normal | 3 | 2.0x |
| Good | 2 | 1.5x |
| Excellent | 1 | 1.0x |

Selected files enter one ordered worker queue. Size and count groups have no
execution role, so the agent does not create batch IDs or advertise batch limits.

**`parallel_streams` grows as the link gets worse.** The opposite mapping (4 for EXCELLENT, 1
for POOR) would be backwards for the mechanism actually implemented. Independent TCP connections help exactly
where a single congestion window cannot fill the pipe — high RTT and/or loss, where one
connection is bounded by roughly `MSS/(RTT*sqrt(p))` no matter how much capacity is idle, and N
connections recover close to N times that. On a fast, low-latency, loss-free link a single
stream already sits near line rate, so extra connections buy nothing and add memory and
shared-fate failure surface. Hence: most streams where the link is worst.

The table has no other columns, on purpose:
- No `compression_level` or compression-ratio numbers. Nothing compresses anything, and
  transport compression is not shipped — see the measured decision above.
- No `chunk_size_kb`. A per-strategy chunk size (512 KB–4 MB) would sit below the server's
  5 MiB S3 part floor, so the config value `upload_agent.chunk_size_bytes` would always win.
  Chunk size must also stay pinned per upload, which forbids adapting it per attempt.

### Chunk size

`upload_agent.chunk_size_bytes` is validated by `robot_vault_config.validate_chunk_size_bytes`
into `[5 MiB, 8 MiB]` — the S3 multipart minimum part size below (a hard server-side
rejection, `chunk_too_small_for_s3_part`) and the ingest server's streaming ceiling above.
The service-wide 50 MiB decoder allowance is separate: it preserves `UploadSmall`'s 48 MiB
file threshold and does not permit larger streaming chunks. A config with an out-of-range
chunk size does not load. The default is
**5 MiB**; `MAX_PARTS (10000) x chunk size` permits about **48.8 GiB / 52.4 GB** at the
default and **78.1 GiB / 83.9 GB** at the 8 MiB maximum. See "Measured performance" for the
throughput caveat: larger chunks are slower against the shipped server, which pipelines its
S3 part uploads.

### What is measured, per connection quality
- **Priority-Based Ordering**: `order_files` sorts files marked
  `priority: "critical"` first, then by size. Standard Phase 01 manifests do
  not define or populate that field, so their files all take the normal path;
  an externally supplied hint changes ordering only, with no preemption or
  separate queue.
- **Performance Metrics**: Prometheus histograms/counters/gauges track upload duration, bytes uploaded, and speed, all labeled by connection quality.

### Implementation
- `smart_batching.py`: `SmartBatcher` — connection detection, file ordering, per-quality strategy.
- `retry.py`: retryable status codes, full-jitter backoff, byte budget.
- `core.py`: `UploadQueue` — chunk plan, pacing, the worker pool with per-worker channels, metrics.
- `bench/`: corpus generator, compression measurement, upload throughput benchmark.

## Measured performance (2026-07-25)

Test host: single NVMe, all services on one docker bridge.
**Single-host loopback, no TLS, page-cache reads, a single-drive object store.
Not a robot-link figure.** Corpus from `bench/make_corpus.py`; harness
`bench/upload_bench.py`; 3–5 trials each, median with full range; every trial asserted
`status=completed` and `integrity_verified=true`.

**Comparability.** Every number below was measured against an ingest server that pipelines
S3 part uploads, so the arms are like-for-like. They do not describe a server that
awaits each part inline.

**U1 — single 500.99 MiB MCAP, one stream.** At the default 5 MiB chunk size the client
measured 193.6 MiB/s (5 trials, 193.2-193.8). An independent re-run at the shipped default on a zstd/binary 500 MiB corpus measured 185.9
(184.7/185.9/188.2) — same ballpark, and the spread across corpora and system load is why the
figures below are quoted with ranges rather than as a single number.

Chunk-size sensitivity, 5 trials each:

| `chunk_size_bytes` | wall (s) | MiB/s (median, range) |
|---|---|---|
| 5 MiB  | 2.585 | **193.6** (193.2–193.8) |
| 8 MiB  | 3.013 | 166.1 (161.6–168.0) |
| 16 MiB | 3.332 | 150.2 (142.5–154.2) |

**Why larger chunks are slower here.** Against a server that awaited each `upload_part`
inline, sequential round trips would dominate and fewer, larger parts would win. Against a
server that pipelines parts, part count does not cost round trips, and larger chunks instead
mean fewer parts in flight and bigger allocations. The 16 MiB arm is not a
valid production setting under the current server contract. The shipped default is therefore **5 MiB**, the
fastest measured arm on this server. Operators can raise it for files beyond the default
~48.8 GiB multipart ceiling only as far as the validated 8 MiB cap, which permits about
78.1 GiB per multipart upload.

**U2 — backlog drain, 8 x 100.7 MiB = 806 MiB.** This is the operational metric: a robot docks
and drains a day's recordings.

| Arm | wall (s) | MiB/s (median, range) | vs baseline |
|---|---|---|---|
| baseline: `rua upload` per file (9 CLI invocations), 5 MiB | 9.509 | **84.7** (82.9–86.8) | 1.00x |
| `rua upload --all`, `parallel_streams=1`, 5 MiB | 4.817 | 167.3 (159.1–177.4) | **1.98x** |
| `rua upload --all`, `parallel_streams=2`, 5 MiB | 4.168 | **193.4** (189.0–206.5) | **2.28x** |
| `rua upload --all`, `parallel_streams=2`, 16 MiB (comparison arm) | 4.641 | 173.6 (167.6–176.7) | **2.05x** |

Attribution: most of the gain (1.98x) is amortising the ~0.5 s per-file Python CLI start —
eight process launches become one. File-level concurrency adds a further **1.16x** on top
(167.3 → 193.4). That is modest and expected on loopback, where a single stream already nears
the box's limit; the mechanism exists for links where one congestion window cannot fill the
pipe, and **that case has not been measured here.**

**Compression experiment.** An experimental gate (not shipped) declined compression on a
loopback path because the path runs at roughly 1.5 Gbit/s while gzip tops out near 66 MiB/s
of input. The experiment measured bytes and CPU per 16 MiB chunk:

| File | internal | gzip-6 ratio | bytes saved | robot CPU | break-even link |
|---|---|---|---|---|---|
| 20.3 MiB MCAP, `compression=NONE`, binary payload | `""` | 1.326x | 24.6 % | 29.1 s/GiB | 69 Mbit/s |
| 20.7 MiB MCAP, `compression=ZSTD`, binary payload | `zstd` | 1.043x | 4.1 % | 25.8 s/GiB | 13 Mbit/s |

In a 20 Mbit/s pacing arm, the experimental gate compressed the uncompressed file and declined
the ZSTD file. Both completed with verified integrity, but wall clock was unchanged
because pacing was based on source rather than wire bytes. An inert or misleading
gate is not worth shipping, so uploads never negotiate gzip.

**Not measured, and therefore not claimed:** any real robot→server throughput (no WAN, LTE,
WiFi, loss, jitter or bandwidth cap was applied), TLS cost, ARM/robot CPU, real-S3 latency,
fleet concurrency or soak.

## File Stability Gate (still-recording files)
- Every upload-candidate selection (`run_once`, `run_many`, `upload_selected`) is gated on phase-01's `is_file_stable`. A file is still recording, and is not uploaded, when either check fails:
  - its mtime is younger than `upload_agent.min_stable_age_seconds`;
  - it is a `.mcap` file that does not end with a complete MCAP footer (a Footer record, opcode `0x02` with length 20, followed by the closing magic `\x89MCAP0\r\n`). A recorder writes these last. A buffered recorder can pause for longer than the mtime window, so the mtime check alone would ship a truncated file, and the full file would later arrive as a second recording.
- Without this gate, a mid-write MCAP gets hashed and streamed, and the `expected_sha256` sent to the server stops matching the file the moment the recorder appends again. Other extensions get only the mtime check.
- Default: `5` seconds, matching `robot_storage_manager.DEFAULT_STABILITY_SECONDS`. It is deliberately *not* `0` — a `0` default would leave the gate inert and the hole open; `5s` only ever defers a file to the next scan.
- `run_once`/`run_many` report deferred files in a `skipped` list and pick them up on a later pass. `upload_selected` (an explicit UI/operator selection) is gated too — the operator picked a file, not a moment, and cannot see that the robot is mid-write — but because nothing re-scans on their behalf it surfaces a per-file error (`{"status": "unstable"}`) instead of silently doing nothing.
- Set `min_stable_age_seconds: 0` to turn off the mtime check (e.g. a recorder that renames files into place only when complete, so freshness carries no information). The MCAP footer check always runs.

## Interrupted Uploads & Shutdown
- A manifest is set to `uploading` before streaming and restored to `failed` in an except block. A hard kill (SIGTERM/SIGKILL, crash) never runs that block, so the manifest would stay `uploading` — a status the scan loops never re-select. Two mechanisms recover it:
  - `robot_storage_manager` reconciles any manifest stuck at `uploading` for more than `DEFAULT_STALE_UPLOADING_SECONDS` (1h) back to `failed` on the next scan. This is the backstop and covers SIGKILL and crashes.
  - The web agent additionally reverts them on graceful shutdown: `create_app` registers an ASGI lifespan shutdown hook (`revert_inflight_uploads`) that walks the in-memory job store for still-running jobs and resets their `uploading` manifests to `failed`. uvicorn — PID 1 in the `robot` container — turns `docker stop`'s SIGTERM into exactly this shutdown, so recovery is immediate instead of an hour later.
  - The hook is best-effort by design: it never joins the daemon upload threads (an upload that still succeeds afterwards simply rewrites `completed`, and `save_manifest` is atomic), and a thread that starts its *next* file after the sweep is still covered by the 1h reconcile.

## Behaviour on a poor connection — still unmeasured

Every number in "Measured performance" above was taken on single-host loopback:
multi-Gbit, ~0.05 ms RTT, no TLS, page-cache reads. **No claim about behaviour on
a poor connection is published here, and none should be until it is measured.**
In particular the `BATCH_STRATEGIES` table's `parallel_streams` values (POOR 4 →
EXCELLENT 1) come from physics reasoning, not from measurement.

The harness for that measurement exists, but the matrix has not been run. What
exists:

- `deploy/docker-compose.bench.yml` — grants `NET_ADMIN` to the `robot` service only,
  as a separate overlay `docker compose up` cannot pick up implicitly.
- `make bench-net PROFILE=good-wifi|poor-wifi|bad-lossy` — recreates ingest
  with the benchmark-only stream cap (default 4) and robot with the capability,
  then verifies shaping. `make bench-net-down` restores both base services.
- `bench/netem.py` — applies the profile, **refuses to proceed unless the link
  measures as shaped**, and always tears the qdisc down, including on failure.
- `tests/unit/test_netem_shaping.py` covers that decision logic, including that
  an unshaped 0.05 ms RTT is rejected for every profile and that
  no profile can be mistaken for a neighbouring one.

Two validity preconditions are enforced. The overlay
sets `PER_ROBOT_CONCURRENCY=${BENCH_PER_ROBOT_CONCURRENCY:-4}` without changing
the normal default of 2, and the upload harness asks HealthCheck for the real
cap and exits before a trial when the requested arm exceeds it. The agent
classifies RTT/loss on the shaped path to ingest and records those measurements,
the quality label, and the requested/strategy/server/effective stream bounds in
the result (each trial also records the actual file-count-bounded value). These
fixes make a future run valid; they are not the run. The matrix remains
unmeasured.

See `bench/README.md` for the matrix to run and what to do if the results
contradict the table.

## Upload history: two endpoints, two questions
- `GET /api/upload/jobs` is the **live** view. It reads the in-memory `JobStore`, so it has job grouping and live byte counters — and it is empty after a restart, by construction. The SIGTERM hook above depends on this store, so it is not going away.
- `GET /api/upload/history?root=<path>` is the **durable** view, reconstructed from the `*.manifest.json` sidecars the storage layer already writes. It survives any number of restarts because it holds no state of its own.
- The two cannot drift, because neither derives from the other: one reports what this process is doing now, the other what happened to each file.
- **Per-file, not per-job.** Manifests are per-file and carry no job identifier.
  Reconstructing jobs would mean either inventing an id the data cannot support
  or persisting a second job-to-files store. A flat log is what the existing
  data supports honestly.
- Status comes from the manifest and is always a canonical `UploadStatus` (`pending|uploading|completed|failed`); `parse()` accepts only these current names. **No status is invented** — there is no `cancelled` or `interrupted`.
- The read path uses `robot_storage_manager.read_manifest`, not `load_or_create_manifest`, for two reasons: `load_or_create_manifest` *writes* (it copies unparseable manifests to `.corrupt`), and it applies the content-change invariant that resets status to `pending` when a file's size/mtime drift. That invariant is right for "may I upload this?" and wrong for "what happened to this file?" — it would erase the record of a completed upload the moment anything touched the file. A stranded `uploading` manifest is still reported as `failed` once past the 1h staleness window, in memory only, matching what the next scan will persist.
- Retention deletes a data file and its manifest together, so history shrinks with retention. Where a manifest outlives its data file, `data_file_present: false` says so rather than implying the bytes are still on the robot.
- The robot SPA consumes this durable endpoint for its History view.

### Recording evidence

`GET /api/files` and `GET /api/upload/history` include these additive fields:

| Field | Meaning |
| --- | --- |
| `data_file_present` | The confined local path currently resolves to a regular file. |
| `server_verified` | `true` when a validated upload receipt matches the current local file; otherwise `null`. |
| `server_verified_at` | UTC time when the agent received and validated the upload response, or `null`. |
| `server_storage_path` | Server location recorded in that receipt, or `null`. |
| `safe_to_remove_local` | Always `false`; this API does not approve deletion. |
| `last_upload_error` | Persisted response or exception with `status`, optional `detail`, and optional gRPC `code`; `null` without a recorded error. |
| `upload_updated_at` | UTC time of the most recent recorded upload transition, or `null` for older manifests. |

New uploads retain a receipt only when the canonical completed response matches
upload ID, size and SHA-256, includes a storage path, and the local file identity
matches before and after transfer. Old completion markers alone do not establish
verification. Changing or removing the local file invalidates the displayed proof.
The timestamp describes verification at upload time. It does not establish that
the server still has the object today. Keep the local recording until server
storage has been checked and the configured retention policy permits removal.

Listing files and history reads manifests without creating them or persisting
recovery transitions. The file listing reports `pending` when the current file
identity no longer matches its manifest, so changed recordings can be selected
for upload. History keeps the previous upload status while verification becomes
unknown. Upload failures remain visible after a restart; beginning
a new attempt clears the previous error and receipt.

## Web filesystem boundary and UI serving
- `BASE_ROOT` (`/tmp/robot_data`) is the outer web sandbox. Request roots are canonicalized with realpath semantics and must resolve beneath it.
- File selections and `/api/files` subpaths are additionally confined to the exact selected root. Relative paths are interpreted below that root; `..`, absolute sibling paths, and symlinks that escape it return `400`.
- Status, listing, resume, retry, history, existence checks, and robot discovery all use the same canonical resolver before a file is read, statted as a data file, manifested, or passed to `UploadQueue`. Directory symlinks are not walked. Poisoned manifests whose recorded data path is outside the selected history root are skipped.
- The only interactive UI is the built Vite SPA. Generated assets live under `src/robot_upload_agent/ui` and are created by `make ui-build`; they are not committed. A missing build produces a fixed, script-free `503` page. There is no inline HTML or `/static/app.js` fallback, because it would interpolate stored manifest/path data with `innerHTML`.

## Optional Per-Chunk Hashes
- `upload_agent.compute_chunk_hashes=true` makes the agent additionally compute and forward a SHA256 `chunk_hash` with each chunk for server-side per-chunk validation. This is separate from the always-computed whole-file SHA256 described above (`ensure_sha256_hash`), which runs regardless of this flag.

## Failure handling and multipart resume
- Retryable network errors use bounded full-jitter backoff. Exhausted or
  non-retryable failures persist `last_upload_error` and mark the manifest `failed`.
- The client requests `resume_version=1`. Only a v1 response with a session token
  can narrow the chunk plan. Version 0, unsupported future versions, or an
  unimplemented resume RPC fail the upload as unsupported. Authentication and transient
  probe failures propagate to the retry policy instead of restarting silently.
- A live v1 session returns its pinned `chunk_size_bytes`, original zero-based
  `missing_chunks`, and `bytes_received` for durable saved parts. The client rejects
  mismatched chunk size, upload ID, invalid or duplicate indices, and inconsistent
  byte totals before sending data.
- `UploadMetadata` echoes version, chunk size, and `resume_session_id`. The last
  transmitted part carries `is_final`, even if a later part is already saved.
  When every part is saved, metadata alone triggers finalization. A completed
  upload also uses metadata alone to validate and replay its completion receipt.
- A stale session returns `ABORTED`; retry probes again. Expired sessions require
  a fresh transfer. Successful retries preserve the original full-file digest.
- New uploads pin a chunk size between 5 and 8 MiB before their first stream.
  The agent increases the initial size if necessary to remain within 10,000
  parts, rejects larger unsupported files, and preserves existing boundaries
  across configuration changes and process restarts.
- File identity is checked against the hashed manifest before every attempt and
  while reading ranges. Changed files cannot reuse parts under their old digest.

### Chunk planning and progress
- `compute_chunks_plan` returns original `(chunk_index, offset, length)` entries.
  It narrows to `missing_chunks` only with explicit negotiated partial resume.
- The core provides a plan-aware source, so saved parts incur no disk read or
  bandwidth pacing. Chunk hashes cover the bytes actually sent.
- `saved_bytes` in Files and History is the server-confirmed count from the last
  probe or validated completion. `resume_supported` records negotiation outcome.
  These fields are saved in the manifest and cleared for a changed upload identity.
- Live `bytes_sent` and `total_bytes` describe bytes queued for this attempt and
  this attempt's missing-byte total. They are not durable server acknowledgements.
  The UI labels these counters separately from saved server bytes. Throughput
  measurements use transmitted bytes, so skipped parts do not inflate link speed.

## Acceptance Criteria
- CLI and web triggers upload stable pending/failed files without an SSID gate.
- Client tests cover sparse missing parts, all-saved finalization, completed
  replay, stale-session re-probing, malformed proof, file changes,
  pacing, persisted chunk boundaries, and actual retry traffic budgets.
- Concurrency and throttling parameters remain enforced.
- gRPC upload client works for `UploadSmall` and chunked `Upload`.

## Implementation Summary
- Library `robot_upload_agent` with queue, chunker, and gRPC transport.
- CLI `rua` with commands: `status`, `upload`, `config validate` (accepts `--config`).
