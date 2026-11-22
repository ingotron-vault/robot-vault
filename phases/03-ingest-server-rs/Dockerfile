# Build stage
# Bullseye's glibc is older than Noble's, so its GNU-target binary remains
# compatible with the Noble (Ubuntu 24.04) runtime while keeping Trixie out of both
# stages. This crate's OpenSSL closure is not configured for a musl cross-build.
FROM rust:1.94-bullseye@sha256:f4f82b80e5f2945fed4ba17af177c6d6be85d98cde38ff318fc7666ce4505617 AS builder
WORKDIR /app
# Static-quality gates run in this same pinned builder image, so their
# formatter and lint versions cannot drift from the compiler used to ship.
RUN rustup component add rustfmt clippy
# Install protoc for tonic-build
RUN apt-get update -y && apt-get install -y --no-install-recommends \
      protobuf-compiler libprotobuf-dev \
 && rm -rf /var/lib/apt/lists/*
# Copy crate files from repo root context
COPY phases/03-ingest-server-rs/Cargo.toml ./Cargo.toml
COPY phases/03-ingest-server-rs/Cargo.lock ./Cargo.lock
COPY phases/03-ingest-server-rs/build.rs ./build.rs
# The build script compiles this schema, so the dependency-cache build must
# have it too; otherwise an ignored warm-up failure silently resolves a second
# graph during the real build.
COPY proto ./proto
RUN mkdir -p src && echo 'fn main() {}' > src/main.rs
# Parallel compile jobs; empty keeps cargo's default of every CPU. Pass a
# bounded value on a shared build host.
ARG BUILD_JOBS
# Compile the exact reviewed lockfile closure. A cache-stage error is a build
# error, never something to mask and rediscover later.
RUN cargo build --release --locked ${BUILD_JOBS:+--jobs "$BUILD_JOBS"}
# Copy full source
COPY phases/03-ingest-server-rs/src ./src
# Build with proto
#
# `find src -exec touch {} +` first: COPY preserves the host mtimes of these
# files (git-checkout or git-archive time), which predate the fingerprint the
# warm-up build above recorded a few minutes INTO this same build. Cargo's
# staleness check is mtime-based, so on a cold Docker build cache -- no
# `--mount=type=cache` is used here, deliberately, so this stage has no state
# from outside this one build -- cargo sees no tracked file newer than the
# warm-up's fingerprint and silently reuses the stub `fn main() {}` binary
# instead of recompiling. `docker build` reports success either way, and the
# image would hold a non-functional ingest-server that exits 0 in <200ms with no
# output. The touch makes every source file newer than that fingerprint so
# cargo always recompiles here, regardless of the surrounding cache state.
RUN find src -exec touch {} + && cargo build --release --locked ${BUILD_JOBS:+--jobs "$BUILD_JOBS"}

# Runtime stage
# Ubuntu 24.04 runtime. Ingest needs only
# CA roots at runtime; its built-in health probe means the image needs no curl.
FROM ubuntu:24.04@sha256:9c7f200c07e3d4a03e7249d2c16bf66a51120fb876aa2242cf31451412543289
RUN apt-get update -y && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY LICENSE /usr/share/licenses/robot-vault/LICENSE
COPY --from=builder /app/target/release/ingest-server /usr/local/bin/ingest-server
# A new named volume inherits these permissions; only ingest receives it
# read-write in the release topology.
RUN groupadd --gid 10001 robotvault \
 && useradd --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin robotvault \
 && install -d -o 10001 -g 10001 -m 0750 /app/uploads
ENV STORAGE_ROOT=/app/uploads
# TLS defaults live in the binary's typed startup configuration. Release
# Compose supplies every TLS value explicitly; duplicating path defaults here
# only made image metadata look as though it contained a private-key secret.
EXPOSE 50051 8080
# Uses the application binary so the runtime does not carry curl or any other
# network client solely for health checks. `/readyz` still performs the S3 and
# Postgres readiness checks and must return 200 before Docker reports healthy.
HEALTHCHECK --interval=5s --timeout=10s --retries=30 CMD ["ingest-server", "--healthcheck"]
CMD ["ingest-server"]

# Metadata-only final layer so a new commit does not rebuild runtime packages.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA

# The release build sets org.opencontainers.image.version to the release
# version. Without this label, ingest ships with a revision but no version.
ARG RELEASE_VERSION=unknown
LABEL org.opencontainers.image.version=$RELEASE_VERSION
ARG RELEASE_SOURCE_URL=https://github.com/ingotron-vault/robot-vault
ARG RELEASE_VENDOR=Ingotron
LABEL org.opencontainers.image.source=$RELEASE_SOURCE_URL
LABEL org.opencontainers.image.vendor=$RELEASE_VENDOR
