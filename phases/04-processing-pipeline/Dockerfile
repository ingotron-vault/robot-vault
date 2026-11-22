# Core processor image: MCAP upload -> integrity -> Parquet.
#
# This image holds only core code. The Pro processor image adds its own parts
# on top of this one (`FROM` this image's tag or digest); that Dockerfile is not
# part of the core source.

# =============================================================================
# Stage 1 -- Python 3.12 runtime closure, built without ROS apt packages
# =============================================================================
#
# CDR decoding is supplied by mcap-ros2-support's schema-driven, pure-Python
# DecoderFactory. It consumes the ros2msg definition embedded in each MCAP,
# so rclpy, generated ROS message packages, linux-libc-dev and compiler
# headers are neither required nor shipped.
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289 AS python-build

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv "$VIRTUAL_ENV"

COPY phases/04-processing-pipeline/pyproject.toml /app/pyproject.toml
# The hash-pinned closure of phase 04's pyproject.toml dependencies.
# `make lock-python-processor` resolves with Noble Python 3.12, matching this
# stage; never hand-edit the generated lock.
COPY phases/04-processing-pipeline/requirements.lock.txt /app/requirements.lock.txt
COPY phases/04-processing-pipeline/src /app/src
# The shared cross-phase vocabulary (UploadStatus + the canonical
# ROS 2 topic normalization). `processor.supported_types` re-exports it, so
# this package has to be inside THIS image too — the processor and the query
# service are separate images with separate installs, and the whole point of
# the shared module is defeated if only one of them ships it.
COPY phases/common-config /app/common-config

# Install the hash-verified closure, then the non-editable first-party
# distributions. pip/setuptools/wheel are upgraded only in this builder stage;
# build-only setuptools/wheel are removed before the venv crosses the stage
# boundary, then the imports and entrypoint are proved.
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
 && python -m pip install --no-cache-dir -r /app/requirements.lock.txt \
 && python -m pip install --no-cache-dir --no-deps /app/common-config \
 && python -m pip install --no-cache-dir --no-deps . \
 && python -m pip uninstall -y setuptools wheel \
 && python -c "import mcap_ros2.decoder, processor.cli, robot_vault_config" \
 && processor --help >/dev/null

# The mcap and mcap-ros2-support wheels carry no licence file. Put the upstream text (repository root
# at the release tag's commit, sha256-pinned) where PEP 639 puts licence files:
# <dist>.dist-info/licenses/. The folder names are exact, so a version bump
# fails here until the pinned texts are reviewed for the new release.
ADD --checksum=sha256:da11235665c17d4c1634072dae92b8ba1b38d6fdde2ccf19a6bbede33253f58d \
    https://raw.githubusercontent.com/foxglove/mcap/b33fa682a5c517b1d213faeabd118e0b4f9d9d93/LICENSE /licence-texts/mcap-LICENSE
ADD --checksum=sha256:da11235665c17d4c1634072dae92b8ba1b38d6fdde2ccf19a6bbede33253f58d \
    https://raw.githubusercontent.com/foxglove/mcap/b13eb43cec62fd812689759a54eadcb6a0c64bfd/LICENSE /licence-texts/mcap-ros2-support-LICENSE
RUN test -d /opt/venv/lib/python3.12/site-packages/mcap-1.4.0.dist-info \
 && test -d /opt/venv/lib/python3.12/site-packages/mcap_ros2_support-0.5.7.dist-info \
 && install -D -m 0644 /licence-texts/mcap-LICENSE /opt/venv/lib/python3.12/site-packages/mcap-1.4.0.dist-info/licenses/LICENSE \
 && install -D -m 0644 /licence-texts/mcap-ros2-support-LICENSE /opt/venv/lib/python3.12/site-packages/mcap_ros2_support-0.5.7.dist-info/licenses/LICENSE

# =============================================================================
# Stage 2 -- shipped minimal processor runtime
# =============================================================================
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY LICENSE /usr/share/licenses/robot-vault/LICENSE
RUN apt-get update -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=python-build /opt/venv /opt/venv

# No ROS headers/compiler toolchain belongs in the shipped dynamic-decoder
# runtime. These assertions detect a base or package-list regression at build
# time, before the strict vulnerability gate sees the image.
RUN ! dpkg-query -W -f='${db:Status-Abbrev}' linux-libc-dev 2>/dev/null \
 && ! command -v gcc \
 && ! command -v g++ \
 && ! command -v make \
 && python -c "import mcap_ros2.decoder, processor.cli, robot_vault_config" \
 && processor --help >/dev/null

EXPOSE 8000
CMD ["python3", "-m", "processor.cli", "--watch", "--interval", "5"]

# Release processor containers run with a numeric non-root identity. Their
# staging tree is mounted at this owned directory rather than written into
# the immutable image layer or a memory-backed /tmp.
RUN groupadd --gid 10001 robotvault \
 && useradd --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin robotvault \
 && install -d -o 10001 -g 10001 -m 0750 /var/lib/robot-vault/processor

# Metadata-only final layer so release provenance does not invalidate the
# hash-locked Python dependency closure.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA
