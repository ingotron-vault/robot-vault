# Third-party notices: Robot Vault core

Robot Vault core is licensed under the GNU Affero General Public License,
version 3 only (the file [`LICENSE`](LICENSE)). It includes and uses software
written by other people. Each component named here is licensed to you under
its own licence, not under the AGPL of Robot Vault. Nothing in the AGPL limits
the rights that a component's own licence gives you.

This file covers the four core container images (`robot-vault-ingest`,
`robot-vault-core-processor`, `robot-vault-core-query` and
`robot-vault-toolkit`), the robot agent bundle, the source tree, and the
third-party images that the core Compose files pull. The Pro edition ships its own notices file. The short notices that
licences ask us to repeat are in `NOTICE`.

**Where the licence texts are.** The licence text of Robot Vault core is in each
image at `/usr/share/licenses/robot-vault/LICENSE`. Most Python packages carry
their own licence files in `site-packages/<name>-<version>.dist-info/`. Debian and
Ubuntu packages carry theirs in `/usr/share/doc/<package>/copyright`. rclone
carries its licence in `/usr/share/licenses/rclone/COPYING` in the toolkit image;
RustFS carries its licence in `/usr/share/licenses/rustfs/LICENSE` in the object-store image.
Some Python packages publish no licence file in their wheels; for `mcap`,
`mcap-ros2-support` and `grpcio-tools` the toolkit image adds the upstream text
in `site-packages/<name>-<version>.dist-info/licenses/`, and the MIT text of
`mcap` is also below. The licences of the fonts and icons are served by the web
application at `/ui/licenses/`.

**Limits of this file.** This file names the principal components and the
licences that need explanation. It is not the list of every package. The list
of every package of an image is its SBOM, which is generated from the exact
image that is released. Licence names in an SBOM are what the packages declare;
they are not a legal conclusion.

---

## rclone (MIT), in the toolkit image

The `robot-vault-toolkit` image contains rclone 1.76.0 (`/usr/bin/rclone`), the
tool that the backup and restore commands use to copy objects between the
object store and a folder. We copy the unmodified binary from the publisher's
image and do not change the source.

| | rclone |
|---|---|
| Version | 1.76.0 (released 2026-11-06) |
| Binary source | `rclone/rclone:1.76.0@sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466` (pinned in `deploy/release-images/toolkit.Dockerfile`) |
| Source code | <https://github.com/rclone/rclone/tree/v1.76.0> (commit `e982ff36668f5130d94b4f70409babb3d964df42`) |
| Licence | MIT, Copyright (C) 2012 by Nick Craig-Wood |
| Copy of the licence in the image | `/usr/share/licenses/rclone/COPYING` (upstream file at the commit above; the build checks its SHA-256, `8cd2e9e750b90a04b7d82dbbca3930c696ae0309d7c10464f90a44f45754cd04`) |

The binary statically links the Go standard library and Go modules that are
listed in the `go.mod` file of that source tree and in the image SBOM. Each
module is under its own licence. rclone runs as a separate process; Robot Vault
code does not link it.

## RustFS (Apache-2.0), in the object-store image

The `robot-vault-objectstore` image is the publisher's RustFS image with two
packages upgraded: the OpenSSL libraries `libcrypto3` and `libssl3` (3.5.9-r1,
a security fix). We do not change the RustFS source or binary. The image is
built by `deploy/release-images/objectstore.Dockerfile`.

| | RustFS |
|---|---|
| Version | 1.0.6 (released 2026-11-15) |
| Base image | `rustfs/rustfs:1.0.6@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a` (pinned in the Dockerfile) |
| Source code | <https://github.com/rustfs/rustfs/tree/1.0.6> (commit `de9009d882644effc2628e777650025c3d8e7c65`) |
| Licence | Apache-2.0 |
| Copy of the licence in the image | `/usr/share/licenses/rustfs/LICENSE` (upstream file at the commit above; the build checks its SHA-256, `8ef87773cd6df917c9cf75bdb3075d92b78ad4cdf8e51d6c98030292442524d8`). RustFS has no `NOTICE` file at that commit. |

