# Changelog

All notable changes to Robot Vault core are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/). The project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - unreleased

This is the first public release of Robot Vault core. It is licensed under
the GNU Affero General Public License, version 3 only (AGPL-3.0-only).

### Added

- Core covers authenticated upload, integrity checks, Parquet processing,
  the query API, and the Orbit UI. `ROBOT_VAULT_EDITION` defaults to `core`.
  The Pro edition is a paid upgrade that adds episodes, media, QA, dataset
  cuts, provenance signing, and export. In core, Pro pages show an
  "Available in Pro" panel.
- The Orbit UI: a left-sidebar layout that follows the data flow, with live
  counts from `/api/v1/metrics/overview`.
- The object store is RustFS (Apache-2.0) in a first-party image
  `robot-vault-objectstore`, with rclone in the toolkit. The names are
  `objectstore` and `OBJECTSTORE_*`.
- The release stack uses Valkey as its cache. Server images run Ubuntu 24.04
  and Python 3.12.
- Sign-in comes from the operator's OIDC provider. The first administrator is
  whoever that provider gives the admin role. `/readyz` checks the provider's
  signing keys.
- MCP server at `/mcp` (core tools; Pro adds its own tools), behind the
  operator's OIDC sign-in, with per-peer rate limits and an audit log.
- `make backup BACKUP_DIR=...` and `make restore BACKUP_DIR=...`: Postgres
  and the object store with a checksummed manifest of every object. Restore
  checks the whole backup before it changes anything, and leaves the services
  stopped. A backup writes `robot-vault-backup.prom` for the backup alerts.
- Each vault serves the robot bootstrap itself: `/robot`, the agent download
  and `/ca.crt`. The Robots page shows an install command that checks the
  script's SHA-256 before it runs, a re-enroll command, and an "Upgrade the
  agent" panel. The robot pins the vault's CA. It works without internet
  access.
- Robot security profiles `token` (default: a per-robot token over TLS
  pinned to the vault's CA) and `mtls`.
- Decommissioning a robot is final. To use the hardware again, add it as a
  new robot.
- Public project files: `CONTRIBUTING.md`, `CLA.md` (draft), `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, and `TRADEMARKS.md`.
