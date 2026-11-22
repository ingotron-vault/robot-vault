#!/usr/bin/env bash
# live_enrollment_gate.sh -- LIVE end-to-end enrollment gate against the REAL
# Rust ingest server (phases/03-ingest-server-rs), not a mock and not a unit
# test.  It:
#
#   1. builds and boots the real ingest server (Postgres + RustFS + TLS +
#      /admin/v2 provisioning API) in disposable Docker containers,
#   2. mints a real one-use enrollment code through the real minter,
#   3. proves the minter/verifier pin conformance gate: the pin
#      inside the minted code must equal robot.sh's own b32_prefix() applied
#      to the served CA,
#   4. runs the REAL robot-side client (ingotron_agent) against it to redeem
#      the code, obtain a per-robot `ignr1.` credential, and write vault.json,
#   5. proves the credential actually authenticates a real gRPC call and a
#      real file upload against the real server,
#   6. disables the robot and proves the SAME credential is then refused,
#
# i.e. it proves the per-robot credential works end-to-end against the landed
# server, not against a stand-in.
#
# ---------------------------------------------------------------------------
# A note on assertion 9's RPC choice (read this before "simplifying" it back
# to HealthCheck).
#
# UploadService.HealthCheck looks like the simplest call to prove that the
# per-robot token authenticates. Reading phases/03-ingest-server-rs/src/main.rs
# shows that it is not enough:
# `health_check()` (main.rs, the `impl UploadService for IngestSvc` block)
# takes `_request` -- unused -- and never calls `resolve_request_identity`.
# The gRPC interceptor wrapping UploadServiceServer only rejects a
# *malformed* bearer; a well-formed-but-unknown, well-formed-but-revoked, or
# well-formed-but-decommissioned `ignr1.` token is deferred to the handler
# (`InterceptorDecision::DeferToHandler`), and HealthCheck's handler does
# nothing with it. This script verified that live: a completely fabricated
# (never-issued) `ignr1.` token, well-formed but never seen by the server,
# gets `{"status":"healthy"}` from HealthCheck every time -- see the
# "[INFO] HealthCheck insufficiency" block below, which reproduces this on
# every run as a documented, non-gating finding.
#
# `ListUploads`, `GetUploadStatus` and `UploadSmall`/`Upload` all DO call
# `resolve_request_identity`, which looks the credential up in
# `robot_credentials` and requires the owning robot's state to be `enrolled`
# (robot_auth.rs `classify_credential_row`). `ListUploads` needs no S3
# to exercise that path, so assertions 9 and 10 use it as the credential-proof
# RPC. Assertion 11 goes further and drives a REAL file upload (UploadSmall,
# via the real rsm/rua CLIs the shipped agent runs), which is the strongest
# evidence available and is why this script also stands up the object store (RustFS) for real.
# ---------------------------------------------------------------------------
#
# Flags:
#   --keep         Skip teardown of containers/network/tmpdir for debugging.
#   --skip-build   Skip building the dev and ingest images and pulling the object
#                  store image; all must already exist locally or the script
#                  blocks loudly.
#   -h, --help     Print this help and exit.
#
# Exit status: 0 iff every one of assertions 1-11 PASSed. Non-zero otherwise,
# including when the run was blocked before some assertions could execute
# (those print as BLOCKED in the summary, never silently skipped).

set -euo pipefail

# ---------------------------------------------------------------------------
# Bookkeeping declared before anything else so `set -u` never trips on it,
# even if we abort during early preflight.
# ---------------------------------------------------------------------------
declare -A RESULT_STATUS=()
declare -A RESULT_TITLE=()
declare -A RESULT_DETAIL=()
ASSERTION_NUMS="1 2 3 4 5 6 7 8 9 10 11"
SUMMARY_PRINTED=0

pass_assertion() { # num title detail
    RESULT_STATUS["$1"]="PASS"
    RESULT_TITLE["$1"]="$2"
    RESULT_DETAIL["$1"]="$3"
    printf '[PASS] assertion %s: %s\n       %s\n' "$1" "$2" "$3"
}

fail_assertion() { # num title detail
    RESULT_STATUS["$1"]="FAIL"
    RESULT_TITLE["$1"]="$2"
    RESULT_DETAIL["$1"]="$3"
    printf '[FAIL] assertion %s: %s\n       %s\n' "$1" "$2" "$3" >&2
}

info() {
    printf '[INFO] %s\n' "$*"
}

blocker() {
    echo "" >&2
    echo "BLOCKER: $*" >&2
    echo "" >&2
    exit 1
}

print_summary() {
    if [ "$SUMMARY_PRINTED" -eq 1 ]; then
        return
    fi
    SUMMARY_PRINTED=1
    echo ""
    echo "================================================================"
    echo "LIVE ENROLLMENT GATE SUMMARY"
    echo "================================================================"
    local i status title detail
    for i in $ASSERTION_NUMS; do
        status="${RESULT_STATUS[$i]:-BLOCKED}"
        title="${RESULT_TITLE[$i]:-not reached}"
        detail="${RESULT_DETAIL[$i]:-the script exited before this assertion ran; see BLOCKER output above}"
        printf '  [%s] %-2s %s\n' "$status" "$i" "$title"
        printf '        %s\n' "$detail"
    done
    echo "================================================================"
}

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
KEEP=0
SKIP_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        -h|--help)
            sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown flag: $arg (expected --keep, --skip-build, -h/--help)" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ROBOT_SH="$REPO_ROOT/packaging/robot-agent/robot.sh"

if [ ! -f "$REPO_ROOT/docker-compose.yml" ] || [ ! -f "$ROBOT_SH" ]; then
    blocker "computed REPO_ROOT=$REPO_ROOT does not look like the repo root (missing docker-compose.yml or packaging/robot-agent/robot.sh). This script must stay at packaging/robot-agent/tests/live_enrollment_gate.sh."
fi