The base image is Alpine Linux; its packages stay under their own licences
(`/lib/apk/db/installed` lists them). RustFS runs as its own container and talks to
Robot Vault over the S3 network protocol only: the programs are separate works that
sit side by side, not one combined work.

## Written offer for source code

This offer covers every component that these notices name under the GPL, the
LGPL or the AGPL, in the form in which it is in an image or release that we
distribute.

We use these components unmodified, from Ubuntu 24.04 or from the Python
Package Index. Their source is published by those projects (links below).

If you received an image or release from us and you want the complete
corresponding source code of any such component on a physical medium, or you
cannot get it from the places above, write to **hello@ingotron.com**. We will
send it to you for the cost of the medium and its shipping. This offer is valid
for three years from the date on which you received the image or release, and
it is valid for any third party who received it from you.

---

## Base operating-system packages (Ubuntu 24.04)

Each core image is built on `ubuntu:24.04` (pinned by digest in the Dockerfile),
and the processor, query and toolkit images use the distribution's Python 3.12.
The images contain the standard libraries and tools of the distribution
(the GNU C library, `bash`, `coreutils`, OpenSSL, `ca-certificates`, `zlib` and
similar) under a mixture of licences that includes GPL-2.0, GPL-3.0, LGPL-2.1,
LGPL-3.0, MIT, BSD, Apache-2.0 and the Perl and Python licences. The packages
are redistributed as published, without change. Each package's licence text is
at `/usr/share/doc/<package>/copyright`. The source of every package is
published by the distribution:

- Ubuntu source packages: <https://launchpad.net/ubuntu>
- Debian source packages: <https://sources.debian.org/>

## LGPL and MPL components

- **psycopg and psycopg-binary 3.3.4** (LGPL-3.0-only), the PostgreSQL adapter
  for Python, in the processor, query and toolkit images. We use it unmodified,
  installed from the publisher's wheels, and Robot Vault loads it with
  `import psycopg`. You can replace it with a modified version of your own.
  Source: <https://github.com/psycopg/psycopg>.
- **certifi** (MPL-2.0), the CA bundle for Python, in the query and toolkit
  images. Unmodified. Source: <https://github.com/certifi/python-certifi>.

## Python packages

The processor, query and toolkit images install packages from the Python
Package Index with exact versions and hashes (`requirements.lock.txt` files).
Principal runtime components:

| Package | Licence |
|---|---|
| FastAPI, Starlette, uvicorn | MIT, BSD-3-Clause, BSD-3-Clause |
| pydantic, pydantic-core | MIT |
| DuckDB | MIT |
| PyArrow | Apache-2.0 |
| NumPy | BSD-3-Clause |
| boto3, botocore, s3transfer | Apache-2.0 |
| psycopg, psycopg-binary | LGPL-3.0-only (see above) |
| httpx, httpcore | BSD-3-Clause |
| PyJWT, cryptography | MIT; Apache-2.0 OR BSD-3-Clause |
| prometheus-client | Apache-2.0 AND BSD-2-Clause |
| grpcio, grpcio-tools, protobuf | Apache-2.0, Apache-2.0, BSD-3-Clause |
| mcap, mcap-ros2-support | MIT (text below) |
| zstandard, lz4 | BSD-3-Clause |
| certifi | MPL-2.0 (see above) |

**MCP server dependencies.** The query image contains the Model Context
Protocol server of Robot Vault core (`query_service/mcp/`). Its additional
packages, with the licence that each declares:

| Package | Version | Licence |
|---|---|---|
| mcp, mcp-types | 2.3.0 | MIT |
| httpx2, httpcore2 | 2.14.0 | BSD-3-Clause |
| truststore | 0.10.5 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| referencing | 0.37.0 | MIT |
| rpds-py | 0.30.0 | MIT |
| attrs | 26.1.0 | MIT |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| python-multipart | 0.0.32 | Apache-2.0 |
| sse-starlette | 3.5.1 | BSD-3-Clause |

The table is a selection. The closure authority is the lock file of each image
and the image SBOM.

**MIT licence of `mcap` and `mcap-ros2-support`.** The wheels of these two
packages carry no licence file. The text of the upstream repository
(<https://github.com/foxglove/mcap>, file `LICENSE` at the release tags
`releases/python/mcap/v1.4.0` and `releases/python/mcap-ros2-support/v0.5.7`):

