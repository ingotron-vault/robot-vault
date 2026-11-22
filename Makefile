.PHONY: docs-check backend-quality bench-net bench-net-down ui-build ui-test ui-dev robot-ui-dev test-observability-config test-workflows test-dev-exposure test-robot-agent-runtime npm-audit presubmit ci-local test-unit test-rust test-e2e test-gc-integration test-gc-integration-config test-objectstore-conformance test-all check robot-upload stack-up stack-down stack-reset robot-ui-start robot-upload-all build build-all build-dev build-ingest build-processor build-query build-toolkit build-objectstore build-release-local build-and-up stack-rebuild db-migrate proto secrets certs debug-up debug-down expose-ingest lock-python lock-python-dev lock-python-processor lock-python-query lock-python-toolkit examples seed-examples help

# One immutable Node toolchain for repository-owned checks. Package locks pin
# npm dependencies; the image digest pins the runtime used to interpret them.
NODE_CHECK_IMAGE := node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
PLAYWRIGHT_CHECK_IMAGE := mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
PROMETHEUS_IMAGE := prom/prometheus:v2.55.1@sha256:2659f4c2ebb718e7695cb9b25ffa7d6be64db013daba13e05c875451cf51b0d3
ALERTMANAGER_IMAGE := prom/alertmanager:v0.27.0@sha256:e13b6ed5cb929eeaee733479dce55e10eb3bc2e9c4586c705a4e8da41e5eacf5
ACTIONLINT_IMAGE := rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
# Keep dependency resolution on the same immutable multi-platform base indexes
# as the Dockerfiles. Refresh each value together with its matching FROM pin.
PYTHON_312_IMAGE := python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
NOBLE_2404_IMAGE := ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289

# The Compose files of the current stack. COMPOSE_FILE wins when it is set
# (private.mk, a test wrapper, or the caller); otherwise this is
# Compose's own default, including a local docker-compose.override.yml. Targets
# that add an overlay append it to this list instead of passing `-f`, because
# any `-f` makes Compose ignore COMPOSE_FILE and the implicit override file.
# Recursive (=) on purpose: private.mk, included at the end, may set COMPOSE_FILE.
COMPOSE_OVERRIDE_FILE := $(wildcard docker-compose.override.yml)
STACK_COMPOSE_FILES = $(or $(COMPOSE_FILE),docker-compose.yml$(if $(COMPOSE_OVERRIDE_FILE),:$(COMPOSE_OVERRIDE_FILE)))
# $(call compose_with,<overlay.yml>) -> `docker compose` over the stack plus that overlay.
compose_with = COMPOSE_FILE="$(STACK_COMPOSE_FILES):$(1)" docker compose

# Compose invocation that also publishes the data plane (ingest 50051/8080,
# postgres 5432, objectstore 9002) on the host; see deploy/docker-compose.debug.yml.
# The base compose file publishes only the user-facing ports.
DEBUG_COMPOSE = $(call compose_with,deploy/docker-compose.debug.yml)

# Benchmark invocation that grants NET_ADMIN to the `robot` service only, so
# the upload bench can shape its link with `tc netem`. Same
# opt-in-by-naming-the-file pattern as DEBUG_COMPOSE above: the base stack
# never gets the capability.
BENCH_COMPOSE = $(call compose_with,deploy/docker-compose.bench.yml)

# =============================================================================
# Python dependency locking
# =============================================================================
#
# One lock file per IMAGE (not per pyproject.toml): every Python package that
# lands in the same site-packages at build time is resolved TOGETHER, because
# that is what actually has to be conflict-free -- e.g. the dev image
# editable-installs five local packages into one environment. Each is generated with `uv pip compile --generate-hashes`
# inside a container running the SAME Python version (and, for query and the
# processor, the same base image) the shipped Dockerfile uses, so resolution
# cannot silently diverge from what that image actually gets. Nothing here
# changes a version FLOOR -- those still live in requirements-dev.txt and each
# phase's pyproject.toml -- it only pins what those floors resolve to today.
#
# Regenerate ALL locks after touching any pyproject.toml/requirements*.txt
# floor, then rebuild the affected images (`make build-all`) and re-run the
# test suites before committing the new lock files alongside the floor change:
#
#   make lock-python
#
# Or regenerate a single image's lock with the targets below. Runs as root
# inside the container (installing `uv` needs to write to site-packages) and
# chowns the result back to you, same pattern as `certs`.
lock-python: lock-python-dev lock-python-processor lock-python-query lock-python-toolkit
	@echo "All Python dependency locks regenerated -- rebuild affected images to pick them up (make build-all)."

# Lock format for the locks that carry extras (dev and query). pip
# (the Ubuntu 24.04 venv of the query image) in --require-hashes mode needs
# `pkg[extra]==v` for a dependency that asks for the extra and `pkg==v` for one
# that asks for the plain name. uv writes one form only (--no-strip-extras keeps
# the extras form), so this appends the plain form, same hashes, for every
# extras-only pin. It runs on the host after the lock container (stdlib only).
define LOCK_PLAIN_PINS_PY
import re, sys
path = sys.argv[1]
note = ("# Plain pins for the extras lines above. pip (Ubuntu 24.04 venv, query image)\n"
        "# in --require-hashes mode needs `pkg[extra]==v` for a dependency that asks for the\n"
        "# extra (mcp -> pyjwt[crypto]) AND `pkg==v` for one that asks for the plain name\n"
        "# (mcp -> uvicorn). uv writes one form only; these lines add the other, same hashes.\n")
text = open(path).read().split(note.split("\n")[0])[0].rstrip("\n") + "\n"
plain = {n.lower() for n in re.findall(r"^([A-Za-z0-9_.-]+)==", text, re.M)}
pins = re.findall(r"^([A-Za-z0-9_.-]+)\[[^]]*\]==(\S+) \\\n((?:    --hash=\S+(?: \\)?\n)+)", text, re.M)
tail = "".join(f"{n}=={v} \\\n{h}" for n, v, h in pins if n.lower() not in plain)
open(path, "w").write(text + (note + tail if tail else ""))
endef
export LOCK_PLAIN_PINS_PY

# dev image (root Dockerfile, PYTHON_312_IMAGE). Covers requirements-dev.txt
# (dev tooling + development/sim_gen's exact pins) PLUS every phase pyproject
# this image editable-installs -- common-config, 01, 02, 04, 05 -- since
# `pip install -e phaseA -e phaseB ...` puts all of their dependencies in the
# one environment the dev/db-migrate/robot/seed/gc compose services share.
lock-python-dev:
	@echo "Locking dev image dependencies ($(PYTHON_312_IMAGE))..."
	docker run --rm -v "$(PWD)":/app -w /app $(PYTHON_312_IMAGE) sh -c "\
	  pip install --quiet --no-cache-dir uv && \
	  uv pip compile --generate-hashes --no-strip-extras --python-version 3.12 \
	    requirements-dev.txt \
	    phases/common-config/pyproject.toml \
	    phases/01-robot-storage/pyproject.toml \
	    phases/02-robot-upload-agent/pyproject.toml \
	    phases/04-processing-pipeline/pyproject.toml \
	    phases/05-query-service/pyproject.toml \
	    -o requirements-dev.lock.txt && \
	  chown $$(id -u):$$(id -g) requirements-dev.lock.txt"
	python3 -c "$$LOCK_PLAIN_PINS_PY" requirements-dev.lock.txt
	@echo "  -> requirements-dev.lock.txt"

# core toolkit image (deploy/release-images/toolkit.Dockerfile): only what its jobs
# import (no sim_gen deps). -c keeps every version equal to the dev lock.
lock-python-toolkit:
	@echo "Locking core toolkit dependencies ($(PYTHON_312_IMAGE))..."
	docker run --rm -v "$(PWD)":/app -w /app $(PYTHON_312_IMAGE) sh -c "\
	  pip install --quiet --no-cache-dir uv && \
	  uv pip compile --generate-hashes --no-strip-extras --python-version 3.12 \
	    -c requirements-dev.lock.txt phases/02-robot-upload-agent/toolkit-requirements.txt \
	    -o phases/02-robot-upload-agent/toolkit.lock.txt && \
	  chown $$(id -u):$$(id -g) phases/02-robot-upload-agent/toolkit.lock.txt"
	python3 -c "$$LOCK_PLAIN_PINS_PY" phases/02-robot-upload-agent/toolkit.lock.txt
	@echo "  -> phases/02-robot-upload-agent/toolkit.lock.txt"