# ---------------------------------------------------------------------------
# Docker access: detect whether the `sg docker` wrapper is needed.
# ---------------------------------------------------------------------------
DOCKER_NEEDS_SG=0
if ! docker info >/dev/null 2>&1; then
    if command -v sg >/dev/null 2>&1 && sg docker -c 'docker info' >/dev/null 2>&1; then
        DOCKER_NEEDS_SG=1
        info "docker requires the 'sg docker' wrapper in this environment; using it for every docker invocation."
    else
        blocker "docker is not usable directly ('docker info' failed) and 'sg docker -c \"docker info\"' also failed. Add the current user to the docker group (or run as a user who is already a member) before running this gate. This environment cannot run the live gate as-is."
    fi
fi

dockerc() {
    if [ "$DOCKER_NEEDS_SG" -eq 1 ]; then
        local quoted
        quoted=$(printf '%q ' docker "$@")
        sg docker -c "$quoted"
    else
        docker "$@"
    fi
}

for tool in openssl jq curl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        blocker "required host tool '$tool' is not installed. Install it and re-run."
    fi
done

# ---------------------------------------------------------------------------
# Unique identifiers for this run, and a scratch directory for certs/state
# shared between the many short-lived containers this gate starts.
# ---------------------------------------------------------------------------
GATE_ID="rdv-live-gate-$$-$(date +%s)"
NET="${GATE_ID}-net"
PG="${GATE_ID}-postgres"
OBJECTSTORE="${GATE_ID}-objectstore"
INGEST="${GATE_ID}-ingest"
IMAGE_DEV="robot-vault-dev:latest"
IMAGE_INGEST="robot-vault-ingest:latest"
# The thin first-party store image (deploy/release-images/objectstore.Dockerfile,
# FROM the pinned RustFS digest of CONTRACT.md section 1), as docker-compose.yml builds it.
IMAGE_OBJECTSTORE="robot-vault-objectstore:latest"
ROBOT_ID="live-gate-robot"

GATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rdv-live-gate.XXXXXX")"
CERTS_DIR="$GATE_DIR/certs"
CA_PRIVATE_DIR="$GATE_DIR/ca-private"
INGEST_LEAF_DIR="$GATE_DIR/ingest-leaf"
SANDBOX_DIR="$GATE_DIR/sandbox"
RECORDINGS_DIR="$GATE_DIR/recordings"
STUBS_DIR="$GATE_DIR/stubs"
SCRIPTS_DIR="$GATE_DIR/scripts"
mkdir -p "$CERTS_DIR" "$CA_PRIVATE_DIR" "$INGEST_LEAF_DIR" \
    "$SANDBOX_DIR" "$RECORDINGS_DIR" "$STUBS_DIR" "$SCRIPTS_DIR"

info "gate id:   $GATE_ID"
info "gate dir:  $GATE_DIR"
info "repo root: $REPO_ROOT"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        echo ""
        echo "--keep given: leaving resources in place for debugging:"
        echo "  containers: $INGEST $OBJECTSTORE $PG"
        echo "  network:    $NET"
        echo "  gate dir:   $GATE_DIR"
        echo "  to tear down by hand later:"
        echo "    docker rm -fv $INGEST $OBJECTSTORE $PG; docker network rm $NET; rm -rf $GATE_DIR"
        return
    fi
    info "tearing down: containers, network, and $GATE_DIR"
    # Postgres declares a data volume in their images. `rm -f` leaves
    # those anonymous volumes behind, so remove the gate containers with -v.
    dockerc rm -fv "$INGEST" "$OBJECTSTORE" "$PG" >/dev/null 2>&1 || true
    dockerc network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$GATE_DIR" 2>/dev/null || true
}

on_exit() {
    local ec=$?
    print_summary
    cleanup
    local bad=0
    local i
    for i in $ASSERTION_NUMS; do
        if [ "${RESULT_STATUS[$i]:-MISSING}" != "PASS" ]; then
            bad=1
        fi
    done
    if [ "$bad" -eq 1 ] || [ "$ec" -ne 0 ]; then
        exit 1
    fi
    exit 0
}
trap on_exit EXIT INT TERM

# ---------------------------------------------------------------------------
# Build (or verify) images
# ---------------------------------------------------------------------------
export GIT_SHA
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

if [ "$SKIP_BUILD" -eq 0 ]; then
    info "building $IMAGE_OBJECTSTORE (docker compose build objectstore) ..."
    if ! ( cd "$REPO_ROOT" && dockerc compose build objectstore ); then
        blocker "'docker compose build objectstore' failed. See the output immediately above."
    fi
    info "building $IMAGE_DEV (docker compose build dev) ..."
    if ! ( cd "$REPO_ROOT" && dockerc compose build dev ); then
        blocker "'docker compose build dev' failed. See the compiler/pip output immediately above for the exact error."
    fi
    info "building $IMAGE_INGEST (docker compose build ingest) -- this reuses the cached rust builder layers from robot-vault-ingest-builder, so it should be fast even though it is a full cargo build ..."
    if ! ( cd "$REPO_ROOT" && dockerc compose build ingest ); then
        blocker "'docker compose build ingest' failed. See the exact cargo/rustc error immediately above -- do not treat this as a script bug, it means the Rust ingest server does not build."
    fi
else
    info "--skip-build given; verifying required images already exist"
    if ! dockerc image inspect "$IMAGE_DEV" >/dev/null 2>&1; then
        blocker "--skip-build given but $IMAGE_DEV does not exist locally. Run 'docker compose build dev' first, or drop --skip-build."
    fi
    if ! dockerc image inspect "$IMAGE_INGEST" >/dev/null 2>&1; then
        blocker "--skip-build given but $IMAGE_INGEST does not exist locally. Run 'docker compose build ingest' first, or drop --skip-build."
    fi
    if ! dockerc image inspect "$IMAGE_OBJECTSTORE" >/dev/null 2>&1; then
        blocker "--skip-build given but $IMAGE_OBJECTSTORE does not exist locally. Run 'docker compose build objectstore' first, or drop --skip-build."
    fi
fi

