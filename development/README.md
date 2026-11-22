# Development guide

This guide is for contributors building and testing Robot Vault from source.
The supported workflow runs primarily in containers. Use the root
[`Makefile`](../Makefile) for repeatable commands and the
[testing guide](../docs/TESTING.md) for their scope and prerequisites.

## Repository layout

- `phases/` contains the five product stages and the shared configuration/UI
  packages. Unit tests normally live beside their owning phase.
- `db/` owns immutable ordered migrations and migration-runner tests.
- `tests/e2e/` owns live whole-pipeline contracts.
- `tests/integration/` owns focused tests against real PostgreSQL and the object store that are
  not part of an ordinary unit gate.
- `development/release/` owns release preflight, role/policy bootstrap, and
  upgrade checks.
- `development/browser-e2e/` owns Chromium checks of the query UI and robot
  UI, including screenshots, traces and machine-readable results.
- `development/sim_gen/` owns the deterministic AMR fixture generator.
- `development/bench/` owns benchmark drivers.

## Working conventions

- Prefer a small change that completes one behavior, its closest invariant
  test, and its documentation.
- Keep phase directories ordered with two-digit prefixes. Do not rename them
  without a migration plan for package paths, Compose, CI, and documentation.
- Put unit contracts beside the owner. Put cross-service behavior in `tests/`
  or `development/release/`, not in a convenient phase that cannot exercise it.
- Use deterministic fixtures. External-service tests must fail clearly or use
  an explicit marker/skip; do not silently turn a required check into a no-op.
- Record long-lived design decisions in the owning `technical.md`, including
  alternatives, compatibility impact, and the test that protects the choice.
- Update [`CHANGELOG.md`](../CHANGELOG.md) for shipped behavior. Track open
  work in issues, not in documentation files. Do not add session
  handovers or completed execution plans as current documentation.

## Documentation shape

A phase may use three audience-specific files when each carries real content:

- `technical.md` — design, invariants, failure modes, and implementation detail
- `user.md` or `usage.md` — operator-facing concepts and examples
- `run-and-test.md` — focused commands, expected results, and troubleshooting

Small phases should link to a shared document instead of creating three thin
duplicates. The repository-wide ownership map is
[`docs/README.md`](../docs/README.md). Run `make docs-check` after moving or
renaming documentation.

Generated files must name their generator and must not be hand-edited. This
includes protobuf stubs, OpenAPI snapshots, supported-topic catalogs, SBOMs,
and dependency locks.

## Supported local loop

Use the same Linux PC for source builds, local validation, and source deployment;
the workflow does not depend on a separate remote build host. The
Docker-backed targets use the local Docker daemon, and the development stack's
published operator ports are loopback-only. A release deployment on this host
uses the distinct release Compose topology and its guarded inputs and image
checks; see [`docs/OPERATIONS.md`](../docs/OPERATIONS.md).

```bash
make check
```

This covers every self-contained backend, frontend, documentation, and
release-contract suite. `make ci-local` runs this gate plus the disposable live
GC integration check. Use `make presubmit` before a release candidate to add
the current npm vulnerability audit. For live data-path changes, start the dev
stack with `make stack-up`, run `make test-e2e`, then stop it with
`make stack-down`. Add the relevant live or mutation gate from
[`docs/TESTING.md`](../docs/TESTING.md).

The stack and Docker-backed test commands require Docker Engine, the Docker
Compose plugin, and GNU Make; they do not require application language
toolchains on the host.
The stdlib-only `make docs-check` gate runs on host Python 3.10 or newer.

## Optional host tooling

A local Python environment can improve editor integration, but running gates
outside Docker is not the authoritative release/test workflow:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e phases/common-config \
  -e phases/01-robot-storage \
  -e phases/02-robot-upload-agent \
  -e phases/04-processing-pipeline \
  -e phases/05-query-service
make proto
```

Running the complete backend gate outside Docker also requires PostgreSQL and
the exact paths and environment encoded by `make test-unit`. A bare `pytest`
misses live migration and release-policy contracts. The GitHub Actions
workflow `.github/workflows/core-ci.yml` runs the core checks on GitHub-hosted
runners; run the local gates for complete evidence.

Host Rust testing requires Rust 1.94 or newer, `protoc`, `pkg-config`, and
OpenSSL development headers:

```bash
cd phases/03-ingest-server-rs
cargo test --locked
```

Host npm installs are likewise optional. The authoritative locked tests/builds
are `make ui-test` and `make ui-build`, which use the repository's pinned Node 22
container. `make test-browser` uses a matching pinned Playwright package and
browser image; see its [guide](browser-e2e/README.md).