# processor image (phases/04-processing-pipeline/Dockerfile, NOBLE_2404_IMAGE
# runtime stage -- distro Python 3.12, matched here exactly rather than
# approximated with python:3.12-slim).
lock-python-processor:
	@echo "Locking processor image dependencies ($(NOBLE_2404_IMAGE), distro Python 3.12)..."
	docker run --rm -v "$(PWD)":/app -w /app $(NOBLE_2404_IMAGE) bash -c "\
	  apt-get update -y >/dev/null && \
	  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv >/dev/null && \
	  python3 -m venv /tmp/rv-lock && \
	  /tmp/rv-lock/bin/pip install --quiet --no-cache-dir uv && \
	  /tmp/rv-lock/bin/uv pip compile --generate-hashes --python-version 3.12 \
	    phases/04-processing-pipeline/pyproject.toml \
	    -o phases/04-processing-pipeline/requirements.lock.txt && \
	  chown $$(id -u):$$(id -g) phases/04-processing-pipeline/requirements.lock.txt"
	@echo "  -> phases/04-processing-pipeline/requirements.lock.txt"

# query image (phases/05-query-service/Dockerfile, distro-managed Python 3.12
# on NOBLE_2404_IMAGE). common-config has no third-party dependencies of its own, so
# nothing else needs to be folded in here.
lock-python-query:
	@echo "Locking query-service image dependencies ($(NOBLE_2404_IMAGE), distro Python 3.12)..."
	docker run --rm -v "$(PWD)":/app -w /app $(NOBLE_2404_IMAGE) bash -c "\
	  apt-get update -y >/dev/null && \
	  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv >/dev/null && \
	  python3 -m venv /tmp/rv-lock && \
	  /tmp/rv-lock/bin/pip install --quiet --no-cache-dir uv && \
	  /tmp/rv-lock/bin/uv pip compile --generate-hashes --no-strip-extras --python-version 3.12 \
	    phases/05-query-service/pyproject.toml \
	    -o phases/05-query-service/requirements.lock.txt && \
	  chown $$(id -u):$$(id -g) phases/05-query-service/requirements.lock.txt"
	python3 -c "$$LOCK_PLAIN_PINS_PY" phases/05-query-service/requirements.lock.txt
	@echo "  -> phases/05-query-service/requirements.lock.txt"

# Regenerate the gRPC/protobuf stubs (upload_pb2.py, upload_pb2_grpc.py) into
# phases/02-robot-upload-agent/src. These are generated, gitignored files: the
# root Dockerfile regenerates them too, but
# the dev/robot/db-migrate/seed compose services bind-mount the repo over
# /app (see docker-compose.yml), which shadows whatever the image baked in at
# build time -- so anything that runs those services against this host tree
# needs the stubs regenerated here first.
# Runs as the invoking user so the generated files land owned by you rather
# than root (the container would otherwise write them as root into your tree).
proto:
	@echo "Generating gRPC/protobuf stubs..."
	docker compose run --rm --no-deps --user "$$(id -u):$$(id -g)" dev python -m grpc_tools.protoc -I proto \
	  --python_out=phases/02-robot-upload-agent/src \
	  --grpc_python_out=phases/02-robot-upload-agent/src \
	  proto/upload.proto proto/enrollment.proto

test-unit: proto
	@echo "Running unit and isolated migration tests in Docker..."
	docker compose --profile test up -d --wait test-postgres
	@status=0; trap 'docker compose --profile test rm -sf test-postgres >/dev/null 2>&1 || true' EXIT HUP INT TERM; \
	  docker compose run --rm --no-deps \
	    -e MIGRATION_TEST_DATABASE_URL=postgres://robotvault:robotvault@test-postgres:5432/robotvault \
	    dev bash -c "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest db/test_migrate_unit.py db/migrate_test.py development/release/test_postgres_role_bootstrap.py development/release/test_objectstore_access.py development/example_data/tests phases/common-config/tests/unit phases/01-robot-storage/tests/unit phases/02-robot-upload-agent/tests/unit phases/04-processing-pipeline/tests/unit phases/05-query-service/tests/unit -q" \
	  || status=$$?; exit $$status

test-e2e: proto
	@echo "Running end-to-end tests in Docker..."
	docker compose run --rm -e REQUIRE_LIVE_STACK=1 dev bash -c "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/e2e -q"

# Host-side E2E suites that start their own containers. They need Docker and a
# host Python with the requirements-dev.txt packages; set E2E_PYTHON to a
# virtualenv's python if the system one lacks them. See docs/TESTING.md.
E2E_PYTHON ?= python3
.PHONY: test-robot-enrollment

# Robot enrollment on disposable token-profile containers (the core default).
# Builds the core images from the core compose file only (a Pro overlay would
# replace query and processor). Report: tmp/robot-enrollment-e2e/latest.json
test-robot-enrollment:
	COMPOSE_FILE=docker-compose.yml GIT_SHA=$$(git rev-parse --short HEAD) docker compose build dev ingest query processor objectstore
	ROBOT_ENROLLMENT_E2E=1 $(E2E_PYTHON) -m pytest tests/e2e/test_robot_enrollment_e2e.py -v -s -p no:cacheprovider

.PHONY: test-upload-jobs test-browser

# Real HTTP and gRPC with a held remote response; private robot-data tmpfs.
# tmp/ is made by the caller first, so the root container only adds subfolders
# and a later run can still write there.
test-upload-jobs: proto
	@mkdir -p tmp
	$(call compose_with,development/docker-compose.upload-jobs-test.yml) run --rm --no-deps dev python development/test_upload_jobs_e2e.py

# Live reachability and deletion-safety checks for the object-store collector.
# The target owns its disposable dependencies so local runs and CI exercise the
# same path. It never points at the normal development Postgres or object-store data.
test-gc-integration:
	@bash development/release/test_gc_integration.sh

test-gc-integration-config:
	@bash development/release/test_gc_integration.sh --check

# Object-store conformance gate. Starts the pinned RustFS in its own Compose project,
# bootstraps the five service keys, runs every probe and the crash test, and exits nonzero on any
# failed or missing probe. Not part of `check` (it takes minutes); run it before a store upgrade.
# Artifacts: development/objectstore-conformance/results/$(CONFORMANCE_LABEL)/ (gate.json is the verdict).
CONFORMANCE_LABEL ?= local
test-objectstore-conformance:
	@sh development/objectstore-conformance/run.sh $(CONFORMANCE_LABEL)

# The pytest half runs in the dev image: host Python has only the stdlib.
test-dev-exposure:
	@bash development/test_dev_exposure.sh
	docker compose run --rm --no-deps dev bash -c "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest development/tests -q"

# Full robot-agent Python runtime/packaging suite. `proto` generates stubs as
# the caller; the dev image supplies the hash-locked Python closure. The
# wrapper preflights mandatory imports and fails if any pytest test is skipped.
test-robot-agent-runtime:
	@test "$$(id -u)" -ne 0 || { echo "test-robot-agent-runtime must run as a non-root user" >&2; exit 1; }
	@set -e; robot_data=$$(mktemp -d); trap 'rm -rf "$$robot_data"' EXIT; export ROBOT_DATA_HOST_DIR="$$robot_data"; $(MAKE) proto; docker compose run --rm --no-deps --user "$$(id -u):$$(id -g)" dev bash -ec 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python development/run_robot_agent_runtime.py'

# Rust ingest-server tests, in Docker -- no host cargo/protoc required.
test-rust:
	@echo "Running Rust ingest tests in Docker..."
	docker compose --profile test run --rm --no-deps rust-test

# Narrow product-source typing plus deterministic Rust formatting/lints. The
# same dev and pinned Rust builder images used elsewhere keep host toolchains
# out of the contract.
backend-quality: proto
	@echo "Running backend static-quality gates in Docker..."
	docker compose run --rm --no-deps dev bash phases/check-backend-quality.sh python
	docker compose --profile test build rust-test
	docker compose --profile test run --rm --no-deps rust-test sh -c "cargo fmt --all -- --check && cargo clippy --locked --all-targets --all-features -- -D warnings"