# ---------------------------------------------------------------------------
# TLS material: a fresh CA + server cert per run, SAN'd for this run's
# ingest container name (the hostname every client in this gate dials, over
# the shared docker network's embedded DNS).
# ---------------------------------------------------------------------------
info "generating CA + server certificate for $INGEST ..."
openssl genrsa -out "$CA_PRIVATE_DIR/ca.key" 4096 >/dev/null 2>&1
openssl req -x509 -new -nodes -key "$CA_PRIVATE_DIR/ca.key" -sha256 -days 3650 \
    -subj "/C=US/ST=CA/O=RobotVault/OU=Vault-Gate-CA/CN=robot-vault-gate-ca" \
    -out "$CERTS_DIR/ca.crt" >/dev/null 2>&1
openssl genrsa -out "$INGEST_LEAF_DIR/ingest.key" 2048 >/dev/null 2>&1

cat > "$INGEST_LEAF_DIR/ingest.cnf" <<EOF
[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no

[dn]
C=US
ST=CA
O=RobotVault
OU=Vault-Gate
CN=$INGEST

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $INGEST
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF

openssl req -new -key "$INGEST_LEAF_DIR/ingest.key" \
    -out "$INGEST_LEAF_DIR/ingest.csr" -config "$INGEST_LEAF_DIR/ingest.cnf" >/dev/null 2>&1
openssl x509 -req -in "$INGEST_LEAF_DIR/ingest.csr" \
    -CA "$CERTS_DIR/ca.crt" -CAkey "$CA_PRIVATE_DIR/ca.key" \
    -CAserial "$CA_PRIVATE_DIR/ca.srl" -CAcreateserial \
    -out "$INGEST_LEAF_DIR/ingest.crt" -days 825 -sha256 \
    -extensions v3_req -extfile "$INGEST_LEAF_DIR/ingest.cnf" >/dev/null 2>&1

if [ ! -s "$INGEST_LEAF_DIR/ingest.crt" ]; then
    blocker "TLS cert generation failed; $INGEST_LEAF_DIR/ingest.crt is missing or empty."
fi

# ---------------------------------------------------------------------------
# Network + Postgres + object store
# ---------------------------------------------------------------------------
info "creating docker network $NET ..."
dockerc network create "$NET" >/dev/null

info "starting postgres ($PG) ..."
dockerc run -d --name "$PG" --network "$NET" \
    -e POSTGRES_USER=robotvault -e POSTGRES_PASSWORD=robotvault -e POSTGRES_DB=robotvault \
    postgres:16-alpine >/dev/null

# Throwaway secrets for this run, in files (never on a command line): the root password for the
# store, and the env file for the bootstrap and for ingest.
OBJECTSTORE_ROOT_USER="gate-root"
OBJECTSTORE_ROOT_PASSWORD="$(openssl rand -hex 16)"
printf '%s' "$OBJECTSTORE_ROOT_PASSWORD" > "$GATE_DIR/objectstore-root-password"
chmod 0644 "$GATE_DIR/objectstore-root-password"
INGEST_S3_SECRET="$(openssl rand -hex 16)"
( umask 077
  {
    echo "S3_ENDPOINT=http://${OBJECTSTORE}:9000"
    echo "S3_BUCKET=robotics-data"
    echo "S3_REGION=us-east-1"
    echo "OBJECTSTORE_ROOT_USER=$OBJECTSTORE_ROOT_USER"
    echo "OBJECTSTORE_ROOT_PASSWORD=$OBJECTSTORE_ROOT_PASSWORD"
    echo "OBJECTSTORE_POLICY_DIR=/app/development/release/objectstore-policies"
    for role in ingest processor exporter query gc; do
        up="$(printf '%s' "$role" | tr a-z A-Z)"
        echo "${up}_S3_ACCESS_KEY=robot-vault-${role}"
        if [ "$role" = ingest ]; then echo "${up}_S3_SECRET_KEY=$INGEST_S3_SECRET"
        else echo "${up}_S3_SECRET_KEY=$(openssl rand -hex 16)"; fi
    done
  } > "$GATE_DIR/objectstore-bootstrap.env"
  { echo "AWS_ACCESS_KEY_ID=robot-vault-ingest"; echo "AWS_SECRET_ACCESS_KEY=$INGEST_S3_SECRET"; } > "$GATE_DIR/ingest-s3.env"
)

info "starting the object store ($OBJECTSTORE) with the settings of development/objectstore/CONTRACT.md ..."
# `--user 0` only because the tmpfs data folder is owned by root.
dockerc run -d --name "$OBJECTSTORE" --network "$NET" --user 0 --tmpfs /data \
    -v "$GATE_DIR/objectstore-root-password":/run/secrets/objectstore_root_password:ro \
    -e RUSTFS_ACCESS_KEY="$OBJECTSTORE_ROOT_USER" \
    -e RUSTFS_SECRET_KEY_FILE=/run/secrets/objectstore_root_password \
    -e RUSTFS_VOLUMES=/data -e RUSTFS_ADDRESS=0.0.0.0:9000 -e RUSTFS_CONSOLE_ENABLE=false \
    -e RUSTFS_DURABILITY_MODE=strict -e RUSTFS_DRIVE_SYNC_ENABLE=true \
    -e RUSTFS_NEW_BUCKET_DURABILITY_MODE=strict -e RUSTFS_PUT_RENAME_EARLY_ACK_ENABLE=false \
    -e RUSTFS_API_STALE_UPLOADS_EXPIRY=360h -e RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL=6h \
    "$IMAGE_OBJECTSTORE" >/dev/null

info "waiting for postgres ..."
pg_ready=0
for _ in $(seq 1 30); do
    if dockerc exec "$PG" pg_isready -U robotvault -d robotvault >/dev/null 2>&1; then
        pg_ready=1
        break
    fi
    sleep 2
done
[ "$pg_ready" -eq 1 ] || blocker "postgres ($PG) did not become ready within 60s. 'docker logs $PG' for detail (left running since --keep was not used only after this blocker's trap runs)."

info "waiting for the object store ..."
objectstore_ready=0
for _ in $(seq 1 30); do
    if dockerc exec "$OBJECTSTORE" curl -fsS -o /dev/null http://127.0.0.1:9000/health/ready >/dev/null 2>&1; then
        objectstore_ready=1
        break
    fi
    sleep 2
done
[ "$objectstore_ready" -eq 1 ] || blocker "the object store ($OBJECTSTORE) did not become ready within 60s."

# The product bootstrap program makes the bucket and the five service keys (CONTRACT.md section 7),
# so ingest runs with its release key and this gate needs no filesystem shortcut.
info "bootstrapping the bucket and the five service keys ..."
if ! dockerc run --rm --network "$NET" --env-file "$GATE_DIR/objectstore-bootstrap.env" \
    -v "$REPO_ROOT":/app:ro -w /app \
    "$IMAGE_DEV" python development/objectstore/bootstrap.py; then
    blocker "development/objectstore/bootstrap.py failed against the object store. See its output immediately above."
fi

# ---------------------------------------------------------------------------
# Migrate
# ---------------------------------------------------------------------------
info "running db/migrate.py against $PG ..."
if ! dockerc run --rm --network "$NET" \
    -e DATABASE_URL="postgres://robotvault:robotvault@${PG}:5432/robotvault" \
    -v "$REPO_ROOT":/app -w /app \
    "$IMAGE_DEV" python db/migrate.py; then
    blocker "db/migrate.py failed against a fresh postgres:16-alpine. See its output immediately above."
fi

# ---------------------------------------------------------------------------
# Ingest server
# ---------------------------------------------------------------------------
AUTH_TOKEN="$(openssl rand -hex 24)"
PROV_TOKEN="$(openssl rand -hex 24)"

info "starting ingest server ($INGEST) with TLS on and a provisioning token set ..."
dockerc run -d --name "$INGEST" --network "$NET" \
    -e DATABASE_URL="postgres://robotvault:robotvault@${PG}:5432/robotvault" \
    -e STORAGE_ROOT=/app/uploads \
    -e S3_BUCKET=robotics-data \
    -e S3_ENDPOINT="http://${OBJECTSTORE}:9000" \
    -e S3_REGION=us-east-1 \
    --env-file "$GATE_DIR/ingest-s3.env" \
    -e ENABLE_FILE_CAS=true \
    -e INGEST_AUTH_TOKEN="$AUTH_TOKEN" \
    -e INGEST_PROVISIONING_TOKEN="$PROV_TOKEN" \
    -e INGEST_SECURITY_PROFILE=dev \
    -e TLS_ENABLED=true \
    -e TLS_MTLS=false \
    -e TLS_SERVER_CERT=/run/ingotron/tls/ingest.crt \
    -e TLS_SERVER_KEY=/run/ingotron/tls/ingest.key \
    -e TLS_CA_CERT=/run/ingotron/tls-public/ca.crt \
    -e INGEST_PUBLIC_HOST="${INGEST}:50051" \
    -e INGEST_ROBOT_SELF_ROTATION=disabled \
    -v "$INGEST_LEAF_DIR":/run/ingotron/tls:ro \
    -v "$CERTS_DIR":/run/ingotron/tls-public:ro \
    "$IMAGE_INGEST" >/dev/null

info "waiting for ingest /readyz (requires postgres AND a working S3 put+delete probe as the ingest key) ..."
ready_http=""
for _ in $(seq 1 30); do
    ready_http="$(dockerc exec "$OBJECTSTORE" curl -s -o /dev/null -w '%{http_code}' "http://${INGEST}:8080/readyz" 2>/dev/null || echo 000)"
    if [ "$ready_http" = "200" ]; then
        break
    fi
    sleep 2
done

# ===== Assertion 1 ==========================================================
# The ingest server starts with TLS on and a provisioning token set.
# Evidence: (a) /readyz is 200 (server up, DB reachable, S3 bucket writable);
# (b) an unauthenticated call to the provisioning plane is refused with 401
#     (proves INGEST_PROVISIONING_TOKEN is actually enforced, not merely set).
unauth_http="$(dockerc exec "$OBJECTSTORE" curl -s -o /dev/null -w '%{http_code}' \
    -X POST "http://${INGEST}:8080/admin/v2/robots" \
    -H 'content-type: application/json' \
    -d "{\"robot_id\":\"${ROBOT_ID}-unauth-probe\",\"name\":\"unauth probe\"}" 2>/dev/null || echo 000)"

