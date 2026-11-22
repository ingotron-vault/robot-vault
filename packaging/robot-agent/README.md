# Robot agent and its bootstrap

This folder builds the Ingotron robot agent and `robot.sh`, the script that
installs it on a robot. Every vault serves both from its query image:

| Route (no sign-in) | File in the query image |
|---|---|
| `GET /robot` | `/opt/robot-vault/robot/robot.sh` |
| `GET /robot/agent-linux-amd64.tar.gz` | `/opt/robot-vault/robot/agent-linux-amd64.tar.gz` |

Operators do not download these files by hand. They copy a command from the
**Robots** page of their vault.

## How a robot is installed

The Robots page shows a command like this one:

```bash
f=$(mktemp) && curl -fsSk --proto =https https://vault.example.com/robot -o "$f" \
  && echo "<sha256>  $f" | sha256sum -c - \
  && sudo sh "$f" --vault https://vault.example.com --ingest ingest.example.com:50051 --code ign1.…
```

Each step checks the next one. No signing key and no outside host is needed,
so it also works air-gapped.

```
admin's signed-in HTTPS page ──gives──> SHA-256 of robot.sh
robot.sh (rendered at build time) ──holds──> version, size, SHA-256 of the agent
enrollment code ──holds──> pin of the vault's ingest CA
pinned CA ──protects──> the code and every upload
```

- `curl -k` is safe here: the downloaded script runs only if its SHA-256
  matches the one copied from the page.
- `robot.sh` fetches the CA and the agent through one helper without
  certificate checks. It checks the CA against the pin in the code, and the
  agent against the size and SHA-256 rendered into the script. Neither request
  carries a credential.
- The agent sends the code to `--ingest` over TLS pinned to that CA.

## The three flows of robot.sh

| Flow | Command | Changes | Keeps |
|---|---|---|---|
| Install | `--vault URL --ingest HOST:PORT --code CODE [--root PATH]` | installs the agent, the CA, the credential, the units | – |
| Re-enroll | `--reenroll --vault URL --ingest HOST:PORT --code CODE` | the CA and the credential | the installed agent, recordings, queue |
| Upgrade | `--upgrade --vault URL` | the agent release and its units | the CA, `vault.json`, the credential |

Rules:

- Install order: check the CA pin, fetch and check the agent, install it, then
  enroll. The code is spent only after the agent that can resume the install
  exists. If a later step fails, re-run the same command: it resumes locally
  and does not send the code again.
- Upgrade installs the one agent version the vault serves. The same version
  does nothing (exit 0). A lower version is refused (exit 2). There is no
  `ingotron-agent upgrade` command.
- The script header in `robot.sh` gives the full state machine.

## Runtime contract of the agent archive

The archive has one root, `ingotron-agent-<version>/`, containing:

- `bin/ingotron-agent`, a PyInstaller **onedir** executable with its own CPython
  3.10 runtime, gRPC/protobuf stubs, and storage/upload packages;
- the run and retention systemd wrappers and units;
- configuration templates, `VERSION`, the root `LICENSE` (AGPL-3.0, like the
  core), third-party notices and licence files, an agent CycloneDX dependency
  bill of materials (BOM), the exact dependency lock, and build metadata.

It carries no `.py` source files and never looks for host Python. It is built
in the pinned Linux/amd64 image of Python 3.10.18 on Debian 11 (glibc 2.31).

Robot host requirements: Linux/amd64, glibc 2.31 or newer, systemd, OpenSSL
1.1.1 or newer, `curl`, `tar`, `flock` and a SHA-256 tool. Ubuntu 20.04 and
Debian 11 meet them. Python and grpcio are not needed.

## How it is built

One recipe, two users:

- The query image (`phases/05-query-service/Dockerfile`) has two stages:
  `robot-agent-builder` (the pinned builder) and `robot-agent`. The second
  stage copies only the agent's inputs, runs `freeze_agent.py` with no network,
  packs the archive with `pack_agent.sh`, and renders `robot.sh` with
  `render-robot-bootstrap.sh`. The archive's timestamps come from the build
  argument `AGENT_SOURCE_EPOCH` (default `1700000000`), so the same inputs give
  the same bytes.
- `build-agent-tarball.sh` builds the same `robot-agent-builder` stage and
  calls the same `pack_agent.sh`. Use it for the reproducibility check.

`render-robot-bootstrap.sh` refuses a template without its four `AGENT_*`
marker lines, a tarball whose root is not `ingotron-agent-<version>/`, and any
`@@` marker left after rendering. `robot.sh` itself refuses to run when it was
not rendered.

## Release assets

```bash
packaging/robot-agent/release-assets.sh <query-image> <outdir>
```

It copies `robot.sh` and `agent-linux-amd64.tar.gz` out of the built query
image and writes `SHA256SUMS`, so the assets are the bytes the vaults serve.
Publishing them on the GitHub release is a maintainer step.

## Checks

| Check | Command |
|---|---|
| Shell gates (POSIX, refusals, pin, vault delivery, upgrade) | `packaging/robot-agent/tests/run-all.sh` |
| Same gates as root under dash and busybox ash, no bash | `packaging/robot-agent/tests/run-in-container.sh` (Docker) |
| Each guard is covered by a failing test | `packaging/robot-agent/tests/check_mutation_guards.sh` |
| Two identical builds, then clean Debian 11 and Ubuntu 20.04 probes | `packaging/robot-agent/tests/check_tarball_determinism.sh` (Docker) |
| Python agent tests | `make test-robot-agent-runtime` |

`test_vault_delivery.sh` runs the install command and `robot.sh` against a
local HTTPS server. The full install and upgrade cases write `/opt`, `/etc` and
`/var`, so they run only as root inside the container gate; on a developer host
they are skipped.
