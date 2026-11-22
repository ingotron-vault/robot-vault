# Operations: backup, restore, and upgrade

Use this runbook to back up and restore a Robot Vault stack, upgrade it, or
operate a release deployment. It is for operators with Docker and host access.
Run the commands from the repository root unless a section says otherwise.

Robot Vault has two systems of record. The development stack keeps them like
this:

| System       | Compose service | Where it lives                         | Container path             |
|--------------|-----------------|----------------------------------------|----------------------------|
| Postgres     | `postgres`      | host folder `./data/postgres`          | `/var/lib/postgresql/data` |
| Object store | `objectstore`   | Docker named volume `objectstore_data` | `/data`                    |

`./data/postgres` is gitignored (it is host state, not repo content). Neither
system is removed by `docker compose down`. `make stack-reset` destroys both —
see [Full reset](#full-reset-destroys-all-data) below. The development checkout
has no scheduled backup; run `make backup` before any risky operation
(upgrade, `stack-reset`, host migration). The release stack keeps both systems
in named volumes (see [Release state and ports](#release-state-and-ports)).

The object store is RustFS, a single-node, S3-compatible server. Its contract
(image, environment, health checks, client settings) is in
[`development/objectstore/CONTRACT.md`](../development/objectstore/CONTRACT.md).

Development credentials (see `configs/*.env` and `docker-compose.yml`):

- Postgres: user `robotvault`, password `robotvault`, database `robotvault`
  (`DATABASE_URL=postgres://robotvault:robotvault@postgres:5432/robotvault`).
  The development Postgres service fixes these values in Compose; changing
  only a shell or `.env` value does not rotate the stored database role.
- Object store: root user `OBJECTSTORE_ROOT_USER` (default `robot-vault-dev-root`),
  root password in the file `configs/objectstore-root-password.dev`
  (`OBJECTSTORE_ROOT_PASSWORD_FILE`), bucket `S3_BUCKET=robotics-data`
  (overridable before first startup). Services do not use the root key: the
  one-shot `objectstore-access-ready` creates the bucket and one key per
  service.
- Ingest: shared bearer token `INGEST_AUTH_TOKEN` (random per machine once
  `make secrets`/`make stack-up` has run; see `.env`)
- Robot provisioning: separate `INGEST_PROVISIONING_TOKEN` in release
  deployments. It must not equal the ingest or metrics token.

None of these are reachable from the host by default — Postgres
`5432`, the object store (`9002`) and ingest `50051/8080` are published only through
`deploy/docker-compose.debug.yml` (`make debug-up`, loopback-only), so the commands
below run *inside* the compose network wherever possible. For development,
rotate the ingest token by editing `.env` and recreating `ingest` and every
client (`robot`, and any fleet robot's config) together; a mismatch makes
uploads fail `UNAUTHENTICATED`. Database and object-store credential rotation
requires changing both the server-side credential and every matching client
setting. Release credentials follow the separate, validated procedure under
[Deploying a release](#deploying-a-release).

The development query/API port and Vite UI port bind to loopback through
`QUERY_BIND_HOST` and `UI_BIND_HOST`. Because both surfaces use the OIDC_DEV
issuer that can mint an administrator token, widening either host requires the
same explicit `ALLOW_UNSAFE_DEV_QUERY_EXPOSURE=1` acknowledgement. Compose
refuses startup without it. Use that override only on a trusted, firewalled
network; it is not a production access mode.

---

## Backup and restore

`make backup` and `make restore` save and restore both systems of record: the
Postgres database and the object-store bucket. They work on the dev stack, and
on the release stack when you add `RELEASE_ENV_FILE` (and `RELEASE_PROJECT` if
you changed it). The tool is `tools/robot_vault_backup.py`.

Requirements:

- Docker Compose and `python3` on the host.
- The toolkit image, which holds rclone and boto3: `make build-toolkit` for the
  dev stack (`robot-vault-toolkit:latest`); the release uses its own
  `robot-vault-toolkit:<VERSION>`. Set `BACKUP_TOOLKIT_IMAGE` to use another tag.
- Free disk space for a full copy of the bucket and the dump.

### Back up

```bash
make backup BACKUP_DIR=/srv/backups/$(date -u +%Y%m%dT%H%M%SZ)
make backup BACKUP_DIR=/srv/backups/$(date -u +%Y%m%dT%H%M%SZ) \
  RELEASE_ENV_FILE=/etc/robot-vault/release.env          # release stack
```

What it does:

1. Starts `postgres` and `objectstore` if they are not running.
2. Stops every other running service (the writers), so that the database and
   the bucket match. It starts them again at the end, also when the backup
   fails.
3. Dumps Postgres with `pg_dump -Fc` into `postgres.dump`.
4. Copies the bucket with `rclone copy`, then compares every object byte with
   `rclone check --download`. It refuses the backup if the bucket changes
   while it runs, or if an object key cannot be stored as a file (for example
   a folder marker that ends with `/`).
5. Writes `objectstore-manifest.jsonl`: one line per object with its key, size,
   SHA-256, content type and all user metadata (for example
   `rdv-resume-session`). The manifest, not file attributes, carries the
   metadata, so you can copy or archive the folder with any tool.
6. Writes `recovery-point.json` last (bucket name, size and SHA-256 of the dump
   and the manifest), then renames `BACKUP_DIR.partial` to `BACKUP_DIR`. A
   failed backup removes its `.partial` folder and publishes nothing.

`BACKUP_DIR` must not exist yet. The folder holds the object store's data in
clear text: keep it on protected storage, and copy it off the host.

Backup metrics. Each `make backup` writes `robot-vault-backup.prom` in
`BACKUP_METRICS_DIR` for the release metrics collector. The default is
`/var/lib/robot-vault/backup-metrics` with `RELEASE_ENV_FILE`, and none on the
dev stack. The folder must exist. The file has exactly three lines, in Unix
seconds (0 means never):

```text
robot_vault_backup_last_success_timestamp_seconds 1795353600
robot_vault_backup_last_failure_timestamp_seconds 0
robot_vault_backup_last_run_success 1
```

Each run rewrites all three lines and keeps the other timestamp from the
previous file. The tool writes a temporary file and renames it, so a reader
never sees half a file. If it cannot write the file, it prints a warning and
the exit code of the backup does not change.

### Check a backup

```bash
python3 tools/robot_vault_backup.py verify /srv/backups/20261122T120000Z
```

This is the restore preflight on its own. It reads only the folder.

### Restore

**Restore replaces the database and the bucket with the backup.** Rows and
objects written after the backup are removed.

```bash
make restore BACKUP_DIR=/srv/backups/20261122T120000Z
make restore BACKUP_DIR=/srv/backups/20261122T120000Z \
  RELEASE_ENV_FILE=/etc/robot-vault/release.env          # release stack
```

What it does:

1. **Preflight.** It checks the whole recovery point before it changes
   anything: the record, the size and SHA-256 of the dump and the manifest,
   every manifest line (fields, unique keys, safe keys with no `..` or
   absolute paths), and exact coverage (every manifest object has its file,
   there is no extra file, and every size and SHA-256 matches). If one check
   fails, it stops with a nonzero exit code and the stack, the database and
   the bucket are unchanged.
2. Stops every service, then starts only `postgres` and `objectstore`. On a
   new host or an empty store volume it creates the bucket.
3. Uploads every object with its content type and user metadata, then reads
   every object back and compares size, content type, metadata and SHA-256
   with the manifest. Only after that does it delete the keys that are not in
   the backup.
4. Restores Postgres with `pg_restore --clean --if-exists --single-transaction`.
   A failed restore leaves the database as it was.

The other services stay stopped, also after a failure. If the restore fails
after the preflight, fix the cause and run the same `make restore` again. When
it succeeds, start the stack: `make stack-up` (dev) or `make release-up
RELEASE_ENV_FILE=...` (release). Startup runs the database migrations, which
bring an older dump up to the current schema. On a new host, the
`objectstore-access-ready` one-shot recreates the service keys at startup.

---

## Upgrade procedure

### Schema changes (migrations)

Development and release share one schema path. Postgres starts empty and the
ordered migration runner (`db/migrate.py`) applies `db/migrations/001_baseline.sql`
and any later files before application services start. There is no separate entrypoint SQL
copy to drift from the checksum ledger.

- Add a new `db/migrations/NNN_description.sql` file (numeric prefix,
  applied in filename order).
- Apply it with:

  ```bash
  make db-migrate
  ```

  (or directly on the Compose network:
  `docker compose run --rm -e DATABASE_URL=postgres://robotvault:robotvault@postgres:5432/robotvault dev python db/migrate.py`)

- The runner tracks applied versions in a `schema_version` table and is
  idempotent — running it again applies nothing new. It also runs
  automatically as the one-shot `db-migrate` compose service, which
  `ingest`, `processor`, and `query` all `depends_on` (`service_completed_successfully`)
  before they start — so `make stack-up` / `docker compose up` always
  applies pending migrations before app services come up.

**Back up before applying a migration** (`make backup`, above) — the runner
applies each migration in its own transaction, but there's no automatic
rollback across the whole run.

### Python dependency locks

Every first-party Python image (`dev`, `processor`, `query`) installs its third-party
packages from a hash-pinned `requirements*.lock.txt` next to that image's
Dockerfile, not straight from `requirements-dev.txt`/`pyproject.toml`
ranges — see the `COPY .../requirements*.lock.txt` step in each Dockerfile.
The floors still live in `requirements-dev.txt` and each phase's
`pyproject.toml`; the lock is only what those floors resolve to *today*,
frozen with sha256 hashes so a rebuild months from now installs byte-identical
packages instead of whatever the index happens to serve then.

| Image | Lock file | Python | Regenerate with |
|---|---|---|---|
| `dev` (+ `db-migrate`/`robot`/`seed`/`gc`) | `requirements-dev.lock.txt` | 3.12 | `make lock-python-dev` |
| `processor` | `phases/04-processing-pipeline/requirements.lock.txt` | 3.12 | `make lock-python-processor` |
| `query` | `phases/05-query-service/requirements.lock.txt` | 3.12 | `make lock-python-query` |
| core `toolkit` | `phases/02-robot-upload-agent/toolkit.lock.txt` (floors: `toolkit-requirements.txt`; versions follow the dev lock) | 3.12 | `make lock-python-toolkit` |

Pro adds images with their own locks (see the Pro documentation).

**When to regenerate:** after editing a version floor (adding/removing/
bumping a dependency in `requirements-dev.txt` or a phase's
`pyproject.toml`) — never hand-edit a `.lock.txt`. Run `make lock-python` to
regenerate all of them at once (each inside a container matching that image's
base, so the resolution can't silently diverge from what actually ships),
then rebuild the affected images (`make build-all`) and
re-run the test suites before committing the updated lock file alongside the
floor change.

**Security bumps:** `make dep-audit` checks every lock file for known
vulnerabilities (see "Source dependency audit" in `docs/TESTING.md`). To move
one package to a fixed version without changing a floor, run the image's lock
command with `-P <package>==<fixed version>` added. uv keeps every other pin.

### Application image upgrades (ingest / processor / query / dev)

1. **Back up** Postgres and the object store with `make backup` (above).
2. Pull/rebuild the updated images: `make build-all` (or `docker compose pull` for
   pinned upstream images).
3. Restart with the existing `./data`: `make stack-down` then `make stack-up`
   (do **not** run `stack-reset` — that destroys `./data`). Pending
   migrations apply automatically as part of `stack-up` via `db-migrate`.
4. Verify health: `docker compose exec -T ingest curl -fsS http://localhost:8080/healthz`
   (ingest — its admin port is not published to the host),
   `curl http://localhost:8003/readyz` (query), `docker compose ps`.
5. Rollback: if the upgrade misbehaves, `docker compose down`, restore the
   pre-upgrade image tags, and if schema/data was also affected, restore
   from the backup taken in step 1.

### Continuous integration

`.github/workflows/core-ci.yml` runs the core checks on GitHub-hosted runners
for pull requests and for pushes to `main`. It uses a read-only token and no
secrets. The local entry point is:

```bash
make ci-local
```

That runs the self-contained repository checks and the disposable live GC
integration. `make test-gc-integration-config` verifies the isolated GC
topology without starting Docker; `make test-gc-integration` uses a unique
project and test-only PostgreSQL and object-store state.

`make test-workflows` semantically checks every tracked GitHub workflow with a
version-and-digest-pinned actionlint image, including YAML structure,
expressions, and job dependencies. Its first run may need to pull that image;
an offline host must already have the exact digest cached. The validator itself
runs with container networking disabled. The target fails if it is unavailable
and never substitutes a weaker parse while claiming success.

### Postgres image upgrades

The compose file pins `postgres:16.4-alpine`. Postgres keeps its on-disk format
compatible within a major line, but still read the target image's release
notes, take a backup, and rehearse the restart before a minor-version bump.
**Do not** bump to a new Postgres major
version (e.g. 17.x) by simply changing the image tag — the on-disk data
format is incompatible across major versions and the container will fail to
start. To move major versions: take a backup with `make backup` (above), point
`./data/postgres` at an empty directory (or delete/relocate it), start the
new major-version image fresh, then run `make restore` with the same
`BACKUP_DIR`.

### Object-store image upgrades

The object store is the first-party image `robot-vault-objectstore`, built by
[`objectstore.Dockerfile`](../deploy/release-images/objectstore.Dockerfile) from
a pinned upstream RustFS digest plus patched OpenSSL packages. Do not change the
base digest or the OpenSSL pins on their own. A new pin needs a new version of
[`development/objectstore/CONTRACT.md`](../development/objectstore/CONTRACT.md)
and a full run of the conformance gate (`make test-objectstore-conformance`,
twice). Before you deploy the new image, take a backup with `make backup` and
rehearse `make restore` on a spare host.

---

## Release procedure

Core releases follow these rules:

- Each release tag is the exact source of the published
  `robot-vault-core-*` image digests, including the build scripts.
- `release/core-images.lock.json` records the digests and the source commit.
  Deploy by digest.
- `EDITION=core` is the default. This repository's release topology runs
  only first-party core images: `robot-vault-toolkit`, `robot-vault-ingest`,
  `robot-vault-core-processor`, `robot-vault-core-query` and the object store
  `robot-vault-objectstore` (see
  [`development/objectstore/CONTRACT.md`](../development/objectstore/CONTRACT.md)).
- Pro images are delivered to Pro customers separately. `EDITION=pro` needs
  the Pro release overlay, which adds the exporter and the Pro processor and query images; only the
  Pro distribution has it, and the preflight refuses `EDITION=pro` without it
  (Pro). Pro also adds the upgrade command that moves a core deployment to
  Pro (see the Pro documentation).

`docker-compose.yml` is the **development** stack: it builds from the working
tree, bind-mounts `.:/app` into five services, and runs `:latest` images.
`deploy/docker-compose.release.yml` is the **deployment** topology and is a different
contract — no `build:`, no source mounts, no debug ports, local-only versioned
first-party tags, and third-party images pinned by sha256 digest. Read its
header before your first release; it documents every choice.

### Versioning

The `VERSION` file is the single source of truth. Everything below reads it;
nothing in this procedure hardcodes the current release number.

### Getting the release images

`release-up` runs only local images tagged with the version in `VERSION`.
Each image must either carry this checkout's commit as its
`org.opencontainers.image.revision` label, or have the digest that
`release/core-images.lock.json` names. Otherwise `release-up` stops with
`core-images: refusing to deploy:` and the image name.

Get the images in one of two ways:

- **Build them from this clean checkout:** `make build-release-local`, then
  `make release-tag`. `release-tag` refuses a dirty working tree and an image
  whose revision label is not this commit.
- **Pull the published digests:** `make core-images-pull`. It reads
  `release/core-images.lock.json` (`version`, `source_commit` and one
  `<repo>@sha256:<digest>` per image), pulls each image by digest, checks the
  digest, and tags it `<name>:<VERSION>`. It refuses a lock whose version is
  not `VERSION`, or that is not filled in yet (the lock in a source checkout
  between releases has `null` values).

### Deploying a release

```bash
# Required -- create this outside the repository with mode 0600. Make refuses
# every release operation without it. Make supplies the image version and
# edition gates; this file carries the deployment-specific credentials,
# identities, hosts, and policy inputs.
sudo install -d -m 0750 /etc/robot-vault
sudo touch /etc/robot-vault/release.env
sudo chown "$USER":"$(id -gn)" /etc/robot-vault/release.env
sudo chmod 0600 /etc/robot-vault/release.env
# Create one host group for private material mounted into the fixed non-root
# release UID (10001). Record its numeric GID in release.env below. Add the
# deployment operator so `make release-config` can perform the OpenSSL checks,
# then start a new login session for group membership to take effect.
sudo groupadd --system robot-vault-secrets
sudo usermod -aG robot-vault-secrets "$USER"
sudo chown root:robot-vault-secrets /etc/robot-vault
getent group robot-vault-secrets

# Install query and ingest server identities whose SANs cover their public DNS
# names. Each leaf must carry an explicit DNS SAN, allow TLS server
# authentication and remain valid for at least seven days. The issuing CA must
# also remain valid for at least seven days. Private keys remain root-owned,
# mode 0640, and readable only by robot-vault-secrets; certificates and public
# CA bundles are mode 0644 so the non-root services (including Prometheus) can
# read them. Existing ACME-managed paths are usable only when they expose this
# exact group/mode contract. The current three-file ingest contract requires
# its server leaf to chain to the same private deployment CA bundle that is
# mounted as the robot trust anchor (`INGEST_TLS_CA_FILE`, below).
sudo install -d -o root -g robot-vault-secrets -m 0750 /etc/robot-vault/tls
sudo install -m 0644 /path/to/fullchain.pem /etc/robot-vault/tls/query-fullchain.pem
sudo install -o root -g robot-vault-secrets -m 0640 /path/to/privkey.pem /etc/robot-vault/tls/query-key.pem
# Use the host's public-root bundle, or install the enterprise/private CA chain
# that issued the query certificate, so release Prometheus can verify it.
sudo install -m 0644 /path/to/query-trust-ca.pem /etc/robot-vault/tls/query-ca.pem
sudo install -m 0644 /path/to/ingest-fullchain.pem /etc/robot-vault/tls/ingest-fullchain.pem
sudo install -o root -g robot-vault-secrets -m 0640 /path/to/ingest-privkey.pem /etc/robot-vault/tls/ingest-key.pem
# The one root CA certificate that issued the ingest server certificate. The
# file must hold exactly one certificate, a self-signed root (CA:TRUE); a bundle
# or an intermediate-only file fails the preflight. Robots pin it, and query
# serves it at /ca.crt. Public certificate only: never copy the CA private key
# onto the server.
sudo install -m 0644 /path/to/ingest-ca.pem /etc/robot-vault/tls/ingest-ca.pem
# Only with INGEST_SECURITY_PROFILE=mtls (see "Robot security profiles"): issue
# one distinct client-auth leaf per robot from that CA. Each leaf must contain
# exactly one URI SAN whose final path segment is the RFC-3986-percent-encoded
# configured robot_id; CN is ignored. For robot_id `rover-7`, the certificate
# extension is:
#   extendedKeyUsage=clientAuth
#   subjectAltName=URI:spiffe://robot-vault/robots/rover-7
# A certificate issued for one robot cannot upload metadata naming another.
# Edit /etc/robot-vault/release.env and set:
#   POSTGRES_PASSWORD=...
#   DATABASE_URL=postgresql://robotvault:<same-password-percent-encoded>@postgres:5432/robotvault
#   INGEST_DATABASE_URL=postgresql://robot_vault_ingest:<distinct-encoded-password>@postgres:5432/robotvault
#   PROCESSOR_DATABASE_URL=postgresql://robot_vault_processor:<distinct-encoded-password>@postgres:5432/robotvault
#   EXPORTER_DATABASE_URL=postgresql://robot_vault_exporter:<distinct-encoded-password>@postgres:5432/robotvault
#   QUERY_DATABASE_URL=postgresql://robot_vault_query:<distinct-encoded-password>@postgres:5432/robotvault
#   GC_DATABASE_URL=postgresql://robot_vault_gc:<distinct-encoded-password>@postgres:5432/robotvault
#   OBJECTSTORE_ROOT_USER=...   # root access key of the object store
#   OBJECTSTORE_ROOT_PASSWORD=...
#   INGEST_S3_SECRET_KEY=...    # access id is fixed: robot-vault-ingest
#   PROCESSOR_S3_SECRET_KEY=... # fixed: robot-vault-processor
#   EXPORTER_S3_SECRET_KEY=...  # fixed: robot-vault-exporter
#   QUERY_S3_SECRET_KEY=...     # fixed: robot-vault-query
#   GC_S3_SECRET_KEY=...        # fixed: robot-vault-gc
#   INGEST_AUTH_TOKEN=...  # >=32 visible ASCII chars; `openssl rand -hex 32`
#   INGEST_PROVISIONING_TOKEN=... # different >=32-char secret; `openssl rand -hex 32`
#   METRICS_AUTH_TOKEN=... # third distinct >=32-char secret; `openssl rand -hex 32`
#   ROBOT_VAULT_SECRETS_GID=<numeric GID printed by getent above>
#   INGEST_PUBLIC_HOST=ingest.example.com
#   INGEST_TLS_CERT_FILE=/etc/robot-vault/tls/ingest-fullchain.pem
#   INGEST_TLS_KEY_FILE=/etc/robot-vault/tls/ingest-key.pem
#   INGEST_TLS_CA_FILE=/etc/robot-vault/tls/ingest-ca.pem
#   # Your identity provider (any OIDC provider; the URLs below are examples):
#   OIDC_ISSUER=https://identity.example/realms/robot-vault
#   OIDC_AUDIENCE=robot-vault-release
#   OIDC_JWKS_URL=https://identity.example/realms/robot-vault/protocol/openid-connect/certs
#   OIDC_CLIENT_ID=robot-vault-browser
#   OIDC_AUTHORIZATION_URL=https://identity.example/realms/robot-vault/protocol/openid-connect/auth
#   OIDC_TOKEN_URL=https://identity.example/realms/robot-vault/protocol/openid-connect/token
#   QUERY_PUBLIC_HOST=vault.example.com
#   QUERY_TLS_CERT_FILE=/etc/robot-vault/tls/query-fullchain.pem
#   QUERY_TLS_KEY_FILE=/etc/robot-vault/tls/query-key.pem
#   QUERY_TLS_CA_FILE=/etc/robot-vault/tls/query-ca.pem
# Optional:
#   INGEST_SECURITY_PROFILE=token   # token (default) or mtls
#   OIDC_ROLE_CLAIM=roles           # the token claim that holds the role (default roles)
#   BACKUP_METRICS_DIR=/var/lib/robot-vault/backup-metrics   # pass it to make too
#   OIDC_END_SESSION_URL=https://identity.example/realms/robot-vault/protocol/openid-connect/logout
#   OIDC_SCOPES=openid profile email offline_access
sudoedit /etc/robot-vault/release.env

make release-up   RELEASE_ENV_FILE=/etc/robot-vault/release.env   # EDITION=core is the default
make release-ps   RELEASE_ENV_FILE=/etc/robot-vault/release.env
make release-down RELEASE_ENV_FILE=/etc/robot-vault/release.env
# release-down keeps volumes; add -v manually to a reviewed Compose command to destroy them
```

Pro needs a dataset-signing key (see the Pro documentation).
`EDITION=core` does not need it.

The core release file keeps `EXPORTER_DATABASE_URL` and
`EXPORTER_S3_SECRET_KEY` as required inputs, also in core. The bootstrap
creates that database role and that object-store user, so a later move to Pro finds
them. Core runs no exporter.

Every database URL is explicit because a raw password cannot safely be inserted
into URI userinfo. Percent-encode the user, password and database components
(for example, `@` becomes `%40`, `/` becomes `%2F`, and `%` becomes `%25`). The
preflight strictly decodes every URI, requires the owner to match the rendered
Postgres bootstrap role, requires the five fixed runtime usernames above, and
requires every URI to target the same database at exactly `postgres:5432`.
Runtime and owner passwords must all be distinct; malformed escapes, external
hosts, queries/fragments or credential mismatches abort before container
creation.
Changing `POSTGRES_PASSWORD` in this file does not rotate a role stored in an
existing `pg_data` volume: alter the database role first, then update both the
raw password and owner URI. Runtime-role rotation is idempotent: update the
corresponding runtime URI and run `release-up`; `postgres-roles-ready` replaces
that role's SCRAM password and verifies the least-privilege ACL matrix before
any runtime starts. The runtime-role membership set is authoritative too: each
run removes every grant where a fixed runtime role is either the member or the
granted role, including grants recorded under another grantor and their
dependent membership chains. Runtime services therefore cannot inherit or
assume a stale parent role, and no stale user or service can assume a runtime
identity. Do not grant these fixed roles to humans or other services; the next
bootstrap will revoke that membership. Object-store runtime keys rotate the same way
through `objectstore-access-ready`, which authoritatively recreates each fixed access
id and reattaches exactly one prefix policy.

`EDITION` drives all three edition gates together — the query service's router
set, the compose files (core, or core plus the Pro overlay that carries the
exporter), and the processor's
`SEGMENTATION_ENABLED`/`QA_ENABLED`/`MEDIA_ENCODE_ENABLED`. Never set them
independently. Confirm what you got:

```bash
curl -fsS https://vault.example.com/api/v1/edition    # {"edition":"core", ...}
make release-ps RELEASE_ENV_FILE=/etc/robot-vault/release.env \
  | grep exporter                                      # no rows in core
```

On a pro-to-core redeploy, `release-up` runs `up` with `--remove-orphans`.
The exporter is not in the core files, so Compose removes it. `release-down`
activates every profile (`--profile '*'`) while it tears down, so a mistaken
`EDITION` argument cannot leave the exporter or Prometheus running on the
project network.

### What release-up checks

`release-up` runs these steps in order. Each step stops the release on a
failure.

1. **Preflight.** It validates `RELEASE_ENV_FILE` and the rendered Compose
   configuration for the edition, before it inspects an image or creates a
   container.
2. **Clean tree and images.** It refuses a dirty working tree, and images
   that are neither built from this commit nor the digests of the core image
   lock (see [Getting the release images](#getting-the-release-images)).
3. **Volume guard.** It compares every existing data volume of the project
   (labels, driver and driver options) with the configuration. If one
   differs, Compose would recreate the volume and delete its data. It then
   stops with:

   ```text
   REFUSING: existing data volumes differ from the release configuration, so compose would recreate them and delete their data:
     robot-vault-release_pg_data: label <name>
   Nothing was changed. Restore the previous configuration, or back up the volume and migrate it by hand.
   ```

   It names the volume and the field, never a value, and changes nothing.
   A Docker Compose upgrade that changes Compose's own volume labels causes
   the same refusal. Do not delete the volume to get past it: back up first
   (see [Backup and restore](#backup-and-restore)), then move the data to a new volume by hand.
4. **`up` with no prompt.** `development/release/compose_up.sh` runs
   `docker compose up -d --remove-orphans` with standard input from
   `/dev/null`, so Compose can never wait for an answer and nobody can answer
   "yes" to a volume recreation. If Compose still asks to recreate a volume,
   the release stops with:

   ```text
   REFUSING: compose found a data volume that differs from its configuration and asked to recreate it (its data would be lost). It kept the volume (the answer from /dev/null is no); the release stops here. Compare 'docker volume inspect' with the compose files.
   ```

   Compare `docker volume inspect <volume>` with the Compose files, and fix
   the configuration. `RELEASE_UP_TIMEOUT` (seconds, default 900) limits this
   step. When it runs out, `release-up` fails with
   `release-up: compose up did not finish within <n>s`.
5. **Readiness and identity.** `wait_release_ready.py` waits for the services
   and one-shots (next paragraph). Then it reads four identity sources: the
   query API (`/api/v1/edition`), the processor's JSON startup line, the
   exporter's startup line (Pro), and the `rv-build` tag of the served UI. It
   prints them on one line:

   ```text
   release identity: {"processor": {...}, "query api": {...}, "query ui": {...}}
   ```

   Each source reports `edition`, `core_version`, `adapter_api` and
   `pro_version`. Every source must name the requested edition, all sources
   must agree on the versions, and in core no source may report a Pro
   version. On a mismatch it prints
   `release identity MISMATCH: <problem>` and `release-up` fails. Typical
   causes: an image from another release, or a core UI image with a Pro
   backend.

Release query configuration always sets `ROBOT_VAULT_RELEASE=true`,
`AUTH_MODE=oidc`, `OIDC_DEV=false`, `OIDC_ALGORITHMS=RS256`, and
`OIDC_ROLE_SOURCE=claim`.
The issuer, audience, JWKS URL, public-client id and browser authorization/token
endpoints flow from `RELEASE_ENV_FILE` through Compose interpolation into the
query container. Each browser endpoint must be an explicit HTTPS URL without
embedded credentials or a fragment; providers may use different trusted
origins. The CSP permits token exchange only with the configured token URL's
origin. The local development token endpoints therefore stay disabled without
changing development-stack behavior.

The release publishes exactly two sockets: query HTTPS (`443` by default) and
ingest TLS gRPC (`50051` by default). Query has no plaintext listener or HTTP
redirect. Ingest admin HTTP `8080`, metrics and every datastore remain private
to the Compose network. Release ingest always sets `TLS_ENABLED=true`; the Rust
process refuses to start in release mode without TLS, a non-development bearer
token, and one of the two security profiles below. A robot must verify
`INGEST_PUBLIC_HOST` against the server certificate. Development certificates and
plaintext local workflows keep their existing behavior.

### First administrator

Robot Vault has no user table. Roles come only from
the access token your identity provider issues. The query service reads the
claim named by `OIDC_ROLE_CLAIM` (default `roles`) and keeps the values
`viewer`, `operator` and `admin`. Whoever your provider gives the value `admin`
in that claim is an administrator. A signed-in user with no role sees
`403` responses and an empty app.

Make your provider put the literal value in the claim:

| Provider | How |
|---|---|
| Microsoft Entra ID | Create an app role with the value `admin` in the app registration, assign it to the user or group. Entra puts it in the `roles` claim. |
| Keycloak | Create a client role `admin`, assign it to the user, and add a client-role mapper that writes the roles to a token claim named `roles` (access token). |
| Okta | Add a custom claim named `roles` to the authorization server with a value expression that returns `admin` for the group of administrators. |

The values `viewer` and `operator` work the same way. If your provider uses
another claim name, set `OIDC_ROLE_CLAIM` in the release env file.

`GET /readyz` on query checks your provider: it fetches and validates the keys at
`OIDC_JWKS_URL`. A wrong URL or an untrusted provider certificate fails
`make release-up` at install time (`"oidc": false` in the readiness output).
Users who are signed in keep working on cached keys. The key cache lasts 300
seconds and a 60-second stale grace follows it. If the provider is down for
longer, `/readyz` returns `503` and Docker marks query unhealthy; nothing
restarts it. Ingest and robot uploads do not depend on query. When the provider
returns, readiness recovers on the next probe.

### Robot security profiles

`INGEST_SECURITY_PROFILE` in the release env file selects how robots prove who
they are. Ingest derives the mTLS setting from it; do not set `TLS_MTLS`.

| Profile | Robot authentication | Robot setup |
|---|---|---|
| `token` (default) | TLS server authentication, plus a per-robot bearer credential (`ignr1.`). The shared `INGEST_AUTH_TOKEN` is refused on the data plane. | One command from the Robots page ([Add a robot](#add-a-robot)). |
| `mtls` | TLS with a client certificate, plus the bearer credential. The certificate leaf must have one URI SAN `spiffe://robot-vault/robots/<percent-encoded-robot-id>` matching the configured `robot_id`; a missing, malformed, ambiguous, CN-only or cross-robot identity is rejected before registry lookup or storage. | By hand, with certificates you issue. The one-line command does not support `mtls`: the robot has no client certificate when it enrolls. |

Any other value fails the preflight. `INGEST_TLS_CA_FILE` is the same file in
both profiles. It must hold exactly one root CA certificate that issued the
ingest server certificate. Robots pin it. Query serves it without
authentication at `GET /ca.crt`. Under `mtls` ingest also trusts it for client
certificates.

The `token` credential is 256 bits of random data, stored only as a SHA-256
hash. It has an expiry, rotates itself, and you can revoke it from the Robots
page; revocation and disabling take effect within about 30 seconds. The
server takes the robot id from the credential, never from request metadata.
Choose `mtls` when your policy needs unauthenticated clients to be unable to
reach the gRPC parser, and you run your own PKI. Never copy the CA private key
onto the server.

### Add a robot

In `token` profile, an administrator adds a robot in the Robots page and gets
an install command. Query serves everything the robot needs, so there is no
outside download site:

- `GET /robot`: the install script `robot.sh`.
- `GET /robot/agent-linux-amd64.tar.gz`: the robot agent.
- `GET /ca.crt`: the ingest CA certificate.

None of these needs authentication. The robot checks each one before it trusts
it: the script by its SHA-256 in the command, the CA certificate against the pin
inside the enrollment code, and the agent by the size and SHA-256 written into
the script at build time.

1. Open **Robots**, add the robot, and mint an enrollment code. The page shows
   the install command. It has this shape (the digest, host names and code are
   yours):

   ```bash
   f=$(mktemp) && curl -fsSk --proto =https https://vault.example.com/robot -o "$f" \
     && echo "<sha256>  $f" | sha256sum -c - \
     && sudo sh "$f" --vault https://vault.example.com --ingest ingest.example.com:50051 --code ign1.…
   ```

   The command is not `curl … | sh`: a pipe cannot check the digest. Query builds
   the command from `PUBLIC_BASE_URL` (default `https://$QUERY_PUBLIC_HOST`,
   which must be a plain `https://host[:port]`). If query cannot build it, the
   page builds one from its own address. With no installer digest (the query
   image has no `robot.sh`) the page shows only the code and no command. The
   code is single-use and short-lived.
2. Run the command on the robot as root. The robot needs OpenSSL 1.1.1 or
   newer (Ubuntu 20.04 and Debian 11 qualify) and `curl`. The agent keeps its
   own names (`ingotron-agent`, `/opt/ingotron-agent`, `/etc/ingotron`).
3. To enroll the robot again (for example after you replaced its disk), use
   **Enroll** on the existing robot; the page shows a re-enroll command.
4. **Decommissioning is final.** A decommissioned robot cannot return. To use
   the hardware again, add it as a new robot and enroll it. Old uploads stay
   with the old robot id.

If your front door is not on port 443, set `PUBLIC_BASE_URL` in the release env
file. The ingest address in the command comes from `INGEST_PUBLIC_HOST` and the
published ingest port (`INGEST_GRPC_PORT`, default `50051`).

### Upgrade the agent

The vault serves exactly one agent version: the one built into its query image.
After you upgrade the vault, the Robots page shows an **Upgrade the agent**
command (also returned by `GET /api/admin/robots/bootstrap`). Run it on each
robot. It is the same digest-checked download with `--upgrade`. An upgrade to a
newer version replaces the agent; the same version is a no-op; a version lower
than the installed one is refused.

The same agent files are available as release assets for robots that cannot
reach the vault: `packaging/robot-agent/release-assets.sh <query image> <outdir>`
copies `robot.sh` and `agent-linux-amd64.tar.gz` out of the built query image
and writes `SHA256SUMS`. They are the same bytes the vaults serve.

`make release-up` does not declare success after one curl. It waits for healthy
Postgres, the object store, Redis, ingest, processor, query, GC and (in pro) exporter, plus
successful completion of all three one-shots: `db-migrate`,
`postgres-roles-ready`, and `objectstore-access-ready`. An EXIT trap removes those
secret-bearing bootstrap containers on success, readiness/edition failure, and
HUP/INT/TERM, so owner/root/runtime credentials are not retained in stopped
container metadata. Cleanup failure makes an otherwise successful `release-up`
fail; run `make release-down` or the reported Compose cleanup command before
retrying. Ingest `/readyz` performs both an S3 write/delete probe and `SELECT 1`;
query `/readyz` performs a bounded JWKS warm-up and a bounded Postgres
`SELECT 1`. Pro adds its own readiness checks (see the Pro documentation).
The same-container query probe skips certificate identity because loopback
cannot match the public SAN; external clients and release Prometheus perform
normal CA/hostname verification.

Every first-party release process, including both database one-shots, runs as
numeric UID/GID `10001:10001`, with
all Linux capabilities dropped, `no-new-privileges`, and an immutable root
filesystem. `/tmp` is a 64 MiB `noexec,nosuid,nodev` tmpfs. Large writable
state is explicit instead: ingest uses `uploads`; the processor work files and
DuckDB spill files use private named work volumes. TLS mounts stay read-only. Postgres, Redis, and Prometheus retain
their upstream image entrypoint/user contracts. The object store runs the
first-party `robot-vault-objectstore` image with its own non-root user.
`objectstore-access-ready` runs the release toolkit image with only six
reviewed, read-only Compose configs (the bootstrap script and five JSON
policies), rather than applying a generic policy that could corrupt
initialization or state.

Every release service also has a hard memory, CPU, and PID ceiling, and every
container uses bounded `json-file` rotation. The defaults are sized as safety
ceilings for a single release host, not capacity promises. Before production,
measure processor/exporter/query peak RSS under representative data and lower
or raise the corresponding `<SERVICE>_MEMORY_LIMIT`, `<SERVICE>_CPU_LIMIT`, and
`<SERVICE>_PIDS_LIMIT` values deliberately. Keep the sum of realistic working
sets below host RAM so the kernel does not turn concurrent limit pressure into
a host-wide outage. `ROBOT_VAULT_LOG_MAX_SIZE` and
`ROBOT_VAULT_LOG_MAX_FILES` control retained container logs.

The optional Prometheus profile retains at most 30 days or 25 GB by default,
whichever limit is reached first. Override `PROMETHEUS_RETENTION_TIME` and
`PROMETHEUS_RETENTION_SIZE` only after checking free space on the Docker volume.
Tracked rules raise active alerts for unavailable targets, sustained query 5xx
responses, query saturation and deadlines, capacity rejections, export dead
letters, upload commit failures, GC failures, a Redis rate-limit fallback, low
volume space, a processing or export backlog older than one hour, missing,
stale or failed backups, expiring public certificates, and failed or stale
operational metric collection. The UI can read
active alerts from Prometheus, but Prometheus alone does not notify a human.
`configs/prometheus/alertmanager.yml` is an inert routing template with separate
critical and warning receivers. Add an operator-owned delivery integration,
run Alertmanager outside this composition, set the validated
`ALERTMANAGER_ENDPOINT` for release Prometheus, and verify a routed alert
before go-live. Run `make test-observability-config` after every edit; it checks
rule syntax and behavior, the routing template, the collector, and the
shipped-metric contract.

### Release operational signals

The optional **release** `obs` profile starts `ops-roles-ready` and
`ops-metrics` before Prometheus. `ops-metrics` listens only inside the Compose
network on port 9110 and uses a dedicated `robot_vault_ops` login with SELECT
on `uploads`, `processed`, `processing_status`, and `export_status`. Its SQL
transaction is read-only and has a three-second statement timeout. No S3 key,
private key, Docker socket, or host state directory is mounted.

To opt in, generate a distinct long password and set `OPS_DATABASE_URL` in the
operator's private release env file to a percent-encoded URL for
`robot_vault_ops` on the same `postgres:5432` database as the other runtime
roles. Set `BACKUP_METRICS_DIR` to the dedicated directory that holds
`robot-vault-backup.prom` (default `/var/lib/robot-vault/backup-metrics`). Create
the directory before you run `make backup` and before you start `obs`; a typo
fails the read-only bind mount rather than creating an empty host directory.
`make backup` writes the file (see [Backup and restore](#backup-and-restore)). The
file has three samples, `robot_vault_backup_last_success_timestamp_seconds`,
`robot_vault_backup_last_failure_timestamp_seconds` and
`robot_vault_backup_last_run_success`. `make` does not read `BACKUP_METRICS_DIR`
from the release env file: if you change the default, pass the same value to
`make backup` and set it in the release env file. A vault where no backup has
run raises the backup alert on purpose; absence is an alert, not a successful zero.

After the ordinary release is ready, start the profile with the same project,
version, edition variables, and private env file used for release-up:

```bash
docker compose -p robot-vault-release --env-file /etc/robot-vault/release.env \
  -f deploy/docker-compose.release.yml --profile obs up -d ops-roles-ready ops-metrics prometheus
docker compose -p robot-vault-release --env-file /etc/robot-vault/release.env \
  -f deploy/docker-compose.release.yml --profile obs rm -sf ops-roles-ready
```

The second command removes the completed owner-credential one-shot from Docker
metadata after `ops-metrics` is healthy. `make release-config` validates the
role URL when supplied; `ops-roles-ready` refuses a missing or incorrect
`OPS_DATABASE_URL`. A release cut must include the toolkit image containing
`tools.operational_metrics`. Prometheus scrapes every 30 seconds with a
15-second timeout. The collector reports `collection_success{source=...}=0`
and omits the affected value samples when a read fails; a fresh zero backlog
is emitted only after a successful database query. Labels are fixed source,
queue, volume, and certificate names.

#### Disk and object capacity

`volume_available_bytes` and `volume_total_bytes` come from `statvfs` on
read-only mounts of the database, object store, uploads and processor work
volumes. The object-store value is the object-store volume's
filesystem headroom, not a bucket quota. Warning starts below 10% free for
ten minutes; critical starts below 5% for five minutes. Check the relevant
Docker volume's backing filesystem, retained exports, upload retention, and
Prometheus retention before expanding capacity or deleting data. A permission
or mount failure raises the collection-failure alert instead of reporting zero.

#### Backlog age

`backlog_age_seconds` is the oldest eligible processor or exporter item, with
the same pending/failed/expired-lease and attempt-limit predicates as their
claim queries. Processor eligibility also requires a completed verified upload
and no processed row. The release Compose file sends the same
`PROCESSOR_MAX_ATTEMPTS` and `EXPORTER_MAX_ATTEMPTS` values to workers and the
collector. The alert fires when an eligible item has waited over
one hour for 15 minutes. Inspect the processor or exporter logs, dead-letter
state, claim lease, and dependent Postgres and object-store health before retrying a job.
Core edition has no exporter queue sample.

#### Backup freshness

The collector reads only the three numeric samples in the atomic
`robot-vault-backup.prom` file that `make backup` writes. A valid file with
success timestamp zero means no completed backup yet; a missing, malformed,
unreadable, or future-dated file means collection failure. Alerts cover no
success, a success older than 26 hours, and a failed latest attempt. `make
backup` does not schedule itself: run it from your own scheduler (cron or a
systemd timer) at least daily. When an alert fires, read the output of the last
run, then take a new backup and check it with `python3 tools/robot_vault_backup.py
verify`. Do not treat a recent failed attempt as erasing an older successful
recovery point.

#### Certificate expiry

The collector parses every certificate in the mounted **public** ingest and
query chain and reports the earliest expiry. The alert starts 14 days before
expiry. Verify the replacement's SAN, issuer, and full chain using the TLS
preflight above. When an atomic file replacement changes the certificate
inode, restart ingest, query, **and ops-metrics** so all file bind mounts see
the new chain; then check the metric and an external TLS handshake. No private
key or broad certificate directory is mounted in the collector.

#### Collection failure

Each source publishes success and last-success time on every scrape. Failure
or no successful collection for three minutes alerts separately from storage,
backlog, backup, and certificate values. Check `ops-metrics` health and logs,
the four mounted sources, public certificate permissions, and the ops database
role. The service does not log database URLs or file contents. If the entire
target is unreachable, `RobotVaultTargetDown{job="ops"}` is the signal.

The checked-in Alertmanager file is still a routing template without an
external destination. Set optional `ALERTMANAGER_ENDPOINT` in the private
release env file to `https://hostname:port` to make release Prometheus forward
firing alerts to an operator-run Alertmanager. HTTPS uses normal certificate
verification; the hostname must be resolvable and trusted from the Prometheus
container. `http://hostname:port` is accepted only for a trusted private
network. Paths, credentials, and query parameters are rejected, and leaving
the value empty disables forwarding. The operator must add a real receiver to
the checked-in routing template (or their own reviewed Alertmanager config),
then send a test alert and verify delivery. A local isolated receiver test can
prove the route but does not establish external notification delivery.

> **`.env` warning.** A direct `docker compose` command without `--env-file`
> can auto-load the repo-root development `.env`. The supported Make workflow
> requires `RELEASE_ENV_FILE`, rejects group/world-readable files, and pins the
> Compose file/project/profile/env-file control variables so an inherited
> `COMPOSE_*` value cannot select a different topology. It also removes
> inherited credential, database, S3, OIDC, TLS and GC variables before
> invoking Compose; the reviewed release file, not an
> operator's stale shell export, is authoritative for those inputs. Every
> first-party service also sets `pull_policy: never`, and `release-up` verifies
> all four required local versioned images before starting anything.

### Release state and ports

Release state lives in **named Docker volumes** (`pg_data`, `objectstore_data`,
`uploads`, `processor_work` and `query_work`, plus
`prometheus_data` when the `obs` profile is enabled) under the
`robot-vault-release` project, not in `./data`. Back them up with `make backup`
and `make restore` and the release env file (see
[Backup and restore](#backup-and-restore)). `make stack-reset` does **not**
touch release volumes.

The release stack sets no `container_name:`, so it can run alongside a
developer's dev stack. Its two possible host-port collisions are moved with
`QUERY_HTTPS_PORT` and `INGEST_GRPC_PORT` (certificate names remain unchanged):

```bash
QUERY_HTTPS_PORT=8443 INGEST_GRPC_PORT=55051 make release-up \
  RELEASE_ENV_FILE=/etc/robot-vault/release.env
# Public URL: https://vault.example.com:8443/ui/
# Robot target: ingest.example.com:55051
```

---

## Full reset (DESTROYS ALL DATA)

`make stack-reset` **permanently deletes** `./data/postgres`, the
`objectstore_data` volume, and `./uploads` — both systems of record, gone, with no undo. It
requires an explicit confirmation flag:

```bash
make stack-reset CONFIRM=1
```

Running `make stack-reset` (without `CONFIRM=1`) aborts with a warning and
does nothing. `make stack-rebuild` also calls `stack-reset` internally and
therefore also requires `CONFIRM=1` (`make stack-rebuild CONFIRM=1`).

**Take a backup first** (`make backup`, above) if
the data has any value — `stack-reset` is meant for throwing away
a broken/dirty local dev environment, not as a general-purpose "fix it"
button in anything resembling production.

---

## Quick reference

```bash
# Backup (Postgres and the object store); add RELEASE_ENV_FILE=... for the release stack
make backup BACKUP_DIR=/srv/backups/$(date -u +%Y%m%dT%H%M%SZ)

# Restore (preflight first; the stack stays stopped afterwards)
make restore BACKUP_DIR=/srv/backups/20261122T120000Z

# Schema change
make db-migrate

# Release
make build-release-local && make release-tag    # or: make core-images-pull
make test-release-config
make release-config RELEASE_ENV_FILE=/etc/robot-vault/release.env
make release-up RELEASE_ENV_FILE=/etc/robot-vault/release.env
make release-down RELEASE_ENV_FILE=/etc/robot-vault/release.env

# Destructive reset (requires confirmation)
make stack-reset CONFIRM=1
```