if [ "$ready_http" = "200" ] && [ "$unauth_http" = "401" ]; then
    pass_assertion 1 "ingest starts with TLS on and a provisioning token set" \
        "GET /readyz=200 (postgres+S3 both ready); unauthenticated POST /admin/v2/robots=401 (provisioning token enforced, no fail-open)"
else
    fail_assertion 1 "ingest starts with TLS on and a provisioning token set" \
        "GET /readyz=$ready_http (want 200); unauthenticated POST /admin/v2/robots=$unauth_http (want 401). docker logs $INGEST for detail."
fi

# ---------------------------------------------------------------------------
# Small helper for the /admin/v2 calls: returns "<http_code>\n<body>".
# ---------------------------------------------------------------------------
admin_call() { # method path body(may be empty) auth_token
    local method="$1" path="$2" body="$3" auth="$4"
    local args=(curl -s -w '\n%{http_code}' -X "$method" "http://${INGEST}:8080${path}")
    if [ -n "$auth" ]; then
        args+=(-H "authorization: Bearer ${auth}")
    fi
    if [ -n "$body" ]; then
        args+=(-H 'content-type: application/json' -d "$body")
    fi
    dockerc exec "$OBJECTSTORE" "${args[@]}"
}

# ===== Assertion 2 ==========================================================
# POST /admin/v2/robots returns 201.
CREATE_BODY="{\"robot_id\":\"${ROBOT_ID}\",\"name\":\"Live Gate Robot\"}"
create_raw="$(admin_call POST /admin/v2/robots "$CREATE_BODY" "$PROV_TOKEN")"
create_http="$(printf '%s' "$create_raw" | tail -n1)"
create_body="$(printf '%s' "$create_raw" | sed '$d')"

if [ "$create_http" = "201" ]; then
    pass_assertion 2 "POST /admin/v2/robots -> 201" "response: $create_body"
else
    fail_assertion 2 "POST /admin/v2/robots -> 201" "got HTTP $create_http, body: $create_body"
fi

# ===== Assertion 3 ==========================================================
# POST .../enrollment-codes returns 201 and `code` matches the ign1.<26>.<32>
# Crockford-base32 shape.
MINT_BODY='{"ttl_seconds":900,"purpose":"enroll"}'
mint_raw="$(admin_call POST "/admin/v2/robots/${ROBOT_ID}/enrollment-codes" "$MINT_BODY" "$PROV_TOKEN")"
mint_http="$(printf '%s' "$mint_raw" | tail -n1)"
mint_body="$(printf '%s' "$mint_raw" | sed '$d')"
CODE="$(printf '%s' "$mint_body" | jq -r '.code // empty')"
CA_SPKI_SHA256_FROM_SERVER="$(printf '%s' "$mint_body" | jq -r '.ca_spki_sha256 // empty')"

CODE_RE='^ign1\.[0-9A-HJKMNP-TV-Z]{26}\.[0-9A-HJKMNP-TV-Z]{32}$'
if [ "$mint_http" = "201" ] && [[ "$CODE" =~ $CODE_RE ]]; then
    pass_assertion 3 "POST .../enrollment-codes -> 201, code matches ign1.<26>.<32>" \
        "code=$CODE"
else
    fail_assertion 3 "POST .../enrollment-codes -> 201, code matches ign1.<26>.<32>" \
        "got HTTP $mint_http, code='$CODE', body: $mint_body"
fi

# ===== Assertion 4 (the minter/verifier conformance gate) ===================
# The pin inside the minted code must equal robot.sh's own b32_prefix()
# applied to the CA the server is actually serving. Computed BOTH ways.
CODE_PIN="$(printf '%s' "$CODE" | cut -d'.' -f2)"

B32_FILE="$GATE_DIR/b32_prefix.sh"
grep '^C32=' "$ROBOT_SH" > "$B32_FILE" || true
sed -n '/^b32_prefix() {$/,/^}$/p' "$ROBOT_SH" >> "$B32_FILE"
if [ ! -s "$B32_FILE" ] || ! grep -q '^b32_prefix() {' "$B32_FILE"; then
    fail_assertion 4 "pin(code) == b32_prefix(served CA)" \
        "could not extract b32_prefix()/C32 from $ROBOT_SH -- its shape changed; grep/sed extraction failed"
else
    CA_SPKI_HEX="$(openssl x509 -in "$CERTS_DIR/ca.crt" -noout -pubkey | openssl pkey -pubin -outform DER | openssl dgst -sha256 -r | cut -d' ' -f1)"
    set +e
    COMPUTED_PIN="$( . "$B32_FILE"; b32_prefix "$CA_SPKI_HEX" 26 )"
    b32_rc=$?
    set -e
    if [ "$b32_rc" -eq 0 ] && [ -n "$CODE_PIN" ] && [ "$CODE_PIN" = "$COMPUTED_PIN" ]; then
        pass_assertion 4 "pin(code) == b32_prefix(served CA)" \
            "code pin=$CODE_PIN, robot.sh b32_prefix(sha256(CA SPKI))=$COMPUTED_PIN, server's own ca_spki_sha256=$CA_SPKI_SHA256_FROM_SERVER, host-computed spki sha256=$CA_SPKI_HEX (all agree)"
    else
        fail_assertion 4 "pin(code) == b32_prefix(served CA)" \
            "code pin='$CODE_PIN' (rc=$b32_rc) vs robot.sh-computed pin='$COMPUTED_PIN'; server ca_spki_sha256=$CA_SPKI_SHA256_FROM_SERVER, host-computed=$CA_SPKI_HEX"
    fi
fi

# ---------------------------------------------------------------------------
# Generate the enrollment + upload protobuf stubs the Python agent needs.
# ---------------------------------------------------------------------------
info "generating enrollment_pb2/upload_pb2 stubs ..."
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
if ! dockerc run --rm --user "${HOST_UID}:${HOST_GID}" \
    -v "$REPO_ROOT":/app -v "$STUBS_DIR":/out -w /app \
    "$IMAGE_DEV" python -m grpc_tools.protoc -I proto \
    --python_out=/out --grpc_python_out=/out proto/upload.proto proto/enrollment.proto; then
    blocker "grpc_tools.protoc stub generation failed. See output above."
fi
if [ ! -f "$STUBS_DIR/enrollment_pb2.py" ] || [ ! -f "$STUBS_DIR/upload_pb2.py" ]; then
    blocker "stub generation reported success but $STUBS_DIR is missing enrollment_pb2.py or upload_pb2.py"
fi

# ---------------------------------------------------------------------------
# A small, stable recording file for assertion 11's real scan/hash/upload
# pass. Backdated so rsm's file-stability gate (30s, from the server's
# recommended min_stable_age_seconds) does not skip it.
# ---------------------------------------------------------------------------
head -c 4096 /dev/urandom > "$RECORDINGS_DIR/episode_001.mcap"
BACKDATE_EPOCH=$(( $(date +%s) - 120 ))
touch -d "@${BACKDATE_EPOCH}" "$RECORDINGS_DIR/episode_001.mcap"

