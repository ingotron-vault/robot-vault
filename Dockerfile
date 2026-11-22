# Dev image for Robot Vault phases (01, 02) with Python CLIs and gRPC stubs
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps (optional: for iwgetid in connectivity checks)
# wireless-tools: iwgetid, for the agent's SSID connectivity check.
# iproute2 + iputils-ping: `tc netem` and the RTT check the shaped-link
# benchmark uses. They are inert without the NET_ADMIN that
# deploy/docker-compose.bench.yml grants, so carrying them here costs a few MB and
# does not widen what the default stack can do -- whereas without them
# `make bench-net` fails at the first command.
# procps supplies `ps` for the restore crash-injection fixture's ancestor walk.
# curl serves the stage-1 install fixture's real HTTP readiness and fetch path.
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    wireless-tools \
    iproute2 \
    iputils-ping \
    dash \
    busybox \
    procps \
    curl \
    openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirement and install. requirements-dev.lock.txt is the
# hash-pinned closure of requirements-dev.txt PLUS every phase pyproject.toml
# this image editable-installs below -- generated together because they all
# land in this one environment (`make lock-python-dev` regenerates it, inside
# a python:3.12-slim container matching this base). requirements-dev.txt
# itself still ships (README's bare-metal robot quickstart and the optional
# host venv steps in development/README.md both install straight from it) and
# stays the source of truth for FLOORS; only the resolved/pinned closure moves
# to the lock file.
COPY requirements-dev.txt requirements-dev.lock.txt /app/
RUN pip install --no-cache-dir -r /app/requirements-dev.lock.txt

# Copy only the runtime and development inputs this image owns. A broad
# checkout copy can capture ignored local credentials, tool state, and
# dependency trees because Docker does not consult
# .gitignore. Keep this list explicit even though .dockerignore also rejects
# those paths.
COPY LICENSE /usr/share/licenses/robot-vault/LICENSE
COPY proto /app/proto
COPY db /app/db
COPY configs /app/configs
COPY tools /app/tools
COPY development/sim_gen /app/development/sim_gen
COPY development/example_data /app/development/example_data
COPY phases/common-config /app/phases/common-config
COPY phases/01-robot-storage /app/phases/01-robot-storage
COPY phases/02-robot-upload-agent /app/phases/02-robot-upload-agent
COPY phases/04-processing-pipeline /app/phases/04-processing-pipeline
COPY phases/05-query-service /app/phases/05-query-service

# Generate gRPC stubs into the agent package AND into /opt/proto-gen.
# The dev/robot/seed/db-migrate compose services bind-mount the repo over /app,
# which shadows the in-package copy generated here -- and since the stubs are
# generated artifacts (gitignored, not committed), the host tree may not have
# them. /opt/proto-gen is outside /app, so it survives the bind mount and is
# picked up via PYTHONPATH; the in-package copy still wins when it exists.
RUN python -m grpc_tools.protoc -I proto \
    --python_out=phases/02-robot-upload-agent/src \
    --grpc_python_out=phases/02-robot-upload-agent/src \
    proto/upload.proto proto/enrollment.proto \
 && mkdir -p /opt/proto-gen \
 && python -m grpc_tools.protoc -I proto \
    --python_out=/opt/proto-gen \
    --grpc_python_out=/opt/proto-gen \
    proto/upload.proto proto/enrollment.proto

# /app/development carries only the dev-only Python packages that are NOT
# phase packages: `sim_gen`, the deterministic simulation MCAP generator, and
# `example_data`. The Compose services bind-mount the checkout over /app, so
# other development scripts reach them from there. Putting it on PYTHONPATH is what makes `python -m sim_gen ...` work from any
# cwd in this image (development/seed.sh relies on it), without pretending it
# is a shipped package with a pyproject.toml and a slot in the unit gate.
ENV PYTHONPATH=/opt/proto-gen:/app/development

# Install local packages. --no-deps: their third-party dependencies were
# already installed, pinned and hash-verified from requirements-dev.lock.txt
# above (it was compiled from these same pyproject.toml files), so this
# step only needs to register the local packages themselves -- not have pip
# re-resolve anything against the network.
RUN pip install --no-cache-dir --no-deps -e phases/common-config -e phases/01-robot-storage -e phases/02-robot-upload-agent -e phases/04-processing-pipeline -e phases/05-query-service

# The image is also the source for the release-only migration and GC jobs.
# Keep the development image's default user unchanged (the bench/dev profiles
# deliberately need tools such as `tc`), but provide a real unprivileged
# identity for the release Compose file to select explicitly.  Numeric IDs are
# fixed so host-owned private deployment inputs can be provisioned without
# depending on a username database inside the container.
RUN groupadd --gid 10001 robotvault \
 && useradd --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin robotvault

CMD ["sleep", "infinity"]

# Keep provenance last: changing the source revision must not invalidate the
# expensive OS and hash-locked Python dependency layers above.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