test-all: test-unit test-rust test-e2e

# The default self-contained local/release confidence gate. Keep the dependency
# list explicit, and keep it equal to the jobs of .github/workflows/core-ci.yml
# (development/test_public_workflow.py checks that every CI target is listed
# here): `test-unit` includes the complete Python unit collection, while
# `test-rust` covers ingest. The remaining targets cover docs, both application
# UIs, release configuration and the observability contracts. Live E2E,
# registry, mutation, vulnerability database and other opt-in checks stay
# separate because they need an already running stack, external state, or
# heavyweight runtimes; see their individual targets below.
check: docs-check backend-quality test-unit test-upload-jobs test-rust test-robot-agent-runtime ui-test ui-build test-browser test-observability-config test-workflows test-dev-exposure test-gc-integration-config test-release-config

# Complete local CI path, the same checks as the hosted CI. The GC
# integration target owns disposable Postgres/object-store state and cleans it up.
ci-local: check test-gc-integration

# The operator pre-submit adds the current registry-backed dependency audit
# (Python, Cargo and npm lock files, with reviewed exceptions). It is separate
# from `check` so a network or registry outage cannot be mistaken for a product
# regression. A release candidate must pass this target. `npm-audit` stays as a
# raw npm view; it also fails on High findings that have no fix yet.
presubmit: check dep-audit

# Build the small public example-data bundle. The TUM ROS bag is ~1.4 GB, so
# point EXAMPLES_SOURCE_DIR at a disk with enough free space.
# Existing sources are reused and verified; the pinned ALOHA shard is
# digest-checked. `python -m example_data --help` exposes explicit source-path
# and no-network controls for air-gapped regeneration.
EXAMPLES_SOURCE_DIR ?= .cache/example-data
EXAMPLES_OUT_DIR ?= examples
examples:
	PYTHONPATH=development python3 -m example_data \
	  --source-dir "$(EXAMPLES_SOURCE_DIR)" \
	  --out "$(EXAMPLES_OUT_DIR)" \
	  --synthetic-face "$(EXAMPLES_OUT_DIR)/assets/synthetic-person.png"

# Normal ingest only: authenticated robot enrollment, rsm scan/hash, rua
# upload. The default runner works from the dev Compose network. A release
# operator can set SEED_EXAMPLES_RUNNER=python3 and pass two mTLS configs in
# SEED_EXAMPLES_ARGS.
SEED_EXAMPLES_RUNNER ?= docker compose run --rm --no-deps dev python3
SEED_EXAMPLES_ARGS ?= --bundle examples --state-dir .example-seed --config-template configs/vault.local.json --admin-url http://ingest:8080/admin/robots
seed-examples:
	$(SEED_EXAMPLES_RUNNER) tools/seed_examples.py $(SEED_EXAMPLES_ARGS)

# Build all Docker images required for the stack
BUILD_REVISION := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)

build-all:
	@echo "Building all Robot Vault Docker images..."
	GIT_SHA=$(BUILD_REVISION) docker compose build dev ingest processor query
	@echo "All images built successfully!"

# Build individual components
build-dev:
	@echo "Building dev/robot base image..."
	GIT_SHA=$(BUILD_REVISION) docker compose build dev

build-ingest:
	@echo "Building ingest server (Rust/Tonic)..."
	GIT_SHA=$(BUILD_REVISION) docker compose build ingest

build-processor:
	@echo "Building processor (MCAP → Parquet)..."
	GIT_SHA=$(BUILD_REVISION) docker compose build processor

build-query:
	@echo "Building query service (FastAPI + DuckDB)..."
	GIT_SHA=$(BUILD_REVISION) docker compose build query

# Release jobs use the allowlisted toolkit image. They must not reuse the
# developer image, whose contents and toolset serve a different purpose.
build-toolkit:
	@echo "Building release toolkit..."
	docker build -f deploy/release-images/toolkit.Dockerfile \
	  --build-arg GIT_SHA=$(BUILD_REVISION) \
	  -t robot-vault-toolkit:latest .

# The first-party object store (pinned RustFS plus patched OpenSSL; development/objectstore/CONTRACT.md section 1).
build-objectstore:
	@echo "Building the release object store..."
	docker build -f deploy/release-images/objectstore.Dockerfile \
	  --build-arg GIT_SHA=$(BUILD_REVISION) --build-arg RELEASE_VERSION=$(VERSION) \
	  -t robot-vault-objectstore:latest .

# The core compose file only: a Pro overlay redefines processor and query.
build-release-local: build-toolkit build-objectstore
	@echo "Building the core release images from docker-compose.yml..."
	COMPOSE_FILE=docker-compose.yml GIT_SHA=$(BUILD_REVISION) docker compose build ingest processor query
	@echo "Local release images built successfully!"

# Alias for build-all
build: build-all

# Build and immediately start the stack
# (stack-up itself now depends on build-all, so this is just an alias)
build-and-up: stack-up

# Rebuild all images (no cache) and restart stack.
# NOTE: this calls stack-reset, which wipes ./data (Postgres, uploads) and the object-store volume and
# ./uploads — so it also requires CONFIRM=1:
#   make stack-rebuild CONFIRM=1
stack-rebuild:
	@echo "Rebuilding all images without cache..."
	GIT_SHA=$(BUILD_REVISION) docker compose build --no-cache dev ingest processor query
	$(MAKE) stack-reset
	$(MAKE) stack-up