# ---------------------------------------------------------------------------
# Helper: run a python module inside the dev image, attached to the gate
# network, with the sandbox/certs/recordings/stubs mounted, and capture its
# exit status + combined output without letting `set -e` abort the script.
# ---------------------------------------------------------------------------
run_agent() { # -> sets AGENT_OUT, AGENT_RC
    set +e
    AGENT_OUT="$(dockerc run --rm --network "$NET" \
        -e INGOTRON_ROOT_PREFIX=/sandbox \
        -e PYTHONPATH=/stubs:/app/packaging/robot-agent/agent/lib \
        -v "$REPO_ROOT":/app \
        -v "$STUBS_DIR":/stubs:ro \
        -v "$SANDBOX_DIR":/sandbox \
        -v "$CERTS_DIR":/certs:ro \
        -v "$RECORDINGS_DIR":/recordings \
        -w /app \
        "$IMAGE_DEV" "$@" 2>&1)"
    AGENT_RC=$?
    set -e
}

# secretstore.py deliberately writes /etc/ingotron/credentials as mode 0700,
# owned by whatever uid the container ran as (root, here, since no --user was
# given to run_agent). That means the HOST user running this script generally
# cannot even traverse into $SANDBOX_DIR/etc/ingotron/credentials -- a plain
# host-side `[ -f "$TOKEN_FILE" ]` silently reads as "not found" (permission
# denied to stat, not a real absence) and would falsely fail every assertion
# downstream of enrollment. Existence/content checks on anything under
# $SANDBOX_DIR therefore always go through a throwaway root container, same
# as the mode/content checks a few lines below.
sandbox_path_exists() { # sandbox-relative path, e.g. etc/ingotron/credentials/robot.token
    dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 test -e "/sandbox/$1"
}

# ===== Assertion 5, 6, 7 (enroll) ===========================================
VAULT_JSON="$SANDBOX_DIR/etc/ingotron/vault.json"
TOKEN_FILE="$SANDBOX_DIR/etc/ingotron/credentials/robot.token"

info "running the real robot-side enrollment client against the real server ..."
run_agent python3 -m ingotron_agent.cli enroll \
    --vault "https://${INGEST}" --ingest "${INGEST}:50051" \
    --code "$CODE" \
    --ca /certs/ca.crt \
    --root /recordings \
    --result-file /sandbox/result.txt
ENROLL_OUT="$AGENT_OUT"
ENROLL_RC="$AGENT_RC"

if [ "$ENROLL_RC" -ne 0 ] || ! sandbox_path_exists etc/ingotron/credentials/robot.token; then
    fail_assertion 5 "enrollment exchanges the code; credential starts ignr1. and is length 58" \
        "enroll exited $ENROLL_RC or token file missing. Output: $ENROLL_OUT"
    fail_assertion 6 "token file lands at mode 0600" "enrollment did not succeed; see assertion 5"
    fail_assertion 7 "vault.json shape is correct and never carries the credential" "enrollment did not succeed; see assertion 5"
else
    CREDENTIAL="$(dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 cat /sandbox/etc/ingotron/credentials/robot.token)"
    CRED_LEN="${#CREDENTIAL}"
    if [[ "$CREDENTIAL" == ignr1.* ]] && [ "$CRED_LEN" -eq 58 ]; then
        pass_assertion 5 "enrollment exchanges the code; credential starts ignr1. and is length 58" \
            "enroll exit=0, credential prefix=ignr1., length=$CRED_LEN, enroll output: $ENROLL_OUT"
    else
        fail_assertion 5 "enrollment exchanges the code; credential starts ignr1. and is length 58" \
            "credential prefix/length wrong: starts-with-ignr1.=$([[ "$CREDENTIAL" == ignr1.* ]] && echo yes || echo no), length=$CRED_LEN"
    fi

    TOKEN_MODE="$(dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 stat -c '%a' /sandbox/etc/ingotron/credentials/robot.token)"
    if [ "$TOKEN_MODE" = "600" ]; then
        pass_assertion 6 "token file mode is 600" "stat -c '%a' == $TOKEN_MODE"
    else
        fail_assertion 6 "token file mode is 600" "stat -c '%a' == $TOKEN_MODE (want 600)"
    fi

    VAULT_JSON_CONTENT="$(dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 cat /sandbox/etc/ingotron/vault.json)"
    has_token_file="$(printf '%s' "$VAULT_JSON_CONTENT" | jq -r '.auth.token_file // empty')"
    has_inline_token="$(printf '%s' "$VAULT_JSON_CONTENT" | jq -r '.auth.token // empty')"
    tls_enabled="$(printf '%s' "$VAULT_JSON_CONTENT" | jq -r '.tls.enabled')"
    tls_mtls="$(printf '%s' "$VAULT_JSON_CONTENT" | jq -r '.tls.mtls')"
    if dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 grep -q 'ignr1\.' /sandbox/etc/ingotron/vault.json 2>/dev/null; then
        leak_grep_failed_as_required=no
    else
        leak_grep_failed_as_required=yes
    fi

    if [ -n "$has_token_file" ] && [ -z "$has_inline_token" ] && [ "$tls_enabled" = "true" ] && [ "$tls_mtls" = "false" ] && [ "$leak_grep_failed_as_required" = "yes" ]; then
        pass_assertion 7 "vault.json: auth.token_file set (not auth.token), tls.enabled=true, tls.mtls=false, no credential leak" \
            "auth.token_file=$has_token_file, auth.token=<absent>, tls.enabled=$tls_enabled, tls.mtls=$tls_mtls, grep 'ignr1\\.' vault.json failed as required"
    else
        fail_assertion 7 "vault.json: auth.token_file set (not auth.token), tls.enabled=true, tls.mtls=false, no credential leak" \
            "auth.token_file='$has_token_file', auth.token='$has_inline_token', tls.enabled=$tls_enabled, tls.mtls=$tls_mtls, grep-for-leak-failed-as-required=$leak_grep_failed_as_required. vault.json: $VAULT_JSON_CONTENT"
    fi
fi

# ===== Assertion 8 ==========================================================
# A second Enroll with the SAME code fails (one-use guarantee), UNAUTHENTICATED.
info "redeeming the SAME code a second time -- must fail (one-use guarantee) ..."
run_agent python3 -m ingotron_agent.cli enroll \
    --vault "https://${INGEST}" --ingest "${INGEST}:50051" \
    --code "$CODE" \
    --ca /certs/ca.crt \
    --root /recordings \
    --force
