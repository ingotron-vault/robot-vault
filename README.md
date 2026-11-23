# Robot Vault

Robot Vault is a self-hosted, offline-first pipeline for robotics recordings.
It accepts MCAP uploads, verifies their integrity, and extracts queryable
Parquet. You run it on your own hardware.

MCAP is a recording file format for timestamped robot messages. Parquet is a
column-based file format used here for queries. The [visual starting
guide](docs/understand.html) explains the data flow and points operators and
developers to the right guide.

This repository is the open-source **core** of Robot Vault. It is licensed
under the [GNU Affero General Public License, version 3 only](LICENSE)
(AGPL-3.0-only). A paid **Pro** edition adds more capability; see
[Core and Pro](#core-and-pro).

```text
robot MCAP
    -> authenticated ingest
    -> Postgres registry + RustFS object lake
    -> Parquet processing
    -> query API + Orbit UI
```

Pro extends this path with episodes, media, QA, signed dataset cuts, and
export (Pro).

## Core and Pro

| Edition | Included capability | Where it comes from |
| --- | --- | --- |
| `core` (default) | Authenticated upload, integrity checks, Parquet processing, query API, and the Orbit UI. | This repository and the public `robot-vault-core-*` images. |
| `pro` | Everything in core, plus segmentation, media, DEFECT/RISK QA, dataset cuts, provenance signing, LeRobot v3 and MCAP export, and GPU face/plate blurring (Pro). | Private Pro images. You buy it as an upgrade. |

The edition is one setting: `ROBOT_VAULT_EDITION`. Its default is `core`. A
service refuses to start if the setting says `pro` but the Pro package is
missing or was built for another core version. The query API reports the
running edition at `/api/v1/edition`.

Pro is an upgrade, not a different product. You keep your data and your
configuration. Pro adds an upgrade command (see the Pro documentation). In the core UI, Pro pages show an
"Available in Pro" panel. Contact [Ingotron](https://ingotron.com) at hello@ingotron.com to buy Pro.

## Install

Two install paths exist. Choose by purpose.

| Path | Use it for | Command |
| --- | --- | --- |
| Evaluation | Try Robot Vault on one machine. Development credentials, `AUTH_MODE=none`, loopback only. | `make stack-up` |
| Production | Real operator access. Your own OIDC identity provider, TLS, per-service credentials. | `make release-up EDITION=core` |

Follow [Quick start](#quick-start) for evaluation. For production, follow
[`docs/OPERATIONS.md`](docs/OPERATIONS.md). It needs a protected release
environment file, valid TLS and authentication inputs, and versioned images.
Development credentials and `:latest` images do not satisfy the release
checks.

After the install:

- The first administrator is whoever your identity provider gives the `admin`
  value in the role claim
  ([first administrator](docs/OPERATIONS.md#first-administrator)).
- Add each robot on the **Robots** page. The page shows a digest-checked install
  command that your vault serves itself
  ([add a robot](docs/OPERATIONS.md#add-a-robot)).
- Back up with `make backup BACKUP_DIR=…` and restore with
  `make restore BACKUP_DIR=…`
  ([backup and restore](docs/OPERATIONS.md#backup-and-restore)).
- Pro upgrades the same install with its own upgrade command (see the Pro documentation).

## Source for every image

Core images are published as `ghcr.io/ingotron-vault/robot-vault-core-ingest`,
`-processor`, `-query`, `-toolkit`, and `-objectstore`.

- Each release tag in this repository is the exact source of that release's
  image digests, including the build scripts.
- [`release/core-images.lock.json`](release/core-images.lock.json) lists the
  image digests of a release and the source commit they were built from.
  Pull images by digest, not by tag.
- Each image carries the license text and third-party notices of its
  contents. The notices are also in `THIRD_PARTY_NOTICES.md` in the
  repository root.

To rebuild the images, check out the release tag and run
`make build-release-local`.

## Network use and AGPL section 13

The AGPL has a rule for network services. If you modify Robot Vault and let
other people use the modified version over a network, you must offer those
users the source of your modified version. The Orbit UI footer links to the
source of the running build. Keep that link accurate when you change the
source. Running an unmodified release does not add duties beyond the AGPL
itself; it is not legal advice. See [`TRADEMARKS.md`](TRADEMARKS.md) for the
use of the Robot Vault and Ingotron names.

## Requirements

- Docker Engine with the Docker Compose plugin (`docker compose`)
- Git and GNU Make
- `curl` for the health checks below
- `jq` for the guided examples and API walkthroughs
- Python 3.10 or newer for the offline `make docs-check` gate

Application Python, Rust, Node, `protoc`, and their dependencies run in
containers. Host Python runs the stdlib-only `make docs-check` and the source
contract in `make test-observability-config`. The example-bundle generator
(`make examples`) also uses host Python and needs the `requirements-dev.txt`
packages installed. One Linux PC can be the build, test, and deployment host.
Docker builds and gates use that host's Docker daemon.

## Quick start

From the repository root:

```bash
make stack-up
```

The first build compiles the Rust ingest server and can take several minutes.
Later builds reuse Docker layers. `stack-up` also builds the first-party object-store
image (`robot-vault-objectstore`: a pinned RustFS plus patched OpenSSL; see its
[contract](development/objectstore/CONTRACT.md)). It generates a local ingest token,
applies database migrations, creates the bucket and one key per service, builds
the SPAs, starts the development stack, and waits for ingest and query readiness.
Run `make build-all` first before a direct `docker compose up`.

Verify it:

```bash
curl -fsS http://localhost:8003/healthz
curl -fsS http://localhost:8003/api/v1/edition
docker compose ps
```

The edition call returns `core`. Seed the real ingest and processing path with
a small MCAP:

```bash
docker compose --profile seed up seed
```

Then open the management UI at <http://localhost:8003/ui>. The global theme
control applies to every route and persists locally. Pro navigation entries
(Review and Verification) show an "Available in Pro" panel in core.

The [user guide](docs/USER_GUIDE.md) walks through the core edition step by
step.

## Upload recordings from a host directory

The robot container sees `ROBOT_DATA_HOST_DIR` at `/tmp/robot_data`. Arrange
files as `<robot-id>/<date>/*.mcap`, enroll the robot as described in the
[upload-agent usage guide](phases/02-robot-upload-agent/docs/usage.md), then run:

```bash
export ROBOT_DATA_HOST_DIR=/absolute/path/to/rosbags
make robot-ui-start

make robot-upload ROBOT_ID=rover-7 CFG=configs/vault.local.json
# or drain every robot directory
make robot-upload-all CFG=configs/vault.local.json
```

The local robot console is <http://localhost:8090>. Streaming uploads resume
from storage-confirmed parts after a disconnect or restart when both peers
support resume version 1. The agent always requests resume version 1, and
the ingest server must support it. The server verifies the complete recording
before reporting success.

For a real robot, give each robot its own config, auth token, and certificate
whose URI SAN is bound to the registered robot ID. Follow the
[operations guide](docs/OPERATIONS.md) instead of copying committed
development credentials.

## Network boundaries

The development stack exposes only operator-facing endpoints by default:

| Host endpoint | Purpose | Default bind |
| --- | --- | --- |
| `8003` | Query API and built Orbit UI | loopback |
| `8090` | Robot upload console | loopback |
| `5173` | Query-SPA development server | loopback |
| `5174` | Robot-SPA development server (`make robot-ui-dev`) | loopback |
| `9108` | GC metrics | loopback |

Ingest gRPC/admin, PostgreSQL, Redis, and the object store remain on the Compose network.
`make debug-up` temporarily publishes ingest, PostgreSQL, and the object store on loopback;
`make debug-down` removes those mappings. Do not widen the debug overlay on an
untrusted network: the development stack uses development credentials. The
Compose preflight refuses non-loopback publication of the fixed-password
PostgreSQL/object-store services, and ingest refuses the committed fallback token when
`DEBUG_BIND_HOST` or `ROBOT_BIND_HOST` is widened. Use `make expose-ingest` with
a random `INGEST_AUTH_TOKEN` for an off-box robot; the robot UI has no separate
login and should remain loopback-only unless its network is explicitly trusted.
The query/UI development service can mint an administrator token, so widening
either `QUERY_BIND_HOST` or `UI_BIND_HOST` requires the explicit
`ALLOW_UNSAFE_DEV_QUERY_EXPOSURE=1` acknowledgement and a trusted, firewalled
network. Use the release topology for any real off-box operator access.

The release topology uses HTTPS query with your own OIDC provider, TLS ingest
with a per-robot credential (client certificates are optional with the `mtls`
profile), per-service database and object-store credentials, non-root containers, and
private internal services. See the [operations runbook](docs/OPERATIONS.md).

## Common commands

```bash
make help                 # complete supported command list
make stack-up             # build and start the development stack
make stack-down           # stop it without deleting persisted data
make check                # self-contained repository gates
make ci-local             # check plus disposable live GC integration
make ui-test              # typecheck and test all npm workspaces in Docker
make test-unit            # Python/unit/migration gates in Docker
make test-rust            # Rust ingest tests in Docker
make test-robot-agent-runtime  # complete robot-agent Python/packaging suite
make test-e2e             # live end-to-end suite
make test-gc-integration  # isolated live Postgres + object-store GC proof
make test-dev-exposure    # development-port safety contract
make test-release-config  # release security/topology contract
make test-observability-config  # alert rules and collector contract
make docs-check           # documentation structure and local links
```

Test scope, prerequisites, and expensive/manual suites are documented in
[`docs/TESTING.md`](docs/TESTING.md).
For the live data-path suite, run `make stack-up`, `make test-e2e`, then
`make stack-down` on the same host.

## Data safety

PostgreSQL and the object store are the two systems of record. Their development
data is stored in `data/postgres/` and the Docker volume `objectstore_data`.
`make backup BACKUP_DIR=…` and `make restore BACKUP_DIR=…` save and restore both
(see [`docs/OPERATIONS.md`](docs/OPERATIONS.md#backup-and-restore)).

```bash
make stack-reset CONFIRM=1
```

That command permanently deletes both stores and local `uploads/`. Back up
first using [`docs/OPERATIONS.md`](docs/OPERATIONS.md). Running it without
`CONFIRM=1` aborts without deleting anything.

Garbage collection is dry-run by default. Review its planned deletions before
setting `GC_DRY_RUN=false`; see [`configs/gc.yaml`](configs/gc.yaml) and the
[configuration reference](docs/CONFIG_REFERENCE.md).

## Documentation

[`docs/README.md`](docs/README.md) is the documentation index and ownership
map. Start with:

- [User guide](docs/USER_GUIDE.md) — cold start through upload, processing, and query
- [Architecture](docs/ARCHITECTURE.md) — system boundaries and data flow
- [Operations](docs/OPERATIONS.md) — backup, restore, upgrade, and release
- [Configuration reference](docs/CONFIG_REFERENCE.md) — environment variables
  and configuration files
- [Testing guide](docs/TESTING.md) — automated and manual verification
- [Database schema](docs/DB_SCHEMA.md) — migration and table reference
- [Changelog](CHANGELOG.md) — completed work

Each phase keeps implementation-specific documentation beside its code under
[`phases/`](phases/).

## Contributing and security

- Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before you open a pull request.
  Outside contributions are not merged until the contributor license
  agreement ([`CLA.md`](CLA.md)) is in force.
- Report a security problem privately. See [`SECURITY.md`](SECURITY.md). Do not
  open a public issue for it.
- All participants follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

Robot Vault core is licensed under AGPL-3.0-only. The full text is in
[`LICENSE`](LICENSE). Third-party components keep their own licenses; see
`NOTICE` and `THIRD_PARTY_NOTICES.md` in the repository root. The
Robot Vault and Ingotron names and logos are not covered by the AGPL; see
[`TRADEMARKS.md`](TRADEMARKS.md).
