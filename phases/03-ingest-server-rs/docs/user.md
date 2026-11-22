# Phase 03 — Ingest server user guide

This guide is for operators checking robot uploads. The ingest server receives
files from the Phase 02 agent over gRPC, checks their SHA-256 hashes, stores the
bytes in the object store (S3 API), and records each upload in PostgreSQL. It limits
concurrent uploads per robot and reserves an upload ID before writing data, so
a reused ID cannot overwrite a different file. Identical uploads may share
stored bytes, but each keeps its own manifest.

To use it:

1. Start the development server. Transport Layer Security (TLS) is optional
   locally; release requires mutual TLS (mTLS) and a bearer token.
2. Set each robot's server host and port.
3. Check arrivals and status in the central query UI or the query API.
   Ingest does not serve upload listings or an HTML admin page.

What you’ll see:
- Health check says it’s running.
- The query service lists uploads, their files and the robot registry.
- Add robots on the Robots page. On a development stack only, you can also
  register a shared-token robot with
  `POST /admin/robots?robot_id=<id>&name=<name>`. Servers with the `token` or
  `mtls` profile refuse this route.
- A `/metrics` page exposes basic counters for monitoring (Prometheus format).

When to use it:
- Any time robots have files ready to upload.
- It works with the existing robot agent without changing the agent.

Robot-side configuration (which server to talk to, host/port, TLS on/off) lives
in the shared vault JSON. For the local client/server smoke path, use
`configs/vault.local.json`, which points robots to `ingest` with TLS disabled;
Compose also starts the PostgreSQL, object-store and bootstrap dependencies. For an off-box release
managed outside the vault's enrollment flow, start from
`configs/vault.remote.json`, not the legacy one-way
`configs/vault.json`. The server is configured separately by environment (see
the technical doc).

`vault.remote.json` is the source-managed mTLS profile: its CA, client
certificate, and private key are provisioned by that deployment's operator. It
does not enroll a robot into a vault. With the default `token` profile, use the
Robots page and run the generated install command instead; that flow issues the
vault's one-use enrollment code and binds the robot's own credential to it.

With `INGEST_SECURITY_PROFILE=mtls`, each robot also needs its own client
certificate, set up by hand. Its
URI subject-alternative-name must be
`spiffe://robot-vault/robots/<percent-encoded-robot-id>` and must match the
configured `robot_id`. The shared ingest token is still required as a second
credential. Development mode does not require this per-robot SAN contract.