```text
MIT License

Copyright (c) Foxglove Technologies Inc

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

`grpcio-tools` (toolkit image) is under the licence of the gRPC project
(Apache-2.0); its wheel also carries no licence file. The toolkit image adds the
gRPC `LICENSE` and `NOTICE.txt` of tag `v1.83.0`.

## Robot agent bundle

The robot agent (`agent-linux-amd64.tar.gz`) is under the AGPL of Robot Vault
core and carries this file. It contains its own CPython 3.10.18 runtime and the
exact Linux x86-64 wheels that it is frozen from, unmodified. Its root has
`AGENT_DEPENDENCIES.lock.txt` (exact pins and wheel SHA-256),
`AGENT_DEPENDENCY_SBOM.cdx.json` (a CycloneDX list of these dependencies) and
the publisher licence and notice files in `THIRD_PARTY_LICENSES/`, including the
CPython licence and the bootloader exception of PyInstaller.

## Rust crates (`robot-vault-ingest`)

The ingest server is a Rust program that links its dependencies statically:
about 340 crates for the Linux x86-64 target, listed with exact versions and
checksums in `phases/03-ingest-server-rs/Cargo.lock`. Principal crates: Tokio,
Hyper, axum, tonic, prost, Serde, tracing, rustls, chrono, uuid, the AWS SDK for
Rust, `tokio-postgres`, `deadpool-postgres`, `metrics`, `sha2`, `x509-parser`,
`dashmap` and `parking_lot`. Their licences are declared in the crates (mostly
`MIT OR Apache-2.0`); copyright notices and licence texts are published with
each crate at <https://crates.io> and in its repository.

## JavaScript and browser assets

The query image serves a single-page web application. Only the compiled
bundles are in the image; the build tools and `node_modules` are not.
Components of the bundles:

| Component | Licence |
|---|---|
| React, React DOM, scheduler | MIT |
| React Router | MIT |
| TanStack Query | MIT |
| axios | MIT |
| Apache ECharts 6.1.0 | Apache-2.0 (see `NOTICE`) |
| ZRender 6.1.0 (rendering engine of ECharts) | BSD-3-Clause |
| echarts-for-react | MIT |
| lucide-react (icons) | ISC |
| tslib | 0BSD |
| Instrument Sans (self-hosted font) | OFL-1.1 |
| IBM Plex Mono (self-hosted font) | OFL-1.1 |
| Tailwind CSS (compiled into the shipped CSS) | MIT |

The fonts are used unmodified. Their licence texts and copyright lines are
served at `/ui/licenses/instrument-sans-OFL-1.1.txt`,
`/ui/licenses/ibm-plex-mono-OFL-1.1.txt` and `/ui/licenses/lucide-ISC.txt`.
The complete list of npm packages and their declared licences is in
`package-lock.json`.

## Example data

The folder `examples/` has data that is not under the AGPL. Its sources,
licences and the attributions that must stay with it are in `NOTICE`
and in the datacard next to each bag.

## Third-party container images that the Compose files pull

The Compose files pull the images below unmodified from their publishers. We do
not build derived images from them, and we do not redistribute them. Each stays
under its publisher's terms. The object store is not in this list: we build our
own image on RustFS and ship it (section "RustFS" above).

| Image | Component | Licence |
|---|---|---|
| `postgres:16.4-alpine` | PostgreSQL on Alpine Linux | PostgreSQL Licence; Alpine packages under their own licences |
| `valkey/valkey:8.1.11-alpine` | Valkey (Redis-protocol server), on Alpine Linux | BSD-3-Clause; Alpine packages under their own licences |
| `prom/prometheus:v2.55.1` (monitoring profile) | Prometheus | Apache-2.0 |
| `busybox:1.36.1` (development Compose file) | BusyBox | GPL-2.0-only |
| `node:20.19-bookworm-slim` (development Compose file) | Node.js on Debian | MIT and others; Debian packages under their own licences |

The Compose service is still named `redis`, and clients still use `redis://`
URLs: Valkey speaks the same protocol. Source: <https://github.com/valkey-io/valkey>.

---

## More information

For SBOMs, source code of the components above or other licence questions,
write to **hello@ingotron.com**.
