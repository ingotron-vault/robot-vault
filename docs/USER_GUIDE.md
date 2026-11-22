# Robot Vault user guide

Use this guide to run Robot Vault from a fresh checkout and check each stage:
upload, Parquet processing, and query. It is for technical operators familiar
with Docker and evaluators checking the system's claims.
Each step gives an expected result. Follow the linked references when you need
the full test matrix, configuration options, or operating procedures.

**Editions.** This guide covers the core edition, which is the default. Pro
adds episodes, QA scores, signed dataset cuts, and LeRobot export (see the Pro
documentation).

- [`docs/TESTING.md`](TESTING.md) — the full test matrix and what each suite proves
- [`docs/CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md) — every environment variable and config knob
- [`docs/EXAMPLES.md`](EXAMPLES.md) — build, seed, inspect, and remove the public examples
- [`docs/OPERATIONS.md`](OPERATIONS.md) — backup, restore, release, upgrade rehearsal
- [`docs/README.md`](README.md) — the documentation ownership map

Allow extra time for the first image build; later runs reuse Docker's cache.

**Where you end up:** a running stack with simulated fleet data in it,
processed into Parquet, and visible in the query UI and API.

---

## 1. Prerequisites and cold start

Install Docker Engine with the Compose v2 plugin, Git, GNU Make, `curl`, and
`jq`. No host Rust, `protoc`, Python environment, or Node installation is
needed; those toolchains run in containers.

```bash
git clone https://github.com/ingotron-vault/robot-vault.git
cd robot-vault

make stack-up         # build everything, migrate, start, wait for health
```

`make stack-up` also runs `make secrets` (a random `INGEST_AUTH_TOKEN` into
`.env`) and `make proto` (regenerates the gRPC stubs).

> **The first build takes a while — plan for it.** The Rust ingest image compiles
> tonic and the AWS SDK from scratch. Subsequent builds reuse those layers. If
> disk space is tight, inspect `docker system df` and remove only unused cache
> you intend to discard.

**What you should see** at the end: `Stack ready`, followed by a `docker compose
ps` table with `postgres`, `objectstore`, `redis`, `ingest`, `processor`, `query`,
`ui`, `robot`, and `gc` up.

```bash
curl -s http://localhost:8003/healthz          # {"status":"ok"}
curl -s http://localhost:8003/api/v1/edition   # {"edition":"core", ...}; "pro" on a Pro stack
```

### The smoke check

These commands are the regression baseline. Run them now so you know the box
is sound before attributing failures to your own data.

```bash
make test-unit     # Python, migrations, and release-policy unit gates
make test-rust     # Rust ingest gate
make test-e2e      # live-stack end-to-end collection
```

The current suite inventory, prerequisites, and
what each gate proves are in [`docs/TESTING.md`](TESTING.md).

---

## 2. Put data in it

For the public example bundle, including two enrolled example robot
identities, four licensed/synthetic recordings, idempotent normal-path upload,
and removal guidance, use [`docs/EXAMPLES.md`](EXAMPLES.md). The seeds below
remain the deterministic engineering fixtures used by this guide's exact
expected-value walkthrough.

There are two seeds. Use both — the first proves the pipe, the second is what the
rest of this guide works with.

**(a) The toy seed** — a 5-message MCAP, seconds to run, proves upload → ingest →
processing → query end to end:

```bash
docker compose exec robot ./development/seed.sh
```

**(b) The simulation seed** — a deterministic ~10 MB AMR recording: 3 episodes of
~15 s separated by >10 s of silence, with CDR-encoded camera / `cmd_vel` / odom /
joint_states / TF / IMU / GPS topics, a `/session/task` label per episode, and
**episode 2 deliberately broken** in five documented ways. This is the fixture
everything in this project is tested against.

```bash
docker compose exec -e SEED_SIM=1 robot ./development/seed.sh
```

Tune with `SEED_SIM_EPISODES`, `SEED_SIM_DURATION`, `SEED_SIM_SEED`,
`SEED_SIM_BAD_EPISODE` (default 2). Generator details:
[`development/sim_gen/README.md`](../development/sim_gen/README.md).

**What you should see** while it runs — the generator announcing exactly what it
made, then the real `rsm scan` / `rsm hash` / `rua upload` path over gRPC:

```
sim_gen 1.0.0: 3 episode(s), 6131 messages, 10102280 bytes, sha256=...
  episode 1: 2177 msgs, task='navigate to dock A'
  episode 2: 1777 msgs, task='deliver tote to station 2' [DEGRADED]
  episode 3: 2177 msgs, task='patrol aisle 3'
```

### Watch it flow

The processor polls every 5 seconds. Give it ~20 s, then look:

```bash
docker compose exec -T postgres psql -U robotvault -d robotvault -c "
  SELECT u.robot_id, u.status, ps.status AS processing, ps.attempts
  FROM uploads u LEFT JOIN processing_status ps ON ps.upload_id = u.id
  ORDER BY u.created_at DESC LIMIT 5;"
```

**What you should see:** `status=completed`, `processing=completed`,
`attempts=1`.

---

## 3. Look at it

### The main UI — `http://localhost:8003/ui`

- **Overview** — deployment posture, independently degrading KPI tiles, an
  attention queue, and fleet drill-downs. An **OBS** utility link appears only while the optional
  Prometheus profile is healthy; `/ui/obs` remains available by direct URL for
  diagnosis.
- **Intake** — every upload, filterable by robot/status/time, with live
  in-flight progress, capture context, per-topic detail, and download actions.
  Automatic S3 discovery is bounded; a storage error is shown as an error,
  not as an empty list. Filter links preserve unrelated URL choices when the
  page resets its offset.
- **Explore** — switch between **Stream** and **SQL** modes. Stream mode chooses
  a robot, topic, and bounded time range for a bucketed chart and CSV export;
  SQL mode is the read-only console. Saved views can be created, renamed,
  replaced, and deleted, and the active mode and filters live in the URL. Use
  <kbd>Ctrl</kbd>+<kbd>Enter</kbd> or <kbd>Cmd</kbd>+<kbd>Enter</kbd> to run SQL.
  Every chart has a keyboard-accessible data table as well as the visual plot.
- **Review** and **Verification** — Pro adds these pages (see the Pro
  documentation). In core they show an "Available in Pro" panel.

The header's **Help** action opens a local operator guide, and an unknown or
stale URL now offers direct recovery links instead of an empty application
shell.

The shared auth client attaches the current OIDC token in release mode. It
fetches a development token automatically only when runtime configuration
explicitly reports development mode. Every SQL statement is validated by
DuckDB's parser before execution. The header's light/dark choice applies to
every route and persists in local storage.

### The robot-side UI — `http://localhost:8090`

A small local dashboard for one robot: pending/uploaded/failed counts, a file
browser with per-file progress, a durable upload history that survives an agent
restart, and "Resume next batch" / "Retry failed" buttons. Start it with a real
data directory. “Resume” starts queued work. The server preserves
confirmed multipart parts and the robot sends only missing parts. An expired
session requires a full-file retry:

```bash
ROBOT_DATA_HOST_DIR=/absolute/path/to/rosbags make robot-ui-start
```

An off-box robot of a release deployment uses a different installation path:
open **Robots** in the Robot Vault UI, add the robot, and run the install
command that the page shows. The command downloads the installer from your
vault (`/robot`), checks its SHA-256, and enrolls the robot with a one-use
code. It installs the versioned frozen agent under `/opt/ingotron-agent`;
Linux/amd64 with glibc 2.31 or newer, systemd, OpenSSL 1.1.1 or newer, `curl`,
`tar`, and a SHA-256 utility are required. After you upgrade the vault, the
same page shows an **Upgrade the agent** command. A decommissioned robot cannot
return: add the hardware again as a new robot. Operator steps are in
[`OPERATIONS.md`](OPERATIONS.md#add-a-robot). Python and grpcio are bundled and need not be installed on the
robot. See the
[frozen-agent guide](../packaging/robot-agent/README.md) for artifact contents,
support boundaries, and release verification.

### Reaching it from another machine

| Port | Service | Bound to |
|---|---|---|
| `8003` | query API + `/ui` | **loopback only** (`QUERY_BIND_HOST` widens it) |
| `8090` | robot upload UI | **loopback only** (`ROBOT_BIND_HOST` widens it) |
| `5173` | SPA dev server | **loopback only** (`UI_BIND_HOST`) |
| `5174` | robot-SPA dev server (`make robot-ui-dev`) | **loopback only** (`ROBOT_UI_BIND_HOST`) |
| `9108` | GC metrics | **loopback only** |
| `50051`/`8080` ingest, `5432` Postgres, `9000` object store | — | **not published at all** |

The data plane is unpublished on purpose: it answers to committed dev
credentials, so binding it would hand the whole lake to the LAN.
`make debug-up` is the explicit loopback-only opt-in; `make expose-ingest`
publishes *only* ingest, which is the right target for a real off-box robot.
Development query can mint an administrator token. A non-loopback
`QUERY_BIND_HOST` is therefore refused unless
`ALLOW_UNSAFE_DEV_QUERY_EXPOSURE=1` explicitly acknowledges that risk. Keep it
behind a trusted firewall, or use the authenticated release topology instead.

On the local deployment PC, open `http://localhost:8090` directly. If you
deliberately deploy on a different host, tunnel its loopback ports rather than
rebinding them:

```bash
# generic
ssh -L 8090:localhost:8090 -L 9108:localhost:9108 user@server
```

```bash
# optional remote host with a custom SSH port and key
ssh -p <SSH_PORT> -i ~/.ssh/<YOUR_KEY> \
    -L 8003:localhost:8003 \
    -L 8090:localhost:8090 \
    root@<HOST>
```

Then open `http://localhost:8090` on the SSH client PC. Tunnel `8003` too when
you need the query UI; it is also loopback-bound by default.

---

## 4. Check the edition

The default edition is `core`. `ROBOT_VAULT_EDITION` is read once at import, so a
change needs a recreated service. A service started with `ROBOT_VAULT_EDITION=pro`
refuses to start when the Pro package is not installed. Pro runs only from the
Pro images.

```bash
curl -s http://localhost:8003/api/v1/edition          # {"edition":"core", ...}
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8003/api/v1/episodes   # 404
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8003/api/v1/datasets   # 404
curl -s http://localhost:8003/openapi.json | jq \
  '[.paths | keys[] | select(startswith("/api/v1/episodes") or startswith("/api/v1/datasets"))]' # []
```

**What you should see:** the episode and dataset routes return **404** and are
absent from `/openapi.json`. A core box does not advertise an API it will not
serve. In `/ui`, Review and Verification show an "Available in Pro" panel.

404 in core means *no such route*. On a Pro stack the same paths answer 401,
which confirms that the routes are mounted and protected.

In core the processor extracts Parquet but does not persist episodes or run the
media and QA stages. Pro adds those stages (see the
[README](../README.md#core-and-pro)).

---

## 5. Troubleshooting — the things that actually bite

**`ModuleNotFoundError: upload_pb2`.** The protobuf stubs are gitignored and
generated. Run `make proto`. (The dev/robot/seed/db-migrate services bind-mount
the repo over `/app`, which shadows the image's own copy — which is why the
Dockerfile also emits them to `/opt/proto-gen` on `PYTHONPATH`.)

**A 401 from an API you expected to work.** The dev token's field is
**`access_token`**, not `token`:
`curl -s "http://localhost:8003/auth/dev/token?roles=admin" | jq -r .access_token`.
`AUTH_MODE=oidc` is set in `configs/query.env`, so the role gates are live.

**An offline query cannot load `httpfs`.** The query image bakes DuckDB's
`httpfs` extension during its build, so a correctly built image does not fetch
it at runtime. If an old/custom image reports an install failure, rebuild the
query image from this tree; do not add egress to an air-gapped deployment.

**Uploads complete but zero rows appear.** The processor only keeps messages
whose MCAP schema name is a known ROS 2 type (`processor/supported_types.py`). A
bag registered as `json` is silently skipped and the upload still completes.

**`permission denied` on every docker command.** On a box where docker-group
membership was granted without a re-login, wrap them: `sg docker -c "make
stack-up"`. On a normal box you never need this.

**A build fails on disk space.** Image builds leave a large build cache. Check
`docker system df`, then remove only unused cache you intend to discard, for
example with `docker builder prune`.

**Start completely over.** `make stack-reset CONFIRM=1` destroys `./data` and
`uploads/` with no undo — back up first if the data matters
([`docs/OPERATIONS.md`](OPERATIONS.md)) — then `make stack-up`.

---

## 6. What to read next

- Something failed and you want to know what the suite was supposed to prove →
  [`docs/TESTING.md`](TESTING.md)
- You want to change a threshold, a gate or a path →
  [`docs/CONFIG_REFERENCE.md`](CONFIG_REFERENCE.md)
- You are deploying this for real → [`docs/OPERATIONS.md`](OPERATIONS.md) and
  `deploy/docker-compose.release.yml`
- You are evaluating it for your organization → [`LICENSE`](../LICENSE),
  [`SECURITY.md`](../SECURITY.md), and the [README](../README.md#core-and-pro)
- You are changing the code → [`docs/README.md`](README.md) for ownership,
  and [`../development/README.md`](../development/README.md) for workflow.
