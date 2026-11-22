# Upload benchmarks

Three scripts, all run **inside the `dev` container** against the running stack. Committed as
generators and harnesses, never as binary corpora.

| Script | What it answers |
|---|---|
| `make_corpus.py` | builds deterministic MCAP arms across `{none,zstd,lz4}` internal compression x `{json,binary}` payload |
| `measure_compression.py` | ratio, compressor throughput, CPU per GiB and break-even link speed, per codec, per arm. This is the measurement that led to compression NOT being shipped — see the phase-02 technical doc. |
| `upload_bench.py` | U1 (one large file) and U2 (backlog drain) throughput through the real `rsm scan` → `rsm hash` → `rua upload` path |

Results are summarised in `../docs/technical.md` ("Measured performance").

## Why the corpus has four corners

`development/seed.sh` and the e2e MCAP writers use the writer's default ZSTD
chunk compression. Real recordings likewise commonly carry zstd or lz4 chunks,
while sensor payloads are usually packed binary. The explicitly uncompressed
JSON arm is therefore a deliberately compression-friendly corner, not the
seed/e2e default. The corpus exposes `--compression` and `--payload` so every
claim states which corner it measured.

## Running

```
# inside the repo root, on the host
WT=/app
PP=/opt/proto-gen:$WT/phases/common-config/src:$WT/phases/01-robot-storage/src:$WT/phases/02-robot-upload-agent/src

docker compose exec -T -e PYTHONPATH=$PP dev sh -c "
  cd $WT/phases/02-robot-upload-agent/bench &&
  python make_corpus.py --out /tmp/bench/single --mib 500 --compression none --payload json --name u1_500 &&
  python upload_bench.py --root /tmp/bench/single --robot-id bench_u1 --arm single --trials 5 \
    --base-config $WT/configs/vault.local.json"
```

`--arm backlog` drives `rua upload --all` (one process, `parallel_streams` files in flight).
`--arm backlog-loop` reproduces the one-file-per-invocation baseline: one CLI invocation per file.
`--set k=v` overrides any `upload_agent` key, e.g. `--set chunk_size_bytes=5242880`.

## House rules the harness obeys

- **Stop the processor first** (`docker compose stop processor`) or its 5 s poll will download
  benchmark objects from the object store mid-run and contaminate later trials. Restart it afterwards.
- **Everything is scoped by `--robot-id`.** The harness registers the robot (ingest refuses
  unregistered ids), and its cleanup deletes exactly that robot's `uploads` /
  `processing_status` / `robots` rows.
- **It does not delete S3 objects.** `tools/s3_gc.py` is the single owner of object deletion in
  this repo — there is a unit test asserting no second sweeper exists, and a benchmark harness is
  exactly the kind of small convenience deleter that invariant exists to prevent. Orphaned keys
  are printed instead.
- **Every trial asserts** `status=completed` and `integrity_verified=true`. A fast wrong answer is
  not a result.
- **Report median and full range over ≥3 trials**, never a single number, and never a number
  without the environment block (`nproc`, RAM, **swap in use**, chunk size, parallel streams, TLS
  state, link).


## `netem.py` — shaped-link profiles

**Status: harness only. No shaped-link numbers have been measured, and none are
published anywhere in this repo.** The measurement is still open work.

### Fail-closed check

The robot image has both `tc` and `ping`, and the benchmark overlay attaches a
netem qdisc. When no live `ingest` service is running (so `ingest` has no DNS
answer), the preflight stops before it measures anything:

```text
$ docker compose stop processor
$ make bench-net PROFILE=poor-wifi
ERROR: could not parse an RTT from ping ingest:
```

This is **not** a poor-WiFi measurement, and no result file is written. The
`shaped()` cleanup path leaves `tc qdisc show dev eth0` at `qdisc noqueue`, and
`make bench-net-down` recreates the robot without the capability
(`HostConfig.CapAdd=null`). To check this without changing any stored data, run
the two commands above, then:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.bench.yml \
  exec -T robot tc qdisc show dev eth0   # expect qdisc noqueue after failure