SECOND_ENROLL_OUT="$AGENT_OUT"
SECOND_ENROLL_RC="$AGENT_RC"

if [ "$SECOND_ENROLL_RC" -ne 0 ] && printf '%s' "$SECOND_ENROLL_OUT" | grep -qi 'invalid or expired enrollment code'; then
    pass_assertion 8 "a second Enroll with the same code fails (one-use), UNAUTHENTICATED" \
        "exit=$SECOND_ENROLL_RC, message: $SECOND_ENROLL_OUT"
else
    fail_assertion 8 "a second Enroll with the same code fails (one-use), UNAUTHENTICATED" \
        "exit=$SECOND_ENROLL_RC, output: $SECOND_ENROLL_OUT"
fi

# Confirm the original credential is still intact (the failed retry above
# must not have clobbered it -- exchange_code raises before any write).
if ! sandbox_path_exists etc/ingotron/credentials/robot.token || [ "$(dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 cat /sandbox/etc/ingotron/credentials/robot.token)" != "${CREDENTIAL:-__unset__}" ]; then
    info "WARNING: the original credential file changed after the failed second-enroll attempt; this would be a real bug, but does not affect assertions 1-8's PASS status above."
fi

# ---------------------------------------------------------------------------
# Write the two small probe scripts assertions 9/10 (and the informational
# HealthCheck check) use. They build a GrpcUploadTransport the same way the
# shipped agent does -- via UploadQueue._new_transport() reading the real
# vault.json -- so they exercise the exact token_file -> transport path.
# ---------------------------------------------------------------------------
cat > "$SCRIPTS_DIR/list_uploads_check.py" <<'PYEOF'
import json
import sys
from pathlib import Path

from robot_upload_agent.core import UploadQueue
import upload_pb2 as pb


def main() -> int:
    config_path = Path(sys.argv[1])
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/recordings")
    q = UploadQueue(root=root, vault_config_path=config_path)
    transport = q._new_transport()
    try:
        resp = transport.stub.ListUploads(
            pb.ListUploadsRequest(robot_id="", page_size=1),
            metadata=transport.auth_metadata,
            timeout=10.0,
        )
        result = {"ok": True, "code": "OK", "details": "", "uploads": len(resp.uploads)}
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", lambda: None)()
        details = getattr(exc, "details", lambda: str(exc))()
        result = {"ok": False, "code": str(code), "details": str(details)}
    finally:
        transport.close()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PYEOF

cat > "$SCRIPTS_DIR/health_check_probe.py" <<'PYEOF'
import json
import sys
from pathlib import Path

from robot_upload_agent.core import UploadQueue


