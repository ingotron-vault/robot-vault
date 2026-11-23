# Robot Vault release toolkit image (core).
#
# Replaces the broader development image (root Dockerfile) for every
# non-service job the core release topology needs:
#
#   * db-migrate               python3 -m db.migrate
#   * postgres-roles-ready     python3 -m release.bootstrap_postgres_roles
#   * gc                    sh tools/gc_runner.sh
#   * robot console         uvicorn robot_upload_agent.web:app (port 8090)
#   * robot-side CLIs       rsm / rua
#   * objectstore-access-ready python3 -m release.bootstrap_objectstore
#                              (the bucket and the five service users; the
#                              policies are in /release/objectstore-policies)
#   * backup and restore    rclone 1.76.0 (the object store itself, RustFS, is
#                           a separate upstream image and is not in this one)
#
# Contains ONLY those inputs — no docs, tests, or unrelated
# source: core uses the operator's own OIDC and TLS. This file just assembles
# the runtime.

# --- Stage 0: rclone, the backup and restore client ---------------------------
# The binary comes from the upstream
# image, pinned by its multi-arch index digest (rclone 1.76.0, released
# 2026-11-06, MIT). The image carries no licence text, so the upstream COPYING
# at the release tag is added and checked by SHA-256 (see stage 3).
FROM rclone/rclone:1.76.0@sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466 AS rclone-src

# --- Stage 1: robot console SPA ---------------------------------------------
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS ui-build
WORKDIR /repo
COPY package.json package-lock.json /repo/
COPY phases/ui-kit /repo/phases/ui-kit
COPY phases/02-robot-upload-agent/src/ui-spa /repo/phases/02-robot-upload-agent/src/ui-spa
# npm ci skips workspace dirs that are absent (phases/05's SPA is not needed
# here) — same pattern as the query image's ui-build stage.
RUN npm ci --no-audit --no-fund
WORKDIR /repo/phases/02-robot-upload-agent/src/ui-spa
# vite.config.ts builds in-tree to ../robot_upload_agent/ui with sourcemaps off.
RUN npm run build

# --- Stage 2: Noble Python 3.12 build closure -------------------------------
#
# The public Python slim variants were all non-clean under the recorded Grype
# database. Ubuntu 24.04 and its distro-managed Python 3.12 are the base;
# use that compatible interpreter and move only a tested venv into the runtime.
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289 AS python-build

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv "$VIRTUAL_ENV"

WORKDIR /app
# Hash-pinned closure of only what the toolkit jobs import (migrate, GC, the
# robot console, rsm / rua, the gRPC stubs). Not the development lock: that one
# also carries sim_gen's Pillow, rosbags and numpy, which the toolkit jobs do not
# need. Lock: `make lock-python-toolkit`.
COPY phases/02-robot-upload-agent/toolkit.lock.txt /app/requirements.lock.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.lock.txt

# The grpcio-tools wheel carries no licence file. Put the upstream text
# (repository root at the release tag's commit, sha256-pinned) where PEP 639
# puts licence files: <dist>.dist-info/licenses/. The folder name is exact, so
# a version bump fails here until the pinned texts are reviewed for the new
# release.
ADD --checksum=sha256:58c86a3f2b5cca0ebdf9ed3e7f4e6dc38e3b38796d8c3dc92064c62a4e39d0dd \
    https://raw.githubusercontent.com/grpc/grpc/c876f4da50f7da2f331888b88b2a7243514139fe/LICENSE /licence-texts/grpcio-tools-LICENSE
ADD --checksum=sha256:693ff28ec216d5112ac1bbfe64ef539005867d1c7bd427b57d579683293b947f \
    https://raw.githubusercontent.com/grpc/grpc/c876f4da50f7da2f331888b88b2a7243514139fe/NOTICE.txt /licence-texts/grpcio-tools-NOTICE.txt
RUN test -d /opt/venv/lib/python3.12/site-packages/grpcio_tools-1.83.0.dist-info \
 && install -D -m 0644 /licence-texts/grpcio-tools-LICENSE /opt/venv/lib/python3.12/site-packages/grpcio_tools-1.83.0.dist-info/licenses/LICENSE \
 && install -D -m 0644 /licence-texts/grpcio-tools-NOTICE.txt /opt/venv/lib/python3.12/site-packages/grpcio_tools-1.83.0.dist-info/licenses/NOTICE.txt

# First-party packages are installed non-editable into the venv. The runtime
# consequently has neither their build directories nor editable-link tooling.
COPY phases/common-config /app/build/common-config
COPY phases/01-robot-storage /app/build/01-robot-storage
COPY phases/02-robot-upload-agent /app/build/02-robot-upload-agent
COPY proto /app/build/proto
RUN python -m pip install --no-cache-dir --no-deps \
      /app/build/common-config /app/build/01-robot-storage /app/build/02-robot-upload-agent \
 && mkdir -p /opt/proto-gen \
 && python -m grpc_tools.protoc -I /app/build/proto \
      --python_out=/opt/proto-gen --grpc_python_out=/opt/proto-gen \
      /app/build/proto/upload.proto

