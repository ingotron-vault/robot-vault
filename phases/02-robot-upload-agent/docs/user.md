# Phase 02 — Robot upload agent user guide

This guide is for operators uploading robot recordings from the command-line
interface (CLI) or robot web UI.

## Two ways to trigger an upload
- CLI: run a command against one robot root.
- Web UI: select files or drain/retry a backlog.

There is no background scheduler or SSID authorization gate.

## Configure the robot and server
Create a shared JSON config with the robot identity and server address. For local
client/server Docker smoke testing, use `configs/vault.local.json` (server host
`ingest`, TLS off inside the local Compose network). For a robot connecting to
another host in a separately source-managed deployment, start from
`configs/vault.remote.json`: it requires mutual Transport Layer Security (mTLS)
and has separate paths for the certificate authority (CA), robot certificate,
and robot private key. Replace its
placeholder robot ID and hostname, and make sure the hostname is covered by the
ingest server certificate. In a release deployment, the robot's own client
certificate must contain a uniform resource identifier (URI) subject
alternative name (SAN)
`spiffe://robot-vault/robots/<percent-encoded-robot-id>` matching that
placeholder's replacement; the shared ingest token is still required too.
Before the first upload, enroll that identity with ingest's authenticated robot
registry endpoint. The exact Compose command and release guidance are in the
[usage guide](usage.md#enroll-the-robot-identity).

This remote JSON profile is not the robot onboarding flow of a deployed Robot
Vault. For a deployed vault, use the **Robots** page to add and enroll the
robot; copy the generated `/robot` command to the robot host. The vault-issued
enrollment credential is deliberately distinct from the source-managed mTLS
files in this template.

## Commands
- Show status and queue (Docker client/server smoke example):
```
rua status --root /robot_data/rover-7 --config configs/vault.local.json
```
- Start an upload:
```
rua upload --root /robot_data/rover-7 --config configs/vault.local.json
```
- Validate configuration:
```
rua config validate --config configs/vault.local.json
```

The agent retries transient failures in process with bounded backoff. Files that
remain failed are retried on the next CLI/UI trigger. Large-file retries reuse saved server parts .
Expired sessions and small uploads can require the whole file
to be sent again. Files and History show server-saved bytes separately from
bytes sent in the current attempt.

When several files are selected, a malformed manifest or other expected
file-preparation error is reported for that file and healthy files continue.
The CLI exits nonzero if any selected file fails; an empty queue or intentional
no-work skip is not an upload failure. `bandwidth_mbps=1` allows
131,072 payload bytes per second across workers in one invocation (binary
Mibit/s); separate CLI processes do not share a cap. A transport payload can
be larger than the one-second allowance and is sent after reserving its full
cost, so the setting controls aggregate pace rather than an instantaneous
wire-rate ceiling.

## Docker (local client/server smoke)
- See `run-and-test.md` for the current local client/server smoke workflow.
- The agent resolves the server at `ingest:50051` via Docker DNS.
- Shared `/tmp/robot_data` is mounted; point `--root` there in examples.