def main() -> int:
    config_path = Path(sys.argv[1])
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/recordings")
    q = UploadQueue(root=root, vault_config_path=config_path)
    transport = q._new_transport()
    try:
        result = transport.health_check()
    finally:
        transport.close()
    print(json.dumps({"health_check_result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PYEOF

run_probe() { # script_path config_path -> sets PROBE_OUT, PROBE_RC
    set +e
    PROBE_OUT="$(dockerc run --rm --network "$NET" \
        -e PYTHONPATH=/stubs:/app/packaging/robot-agent/agent/lib \
        -v "$REPO_ROOT":/app \
        -v "$STUBS_DIR":/stubs:ro \
        -v "$SANDBOX_DIR":/sandbox \
        -v "$CERTS_DIR":/certs:ro \
        -v "$RECORDINGS_DIR":/recordings \
        -v "$SCRIPTS_DIR":/scripts:ro \
        -w /app \
        "$IMAGE_DEV" python3 "$1" "$2" /recordings 2>&1)"
    PROBE_RC=$?
    set -e
}

# ===== Assertion 9 ==========================================================
# An upload authenticates with the per-robot token. Proven with a real
# authenticated RPC (ListUploads) built from the written vault.json via the
# real UploadQueue._new_transport() path -- see the header comment for why
# this is used instead of HealthCheck.
info "proving the per-robot credential authenticates a real RPC (ListUploads) ..."
run_probe /scripts/list_uploads_check.py /sandbox/etc/ingotron/vault.json
LIST_UPLOADS_BEFORE_OUT="$PROBE_OUT"
# NOTE: no `// default` here -- jq's `//` treats a JSON `false` value itself
# as falsy and silently substitutes the default, which would corrupt exactly
# the case this is checking. Plain `.ok` and an explicit fallback on jq
# failure (not on a false *value*) is the only correct form.
LIST_UPLOADS_BEFORE_OK="$(printf '%s' "$PROBE_OUT" | tail -n1 | jq -r '.ok' 2>/dev/null || echo parse_error)"

if [ "$PROBE_RC" -eq 0 ] && [ "$LIST_UPLOADS_BEFORE_OK" = "true" ]; then
    pass_assertion 9 "the enrolled robot's credential authenticates a real RPC (ListUploads)" \
        "$LIST_UPLOADS_BEFORE_OUT"
else
    fail_assertion 9 "the enrolled robot's credential authenticates a real RPC (ListUploads)" \
        "probe exit=$PROBE_RC, output: $LIST_UPLOADS_BEFORE_OUT"
fi

# ----- Informational only: reproduce the HealthCheck insufficiency finding -
# Does NOT gate pass/fail. Documents, on every run, why assertion 9 does not
# use HealthCheck.
info "reproducing the HealthCheck-insufficiency finding (informational, not a gating assertion) ..."
run_probe /scripts/health_check_probe.py /sandbox/etc/ingotron/vault.json
HC_VALID_OUT="$PROBE_OUT"

BOGUS_SANDBOX="$GATE_DIR/bogus-sandbox"
mkdir -p "$BOGUS_SANDBOX/etc/ingotron/credentials"
cp "$VAULT_JSON" "$BOGUS_SANDBOX/etc/ingotron/vault.json" 2>/dev/null || true
if [ -f "$BOGUS_SANDBOX/etc/ingotron/vault.json" ]; then
    sed -i "s#${SANDBOX_DIR}/etc/ingotron#/bogus/etc/ingotron#g; s#/sandbox/etc/ingotron#/bogus/etc/ingotron#g" "$BOGUS_SANDBOX/etc/ingotron/vault.json"
    BOGUS_SECRET="$(dockerc run --rm alpine:3.20 sh -c "head -c 64 /dev/urandom | tr -dc '0-9A-HJKMNP-TV-Z' | head -c 52")"
    printf 'ignr1.%s' "$BOGUS_SECRET" > "$BOGUS_SANDBOX/etc/ingotron/credentials/robot.token"

    set +e
    HC_BOGUS_OUT="$(dockerc run --rm --network "$NET" \
        -e PYTHONPATH=/stubs:/app/packaging/robot-agent/agent/lib \
        -v "$REPO_ROOT":/app \
        -v "$STUBS_DIR":/stubs:ro \
        -v "$BOGUS_SANDBOX":/bogus \
        -v "$CERTS_DIR":/certs:ro \
        -v "$RECORDINGS_DIR":/recordings \
        -v "$SCRIPTS_DIR":/scripts:ro \
        -w /app \
        "$IMAGE_DEV" python3 /scripts/health_check_probe.py /bogus/etc/ingotron/vault.json /recordings 2>&1)"
    set -e

    info "HealthCheck with the REAL credential:   $HC_VALID_OUT"
    info "HealthCheck with a FABRICATED, never-issued ignr1. credential: $HC_BOGUS_OUT"
    if printf '%s' "$HC_BOGUS_OUT" | grep -q '"status": "healthy"'; then
        info "CONFIRMED live: HealthCheck reports healthy even for a credential the server never issued -- main.rs's health_check() never calls resolve_request_identity. Do not rely on HealthCheck to prove per-robot authentication."
    else
        info "NOTE: this run's HealthCheck-with-bogus-credential probe did not reproduce the documented gap (got: $HC_BOGUS_OUT); the code-reading finding in the header comment stands regardless, but this environment's live reproduction was inconclusive."
    fi
fi

# ===== Assertion 11 =========================================================
# Run before revocation. The agent's own runner, doing one real scan -> hash
# -> upload pass against the real server (which is why this gate stands up
# the real object store: `rua upload` on this path is UploadSmall over real S3).
info "running the agent's own runner once (scan -> hash -> upload), before revocation ..."
run_agent python3 -m ingotron_agent.runner --config /sandbox/etc/ingotron/vault.json
RUNNER_OUT="$AGENT_OUT"
RUNNER_RC="$AGENT_RC"

if [ "$RUNNER_RC" -eq 0 ] && sandbox_path_exists var/lib/ingotron/state/last_run.json; then
    LAST_RUN_SUMMARY="$(dockerc run --rm -v "$SANDBOX_DIR":/sandbox alpine:3.20 cat /sandbox/var/lib/ingotron/state/last_run.json | jq -c '{exit_status, stages: [.stages[] | {stage, ok, result}]}' 2>/dev/null || echo '(could not parse)')"
    pass_assertion 11 "the enrolled robot's own runner completes one real scan/hash/upload pass" \
        "runner exit=$RUNNER_RC, state/last_run.json written, summary: $LAST_RUN_SUMMARY"
else
    last_run_present=$(sandbox_path_exists var/lib/ingotron/state/last_run.json && echo yes || echo no)
    fail_assertion 11 "the enrolled robot's own runner completes one real scan/hash/upload pass" \
        "runner exit=$RUNNER_RC, last_run.json present=$last_run_present. Output: $RUNNER_OUT"
fi

# ---------------------------------------------------------------------------
# Revoke: terminal-state test. Disable the robot, then repeat assertion 9's
# check and require it to now fail.
# ---------------------------------------------------------------------------
info "disabling the robot ($ROBOT_ID) via /admin/v2/robots/.../state ..."
DISABLE_BODY='{"state":"disabled","reason":"live_enrollment_gate.sh assertion 10"}'
disable_raw="$(admin_call POST "/admin/v2/robots/${ROBOT_ID}/state" "$DISABLE_BODY" "$PROV_TOKEN")"
disable_http="$(printf '%s' "$disable_raw" | tail -n1)"
disable_body="$(printf '%s' "$disable_raw" | sed '$d')"
if [ "$disable_http" != "200" ]; then
    blocker "could not disable the robot (POST .../state -> HTTP $disable_http, body: $disable_body); assertion 10 cannot run without this."
fi
info "robot state transition: $disable_body"

# ===== Assertion 10 =========================================================
info "repeating the ListUploads probe with the now-revoked credential -- must fail ..."
run_probe /scripts/list_uploads_check.py /sandbox/etc/ingotron/vault.json
LIST_UPLOADS_AFTER_OUT="$PROBE_OUT"
# See the note on assertion 9's extraction: no `// default` on a boolean field.
LIST_UPLOADS_AFTER_OK="$(printf '%s' "$PROBE_OUT" | tail -n1 | jq -r '.ok' 2>/dev/null || echo parse_error)"
LIST_UPLOADS_AFTER_CODE="$(printf '%s' "$PROBE_OUT" | tail -n1 | jq -r '.code // empty' 2>/dev/null || echo '')"

if [ "$PROBE_RC" -eq 0 ] && [ "$LIST_UPLOADS_AFTER_OK" = "false" ] && printf '%s' "$LIST_UPLOADS_AFTER_CODE" | grep -qiE 'UNAUTHENTICATED|PERMISSION_DENIED'; then
    pass_assertion 10 "a disabled robot's next authenticated call fails (UNAUTHENTICATED/PERMISSION_DENIED)" \
        "$LIST_UPLOADS_AFTER_OUT"
else
    fail_assertion 10 "a disabled robot's next authenticated call fails (UNAUTHENTICATED/PERMISSION_DENIED)" \
        "expected ok=false with an UNAUTHENTICATED/PERMISSION_DENIED code; got: $LIST_UPLOADS_AFTER_OUT"
fi

print_summary
info "done."