# setuptools and wheel never serve a toolkit process. Remove those vulnerable
# build-only artifacts before copying the venv, then smoke both installed
# imports and the console entrypoints that Compose/operators invoke.
RUN python -m pip uninstall -y setuptools wheel \
 && python -c "import robot_storage_manager.cli, robot_upload_agent.web, robot_vault_config" \
 && rsm --help >/dev/null \
 && rua --help >/dev/null

# --- Stage 3: shipped Noble runtime -----------------------------------------
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY LICENSE /usr/share/licenses/robot-vault/LICENSE

# OpenSSL is a runtime dependency of the one-shot TLS certificate issuance. Keep its
# major-version assertion in the shipped stage, rather than assuming a base
# image happens to provide the CLI.
RUN apt-get update -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
      openssl ca-certificates curl python3 \
 && rm -rf /var/lib/apt/lists/* \
 && openssl version \
 && openssl version | awk '{split($2, v, "."); if (v[1] < 3) { print "openssl >= 3.0 required, found " $2 > "/dev/stderr"; exit 1 } }'
COPY --from=python-build /opt/venv /opt/venv
COPY --from=python-build /opt/proto-gen /opt/proto-gen
COPY --from=rclone-src /usr/local/bin/rclone /usr/bin/rclone
# rclone is MIT: the licence text must travel with the binary.
ADD --chmod=0644 --checksum=sha256:8cd2e9e750b90a04b7d82dbbca3930c696ae0309d7c10464f90a44f45754cd04 \
    https://raw.githubusercontent.com/rclone/rclone/e982ff36668f5130d94b4f70409babb3d964df42/COPYING /usr/share/licenses/rclone/COPYING
RUN /usr/bin/rclone version

# The built robot-console SPA, at the path web.py serves it from
# (Path(robot_upload_agent.__file__).parent / "ui").
COPY --from=ui-build /repo/phases/02-robot-upload-agent/src/robot_upload_agent/ui \
     /opt/venv/lib/python3.12/site-packages/robot_upload_agent/ui

# Job inputs.
COPY db/migrate.py /app/db/migrate.py
COPY db/migrations /app/db/migrations
COPY tools/s3_gc.py /app/tools/s3_gc.py
COPY configs/gc.yaml /app/configs/gc.yaml
COPY development/release/bootstrap_postgres_roles.py /app/release/bootstrap_postgres_roles.py
# The object-store bootstrap and its five policies, so the release one-shot
# needs no bind mount. bootstrap.py reads /release/objectstore-policies by default.
COPY development/objectstore/bootstrap.py /app/release/bootstrap_objectstore.py
COPY development/release/objectstore-policies /release/objectstore-policies
# The mode is applied with a plain chmod rather than `COPY --chmod`, which
# needs BuildKit; this works on either builder.
RUN printf '#!/bin/sh\nexec python3 -m tools.s3_gc\n' > /app/tools/gc_runner.sh \
 && chmod 0555 /app/tools/gc_runner.sh

# Exercise the exact module entrypoints that the Compose services and runner
# scripts call.
RUN set -e; \
    python3 -m release.bootstrap_postgres_roles --help >/dev/null; \
    test "$(ls /release/objectstore-policies | wc -l)" -eq 5; \
    env -i PATH="$PATH" python3 -m release.bootstrap_objectstore 2>&1 | grep -F 'S3_ENDPOINT is required'; \
    set +e; \
    migrate_output=$(env -u DATABASE_URL python3 -m db.migrate 2>&1); migrate_rc=$?; \
    gc_output=$(DATABASE_URL=postgresql://unused S3_BUCKET=unused GC_DRY_RUN=invalid sh /app/tools/gc_runner.sh 2>&1); gc_rc=$?; \
    set -e; \
    [ "$migrate_rc" -eq 1 ]; \
    printf '%s\n' "$migrate_output" | grep -Fx 'ERROR: DATABASE_URL is not set'; \
    [ "$gc_rc" -eq 2 ]; \
    printf '%s\n' "$gc_output" | grep -Fx 'gc_config_error: GC_DRY_RUN must be exactly true or false'

# The generated gRPC stubs live in /opt/proto-gen.
ENV PYTHONPATH=/opt/proto-gen

# The same fixed unprivileged identity every first-party image carries.
RUN groupadd --gid 10001 robotvault \
 && useradd --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin robotvault

EXPOSE 8090
CMD ["sleep", "infinity"]

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
ARG RELEASE_SOURCE_URL=https://github.com/ingotron-vault/robot-vault
LABEL org.opencontainers.image.source=$RELEASE_SOURCE_URL