make bench-net-down
docker inspect robot-vault-robot --format '{{json .HostConfig.CapAdd}}' # expect null
```

### Validity gates; matrix still not run

Two conditions would make a completed run misleading. Code enforces both:

1. The client uses
   `min(config.parallel_streams, strategy.parallel_streams, server limit, 4,
   file count)`. The benchmark overlay overrides ingest with
   `PER_ROBOT_CONCURRENCY=${BENCH_PER_ROBOT_CONCURRENCY:-4}` (normal Compose
   remains 2), and `upload_bench.py` probes the advertised value and fails
   before the first trial if the configured arm exceeds it.
2. `SmartBatcher` pings the configured ingest host, parses five-probe RTT
   and loss, and maps those real measurements to the four strategy levels. A
   long-running agent adds its most recent successful whole-file throughput at
   the next refresh. The preflight and each upload result record the
   measurements, quality, and requested/strategy/server/effective bounds.

These are validity gates, not benchmark results. No shaped-link matrix has
been run. On a fresh host, use at least 10 GiB of scratch space, start the real
data plane, and prove the preconditions before generating the corpus:

```bash
docker compose up -d --build ingest
docker compose exec -T robot getent hosts ingest
docker compose exec -T robot curl -fsS http://ingest:8080/readyz
docker compose stop processor
BENCH_PER_ROBOT_CONCURRENCY=4 make bench-net PROFILE=poor-wifi
docker compose -f docker-compose.yml -f deploy/docker-compose.bench.yml \
  exec -T ingest sh -c 'test "${PER_ROBOT_CONCURRENCY:-0}" -ge 4'
# Stop unless `rua status --probe-server` reports the shaped measurements,
# the expected quality, and server_per_robot_streams >= the requested arm.
```

Always finish with `make bench-net-down`, including after a failed cell. Keep
the corpus in a dedicated scratch path; never point `upload_bench.py` at an
operator's existing `data/` tree because it resets manifests below its root.

| Profile | Rate | Delay | Loss |
|---|---|---|---|
| `good-wifi` | 100 Mbit | 20 ms | 0 % |
| `poor-wifi` | 20 Mbit | 200 ms | 1 % |
| `bad-lossy` | 5 Mbit | 400 ms | 3 % |

```bash
docker compose stop processor          # its 5s poll contaminates trials
make bench-net PROFILE=poor-wifi       # benchmark cap + robot NET_ADMIN
# ...drive upload_bench.py inside a netem.shaped() block; see below...
make bench-net-down                    # drop the capability again
```

`NET_ADMIN` is granted by `deploy/docker-compose.bench.yml` to the `robot`
service and nothing else. The same overlay sets only ingest's benchmark
`PER_ROBOT_CONCURRENCY`; `make bench-net-down` recreates both services from the
base file, restoring cap 2 and dropping the capability. It is a separate
overlay so `docker compose up` can never pick it up implicitly — the same
pattern as `deploy/docker-compose.debug.yml`. Confirm the base stack is
unaffected with `docker compose config | grep -c NET_ADMIN` (0).

### Why the harness refuses to run more often than it runs

A `tc` command that silently does nothing leaves the link at full loopback
speed, and the run then gets written down as "20 Mbit, 200 ms, 1 % loss". That
is worse than having no number, because it looks like a measurement. So
`netem.shaped()` fails closed on every path: no `tc`, no `ping`, netem not
attached after adding it, or a measured RTT that does not match the profile —
each is a hard error, not a warning. It also tears the qdisc down in a
`finally`, so a benchmark that raises cannot leave the box shaped at 5 Mbit
with every later measurement silently wrong.

The RTT tolerance is ±25 %, not something looser, because the check has to
separate the profiles from *each other* as well as from an unshaped link: at
±50 % a 200 ms measurement is accepted as the 400 ms profile.
`tests/unit/test_netem_shaping.py` asserts every pairing.

### What to measure when a capable box is available

Per profile, single-file upload and backlog drain at `parallel_streams` = 1, 2
and 4 — that matrix is what tests the `BATCH_STRATEGIES` table's central claim
(POOR 4 → EXCELLENT 1), which comes from physics reasoning rather than
measurement. **If independent channels do not help on the poor/lossy profiles,
correct the table rather than defending it.** The validity preflight prevents
a label saying “4” while the server cap is 2; the result still records every
bound so the strategy and file-count caps remain visible.

Netem emulates a link; it is not a robot radio. It does not reproduce
retransmit behaviour, contention, roaming, or bursty loss in a warehouse.
Publish the results as netem numbers.
