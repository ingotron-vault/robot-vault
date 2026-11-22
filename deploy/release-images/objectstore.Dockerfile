# Robot Vault object-store image: the pinned upstream RustFS plus a patched OpenSSL.
#
# Why this file exists: rustfs/rustfs:1.0.6 (Alpine) ships libcrypto3 and libssl3
# 3.5.9-r0, which have four High CVEs that 3.5.9-r1 fixes. The rustfs
# binary is static (musl) and does not link OpenSSL; apk, curl and the entrypoint
# tools do. This layer changes only those two packages. The user, the entrypoint,
# the command and the healthcheck come from the base image unchanged.
#
# The base is the digest frozen in development/objectstore/CONTRACT.md section 1
# (RustFS 1.0.6, tag commit de9009d8, Apache-2.0). A new base digest or a new
# libcrypto3/libssl3 pin needs the whole gate (development/objectstore-conformance/run.sh, twice).
#
# Redistribution: this image carries RustFS, so it ships RustFS's Apache-2.0
# LICENSE at the tag commit (sha256 pinned). RustFS has no NOTICE file at that commit.
FROM rustfs/rustfs:1.0.6@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a

USER root
# shortcut: the exact apk versions are served only while Alpine's current repository
# carries them; when apk cannot find 3.5.9-r1, bump both pins and rerun the gate.
RUN apk add --no-cache libcrypto3=3.5.9-r1 libssl3=3.5.9-r1 \
 && mkdir -p /usr/share/licenses/rustfs && chmod 0755 /usr/share/licenses/rustfs
ADD --chmod=0644 --checksum=sha256:8ef87773cd6df917c9cf75bdb3075d92b78ad4cdf8e51d6c98030292442524d8 \
    https://raw.githubusercontent.com/rustfs/rustfs/de9009d882644effc2628e777650025c3d8e7c65/LICENSE /usr/share/licenses/rustfs/LICENSE
USER rustfs

ARG GIT_SHA=unknown
ARG RELEASE_VERSION=unknown
ARG RELEASE_SOURCE_URL=https://github.com/ingotron-vault/robot-vault
ARG RELEASE_VENDOR=Ingotron
LABEL org.opencontainers.image.revision=$GIT_SHA \
      org.opencontainers.image.version=$RELEASE_VERSION \
      org.opencontainers.image.source=$RELEASE_SOURCE_URL \
      org.opencontainers.image.vendor=$RELEASE_VENDOR \
      org.opencontainers.image.base.name="rustfs/rustfs:1.0.6"