# Apply pending schema migrations (db/migrations/NNN_*.sql) to the database
# named by DATABASE_URL (defaults to the compose postgres service).
db-migrate:
	@echo "Applying database migrations..."
	docker compose up -d postgres
	@echo "Waiting for postgres..."; \
	OK=0; for i in $$(seq 1 30); do \
	  docker compose exec -T postgres pg_isready -U robotvault -d robotvault >/dev/null 2>&1 && OK=1 && break; \
	  sleep 1; \
	done; \
	[ $$OK -eq 1 ] || (echo "postgres not ready" && exit 1)
	docker compose run --rm -e DATABASE_URL=$${DATABASE_URL:-postgres://robotvault:robotvault@postgres:5432/robotvault} dev python db/migrate.py

# Give this machine its own ingest auth token instead of leaving it
# on the committed dev fallback in docker-compose.yml. Idempotent, and never
# overwrites an existing value (yours, or one already in .env). .env is
# git-ignored and read automatically by every `docker compose` invocation, so
# both the server and the rua clients pick the same token up.
secrets:
	@if [ -n "$$INGEST_AUTH_TOKEN" ]; then \
	  echo "INGEST_AUTH_TOKEN is set in your environment; not touching .env"; \
	elif grep -qs '^INGEST_AUTH_TOKEN=' .env; then \
	  echo "INGEST_AUTH_TOKEN already present in .env"; \
	else \
	  TOKEN=$$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'); \
	  [ -n "$$TOKEN" ] || (echo "could not generate a token from /dev/urandom" && exit 1); \
	  printf '\n# Shared ingest auth token, generated by `make secrets`. Keep out of git.\nINGEST_AUTH_TOKEN=%s\n' "$$TOKEN" >> .env; \
	  echo "Wrote a random INGEST_AUTH_TOKEN to .env (recreate ingest/robot to pick it up)"; \
	fi

# Generate the dev CA + ingest server cert + robot client cert into ./certs,
# in Docker (no host openssl needed). Required before TLS_ENABLED=true /
# TLS_MTLS=true — see docs/CONFIG_REFERENCE.md (ingest TLS/mTLS) and the
# phase 03 "TLS variant" walkthrough. Refuses to clobber existing
# certs. Runs as root inside the container and chowns the result back: ./certs
# is a bind-mount source, so Docker will already have created it root-owned on
# the first `docker compose up`, and an unprivileged container user could not
# write into it.
certs:
	@if [ -f certs/ca.crt ]; then \
	  echo "certs/ already populated — delete ./certs to regenerate"; \
	else \
	  echo "Generating dev certs into ./certs ..."; \
	  docker compose run --rm --no-deps dev bash -c "bash development/generate-dev-certs.sh && chown -R $$(id -u):$$(id -g) certs"; \
	fi

# Publish the data plane on the host (loopback only by default) for debugging.
# Recreates just those three services with the extra port mappings; everything
# else keeps running untouched.
debug-up:
	$(DEBUG_COMPOSE) up -d ingest postgres objectstore
	@echo "Data plane published on 127.0.0.1: ingest 50051/8080, postgres 5432, objectstore 9002"
	@echo "Run 'make debug-down' to unpublish. The credential guard refuses non-loopback storage bindings."

# Publish ONLY ingest (gRPC 50051 + admin 8080), leaving Postgres and the object store
# unreachable. This is the target for letting a real off-box robot upload:
# `DEBUG_BIND_HOST=0.0.0.0 make expose-ingest`. Do NOT use `debug-up` for that
# -- it would also bind Postgres (robotvault:robotvault) and the object store to
# the LAN, which the base compose file avoids on purpose.
expose-ingest:
	$(DEBUG_COMPOSE) up -d --no-deps ingest
	@echo "ingest published on $${DEBUG_BIND_HOST:-127.0.0.1}: 50051 (gRPC), 8080 (admin)"
	@echo "Postgres and the object store remain untouched. 'make debug-down' unpublishes ingest again."

# Undo debug-up: recreate the same services from the base compose file, which
# has no host port mappings for them.
debug-down:
	docker compose up -d ingest postgres objectstore
	@echo "Data plane unpublished (compose-network access only)"

# Build all three npm workspaces (phases/ui-kit, the query-service SPA, and the
# robot upload-agent SPA) in Docker -- no host Node required. `-v /repo/node_modules`
# is an anonymous volume that masks the host's node_modules so a container install
# cannot clobber it.
ui-build:
	docker run --rm -v "$(PWD)":/repo -v /repo/node_modules -w /repo $(NODE_CHECK_IMAGE) sh -c "npm ci && npm run build"

# Typecheck + unit-test every npm workspace, same container shape as ui-build.
#
# `--if-present` keeps the workspace runner compatible with packages that do
# not own every script. ui-kit also has a focused component suite, while both
# SPAs exercise it in their consuming contexts. Running typecheck first is
# deliberate: a type error and a failing assertion read very differently, and
# seeing the type error first saves chasing the assertion.
ui-test:
	docker run --rm -v "$(PWD)":/repo -v /repo/node_modules -w /repo $(NODE_CHECK_IMAGE) sh -c "npm ci && npm run typecheck --workspaces --if-present && npm run test --workspaces --if-present"

# Match the locked Playwright version to its browser/runtime image. API fixtures
# control remote responses; both applications and controls run in Chromium.
# BROWSER_APPS is set here, not inherited, so the core gate always covers
# both core apps; the runner fails unless every selected app ran checks.
test-browser:
	@mkdir -p tmp
	docker run --rm --init --shm-size=1g -e BROWSER_APPS=query,robot \
	  -v "$(PWD)":/repo -v /repo/node_modules \
	  -v /repo/development/browser-e2e/node_modules \
	  -v /repo/phases/02-robot-upload-agent/src/ui-spa/node_modules \
	  -v /repo/phases/05-query-service/src/ui-spa/node_modules \
	  -w /repo $(PLAYWRIGHT_CHECK_IMAGE) sh -ec \
	  'npm ci && npm ci --prefix development/browser-e2e && npm test --prefix development/browser-e2e'

test-observability-config:
	docker run --rm --entrypoint /bin/promtool -v "$(PWD)/configs/prometheus/release-alerts.yml":/rules.yml:ro $(PROMETHEUS_IMAGE) check rules /rules.yml
	# The release Prometheus loads rules and scrape files by glob; core has no scrape file.
	docker run --rm --entrypoint /bin/sh -v "$(PWD)/configs/prometheus/release-alerts.yml":/etc/prometheus/rules/release-alerts.yml:ro \
	  $(PROMETHEUS_IMAGE) -ec 'printf "%s\n" "rule_files: [/etc/prometheus/rules/*.yml]" "scrape_config_files: [/etc/prometheus/scrape.d/*.yml]" > /tmp/p.yml && promtool check config /tmp/p.yml'
	docker run --rm --entrypoint /bin/promtool -v "$(PWD)/configs/prometheus":/rules:ro -w /rules $(PROMETHEUS_IMAGE) test rules operational-alerts.test.yml
	docker run --rm --entrypoint /bin/amtool -v "$(PWD)/configs/prometheus/alertmanager.yml":/alertmanager.yml:ro $(ALERTMANAGER_IMAGE) check-config /alertmanager.yml
	python3 development/release/test_observability_contract.py
	@test "$$(id -u)" -ne 0 || { echo "test-observability-config runtime tests must run as a non-root user" >&2; exit 1; }
	@set -e; robot_data=$$(mktemp -d); trap 'rm -rf "$$robot_data"' EXIT; ROBOT_DATA_HOST_DIR="$$robot_data" docker compose run --rm --no-deps --user "$$(id -u):$$(id -g)" dev bash -ec 'PYTHONPATH=/app:/app/development:/opt/proto-gen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest development/release/test_operational_metrics.py -q'

# Semantic GitHub workflow validation. Shell snippets have their own repository
# gates, so actionlint's shellcheck adapter is disabled and this gate checks
# workflow syntax, expressions, and job dependencies only.
#
# test_public_workflow.py then checks the public core workflow's own rules
# (triggers, permissions, pinned actions, no secrets) and that each of its jobs
# calls only targets listed in `check:` below. It needs host PyYAML.
test-workflows:
	docker run --rm --network none -v "$(PWD)":/repo:ro -w /repo $(ACTIONLINT_IMAGE) -no-color -shellcheck=
	python3 development/test_public_workflow.py

# Registry-backed advisory data is current by design, so this gate is part of
# `presubmit` rather than the hermetic `check` target.
# private.mk (optional local extensions) may append npm packages to NPM_AUDIT_DIRS.
NPM_AUDIT_DIRS := .
npm-audit:
	docker run --rm -v "$(PWD)":/repo -w /repo $(NODE_CHECK_IMAGE) sh -ec 'for package_dir in $(NPM_AUDIT_DIRS); do echo "Auditing $$package_dir"; npm audit --prefix "$$package_dir" --audit-level=high; done'

# Source dependency vulnerability gate: every tracked Python lock file and
# Cargo.lock with the pinned Grype 0.116.1 image and policy, and
# every npm lock file with `npm audit`. A fixable High or Critical finding fails
# unless development/dep-audit/ignore.toml has a current reviewed exception.
# The report goes to tmp/dep-audit/latest/. Like npm-audit, it needs current
# advisory data, so it is in `presubmit`, not `check`.
dep-audit:
	NODE_CHECK_IMAGE=$(NODE_CHECK_IMAGE) PYTHON_CHECK_IMAGE=$(PYTHON_312_IMAGE) development/dep-audit/run.sh

# Query-service SPA dev server via compose (port 5173, loopback-bound).
ui-dev:
	docker compose up ui

# Robot upload-agent SPA dev server, in Docker for parity with ui-build.
# Published on loopback by default like every other dev port (the compose `ui`
# service, robot 8090, gc 9108); set ROBOT_UI_BIND_HOST=0.0.0.0 deliberately to
# expose it on a trusted network. `--host 0.0.0.0` is container-internal — it
# only lets Vite accept the published connection.
robot-ui-dev:
	docker run --rm -v "$(PWD)":/repo -v /repo/node_modules -w /repo -p "$${ROBOT_UI_BIND_HOST:-127.0.0.1}":5174:5174 node:20-slim sh -c "npm ci && npm run dev -w robot-vault-robot-ui -- --host 0.0.0.0"

# Shaped-link upload benchmark. Recreates ONLY the robot service
# with NET_ADMIN, applies a netem profile inside it, and refuses to run if the
# shaping is not measurably in effect -- an unshaped run labelled as a shaped
# one is worse than no result.
#
#   make bench-net PROFILE=poor-wifi        # good-wifi | poor-wifi | bad-lossy
#
# Stop the processor first (`docker compose stop processor`): its 5s poll
# downloads benchmark objects from the object store mid-run and contaminates later trials.
bench-net: PROFILE ?= poor-wifi
bench-net:
	@echo "Recreating 'ingest' with the benchmark stream cap and 'robot' with NET_ADMIN..."
	$(BENCH_COMPOSE) up -d ingest robot
	$(BENCH_COMPOSE) exec -T robot python3 \
	  phases/02-robot-upload-agent/bench/netem.py \
	  --profile $(PROFILE) --interface eth0 --verify-host ingest
	@echo ""
	@echo "Shaping verified for profile '$(PROFILE)'. The qdisc is torn down when"
	@echo "netem.py exits, so drive the upload bench INSIDE a shaped() block --"
	@echo "see phases/02-robot-upload-agent/bench/README.md."
	@echo "Run 'make bench-net-down' to drop the NET_ADMIN grant again."

bench-net-down:
	docker compose up -d ingest robot
	@echo "ingest/robot restored to base config (default cap 2; no NET_ADMIN)."

# Show help with all available commands
help::
	@echo "Robot Vault Make Commands"
	@echo "======================"
	@echo ""
	@echo "  test-browser     - Chromium time-zone, robot-switch and Pro-panel regressions with artifacts"
	@echo "  test-upload-jobs - Real HTTP/gRPC overlapping upload-job outcomes with artifacts"
	@echo "🏗️  BUILD COMMANDS:"
	@echo "  build, build-all     - Build all Docker images"
	@echo "  build-dev           - Build dev/robot base image"
	@echo "  build-ingest        - Build ingest server (Rust/Tonic)"
	@echo "  build-processor     - Build processor (MCAP → Parquet)"
	@echo "  build-query         - Build query service (FastAPI + DuckDB)"
	@echo "  build-toolkit       - Build the allowlisted release jobs image"
	@echo "  build-objectstore   - Build the release object store image (RustFS, patched OpenSSL)"
	@echo "  build-release-local - Build the five local release images"
	@echo ""
	@echo "🚀 STACK COMMANDS:"
	@echo "  stack-up            - Start full infrastructure stack"
	@echo "  stack-down          - Stop all containers"
	@echo "  stack-reset         - Complete reset, DESTROYS ./data + uploads/ (requires CONFIRM=1; see docs/OPERATIONS.md to back up first)"
	@echo "  backup              - Back up Postgres and the bucket to BACKUP_DIR (RELEASE_ENV_FILE=... for the release stack)"
	@echo "  restore             - Check BACKUP_DIR, then restore it; the stack stays stopped afterwards"
	@echo "  stack-rebuild       - Rebuild images (no cache) and restart (requires CONFIRM=1 — runs stack-reset first)"
	@echo "  build-and-up        - Build all images and start stack"
	@echo ""
	@echo "🔐 SECURITY COMMANDS:"
	@echo "  secrets             - Write a random INGEST_AUTH_TOKEN into .env (run by stack-up)"
	@echo "  certs               - Generate the dev CA/server/client certs into ./certs (for TLS/mTLS)"
	@echo "  debug-up            - Publish the data plane (ingest/postgres/objectstore) on 127.0.0.1"
	@echo "  debug-down          - Unpublish it again (compose-network access only)"
	@echo "  expose-ingest       - Publish only ingest gRPC/admin for an off-box robot (DEBUG_BIND_HOST)"
	@echo ""
	@echo "🗄️  DATABASE COMMANDS:"
	@echo "  db-migrate          - Apply pending schema migrations (db/migrations/)"
	@echo ""
	@echo "🤖 ROBOT COMMANDS:"
	@echo "  robot-ui-start      - Start robot UI backend"
	@echo "  robot-upload        - Upload robot data (interrupted files retry from byte zero)"
	@echo "  robot-upload-all    - Upload all robots"
	@echo "  examples            - Build the public example-data bundle into examples/"
	@echo "  seed-examples       - Upload the example bundle through normal robot ingest"
	@echo ""
	@echo "🧪 TESTING (Docker-based):"
	@echo "  backend-quality    - Run Python typing and Rust fmt/clippy gates"
	@echo "  test-unit          - Run Python unit tests in Docker"
	@echo "  test-rust          - Run Rust ingest tests in Docker"
	@echo "  test-e2e           - Run end-to-end tests in Docker"
	@echo "  bench-net PROFILE= - Shaped-link upload benchmark (NET_ADMIN, overlay only)"
	@echo "  bench-net-down     - Restore ingest/robot to the base config (drops NET_ADMIN)"
	@echo "  test-all           - Run all tests (unit + rust + e2e)"
	@echo "  check              - Run every self-contained repository confidence gate"
	@echo "  presubmit          - Run check plus the current npm vulnerability audit"
	@echo "  ci-local           - Run check plus the disposable GC integration test"
	@echo "  npm-audit          - Audit every npm package for high-severity advisories (network)"
	@echo "  test-robot-agent-runtime - Full robot-agent runtime and packaging suite"
	@echo "  test-dev-exposure  - Guard dev Compose credentials, bind hosts, start order, and image copies"
	@echo "  test-gc-integration - Live GC test with disposable Postgres and object store"
	@echo "  test-objectstore-conformance - Object-store gate: RustFS, bootstrap, every probe, crash test (nonzero on any miss)"
	@echo "  test-gc-integration-config - Validate the GC integration Compose config only"
	@echo "  test-observability-config - Validate release Prometheus rules and Alertmanager routing"
	@echo "  test-workflows      - Semantically validate GitHub workflow YAML and expressions"
	@echo ""
	@echo "📄 DOCS:"
	@echo "  docs-check         - Check Markdown structure, local links, anchors, and fences"
	@echo ""
	@echo "📦 RELEASE (see docs/OPERATIONS.md 'Release procedure'):"
	@echo "  release-tag        - Tag the first-party images with \$$(VERSION) (from the VERSION file)"
	@echo "  core-images-pull   - Pull the core images of release/core-images.lock.json by digest and tag them"
	@echo "  release-config     - Validate release inputs (EDITION=core by default, requires RELEASE_ENV_FILE)"
	@echo "  test-release-config - Prove release HTTPS/mTLS/OIDC/edition contracts"
	@echo "  release-env-file   - Check RELEASE_ENV_FILE metadata without reading its secrets"
	@echo "  release-preflight  - Validate release inputs before images or containers are touched"
	@echo "  release-up         - Start HTTPS/mTLS release topology (requires RELEASE_ENV_FILE)"
	@echo "  release-ps         - Status of release stack (requires RELEASE_ENV_FILE)"
	@echo "  release-down       - Stop it; keep volumes (requires RELEASE_ENV_FILE)"
	@echo ""
	@echo "🛠️ DEVELOPMENT:"
	@echo "  proto              - Regenerate upload_pb2*.py gRPC stubs into the host tree"
	@echo "  ui-build           - Build both SPAs in Docker (resolving the ui-kit workspace)"
	@echo "  ui-test            - Typecheck + run all workspace unit tests in Docker"
	@echo "  ui-dev             - Query-service SPA dev server (port 5173)"
	@echo "  robot-ui-dev       - Robot upload-agent SPA dev server (127.0.0.1:5174; ROBOT_UI_BIND_HOST widens)"
	@echo "  lock-python        - Regenerate all per-image Python dependency locks (requirements*.lock.txt)"
	@echo "  lock-python-dev/-processor/-query/-toolkit - Regenerate just one image's lock"
	@echo ""
	@echo "📋 EXAMPLES:"
	@echo "  make build-and-up                    # Build and start everything"
	@echo "  make stack-rebuild                   # Force rebuild and restart"
	@echo "  ROBOT_DATA_HOST_DIR=/path make robot-ui-start"
	@echo ""

# Upload all pending files from the mounted bag directory. Interrupted files
# retry idempotently from byte zero into a fresh server-side multipart upload.
# The agent takes its identity only from the config's `robot_id`, so the
# helper uploads each robot directory with a config copy that names it.
# Usage:
#   make robot-upload ROBOT_ID=demo CFG=configs/vault.remote.json
# Environment:
#   export ROBOT_DATA_HOST_DIR=/path/to/your/rosbags && docker compose up -d robot
# Depends on proto for the same reason stack-up does: rua's transport imports
# upload_pb2, and the robot service's bind mount shadows the image's copy.
robot-upload: proto
	docker exec -it \
	  -e ROBOT_ID="$(or $(ROBOT_ID),demo)" \
	  -e CFG="$(or $(CFG),configs/vault.local.json)" \
	  robot-vault-robot bash -lc 'development/upload-robot-dir.sh "$$ROBOT_ID" "$$CFG"'

# Bring up the full server-side stack and wait for health.
# Depends on build-all so a fresh checkout builds robot-vault-dev:latest (used
# by db-migrate/postgres-roles-ready/robot/seed/gc via `build: .`),
# robot-vault-ingest, -processor,
# and -query images locally instead of compose trying to pull nonexistent
# images from a registry. Also depends on proto: the `robot` service's
# robot_upload_agent code imports upload_pb2 directly, and its bind mount
# shadows the stubs the dev image generated at build time (see `proto`); and on
# secrets, which gives this machine its own ingest auth token instead of the
# committed dev fallback; and on ui-build because the robot source bind
# mount deliberately shadows the image layer while the generated SPA is
# gitignored. Without that prerequisite a clean checkout serves a truthful but
# unusable 503 at :8090.
#
# STACK_SERVICES are the long-running services; private.mk may append its own.
#
# The ingest readiness probe runs *inside* the container: the admin
# port is not published to the host, so curling localhost:8080 from here would
# now fail even on a perfectly healthy stack. The query probe stays on the host
# because 8003 is the user-facing front door and is still published.
STACK_SERVICES := ingest processor query ui robot gc
stack-up: build-all proto secrets ui-build
	docker compose up -d postgres objectstore redis
	@echo "Waiting for infrastructure..."
	docker compose up -d objectstore-access-ready
	docker compose wait objectstore-access-ready
	docker compose up -d $(STACK_SERVICES)
	@echo "Waiting for ingest (admin /readyz, in-network)..."; \
	OK=0; for i in $$(seq 1 60); do sleep 0.5; if docker compose exec -T ingest ingest-server --healthcheck >/dev/null 2>&1; then OK=1; break; fi; done; \
	[ $$OK -eq 1 ] || (echo "ingest readiness check failed" && exit 1)
	@echo "Waiting for query ($${QUERY_PORT:-8003}/readyz)..."; \
	OK=0; for i in $$(seq 1 60); do sleep 0.5; if curl -fsS http://localhost:$${QUERY_PORT:-8003}/readyz >/dev/null; then OK=1; break; fi; done; \
	[ $$OK -eq 1 ] || (echo "query readiness check failed" && exit 1)
	@echo "Stack ready"; docker compose ps | cat

# Start robot UI backend (requires ROBOT_DATA_HOST_DIR on host). The generated
# SPA is ignored and must exist in the bind-mounted source tree before startup.
robot-ui-start: proto ui-build
	@[ -n "$$ROBOT_DATA_HOST_DIR" ] || (echo "Set ROBOT_DATA_HOST_DIR=/absolute/path/to/rosbags" && exit 1)
	docker compose up -d robot
	@echo "Robot UI: http://localhost:8090"

# Convenience: upload all robots using defaults
robot-upload-all:
	$(MAKE) robot-upload ROBOT_ID=all CFG="$(or $(CFG),configs/vault.local.json)"

# Stop stack containers
stack-down:
	docker compose down --remove-orphans

# Full reset: stop containers, PERMANENTLY DESTROY the Postgres data under ./data/,
# the object-store volume (and the other compose volumes), and clear uploads/. This is irreversible — there is no undo. Take a
# backup first (see docs/OPERATIONS.md) if the data matters. Requires CONFIRM=1 to
# run at all, as a guard against accidental data loss.
#   make stack-reset CONFIRM=1
stack-reset:
	@if [ "$$CONFIRM" != "1" ]; then \
	  echo "=============================================================="; \
	  echo "  stack-reset ABORTED: this PERMANENTLY DELETES all data in"; \
	  echo "    ./data/postgres  (Postgres system of record)"; \
	  echo "    objectstore_data (object storage system of record, a named volume)"; \
	  echo "    ./uploads        (local upload staging)"; \
	  echo ""; \
	  echo "  This is IRREVERSIBLE. Back up first — see docs/OPERATIONS.md for"; \
	  echo "  backup commands."; \
	  echo ""; \
	  echo "  Re-run with CONFIRM=1 to proceed:"; \
	  echo "    make stack-reset CONFIRM=1"; \
	  echo "=============================================================="; \
	  exit 1; \
	fi
	@echo "WARNING: wiping persisted data (object store, Postgres, uploads)"
	docker compose down -v --remove-orphans
	docker run --rm -v "$$PWD":/w alpine:3 sh -c "set -e; cd /w; mkdir -p ./data/postgres ./uploads; rm -rf ./data/postgres/* ./uploads/*; echo Data folders cleared"

# =============================================================================
# Release engineering
# =============================================================================
.PHONY: release-tag release-env-file release-up release-down release-config release-preflight release-ps test-release-config core-images-pull

# The single source of truth for what version this tree is. Everything below
# reads it; nothing hardcodes a version string.
VERSION := $(shell cat VERSION 2>/dev/null)

# The five core release images (local tag names). The developer image is
# intentionally absent: release jobs run from the explicit, allowlisted toolkit
# image. Every edition runs these; Pro replaces processor and query by digest.
RELEASE_IMAGES := robot-vault-toolkit robot-vault-ingest robot-vault-core-processor robot-vault-core-query robot-vault-objectstore

# The core images by digest (release/core-images.lock.json). `make
# core-images-pull` pulls and checks them and tags them as <name>:$(VERSION);
# release-up accepts an image built from this checkout or the lock's digest.
CORE_IMAGES_LOCK ?= release/core-images.lock.json

# Compose invocation for the release topology. A distinct project name (-p) is
# what lets a release stack exist alongside a developer's dev stack without
# fighting over container names -- deploy/docker-compose.release.yml sets no
# `container_name:` for exactly this reason. RELEASE_ENV_FILE points compose at
# a deployment's own env file instead of the repo-root dev `.env` (which
# compose auto-loads and which carries dev credentials). Every operational
# release target requires this argument; there is no development fallback.
RELEASE_PROJECT ?= robot-vault-release
RELEASE_ENV_FILE ?=
# Seconds `compose up` may take (image start, init jobs); then release-up fails.
RELEASE_UP_TIMEOUT ?= 900
# Internal release plumbing is deliberately not a caller-overridable Make
# variable.  EDITION, RELEASE_PROJECT and RELEASE_ENV_FILE are the supported
# knobs; letting `make ... RELEASE_COMPOSE=...` replace the reviewed topology
# would make the preflight's guarantees illusory.
# The Pro distribution (private.mk) appends its overlay for EDITION=pro, which
# is why these two are recursively expanded.
# RELEASE_TEST_OVERLAYS is for isolated test stacks only (the throwaway OIDC
# issuer and a test wrapper's override). Production leaves it empty; the preflight
# validates the merged result either way.
override RELEASE_COMPOSE_FILES := deploy/docker-compose.release.yml
RELEASE_TEST_OVERLAYS ?=
override RELEASE_COMPOSE = docker compose -p "$(RELEASE_PROJECT)" --env-file "$(RELEASE_ENV_FILE)" $(foreach f,$(RELEASE_COMPOSE_FILES) $(RELEASE_TEST_OVERLAYS),-f $(f))

# core|pro. Drives all three gates together (query routers, compose topology,
# processor stage flags) -- they must never be set independently. This tree's
# compose file is core; EDITION=pro needs the Pro release overlay (Pro only;
# the preflight refuses a pro request without it).
EDITION ?= core
ifeq ($(EDITION),core)
  override RELEASE_EDITION_ENV := ROBOT_VAULT_EDITION=core SEGMENTATION_ENABLED=false QA_ENABLED=false MEDIA_ENCODE_ENABLED=false
else ifeq ($(EDITION),pro)
  override RELEASE_EDITION_ENV := ROBOT_VAULT_EDITION=pro SEGMENTATION_ENABLED=true QA_ENABLED=true MEDIA_ENCODE_ENABLED=true
else
  $(error EDITION must be core or pro, got '$(EDITION)')
endif

# Compose control variables can change its file, project, profile and dotenv
# inputs before the CLI is evaluated. An optional extension may add its own
# settings to this unset list. Pin their security-relevant values in
# the process environment so neither the caller's shell nor RELEASE_ENV_FILE
# can redirect this Make workflow. Explicit CLI arguments remain the final
# authority. Host-port selectors remain the only shell-level release knobs.
override RELEASE_ENV := env \
  -u DATABASE_URL -u POSTGRES_PASSWORD -u OBJECTSTORE_ROOT_PASSWORD -u INGEST_AUTH_TOKEN \
  -u INGEST_PROVISIONING_TOKEN \
  -u INGEST_DATABASE_URL -u PROCESSOR_DATABASE_URL -u EXPORTER_DATABASE_URL \
  -u QUERY_DATABASE_URL -u GC_DATABASE_URL \
  -u INGEST_S3_SECRET_KEY -u PROCESSOR_S3_SECRET_KEY -u EXPORTER_S3_SECRET_KEY \
  -u QUERY_S3_SECRET_KEY -u GC_S3_SECRET_KEY \
  -u METRICS_AUTH_TOKEN \
  -u POSTGRES_USER -u POSTGRES_DB -u OBJECTSTORE_ROOT_USER \
  -u S3_BUCKET -u S3_PARQUET_BUCKET -u S3_ENDPOINT -u S3_REGION \
  -u S3_SECURE -u INGEST_SECURITY_PROFILE -u S3_PUBLIC_ENDPOINT -u S3_PREFIX_SCOPE \
  -u OIDC_ISSUER -u OIDC_AUDIENCE -u OIDC_JWKS_URL -u OIDC_CLIENT_ID \
  -u OIDC_AUTHORIZATION_URL -u OIDC_TOKEN_URL -u OIDC_END_SESSION_URL \
  -u OIDC_SCOPES -u OIDC_ROLE_CLAIM -u OIDC_ALGORITHMS -u OIDC_ROLE_SOURCE \
  -u ROBOT_VAULT_RELEASE -u QUERY_PUBLIC_HOST -u INGEST_PUBLIC_HOST \
  -u QUERY_TLS_CERT_FILE -u QUERY_TLS_KEY_FILE -u QUERY_TLS_CA_FILE \
  -u INGEST_TLS_CERT_FILE -u INGEST_TLS_KEY_FILE -u INGEST_TLS_CA_FILE \
  -u ROBOT_VAULT_SECRETS_GID \
  -u TLS_ENABLED -u TLS_MTLS -u TLS_SERVER_CERT -u TLS_SERVER_KEY -u TLS_CA_CERT \
  -u EXPORTER_MAX_ATTEMPTS \
  -u DUCKDB_THREADS -u DUCKDB_MEMORY_LIMIT_MB -u DUCKDB_QUERY_CONCURRENCY \
  -u DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS -u DUCKDB_QUERY_TIMEOUT_SECONDS \
  -u QUERY_MAX_SOURCE_RANGE_SECONDS -u QUERY_MAX_SOURCE_FILES \
  -u QUERY_MAX_SOURCE_ROWS -u QUERY_MAX_SOURCE_BYTES \
  -u SQL_CONSOLE_MEMORY_LIMIT_MB \
  -u SEGMENTATION_ENABLED -u QA_ENABLED -u MEDIA_ENCODE_ENABLED \
  -u TIME_INDEX_CACHE_SIZE_MB \
  -u GC_MIN_AGE_HOURS -u GC_MPU_MAX_AGE_HOURS -u GC_DELETE_LIMIT \
  -u GC_DRY_RUN -u GC_PREFIXES -u GC_METRICS_PORT -u GC_INTERVAL_SECONDS \
  -u ROBOT_VAULT_LOG_MAX_SIZE -u ROBOT_VAULT_LOG_MAX_FILES \
  -u FIRST_PARTY_PIDS_LIMIT -u POSTGRES_SHM_SIZE \
  -u POSTGRES_MEMORY_LIMIT -u POSTGRES_CPU_LIMIT -u POSTGRES_PIDS_LIMIT \
  -u OBJECTSTORE_MEMORY_LIMIT -u OBJECTSTORE_CPU_LIMIT -u OBJECTSTORE_PIDS_LIMIT \
  -u OBJECTSTORE_BOOTSTRAP_MEMORY_LIMIT -u OBJECTSTORE_BOOTSTRAP_CPU_LIMIT \
  -u REDIS_MEMORY_LIMIT -u REDIS_CPU_LIMIT -u REDIS_PIDS_LIMIT \
  -u DB_MIGRATE_MEMORY_LIMIT -u DB_MIGRATE_CPU_LIMIT \
  -u DB_BOOTSTRAP_MEMORY_LIMIT -u DB_BOOTSTRAP_CPU_LIMIT \
  -u INGEST_MEMORY_LIMIT -u INGEST_CPU_LIMIT -u INGEST_PIDS_LIMIT \
  -u PROCESSOR_MEMORY_LIMIT -u PROCESSOR_CPU_LIMIT -u PROCESSOR_PIDS_LIMIT \
  -u QUERY_MEMORY_LIMIT -u QUERY_CPU_LIMIT -u QUERY_PIDS_LIMIT \
  -u GC_MEMORY_LIMIT -u GC_CPU_LIMIT -u GC_PIDS_LIMIT \
  -u PROMETHEUS_MEMORY_LIMIT -u PROMETHEUS_CPU_LIMIT -u PROMETHEUS_PIDS_LIMIT \
  -u PROMETHEUS_RETENTION_TIME -u PROMETHEUS_RETENTION_SIZE \
  -u MCP_ENABLED -u MCP_WRITE_TOOLS -u MCP_SQL_ENABLED -u MCP_ALLOW_NO_AUTH \
  -u MCP_TOOLS_DENY -u MCP_ALLOWED_ORIGINS -u PUBLIC_BASE_URL \
  COMPOSE_FILE= COMPOSE_PROJECT_NAME= COMPOSE_PROFILES= COMPOSE_ENV_FILES= \
  COMPOSE_DISABLE_ENV_FILE=1 COMPOSE_PATH_SEPARATOR=: \
  COMPOSE_IGNORE_ORPHANS=false COMPOSE_REMOVE_ORPHANS=false \
  ROBOT_VAULT_VERSION=$(VERSION) $(RELEASE_EDITION_ENV) \
  GIT_SHA=$$(git rev-parse HEAD 2>/dev/null || echo unknown)

# Tag the locally-built images with $(VERSION), turning five ambiguous `:latest`
# references into explicit version tags the release compose file can name.
#
# This target only tags local images for a source release. It pushes nothing
# and pins no digests. Published releases record their image digests in
# release/core-images.lock.json; `make core-images-pull` deploys from that lock.
release-tag:
	@[ -n "$(VERSION)" ] || (echo "VERSION file is missing or empty" && exit 1)
	@test -z "$$(git status --porcelain)" || { \
	  echo "REFUSING to tag a release from a dirty working tree" >&2; \
	  git status --short >&2; exit 1; \
	}
	@echo "Tagging first-party images as :$(VERSION)"
	@revision=$$(git rev-parse HEAD); for img in $(RELEASE_IMAGES); do \
	  source_id=$$(docker image inspect --format '{{.Id}}' "$$img:latest" 2>/dev/null) || { echo "  MISSING $$img:latest -- run 'make build-release-local' first"; exit 1; }; \
	  image_revision=$$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$$img:latest" 2>/dev/null); \
	  [ "$$image_revision" = "$$revision" ] || { echo "  REFUSING $$img:latest: OCI revision '$$image_revision' != checkout $$revision -- rebuild from this clean tree" >&2; exit 1; }; \
	  target_id=$$(docker image inspect --format '{{.Id}}' "$$img:$(VERSION)" 2>/dev/null || true); \
	  [ -z "$$target_id" ] || [ "$$target_id" = "$$source_id" ] || { echo "  REFUSING to overwrite $$img:$(VERSION) (it points at a different image)"; exit 1; }; \
	done
	@for img in $(RELEASE_IMAGES); do \
	  if docker image inspect "$$img:$(VERSION)" >/dev/null 2>&1; then \
	    echo "  $$img:$(VERSION) already points at $$img:latest"; \
	  else \
	    docker tag "$$img:latest" "$$img:$(VERSION)" || exit 1; \
	    echo "  $$img:latest -> $$img:$(VERSION)"; \
	  fi; \
	done
	@echo "Done. Verify with: docker images --filter reference='robot-vault-*:$(VERSION)'"

# Bring up the release topology. EDITION=core uses only the core compose file,
# so the exporter container is never created -- check with `make release-ps`.
# Validate the secret-bearing dotenv before Compose sees it. The helper reads
# filesystem metadata only; it never sources or echoes the file.
release-env-file:
	@[ -n "$(RELEASE_ENV_FILE)" ] || (echo "RELEASE_ENV_FILE is required (for example /etc/robot-vault/release.env)" >&2 && exit 1)
	@python3 development/release/validate_release_inputs.py --env-file "$(RELEASE_ENV_FILE)"

# Validate the exact deployment inputs before inspecting images or creating a
# container.  The Compose file repeats these required-variable guards, so a
# direct `docker compose up` is protected too.
release-preflight: release-env-file
	@[ -n "$(VERSION)" ] || (echo "VERSION file is missing or empty" && exit 1)
	@echo "Preflighting the $(EDITION) release topology and deployment configuration"
	@RENDERED=$$(mktemp); trap 'rm -f "$$RENDERED"' EXIT HUP INT TERM; \
	  $(RELEASE_ENV) $(RELEASE_COMPOSE) --profile obs config --format json >"$$RENDERED" && \
	  python3 development/release/validate_release_inputs.py \
	    --env-file "$(RELEASE_ENV_FILE)" --config "$$RENDERED" --edition "$(EDITION)"

release-up: release-preflight
	@[ -n "$(VERSION)" ] || (echo "VERSION file is missing or empty" && exit 1)
	@test -z "$$(git status --porcelain)" || { \
	  echo "REFUSING to deploy a release from a dirty working tree" >&2; \
	  git status --short >&2; exit 1; \
	}
	@python3 development/release/core_images.py check --lock "$(CORE_IMAGES_LOCK)" \
	  --version "$(VERSION)" --revision "$$(git rev-parse HEAD)" $(RELEASE_IMAGES)
	@echo "Starting the $(EDITION) release topology, version $(VERSION), project $(RELEASE_PROJECT)"
	# Bootstrap containers retain owner/root credentials in Docker metadata. An
	# EXIT trap is registered before `up` so success, readiness failure, edition
	# mismatch, and signals all stop/remove those one-shots. Cleanup failure also
	# fails an otherwise-successful deployment instead of claiming secrets were removed.
	# --remove-orphans: services of another edition (a Pro exporter after a Pro ->
	# core redeploy) are not in this edition's files, so compose removes them.
	# Data volumes: volume_guard.py refuses when compose would recreate one (and
	# delete its data); compose_up.sh runs `up` with stdin from /dev/null, fails
	# if compose still asks to recreate a volume, and has a timeout. Never pass -y/--yes.
	@set -eu; \
	$(RELEASE_ENV) $(RELEASE_COMPOSE) config --format json \
	  | python3 development/release/volume_guard.py --project "$(RELEASE_PROJECT)"; \
	cleanup_bootstrap() { \
	  status=$$?; \
	  trap - EXIT HUP INT TERM; \
	  if ! $(RELEASE_ENV) $(RELEASE_COMPOSE) rm -sf \
	      db-migrate postgres-roles-ready objectstore-access-ready >/dev/null; then \
	    echo "WARNING: failed to remove secret-bearing release bootstrap containers" >&2; \
	    if [ "$$status" -eq 0 ]; then status=1; fi; \
	  fi; \
	  exit "$$status"; \
	}; \
	trap cleanup_bootstrap EXIT; \
	trap 'exit 129' HUP; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	$(RELEASE_ENV) sh development/release/compose_up.sh $(RELEASE_UP_TIMEOUT) $(RELEASE_COMPOSE); \
	$(RELEASE_ENV) python3 development/release/wait_release_ready.py \
	  --project "$(RELEASE_PROJECT)" \
	  --env-file "$(RELEASE_ENV_FILE)" \
	  $(foreach f,$(RELEASE_COMPOSE_FILES) $(RELEASE_TEST_OVERLAYS),--compose-file $(f)) \
	  --edition "$(EDITION)"; \
	echo "Release stack ready ($(EDITION))."

release-down: release-env-file
	# `down` ignores services in inactive profiles. Activate every profile for
	# teardown so an opt-in profile service cannot leave a live container
	# (and its project network) behind; --remove-orphans removes services of
	# another edition.
	$(RELEASE_ENV) $(RELEASE_COMPOSE) --profile '*' down --remove-orphans

release-ps: release-env-file
	$(RELEASE_ENV) $(RELEASE_COMPOSE) ps

# Backup and restore of Postgres and the object-store bucket
# (tools/robot_vault_backup.py; docs/OPERATIONS.md "Backup and restore").
# Dev stack by default; set RELEASE_ENV_FILE (and RELEASE_PROJECT) for the
# release stack. The object copy runs in the toolkit image: build it with
# `make build-toolkit` (dev) or use the release's robot-vault-toolkit:$(VERSION).
.PHONY: backup restore
BACKUP_DIR ?=
# Backup metrics textfile for the release ops-metrics source; empty (dev) writes none.
BACKUP_METRICS_DIR ?= $(if $(RELEASE_ENV_FILE),/var/lib/robot-vault/backup-metrics,)
BACKUP_TOOLKIT_IMAGE ?= $(if $(RELEASE_ENV_FILE),robot-vault-toolkit:$(VERSION),robot-vault-toolkit:latest)
override BACKUP_RUN = $(if $(RELEASE_ENV_FILE),$(RELEASE_ENV),) python3 tools/robot_vault_backup.py
override BACKUP_COMPOSE = $(if $(RELEASE_ENV_FILE),$(RELEASE_COMPOSE),docker compose)

backup restore:
	@[ -n "$(BACKUP_DIR)" ] || (echo "BACKUP_DIR is required, for example make $@ BACKUP_DIR=/srv/backups/$$(date -u +%Y%m%dT%H%M%SZ)" >&2 && exit 2)
	$(BACKUP_RUN) $@ --dir "$(BACKUP_DIR)" --compose '$(BACKUP_COMPOSE)' --image "$(BACKUP_TOOLKIT_IMAGE)" \
		$(if $(and $(filter backup,$@),$(BACKUP_METRICS_DIR)),--metrics-dir "$(BACKUP_METRICS_DIR)")

# Pull the core images of the lock by digest (skipped when present), check the
# digest and tag each as <name>:$(VERSION) for release-up.
core-images-pull:
	@[ -n "$(VERSION)" ] || (echo "VERSION file is missing or empty" && exit 1)
	python3 development/release/core_images.py pull --lock "$(CORE_IMAGES_LOCK)" --version "$(VERSION)"

# Render + validate the release topology against one deployment's env file,
# without starting anything. Use test-release-config for the hermetic structural
# check; use this target to catch absent production inputs:
#   make release-config EDITION=core RELEASE_ENV_FILE=/etc/robot-vault/release.env
release-config: release-preflight
	@echo "Validating deploy/docker-compose.release.yml for EDITION=$(EDITION), version $(VERSION)"
	@echo "  valid. Services in this edition:"
	@$(RELEASE_ENV) $(RELEASE_COMPOSE) config --services | sort | sed 's/^/    /'

# Hermetic structural regression: renders both editions with a temporary,
# non-secret deployment env and proves release auth cannot be switched to the
# development path.  This starts no containers and performs no network I/O.
test-release-config:
	@bash development/release/test_release_config.sh

# Keep tracked Markdown structure, local links, anchors and fences honest.
docs-check:
	python3 development/check_docs.py

# Optional local extensions (the Pro distribution uses this hook).
-include private.mk
