# ---------------------------------------------------------------------------
# The robot agent this vault serves at /robot (robot.sh) and
# /robot/agent-linux-amd64.tar.gz. packaging/robot-agent/build-agent-tarball.sh
# builds the same builder stage with `--target robot-agent-builder` for its
# two-build reproducibility check: one recipe, two users.
#
# The amd64 manifest is pinned, not the mutable multi-arch tag. It is Python
# 3.10.18 on Debian 11 (bullseye), whose glibc is 2.31, so anything PyInstaller
# links here keeps the Ubuntu 20.04+ floor.
FROM --platform=linux/amd64 python:3.10.18-slim-bullseye@sha256:b3061b93c8df9809c3783a4f17bbf2520425ec6b40bd3e5e7538870e21ba7209 AS robot-agent-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

# PyInstaller examines ELF dependencies with objdump. The upstream image keeps
# the exact snapshot sources that produced it as commented lines; enable that
# immutable 2025-07-21 snapshot and pin the complete binutils closure instead
# of resolving today's Debian repository state.
RUN sed -i \
      -e 's|^# deb http://snapshot.debian.org|deb http://snapshot.debian.org|' \
      -e 's|^deb http://deb.debian.org|# deb http://deb.debian.org|' \
      /etc/apt/sources.list \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends \
      binutils=2.35.2-2 \
      binutils-common=2.35.2-2 \
      binutils-x86-64-linux-gnu=2.35.2-2 \
      libbinutils=2.35.2-2 \
      libctf-nobfd0=2.35.2-2 \
      libctf0=2.35.2-2 \
 && rm -rf /var/lib/apt/lists/*

COPY packaging/robot-agent/requirements-build.lock.txt /build/requirements-build.lock.txt
RUN python -m pip install \
      --require-hashes --only-binary=:all: --no-deps \
      -r /build/requirements-build.lock.txt \
 && python -m pip check

COPY packaging/robot-agent/freeze_agent.py /build/freeze_agent.py
ENTRYPOINT ["python", "/build/freeze_agent.py"]

FROM robot-agent-builder AS robot-agent

# A fixed epoch: the archive's bytes depend only on its inputs, not on when or
# from which commit the image was built.
ARG AGENT_SOURCE_EPOCH=1700000000
# Only the agent's inputs. The frozen bundle carries no source.
COPY LICENSE VERSION THIRD_PARTY_NOTICES.md /source/
COPY proto /source/proto
COPY phases/common-config/src /source/phases/common-config/src
COPY phases/01-robot-storage/src/robot_storage_manager /source/phases/01-robot-storage/src/robot_storage_manager
COPY phases/02-robot-upload-agent/src/robot_upload_agent /source/phases/02-robot-upload-agent/src/robot_upload_agent
COPY packaging/robot-agent /source/packaging/robot-agent
RUN --network=none set -eu \
 && v=$(tr -d '[:space:]' < /source/VERSION) \
 && python /build/freeze_agent.py --source /source \
      --output-root "/stage/ingotron-agent-$v" --version "$v" \
      --source-epoch "$AGENT_SOURCE_EPOCH" \
 && bash /source/packaging/robot-agent/pack_agent.sh /stage "ingotron-agent-$v" \
      "$AGENT_SOURCE_EPOCH" /robot/agent-linux-amd64.tar.gz \
 && sh /source/packaging/robot-agent/render-robot-bootstrap.sh \
      --tarball /robot/agent-linux-amd64.tar.gz --version "$v" --out /robot/robot.sh \
 && chmod 0644 /robot/agent-linux-amd64.tar.gz /robot/robot.sh

FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS ui-build

# The viewer depends on the @robot-vault/ui-kit workspace package (shared
# Tailwind preset + tokens.css + components). npm workspaces resolve that
# dependency via a node_modules symlink into phases/ui-kit, so the workspace
# root manifests AND phases/ui-kit itself must be in the build context here,
# not just this SPA's own directory -- copying only ui-spa would leave
# "@robot-vault/ui-kit" unresolvable. phases/02-robot-upload-agent's ui-spa is
# a sibling workspace declared in the root package.json but is intentionally
# NOT copied: npm ci tolerates a missing workspace directory (skips it), and
# this image has no need for that phase's SPA.
WORKDIR /repo
COPY package.json package-lock.json /repo/
COPY phases/ui-kit /repo/phases/ui-kit
COPY phases/05-query-service/src/ui-spa /repo/phases/05-query-service/src/ui-spa
# The committed workspace lock is the build input. Never rewrite it or resolve
# a different dependency graph as an automatic fallback inside an image build.
RUN npm ci --no-audit --no-fund

# The footer shows this version when the API does not send core_version.
COPY VERSION /repo/VERSION
WORKDIR /repo/phases/05-query-service/src/ui-spa
# rv-build: the build identity that the release readiness check compares with the API.
RUN VITE_RV_VERSION="$(cat /repo/VERSION)" npm run build \
  && sed -i "s|<head>|<head><meta name=\"rv-build\" content=\"bundle=core core=$(cat /repo/VERSION) pro=none\">|" dist/index.html \
  && grep -q 'name="rv-build"' dist/index.html \
  && mkdir -p dist/uploads dist/episodes dist/explore dist/admin dist/robots dist/obs dist/datasets dist/auth/callback \
  && cp dist/index.html dist/uploads/index.html \
  && cp dist/index.html dist/episodes/index.html \
  && cp dist/index.html dist/explore/index.html \
  && cp dist/index.html dist/admin/index.html \
  && cp dist/index.html dist/robots/index.html \
  && cp dist/index.html dist/obs/index.html \
  && cp dist/index.html dist/datasets/index.html \
  && cp dist/index.html dist/auth/callback/index.html

# The official Python slim images tested with the release Grype database carry
# High/Critical findings. Ubuntu 24.04's distro Python 3.12 matches the
# lock (`make lock-python-query` resolves on the same base), so build a venv from
# that OS-managed interpreter and copy only the runtime closure forward.
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289 AS python-build

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb-extensions

RUN apt-get update -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv "$VIRTUAL_ENV"

WORKDIR /app
COPY phases/05-query-service/pyproject.toml /app/pyproject.toml
# This is the reviewed hash-pinned dependency closure. The lock is resolved
# for this distro interpreter, Python 3.12.
COPY phases/05-query-service/requirements.lock.txt /app/requirements.lock.txt
COPY phases/05-query-service/src /app/src
COPY phases/common-config /app/common-config

# Third-party dependencies are installed from the lock before the local,
# non-editable distributions. Non-editable installation means the final image
# contains no build context or editable-link machinery.
RUN python -m pip install --no-cache-dir -r /app/requirements.lock.txt \
 && python -m pip install --no-cache-dir --no-deps /app/common-config \
 && python -m pip install --no-cache-dir --no-deps .

# The mcap wheel carries no licence file. Put the upstream text (repository root
# at the release tag's commit, sha256-pinned) where PEP 639 puts licence files:
# <dist>.dist-info/licenses/. The folder name is exact, so a version bump
# fails here until the pinned texts are reviewed for the new release.
ADD --checksum=sha256:da11235665c17d4c1634072dae92b8ba1b38d6fdde2ccf19a6bbede33253f58d \
    https://raw.githubusercontent.com/foxglove/mcap/b33fa682a5c517b1d213faeabd118e0b4f9d9d93/LICENSE /licence-texts/mcap-LICENSE
RUN test -d /opt/venv/lib/python3.12/site-packages/mcap-1.4.0.dist-info \
 && install -D -m 0644 /licence-texts/mcap-LICENSE /opt/venv/lib/python3.12/site-packages/mcap-1.4.0.dist-info/licenses/LICENSE

# Air-gap fix: DuckDB's `httpfs`
# is fetched during the build and copied into the final image, never installed
# by a running deployment.
RUN python -c "\
import duckdb; \
con = duckdb.connect(); \
con.execute(\"SET extension_directory='/opt/duckdb-extensions'\"); \
con.execute('INSTALL httpfs')"

# `query_service.main` resolves the UI beside its installed package. Put the
# generated bundle in that final location before the import smoke test, then
# carry it across inside the already-tested venv.
COPY --from=ui-build /repo/phases/05-query-service/src/ui-spa/dist \
     /opt/venv/lib/python3.12/site-packages/ui

# setuptools and wheel are build-only. Their
# vulnerable versions must not ship; remove them before the venv crosses the
# stage boundary, then prove imports and the actual ASGI entrypoint survive.
RUN python -m pip uninstall -y setuptools wheel \
 && python -c "import query_service.main, robot_vault_config" \
 && uvicorn --version >/dev/null

FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb-extensions

WORKDIR /app
COPY LICENSE /usr/share/licenses/robot-vault/LICENSE
RUN apt-get update -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=python-build /opt/venv /opt/venv
COPY --from=python-build /opt/duckdb-extensions /opt/duckdb-extensions
# Served without authentication by GET /robot and
# GET /robot/agent-linux-amd64.tar.gz (query_service settings
# ROBOT_BOOTSTRAP_DIR). Root-owned and read-only for the service user.
COPY --from=robot-agent /robot /opt/robot-vault/robot

# Release Compose selects this unprivileged identity and gives DuckDB a
# dedicated writable volume for spill files. The baked extension directory
# remains immutable/read-only at runtime; only query's bounded work directory
# and /tmp are writable.
RUN groupadd --gid 10001 robotvault \
 && useradd --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin robotvault \
 && install -d -o 10001 -g 10001 -m 0750 /var/lib/robot-vault/query

EXPOSE 8003

CMD ["uvicorn", "query_service.main:app", "--host", "0.0.0.0", "--port", "8003"]

# Metadata-only final layer so a new commit retains cached OS, Python, UI and
# baked DuckDB-extension layers.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
ENV GIT_SHA=$GIT_SHA
