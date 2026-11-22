#!/usr/bin/env bash
# Hermetic regression test for the security-sensitive release Compose contract.
# It never starts a container or contacts the configured identity provider.
#
# The release compose file is the core edition. RELEASE_CONFIG_EXTENSION (a
# path relative to the repository root, optional) names a bash file that runs
# at the end with this script's fixtures and helpers; an edition overlay uses
# it to render its own topology and test its own refusals.
set -euo pipefail

# The fixtures below set edition flags explicitly for each render.  Drop any
# caller copies up front so the missing-required-input checks cannot inherit
# them accidentally.
unset ROBOT_VAULT_EDITION SEGMENTATION_ENABLED QA_ENABLED MEDIA_ENCODE_ENABLED

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.release.yml"
EDITIONS=(core)
EXTENSION=""
if [[ -n "${RELEASE_CONFIG_EXTENSION:-}" ]]; then
  EXTENSION="$ROOT_DIR/$RELEASE_CONFIG_EXTENSION"
  [[ -f "$EXTENSION" ]] || { echo "RELEASE_CONFIG_EXTENSION is not a file: $EXTENSION" >&2; exit 1; }
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required for the release TLS contract test" >&2
  exit 1
}

TEST_SECRETS_GID="$(id -g)"
if [[ "$TEST_SECRETS_GID" == "0" ]]; then
  TEST_SECRETS_GID=65534
fi

# Real certificates are intentional: the release preflight checks the key
# match, SAN, EKU, validity window, CA constraints and signature chain. Text
# placeholders would let a TLS-crash-looping deployment pass CI.
CA_FILE="$TMP_DIR/release-ca.pem"
CA_KEY_FILE="$TMP_DIR/release-ca-key.pem"
CERT_FILE="$TMP_DIR/query-fullchain.pem"
KEY_FILE="$TMP_DIR/query-key.pem"
INGEST_CERT_FILE="$TMP_DIR/ingest-fullchain.pem"
INGEST_KEY_FILE="$TMP_DIR/ingest-key.pem"
QUERY_EXT_FILE="$TMP_DIR/query-ext.cnf"
INGEST_EXT_FILE="$TMP_DIR/ingest-ext.cnf"

openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 30 \
  -subj '/CN=Robot Vault release test CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:1' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash' \
  -keyout "$CA_KEY_FILE" -out "$CA_FILE" >/dev/null 2>&1

printf '%s\n' \
  'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature,keyEncipherment' \
  'extendedKeyUsage=serverAuth' \
  'subjectAltName=DNS:vault.example.invalid' \
  'subjectKeyIdentifier=hash' \
  'authorityKeyIdentifier=keyid,issuer' \
  >"$QUERY_EXT_FILE"
printf '%s\n' \
  'basicConstraints=critical,CA:FALSE' \
  'keyUsage=critical,digitalSignature,keyEncipherment' \
  'extendedKeyUsage=serverAuth' \
  'subjectAltName=DNS:ingest.example.invalid' \
  'subjectKeyIdentifier=hash' \
  'authorityKeyIdentifier=keyid,issuer' \
  >"$INGEST_EXT_FILE"

openssl req -newkey rsa:2048 -nodes -sha256 \
  -subj '/CN=vault.example.invalid' \
  -keyout "$KEY_FILE" -out "$TMP_DIR/query.csr" >/dev/null 2>&1
openssl x509 -req -sha256 -days 30 -in "$TMP_DIR/query.csr" \
  -CA "$CA_FILE" -CAkey "$CA_KEY_FILE" -CAcreateserial \
  -extfile "$QUERY_EXT_FILE" -out "$CERT_FILE" >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -sha256 \
  -subj '/CN=ingest.example.invalid' \
  -keyout "$INGEST_KEY_FILE" -out "$TMP_DIR/ingest.csr" >/dev/null 2>&1
openssl x509 -req -sha256 -days 30 -in "$TMP_DIR/ingest.csr" \
  -CA "$CA_FILE" -CAkey "$CA_KEY_FILE" -CAcreateserial \
  -extfile "$INGEST_EXT_FILE" -out "$INGEST_CERT_FILE" >/dev/null 2>&1

chmod 0600 "$CA_KEY_FILE"
chmod 0640 "$KEY_FILE" "$INGEST_KEY_FILE"
chmod 0644 "$CERT_FILE" "$INGEST_CERT_FILE" "$CA_FILE"
chgrp "$TEST_SECRETS_GID" "$KEY_FILE" "$INGEST_KEY_FILE"

VALID_ENV="$TMP_DIR/release.env"
printf '%s\n' \
  'ROBOT_VAULT_VERSION=release-config-test' \
  'POSTGRES_PASSWORD="p@ss:/?#% value"' \
  'DATABASE_URL=postgresql://robotvault:p%40ss%3A%2F%3F%23%25%20value@postgres:5432/robotvault' \
  'INGEST_DATABASE_URL=postgresql://robot_vault_ingest:ingest-db-secret-000000000000000001@postgres:5432/robotvault' \
  'PROCESSOR_DATABASE_URL=postgresql://robot_vault_processor:processor-db-secret-0000000000000001@postgres:5432/robotvault' \
  'EXPORTER_DATABASE_URL=postgresql://robot_vault_exporter:exporter-db-secret-00000000000000001@postgres:5432/robotvault' \
  'QUERY_DATABASE_URL=postgresql://robot_vault_query:query-db-secret-0000000000000000001@postgres:5432/robotvault' \
  'GC_DATABASE_URL=postgresql://robot_vault_gc:gc-db-secret-00000000000000000000001@postgres:5432/robotvault' \
  'OBJECTSTORE_ROOT_USER=release-config-root-user-000001' \
  'OBJECTSTORE_ROOT_PASSWORD=release-config-objectstore-root-secret-000000001' \
  'INGEST_S3_SECRET_KEY=ingest-s3-secret-000000000000000001' \
  'PROCESSOR_S3_SECRET_KEY=processor-s3-secret-0000000000000001' \
  'EXPORTER_S3_SECRET_KEY=exporter-s3-secret-00000000000000001' \
  'QUERY_S3_SECRET_KEY=query-s3-secret-0000000000000000001' \
  'GC_S3_SECRET_KEY=gc-s3-secret-00000000000000000000001' \
  'INGEST_AUTH_TOKEN=release-config-ingest-token-32chars' \
  'INGEST_PROVISIONING_TOKEN=release-config-provisioning-token-32chars' \
  'METRICS_AUTH_TOKEN=release-config-metrics-token-32chars' \
  'S3_BUCKET=release-config-data' \
  'S3_REGION=ap-south-1' \
  'OIDC_ISSUER=https://identity.example.invalid/realms/robot-vault' \
  'OIDC_AUDIENCE=robot-vault-release' \
  'OIDC_JWKS_URL=https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/certs' \
  'OIDC_CLIENT_ID=robot-vault-browser' \
  'OIDC_AUTHORIZATION_URL=https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/auth' \
  'OIDC_TOKEN_URL=https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/token' \
  'OIDC_END_SESSION_URL=https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/logout' \
  'OIDC_SCOPES=openid profile email' \
  'QUERY_PUBLIC_HOST=vault.example.invalid' \
  "QUERY_TLS_CERT_FILE=$CERT_FILE" \
  "QUERY_TLS_KEY_FILE=$KEY_FILE" \
  "QUERY_TLS_CA_FILE=$CA_FILE" \
  'INGEST_PUBLIC_HOST=ingest.example.invalid' \
  "INGEST_TLS_CERT_FILE=$INGEST_CERT_FILE" \
  "INGEST_TLS_KEY_FILE=$INGEST_KEY_FILE" \
  "INGEST_TLS_CA_FILE=$CA_FILE" \
  "ROBOT_VAULT_SECRETS_GID=$TEST_SECRETS_GID" \
  'AUTH_MODE=none' \
  'OIDC_DEV=true' \
  'ROBOT_VAULT_RELEASE=false' \
  'OIDC_ALGORITHMS=HS256' \
  'OIDC_ROLE_SOURCE=resource_access' \
  'ENABLE_HSTS=false' \
  'TLS_ENABLED=false' \
  'TLS_MTLS=false' \
  'EXPORTER_MAX_ATTEMPTS=7' \
  'DUCKDB_THREADS=3' \
  'DUCKDB_MEMORY_LIMIT_MB=768' \
  'DUCKDB_QUERY_CONCURRENCY=6' \
  'DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS=7' \
  'DUCKDB_QUERY_TIMEOUT_SECONDS=45' \
  'QUERY_MAX_SOURCE_RANGE_SECONDS=1209600' \
  'QUERY_MAX_SOURCE_FILES=4000' \
  'QUERY_MAX_SOURCE_ROWS=9000000' \
  'QUERY_MAX_SOURCE_BYTES=17179869184' \
  'SQL_CONSOLE_MEMORY_LIMIT_MB=700' \
  'GC_DRY_RUN=true' \
  'GC_MIN_AGE_HOURS=72' \
  'GC_MPU_MAX_AGE_HOURS=48' \
  'GC_DELETE_LIMIT=500' \
  'GC_INTERVAL_SECONDS=1800' \
  'GC_METRICS_PORT=9999' \
  'ROBOT_VAULT_LOG_MAX_SIZE=17m' \
  'ROBOT_VAULT_LOG_MAX_FILES=4' \
  'QUERY_MEMORY_LIMIT=3g' \
  'PROMETHEUS_RETENTION_TIME=21d' \
  'PROMETHEUS_RETENTION_SIZE=12GB' \
  'ROBOT_VAULT_EDITION=pro' \
  'SEGMENTATION_ENABLED=true' \
  'QA_ENABLED=true' \
  'MEDIA_ENCODE_ENABLED=true' \
  >"$VALID_ENV"
chmod 0600 "$VALID_ENV"

# Keep a developer shell and the checkout's .env from influencing direct
# renders. Make has its own poisoned-control-variable check below. An
# extension adds the names of its own settings to CLEAN_COMPOSE_UNSET.
CLEAN_COMPOSE_UNSET=()
clean_compose() {
  env "${CLEAN_COMPOSE_UNSET[@]/#/--unset=}" \
    -u ROBOT_VAULT_VERSION -u DATABASE_URL -u POSTGRES_PASSWORD \
    -u OBJECTSTORE_ROOT_USER -u OBJECTSTORE_ROOT_PASSWORD \
    -u INGEST_DATABASE_URL -u PROCESSOR_DATABASE_URL -u EXPORTER_DATABASE_URL \
    -u QUERY_DATABASE_URL -u GC_DATABASE_URL \
    -u INGEST_S3_SECRET_KEY -u PROCESSOR_S3_SECRET_KEY -u EXPORTER_S3_SECRET_KEY \
    -u QUERY_S3_SECRET_KEY -u GC_S3_SECRET_KEY \
    -u INGEST_AUTH_TOKEN -u INGEST_PROVISIONING_TOKEN -u METRICS_AUTH_TOKEN \
    -u OIDC_ISSUER -u OIDC_AUDIENCE -u OIDC_JWKS_URL \
    -u OIDC_CLIENT_ID -u OIDC_AUTHORIZATION_URL -u OIDC_TOKEN_URL \
    -u OIDC_END_SESSION_URL -u OIDC_SCOPES -u QUERY_PUBLIC_HOST \
    -u QUERY_TLS_CERT_FILE -u QUERY_TLS_KEY_FILE -u QUERY_TLS_CA_FILE \
    -u INGEST_PUBLIC_HOST -u INGEST_TLS_CERT_FILE -u INGEST_TLS_KEY_FILE \
    -u INGEST_TLS_CA_FILE -u INGEST_GRPC_PORT -u INGEST_SECURITY_PROFILE \
    -u ROBOT_VAULT_SECRETS_GID \
    -u AUTH_MODE -u OIDC_DEV -u ROBOT_VAULT_RELEASE \
    -u OIDC_ALGORITHMS -u OIDC_ROLE_SOURCE \
    -u S3_BUCKET -u S3_REGION -u EXPORTER_MAX_ATTEMPTS \
    -u DUCKDB_THREADS -u DUCKDB_MEMORY_LIMIT_MB -u DUCKDB_QUERY_CONCURRENCY \
    -u DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS -u DUCKDB_QUERY_TIMEOUT_SECONDS \
    -u QUERY_MAX_SOURCE_RANGE_SECONDS -u QUERY_MAX_SOURCE_FILES \
    -u QUERY_MAX_SOURCE_ROWS -u QUERY_MAX_SOURCE_BYTES \
    -u SQL_CONSOLE_MEMORY_LIMIT_MB \
    -u TLS_ENABLED -u TLS_MTLS \
    -u TLS_SERVER_CERT -u TLS_SERVER_KEY -u TLS_CA_CERT \
    -u GC_MIN_AGE_HOURS -u GC_MPU_MAX_AGE_HOURS -u GC_DELETE_LIMIT \
    -u GC_DRY_RUN -u GC_PREFIXES -u GC_METRICS_PORT -u GC_INTERVAL_SECONDS \
    -u ROBOT_VAULT_LOG_MAX_SIZE -u ROBOT_VAULT_LOG_MAX_FILES \
    -u FIRST_PARTY_PIDS_LIMIT -u POSTGRES_SHM_SIZE \
    -u POSTGRES_MEMORY_LIMIT -u POSTGRES_CPU_LIMIT -u POSTGRES_PIDS_LIMIT \
    -u OBJECTSTORE_MEMORY_LIMIT -u OBJECTSTORE_CPU_LIMIT -u OBJECTSTORE_PIDS_LIMIT \
    -u OBJECTSTORE_BOOTSTRAP_MEMORY_LIMIT -u OBJECTSTORE_BOOTSTRAP_CPU_LIMIT \
    -u REDIS_MEMORY_LIMIT -u REDIS_CPU_LIMIT -u REDIS_PIDS_LIMIT \
    -u DB_MIGRATE_MEMORY_LIMIT -u DB_MIGRATE_CPU_LIMIT \
    -u DB_BOOTSTRAP_MEMORY_LIMIT -u DB_BOOTSTRAP_CPU_LIMIT \
    -u INGEST_MEMORY_LIMIT -u INGEST_CPU_LIMIT -u INGEST_PIDS_LIMIT \
    -u PROCESSOR_MEMORY_LIMIT -u PROCESSOR_CPU_LIMIT -u PROCESSOR_PIDS_LIMIT \
    -u QUERY_MEMORY_LIMIT -u QUERY_CPU_LIMIT -u QUERY_PIDS_LIMIT \
    -u GC_MEMORY_LIMIT -u GC_CPU_LIMIT -u GC_PIDS_LIMIT \
    -u PROMETHEUS_MEMORY_LIMIT -u PROMETHEUS_CPU_LIMIT -u PROMETHEUS_PIDS_LIMIT \
    -u PROMETHEUS_RETENTION_TIME -u PROMETHEUS_RETENTION_SIZE \
    -u ENABLE_HSTS -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME \
    -u COMPOSE_PROFILES -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
    docker compose "$@"
}

# render_and_assert <edition> [compose args, e.g. -f <overlay>]: renders to
# $TMP_DIR/<edition>.json and checks what every edition shares.
render_and_assert() {
  local edition="$1"
  shift
  local rendered="$TMP_DIR/$edition.json"
  local stage_enabled="true"
  local -a profile_args=("$@")
  if [[ "$edition" == "core" ]]; then
    stage_enabled="false"
  fi

  ROBOT_VAULT_EDITION="$edition" SEGMENTATION_ENABLED="$stage_enabled" \
    QA_ENABLED="$stage_enabled" \
    MEDIA_ENCODE_ENABLED="$stage_enabled" clean_compose \
    -p "robot-vault-release-config-$edition" \
    --env-file "$VALID_ENV" \
    -f "$COMPOSE_FILE" \
    "${profile_args[@]}" \
    config --format json >"$rendered"

  python3 - "$rendered" "$edition" "$TEST_SECRETS_GID" "$ROOT_DIR" <<'PY'
import json
import pathlib
import re
import sys

path, edition, secrets_gid, root_dir = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    config = json.load(handle)

services = config["services"]
query = services["query"]
query_env = query["environment"]
ingest = services["ingest"]
ingest_env = ingest["environment"]
processor_env = services["processor"]["environment"]
expected = {
    "AUTH_MODE": "oidc",
    "OIDC_DEV": "false",
    "ROBOT_VAULT_RELEASE": "true",
    "OIDC_ALGORITHMS": "RS256",
    "OIDC_ROLE_SOURCE": "claim",
    "ENABLE_HSTS": "true",
    "OIDC_ISSUER": "https://identity.example.invalid/realms/robot-vault",
    "OIDC_AUDIENCE": "robot-vault-release",
    "OIDC_JWKS_URL": "https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/certs",
    "OIDC_CLIENT_ID": "robot-vault-browser",
    "OIDC_AUTHORIZATION_URL": "https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/auth",
    "OIDC_TOKEN_URL": "https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/token",
    "OIDC_END_SESSION_URL": "https://identity.example.invalid/realms/robot-vault/protocol/openid-connect/logout",
    "OIDC_SCOPES": "openid profile email",
    "QUERY_PUBLIC_HOST": "vault.example.invalid",
    "INGEST_PROVISIONING_TOKEN": "release-config-provisioning-token-32chars",
    "METRICS_AUTH_TOKEN": "release-config-metrics-token-32chars",
    "ROBOT_VAULT_EDITION": edition,
    "ROBOT_VAULT_VERSION": "release-config-test",
}
for name, value in expected.items():
    actual = str(query_env.get(name, "")).lower() if name in {"OIDC_DEV", "ENABLE_HSTS", "ROBOT_VAULT_RELEASE"} else query_env.get(name)
    assert actual == value, f"{edition}: query {name} rendered as {actual!r}, expected {value!r}"

# Core runs the local core images and defines no exporter; an edition
# overlay checks its own images.
if edition == "core":
    assert "exporter" not in services, "core: the core files define an exporter"
    assert services["processor"]["image"] == "robot-vault-core-processor:release-config-test", services["processor"]["image"]
    assert query["image"] == "robot-vault-core-query:release-config-test", query["image"]
# MCP is core: on by default, write tools off, the public origin from the host.
assert query_env["MCP_ENABLED"] == "true", query_env.get("MCP_ENABLED")
assert query_env["MCP_WRITE_TOOLS"] == "false", query_env.get("MCP_WRITE_TOOLS")
assert query_env["PUBLIC_BASE_URL"] == "https://vault.example.invalid", query_env.get("PUBLIC_BASE_URL")
stage_enabled = "true" if edition == "pro" else "false"
assert processor_env["ROBOT_VAULT_EDITION"] == edition
assert processor_env["SEGMENTATION_ENABLED"] == stage_enabled
assert processor_env["QA_ENABLED"] == stage_enabled
assert processor_env["MEDIA_ENCODE_ENABLED"] == stage_enabled

for name, value in {
    "INGEST_PUBLIC_HOST": "ingest.example.invalid",
    "INGEST_PROVISIONING_TOKEN": "release-config-provisioning-token-32chars",
    "INGEST_SECURITY_PROFILE": "token",
    "INGEST_PUBLIC_PORT": "50051",
    "TLS_ENABLED": "true",
    "TLS_SERVER_CERT": "/run/robot-vault/tls/ingest.crt",
    "TLS_SERVER_KEY": "/run/robot-vault/tls/ingest.key",
    "TLS_CA_CERT": "/run/robot-vault/tls/ingest-ca.pem",
}.items():
    assert ingest_env[name] == value, f"{edition}: ingest {name}={ingest_env[name]!r}"
# Ingest derives TLS_MTLS from the profile; the env file's TLS_MTLS=false poison
# must not reach it.
assert "TLS_MTLS" not in ingest_env, f"{edition}: ingest sets TLS_MTLS"
assert query_env["INGEST_CA_CERT_FILE"] == "/run/robot-vault/tls/ingest-ca.pem"
assert "ROBOT_VAULT_RELEASE" not in ingest_env, f"{edition}: ingest must not set ROBOT_VAULT_RELEASE"
assert query_env["INGEST_PROVISIONING_TOKEN"] == \
    ingest_env["INGEST_PROVISIONING_TOKEN"]
provisioning_token_holders = {
    name
    for name, service in services.items()
    if "INGEST_PROVISIONING_TOKEN" in service.get("environment", {})
}
assert provisioning_token_holders == {"ingest", "query"}, provisioning_token_holders

first_party = ["db-migrate", "postgres-roles-ready", "ingest", "processor", "query", "gc"]
for service_name in first_party + ["objectstore-access-ready"]:
    service = services[service_name]
    assert service.get("pull_policy") == "never", (
        f"{edition}: {service_name} may pull a first-party image: {service!r}"
    )
for service_name in first_party + ["objectstore-access-ready"]:
    service = services[service_name]
    assert service.get("user") == "10001:10001", service
    assert service.get("read_only") is True, service
    assert service.get("cap_drop") == ["ALL"], service
    assert "no-new-privileges:true" in service.get("security_opt", []), service
    assert service.get("tmpfs") == [
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777"
    ], service
    assert service.get("init") is True, service
    assert int(service.get("pids_limit", 0)) > 0, service
    assert service.get("logging", {}).get("driver") == "json-file", service
    assert service["logging"]["options"] == {
        "max-file": "4", "max-size": "17m"
    }, service
    for name, value in {
        "HOME": "/tmp/robot-vault",
        "XDG_CACHE_HOME": "/tmp/robot-vault/.cache",
        "TMPDIR": "/tmp",
    }.items():
        assert service["environment"][name] == value, (service_name, name, service)

for service_name in ("postgres", "redis"):  # objectstore is our local image (robot-vault-objectstore:<version>), checked below
    image = services[service_name].get("image", "")
    assert re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image), (
        service_name, image
    )
bootstrap = services["objectstore-access-ready"]
assert bootstrap.get("image") == "robot-vault-toolkit:release-config-test", bootstrap
assert bootstrap.get("pull_policy") == "never", bootstrap

# The object store, exactly as development/objectstore/CONTRACT.md freezes it.
contract = (pathlib.Path(root_dir) / "development/objectstore/CONTRACT.md").read_text()
store = services["objectstore"]
assert store["image"] == "robot-vault-objectstore:release-config-test", store["image"]
assert store.get("pull_policy") == "never", store
assert "`robot-vault-objectstore:<version>`" in contract and "rustfs/rustfs:1.0.6@sha256:ee717072" in contract
assert not store.get("user") and not store.get("entrypoint") and not store.get("command"), store
assert store["environment"] == {
    "RUSTFS_ACCESS_KEY": "release-config-root-user-000001",
    "RUSTFS_SECRET_KEY_FILE": "/run/secrets/objectstore_root_password",
    "RUSTFS_VOLUMES": "/data",
    "RUSTFS_ADDRESS": "0.0.0.0:9000",
    "RUSTFS_CONSOLE_ENABLE": "false",
    "RUSTFS_DURABILITY_MODE": "strict",
    "RUSTFS_DRIVE_SYNC_ENABLE": "true",
    "RUSTFS_NEW_BUCKET_DURABILITY_MODE": "strict",
    "RUSTFS_PUT_RENAME_EARLY_ACK_ENABLE": "false",
    "RUSTFS_API_STALE_UPLOADS_EXPIRY": "360h",
    "RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL": "6h",
}, store["environment"]
assert "ports" not in store and "expose" not in store, store
assert [(v["type"], v["source"], v["target"]) for v in store["volumes"]] == [
    ("volume", "objectstore_data", "/data")
], store["volumes"]
assert store["healthcheck"]["test"] == [
    "CMD", "curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:9000/health/ready"
], store["healthcheck"]
assert [item["source"] for item in store["secrets"]] == ["objectstore_root_password"]
assert not bootstrap.get("secrets"), bootstrap
assert config["secrets"]["objectstore_root_password"]["environment"] == "OBJECTSTORE_ROOT_PASSWORD"
assert "file" not in config["secrets"]["objectstore_root_password"]
for service_name, service in services.items():
    env = service.get("environment") or {}
    if service_name != "objectstore-access-ready":
        assert "OBJECTSTORE_ROOT_PASSWORD" not in env, service_name
        assert "release-config-objectstore-root-secret-000000001" not in json.dumps(service), service_name
    if service_name != "objectstore":
        assert not [k for k in env if k.startswith("RUSTFS_")], service_name
    if service_name != "objectstore":
        assert not service.get("secrets"), service_name

bounded_services = [
    "postgres", "objectstore", "objectstore-access-ready", "redis", "db-migrate",
    "postgres-roles-ready", "ingest", "processor", "query", "gc",
]
for service_name in bounded_services:
    service = services[service_name]
    assert int(service.get("mem_limit", 0)) > 0, (service_name, service)
    assert float(service.get("cpus", 0)) > 0, (service_name, service)
assert int(services["query"]["mem_limit"]) == 3 * 1024 * 1024 * 1024
for service_name in ("postgres", "redis"):  # objectstore is our local image (robot-vault-objectstore:<version>), checked below
    service = services[service_name]
    assert int(service.get("pids_limit", 0)) > 0, (service_name, service)
    assert service.get("logging", {}).get("driver") == "json-file", service

assert ingest.get("group_add") == [secrets_gid], ingest
assert query.get("group_add") == [secrets_gid], query
expected_writable = {
    "db-migrate": {},
    "postgres-roles-ready": {},
    "objectstore-access-ready": {},
    "ingest": {"/app/uploads": "uploads"},
    "processor": {"/var/lib/robot-vault/processor": "processor_work"},
    "query": {"/var/lib/robot-vault/query": "query_work"},
    "gc": {},
}
for service_name, expected_volumes in expected_writable.items():
    writable = {
        item["target"]: item["source"]
        for item in services[service_name].get("volumes", [])
        if not item.get("read_only", False)
    }
    assert writable == expected_volumes, (service_name, writable)

assert processor_env["MEDIA_SPOOL_DIR"] == "/var/lib/robot-vault/processor"
assert query_env["DUCKDB_TEMP_DIRECTORY"] == "/var/lib/robot-vault/query"

owner_database_url = "postgresql://robotvault:p%40ss%3A%2F%3F%23%25%20value@postgres:5432/robotvault"
assert services["db-migrate"]["environment"]["DATABASE_URL"] == owner_database_url
role_bootstrap = services["postgres-roles-ready"]["environment"]
assert role_bootstrap["DATABASE_URL"] == owner_database_url
runtime_database_urls = {
    "ingest": ("INGEST_DATABASE_URL", "robot_vault_ingest"),
    "processor": ("PROCESSOR_DATABASE_URL", "robot_vault_processor"),
    "exporter": ("EXPORTER_DATABASE_URL", "robot_vault_exporter"),
    "query": ("QUERY_DATABASE_URL", "robot_vault_query"),
    "gc": ("GC_DATABASE_URL", "robot_vault_gc"),
}
for service_name, (variable, username) in runtime_database_urls.items():
    assert role_bootstrap[variable].startswith(f"postgresql://{username}:")
    if service_name in services:
        assert services[service_name]["environment"]["DATABASE_URL"] == role_bootstrap[variable]
        assert services[service_name]["environment"]["DATABASE_URL"] != owner_database_url

ready_services = ["postgres", "objectstore", "redis", "ingest", "processor", "query", "gc"]
for service_name in ready_services:
    assert services[service_name].get("healthcheck", {}).get("test"), (
        f"{edition}: {service_name} has no readiness healthcheck"
    )

for service_name in ["ingest", "processor", "query", "gc"]:
    storage = services[service_name]["environment"]
    assert storage["S3_BUCKET"] == "release-config-data"
    assert storage["S3_ENDPOINT"] == "http://objectstore:9000"
    assert storage["S3_REGION"] == "ap-south-1"
    assert storage["S3_SECURE"] == "false"
assert query_env["S3_PARQUET_BUCKET"] == "release-config-data"
assert query_env["DUCKDB_THREADS"] == "3"
assert query_env["DUCKDB_MEMORY_LIMIT_MB"] == "768"
assert query_env["DUCKDB_QUERY_CONCURRENCY"] == "6"
assert query_env["DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS"] == "7"
assert query_env["DUCKDB_QUERY_TIMEOUT_SECONDS"] == "45"
assert query_env["QUERY_MAX_SOURCE_RANGE_SECONDS"] == "1209600"
assert query_env["QUERY_MAX_SOURCE_FILES"] == "4000"
assert query_env["QUERY_MAX_SOURCE_ROWS"] == "9000000"
assert query_env["QUERY_MAX_SOURCE_BYTES"] == "17179869184"
assert query_env["SQL_CONSOLE_MEMORY_LIMIT_MB"] == "700"
gc_env = services["gc"]["environment"]
assert gc_env["GC_MIN_AGE_HOURS"] == "72"
assert gc_env["GC_MPU_MAX_AGE_HOURS"] == "48"
assert gc_env["GC_DELETE_LIMIT"] == "500"
assert gc_env["GC_DRY_RUN"] == "true"
assert gc_env["GC_INTERVAL_SECONDS"] == "1800"
assert gc_env["GC_PREFIXES"] == "raw/,manifests/,processed/parquet/,cas/files/,processed/media/,qa/reports/,exports/"
assert gc_env["METRICS_PORT"] == "9108"
assert bootstrap["environment"]["S3_BUCKET"] == "release-config-data"
assert bootstrap["environment"]["S3_ENDPOINT"] == "http://objectstore:9000"
assert bootstrap["entrypoint"] == ["python3", "/release/objectstore_bootstrap.py"]
assert {
    item["target"]: (item["source"], item["mode"])
    for item in bootstrap["configs"]
} == {
    "/release/objectstore_bootstrap.py": ("objectstore_bootstrap", "0444"),
    "/release/objectstore-policies/ingest.json": ("objectstore_policy_ingest", "0444"),
    "/release/objectstore-policies/processor.json": ("objectstore_policy_processor", "0444"),
    "/release/objectstore-policies/exporter.json": ("objectstore_policy_exporter", "0444"),
    "/release/objectstore-policies/query.json": ("objectstore_policy_query", "0444"),
    "/release/objectstore-policies/gc.json": ("objectstore_policy_gc", "0444"),
}
assert bootstrap["environment"]["OBJECTSTORE_ROOT_PASSWORD"] == \
    "release-config-objectstore-root-secret-000000001"
assert bootstrap["environment"]["OBJECTSTORE_ROOT_USER"] == \
    store["environment"]["RUSTFS_ACCESS_KEY"] == "release-config-root-user-000001"
runtime_s3 = {
    "ingest": ("robot-vault-ingest", "INGEST_S3_SECRET_KEY"),
    "processor": ("robot-vault-processor", "PROCESSOR_S3_SECRET_KEY"),
    "exporter": ("robot-vault-exporter", "EXPORTER_S3_SECRET_KEY"),
    "query": ("robot-vault-query", "QUERY_S3_SECRET_KEY"),
    "gc": ("robot-vault-gc", "GC_S3_SECRET_KEY"),
}
for service_name, (access_key, secret_variable) in runtime_s3.items():
    assert bootstrap["environment"][f"{service_name.upper()}_S3_ACCESS_KEY"] == access_key
    if service_name in services:
        service_env = services[service_name]["environment"]
        assert service_env["AWS_ACCESS_KEY_ID"] == access_key
        assert service_env["AWS_SECRET_ACCESS_KEY"] == bootstrap["environment"][secret_variable]
        assert "OBJECTSTORE_ROOT_USER" not in service_env

command = query["command"]
assert command == [
    "uvicorn", "query_service.main:app", "--host", "0.0.0.0", "--port", "8003",
    "--ssl-certfile", "/run/robot-vault/tls/query.crt",
    "--ssl-keyfile", "/run/robot-vault/tls/query.key",
], f"{edition}: unexpected query TLS command: {command!r}"

ports = query["ports"]
assert len(ports) == 1, f"{edition}: query must publish exactly one port: {ports!r}"
assert int(ports[0]["target"]) == 8003 and str(ports[0]["published"]) == "443", ports

published = {
    name: [int(item["target"]) for item in service.get("ports", [])]
    for name, service in services.items()
    if service.get("ports")
}
assert published == {"ingest": [50051], "query": [8003]}, published
assert str(ingest["ports"][0]["published"]) == "50051", ingest["ports"]

by_target = {item.get("target"): item for item in query["volumes"] if isinstance(item, dict)}
for target in ("/run/robot-vault/tls/query.crt", "/run/robot-vault/tls/query.key"):
    mount = by_target[target]
    assert mount["type"] == "bind" and mount["read_only"] is True, mount
    assert mount["bind"].get("create_host_path", False) is False, mount

ingest_by_target = {
    item.get("target"): item
    for item in ingest["volumes"]
    if isinstance(item, dict)
}
for target in (
    "/run/robot-vault/tls/ingest.crt",
    "/run/robot-vault/tls/ingest.key",
    "/run/robot-vault/tls/ingest-ca.pem",
):
    mount = ingest_by_target[target]
    assert mount["type"] == "bind" and mount["read_only"] is True, mount
    assert mount["bind"].get("create_host_path", False) is False, mount
# Query serves the same ingest CA file at /ca.crt.
query_ca_mount = by_target["/run/robot-vault/tls/ingest-ca.pem"]
assert query_ca_mount["read_only"] is True, query_ca_mount
assert query_ca_mount["source"] == ingest_by_target["/run/robot-vault/tls/ingest-ca.pem"]["source"]
assert "/app/certs" not in ingest_by_target

health_test = " ".join(query["healthcheck"]["test"])
assert "https://127.0.0.1:8003/readyz" in health_test, health_test
assert "http://127.0.0.1:8003" not in health_test, health_test
assert "/healthz" not in health_test, health_test
ingest_health_test = " ".join(ingest["healthcheck"]["test"])
assert ingest["healthcheck"]["test"] == ["CMD", "ingest-server", "--healthcheck"], ingest_health_test
assert "/admin/" not in ingest_health_test, ingest_health_test
PY
}

# assert_obs_profile <edition> [compose args]: renders to $TMP_DIR/obs.json.
assert_obs_profile() {
  local edition="$1"
  shift
  local rendered="$TMP_DIR/obs.json"
  local generated="$TMP_DIR/prometheus-$edition.yml"
  local -a profile_args=(--profile obs "$@")
  local stage_enabled="true"
  if [[ "$edition" == "core" ]]; then
    stage_enabled="false"
  fi
  ROBOT_VAULT_EDITION="$edition" SEGMENTATION_ENABLED="$stage_enabled" \
    QA_ENABLED="$stage_enabled" \
    MEDIA_ENCODE_ENABLED="$stage_enabled" clean_compose \
    -p robot-vault-release-config-obs \
    --env-file "$VALID_ENV" \
    -f "$COMPOSE_FILE" \
    "${profile_args[@]}" config --format json >"$rendered"

  python3 - "$rendered" "$edition" "$generated" <<'PY'
import json
import os
import re
import shlex
import subprocess
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    services = json.load(handle)["services"]
prometheus = services["prometheus"]
edition, generated = sys.argv[2:]
if edition == "core":
    assert "exporter" not in services
assert re.fullmatch(
    r"[^@\s]+@sha256:[0-9a-f]{64}", prometheus.get("image", "")
), prometheus.get("image")
assert prometheus["environment"]["QUERY_PUBLIC_HOST"] == "vault.example.invalid"
assert prometheus["environment"]["ROBOT_VAULT_EDITION"] == edition
assert prometheus["environment"]["METRICS_AUTH_TOKEN"] == "release-config-metrics-token-32chars"
assert prometheus["environment"]["PROMETHEUS_RETENTION_TIME"] == "21d"
assert prometheus["environment"]["PROMETHEUS_RETENTION_SIZE"] == "12GB"
assert prometheus["environment"]["ALERTMANAGER_ENDPOINT"] == ""
assert prometheus["entrypoint"] == ["/bin/sh", "-eu", "-c"]
assert int(prometheus["mem_limit"]) > 0
assert float(prometheus["cpus"]) > 0
assert int(prometheus["pids_limit"]) > 0
assert prometheus["logging"]["options"] == {"max-file": "4", "max-size": "17m"}
assert prometheus.get("healthcheck", {}).get("test")
# Core mounts its rules; an edition overlay adds only files under the two globs.
configs = {item["target"]: item["source"] for item in prometheus.get("configs", [])}
assert configs["/etc/prometheus/rules/release-alerts.yml"] == "prometheus_release_alerts", configs
assert all(t.startswith(("/etc/prometheus/rules/", "/etc/prometheus/scrape.d/")) for t in configs), configs
if edition == "core":
    assert len(configs) == 1, configs
command = "\n".join(prometheus["command"])
assert "targets: ['query:8003']" in command
assert "scheme: https" in command
assert "server_name: $${host}" in command
assert "ca_file: /run/robot-vault/tls/query-ca.pem" in command
assert "credentials_file: /tmp/query-metrics-token" in command
assert "release-config-metrics-token-32chars" not in command
assert "insecure_skip_verify" not in command
assert "robot-vault-query" not in command
assert "rule_files:" in command
assert "- /etc/prometheus/rules/*.yml" in command
assert re.search(r"scrape_config_files:\n\s*- /etc/prometheus/scrape\.d/\*\.yml", command), command
assert "exporter" not in command, "the core Prometheus config names a Pro service"
assert '--storage.tsdb.retention.time="$${PROMETHEUS_RETENTION_TIME}"' in command
assert '--storage.tsdb.retention.size="$${PROMETHEUS_RETENTION_SIZE}"' in command

mounts = {
    item.get("target"): item
    for item in prometheus["volumes"]
    if isinstance(item, dict)
}
ca_mount = mounts["/run/robot-vault/tls/query-ca.pem"]
assert ca_mount["type"] == "bind" and ca_mount["read_only"] is True
assert ca_mount["bind"].get("create_host_path", False) is False

# Exercise the exact generated shell payload without starting Prometheus. A
# config render retains Compose's $$ escape; the runtime hands one $ to sh.
script = prometheus["command"][0].replace("$$", "$")
script = script.replace("/tmp/prometheus.release.yml", shlex.quote(generated))
token_file = generated + ".token"
script = script.replace("/tmp/query-metrics-token", shlex.quote(token_file))
script = script[:script.index("exec /bin/prometheus")] + ":"
environment = dict(
    os.environ,
    QUERY_PUBLIC_HOST="vault.example.invalid",
    ROBOT_VAULT_EDITION=edition,
    METRICS_AUTH_TOKEN="release-config-metrics-token-32chars",
    PROMETHEUS_RETENTION_TIME="21d",
    PROMETHEUS_RETENTION_SIZE="12GB",
    ALERTMANAGER_ENDPOINT="",
)
subprocess.run(["sh", "-eu", "-c", script], check=True, env=environment)
with open(generated, encoding="utf-8") as handle:
    generated_config = handle.read()
assert "server_name: vault.example.invalid" in generated_config
assert "ca_file: /run/robot-vault/tls/query-ca.pem" in generated_config
assert f"credentials_file: {token_file}" in generated_config
assert "release-config-metrics-token-32chars" not in generated_config
assert "rule_files:" in generated_config
assert "/etc/prometheus/rules/*.yml" in generated_config
assert "/etc/prometheus/scrape.d/*.yml" in generated_config
with open(token_file, encoding="utf-8") as handle:
    assert handle.read() == "release-config-metrics-token-32chars"
assert os.stat(token_file).st_mode & 0o077 == 0
# The exporter job comes from an overlay's scrape file, never from this script.
assert "job_name: exporter" not in generated_config
assert "alerting:" not in generated_config

# The same startup script that writes the production config accepts only a
# bounded endpoint and emits a normal TLS-verifying Prometheus alert route.
environment["ALERTMANAGER_ENDPOINT"] = "https://alerts.example.invalid:9093"
subprocess.run(["sh", "-eu", "-c", script], check=True, env=environment)
with open(generated, encoding="utf-8") as handle:
    routed_config = handle.read()
assert "alerting:\n  alertmanagers:\n    - scheme: https" in routed_config
assert "targets: ['alerts.example.invalid:9093']" in routed_config
assert "insecure_skip_verify" not in routed_config
for malicious in ("ftp://alerts.example.invalid:9093", "http://a:0", "https://a:65536", "http://a:9093/path", "http://a:9093'\\n  evil: true"):
    environment["ALERTMANAGER_ENDPOINT"] = malicious
    result = subprocess.run(["sh", "-eu", "-c", script], env=environment, capture_output=True, text=True)
    assert result.returncode == 64, (malicious, result.returncode, result.stderr)
PY
}

assert_required() {
  local missing="$1"
  local incomplete_env="$TMP_DIR/missing-$missing.env"
  awk -v name="$missing" 'index($0, name "=") != 1' "$VALID_ENV" >"$incomplete_env"

  if clean_compose \
      -p robot-vault-release-config-missing \
      --env-file "$incomplete_env" \
      -f "$COMPOSE_FILE" \
      config --quiet >/dev/null 2>&1; then
    echo "release config unexpectedly rendered without $missing" >&2
    exit 1
  fi
}

expect_make_failure() {
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "release Make preflight unexpectedly accepted $description" >&2
    exit 1
  fi
}

for edition in "${EDITIONS[@]}"; do
  render_and_assert "$edition"
  assert_obs_profile "$edition"
done

# The core files alone cannot become a Pro deployment: the preflight refuses
# a pro request without the Pro overlay, with a message that says so.
ROBOT_VAULT_EDITION=pro SEGMENTATION_ENABLED=true QA_ENABLED=true MEDIA_ENCODE_ENABLED=true \
  clean_compose -p robot-vault-release-config-pro-on-core --env-file "$VALID_ENV" \
  -f "$COMPOSE_FILE" --profile obs config --format json >"$TMP_DIR/pro-on-core.json"
if python3 "$ROOT_DIR/development/release/validate_release_inputs.py" --env-file "$VALID_ENV" \
    --config "$TMP_DIR/pro-on-core.json" --edition pro 2>"$TMP_DIR/pro-on-core.err"; then
  echo "the preflight accepted EDITION=pro on the core release files" >&2; exit 1
fi
grep -F "needs the Pro release overlay" "$TMP_DIR/pro-on-core.err" >/dev/null || {
  echo "pro-on-core refusal does not name the overlay: $(cat "$TMP_DIR/pro-on-core.err")" >&2; exit 1; }

for name in \
  ROBOT_VAULT_VERSION POSTGRES_PASSWORD DATABASE_URL OBJECTSTORE_ROOT_USER OBJECTSTORE_ROOT_PASSWORD \
  INGEST_AUTH_TOKEN INGEST_PROVISIONING_TOKEN \
  INGEST_DATABASE_URL PROCESSOR_DATABASE_URL EXPORTER_DATABASE_URL QUERY_DATABASE_URL GC_DATABASE_URL \
  INGEST_S3_SECRET_KEY PROCESSOR_S3_SECRET_KEY EXPORTER_S3_SECRET_KEY QUERY_S3_SECRET_KEY GC_S3_SECRET_KEY \
  METRICS_AUTH_TOKEN \
  INGEST_PUBLIC_HOST INGEST_TLS_CERT_FILE INGEST_TLS_KEY_FILE INGEST_TLS_CA_FILE \
  OIDC_ISSUER OIDC_AUDIENCE OIDC_JWKS_URL OIDC_CLIENT_ID \
  OIDC_AUTHORIZATION_URL OIDC_TOKEN_URL QUERY_PUBLIC_HOST \
  QUERY_TLS_CERT_FILE QUERY_TLS_KEY_FILE QUERY_TLS_CA_FILE \
  ROBOT_VAULT_EDITION SEGMENTATION_ENABLED QA_ENABLED MEDIA_ENCODE_ENABLED; do
  assert_required "$name"
done

# RELEASE_ENV_FILE is mandatory for every operational target, and
# secret-bearing files must be private.
for target in release-config release-up release-ps release-down; do
  expect_make_failure "a missing RELEASE_ENV_FILE for $target" \
    make --no-print-directory -s -C "$ROOT_DIR" "$target" EDITION=core
done

OPEN_ENV="$TMP_DIR/open.env"
cp "$VALID_ENV" "$OPEN_ENV"
chmod 0644 "$OPEN_ENV"
expect_make_failure "a group/world-readable RELEASE_ENV_FILE" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$OPEN_ENV"

MISSING_KEY_ENV="$TMP_DIR/missing-key.env"
sed "s#^QUERY_TLS_KEY_FILE=.*#QUERY_TLS_KEY_FILE=$TMP_DIR/absent.key#" \
  "$VALID_ENV" >"$MISSING_KEY_ENV"
chmod 0600 "$MISSING_KEY_ENV"
expect_make_failure "a missing TLS private key" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISSING_KEY_ENV"

chmod 0644 "$KEY_FILE"
expect_make_failure "a world-readable TLS private key" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$VALID_ENV"
chmod 0640 "$KEY_FILE"

WRONG_SECRETS_GROUP_ENV="$TMP_DIR/wrong-secrets-group.env"
sed "s/^ROBOT_VAULT_SECRETS_GID=.*/ROBOT_VAULT_SECRETS_GID=$(( TEST_SECRETS_GID + 1 ))/" \
  "$VALID_ENV" >"$WRONG_SECRETS_GROUP_ENV"
chmod 0600 "$WRONG_SECRETS_GROUP_ENV"
expect_make_failure "a TLS private key outside the configured secrets group" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$WRONG_SECRETS_GROUP_ENV"

chmod 0664 "$CERT_FILE"
expect_make_failure "a group-writable TLS certificate" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$VALID_ENV"
chmod 0644 "$CERT_FILE"

chmod 0664 "$CA_FILE"
expect_make_failure "a group-writable TLS CA bundle" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$VALID_ENV"
chmod 0644 "$CA_FILE"

chmod 0640 "$CA_FILE"
expect_make_failure "a CA bundle unreadable by non-root Prometheus" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$VALID_ENV"
chmod 0644 "$CA_FILE"

MISSING_CA_ENV="$TMP_DIR/missing-ca.env"
sed "s#^QUERY_TLS_CA_FILE=.*#QUERY_TLS_CA_FILE=$TMP_DIR/absent-ca.pem#" \
  "$VALID_ENV" >"$MISSING_CA_ENV"
chmod 0600 "$MISSING_CA_ENV"
expect_make_failure "a missing TLS CA bundle" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISSING_CA_ENV"

chmod 0644 "$INGEST_KEY_FILE"
expect_make_failure "a world-readable ingest TLS private key" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$VALID_ENV"
chmod 0640 "$INGEST_KEY_FILE"

MISMATCHED_INGEST_KEY_ENV="$TMP_DIR/mismatched-ingest-key.env"
sed "s#^INGEST_TLS_KEY_FILE=.*#INGEST_TLS_KEY_FILE=$KEY_FILE#" \
  "$VALID_ENV" >"$MISMATCHED_INGEST_KEY_ENV"
chmod 0600 "$MISMATCHED_INGEST_KEY_ENV"
expect_make_failure "an ingest certificate/private-key mismatch" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISMATCHED_INGEST_KEY_ENV"

WRONG_QUERY_SAN_ENV="$TMP_DIR/wrong-query-san.env"
sed 's/^QUERY_PUBLIC_HOST=.*/QUERY_PUBLIC_HOST=other.example.invalid/' \
  "$VALID_ENV" >"$WRONG_QUERY_SAN_ENV"
chmod 0600 "$WRONG_QUERY_SAN_ENV"
expect_make_failure "a query certificate SAN mismatch" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$WRONG_QUERY_SAN_ENV"

# OpenSSL's generic hostname verifier accepts a legacy Common Name when SAN is
# absent. The release contract intentionally requires an explicit DNS SAN.
NO_SAN_EXT_FILE="$TMP_DIR/query-no-san-ext.cnf"
grep -v '^subjectAltName=' "$QUERY_EXT_FILE" >"$NO_SAN_EXT_FILE"
NO_SAN_CERT_FILE="$TMP_DIR/query-no-san.pem"
openssl x509 -req -sha256 -days 30 -in "$TMP_DIR/query.csr" \
  -CA "$CA_FILE" -CAkey "$CA_KEY_FILE" -CAserial "$TMP_DIR/release-ca.srl" \
  -extfile "$NO_SAN_EXT_FILE" -out "$NO_SAN_CERT_FILE" >/dev/null 2>&1
chmod 0644 "$NO_SAN_CERT_FILE"
NO_SAN_ENV="$TMP_DIR/query-no-san.env"
sed "s#^QUERY_TLS_CERT_FILE=.*#QUERY_TLS_CERT_FILE=$NO_SAN_CERT_FILE#" \
  "$VALID_ENV" >"$NO_SAN_ENV"
chmod 0600 "$NO_SAN_ENV"
expect_make_failure "a query certificate without a DNS SAN" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$NO_SAN_ENV"

MALFORMED_CERT_FILE="$TMP_DIR/malformed-cert.pem"
printf '%s\n' 'not a certificate' >"$MALFORMED_CERT_FILE"
chmod 0644 "$MALFORMED_CERT_FILE"
MALFORMED_CERT_ENV="$TMP_DIR/malformed-cert.env"
sed "s#^QUERY_TLS_CERT_FILE=.*#QUERY_TLS_CERT_FILE=$MALFORMED_CERT_FILE#" \
  "$VALID_ENV" >"$MALFORMED_CERT_ENV"
chmod 0600 "$MALFORMED_CERT_ENV"
expect_make_failure "malformed query certificate PEM" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MALFORMED_CERT_ENV"

NON_CA_ENV="$TMP_DIR/non-ca.env"
sed "s#^INGEST_TLS_CA_FILE=.*#INGEST_TLS_CA_FILE=$CERT_FILE#" \
  "$VALID_ENV" >"$NON_CA_ENV"
chmod 0600 "$NON_CA_ENV"
expect_make_failure "a non-CA ingest trust bundle" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$NON_CA_ENV"

# The ingest CA file holds exactly one certificate: the root robots pin.
TWO_CA_FILE="$TMP_DIR/two-ca.pem"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 30 \
  -subj '/CN=Robot Vault second test CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout "$TMP_DIR/second-ca-key.pem" -out "$TMP_DIR/second-ca.pem" >/dev/null 2>&1
cat "$CA_FILE" "$TMP_DIR/second-ca.pem" >"$TWO_CA_FILE"
chmod 0644 "$TWO_CA_FILE"
TWO_CA_ENV="$TMP_DIR/two-ca.env"
sed "s#^INGEST_TLS_CA_FILE=.*#INGEST_TLS_CA_FILE=$TWO_CA_FILE#" \
  "$VALID_ENV" >"$TWO_CA_ENV"
chmod 0600 "$TWO_CA_ENV"
expect_make_failure "an ingest CA file with two certificates" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$TWO_CA_ENV"
ROBOT_VAULT_EDITION=core SEGMENTATION_ENABLED=false QA_ENABLED=false \
  MEDIA_ENCODE_ENABLED=false clean_compose \
  -p robot-vault-release-config-two-ca --env-file "$TWO_CA_ENV" \
  -f "$COMPOSE_FILE" --profile obs config --format json >"$TMP_DIR/two-ca.json"
if python3 "$ROOT_DIR/development/release/validate_release_inputs.py" --env-file "$TWO_CA_ENV" \
    --config "$TMP_DIR/two-ca.json" --edition core 2>"$TMP_DIR/two-ca.err"; then
  echo "the preflight accepted a two-certificate ingest CA file" >&2; exit 1
fi
grep -F "must contain exactly one certificate" "$TMP_DIR/two-ca.err" >/dev/null || {
  echo "two-CA refusal does not name the rule: $(cat "$TMP_DIR/two-ca.err")" >&2; exit 1; }

# The ingest port robots are told to use must be a bare port.
BAD_PORT_ENV="$TMP_DIR/bad-port.env"
cp "$VALID_ENV" "$BAD_PORT_ENV"
printf 'INGEST_GRPC_PORT=0.0.0.0:50051\n' >>"$BAD_PORT_ENV"
chmod 0600 "$BAD_PORT_ENV"
ROBOT_VAULT_EDITION=core SEGMENTATION_ENABLED=false QA_ENABLED=false \
  MEDIA_ENCODE_ENABLED=false clean_compose \
  -p robot-vault-release-config-bad-port --env-file "$BAD_PORT_ENV" \
  -f "$COMPOSE_FILE" --profile obs config --format json >"$TMP_DIR/bad-port.json"
if python3 "$ROOT_DIR/development/release/validate_release_inputs.py" --env-file "$BAD_PORT_ENV" \
    --config "$TMP_DIR/bad-port.json" --edition core 2>"$TMP_DIR/bad-port.err"; then
  echo "the preflight accepted INGEST_GRPC_PORT with a bind address" >&2; exit 1
fi
grep -F "INGEST_GRPC_PORT must be a bare port" "$TMP_DIR/bad-port.err" >/dev/null || {
  echo "port refusal does not name the rule: $(cat "$TMP_DIR/bad-port.err")" >&2; exit 1; }

# INGEST_SECURITY_PROFILE: `token` is the default (asserted in the render
# above), `mtls` is the one alternative, every other value is refused.
for profile in mtls dev production tls; do
  PROFILE_ENV="$TMP_DIR/profile-$profile.env"
  cp "$VALID_ENV" "$PROFILE_ENV"
  printf 'INGEST_SECURITY_PROFILE=%s\n' "$profile" >>"$PROFILE_ENV"
  chmod 0600 "$PROFILE_ENV"
  ROBOT_VAULT_EDITION=core SEGMENTATION_ENABLED=false QA_ENABLED=false \
    MEDIA_ENCODE_ENABLED=false clean_compose \
    -p robot-vault-release-config-profile --env-file "$PROFILE_ENV" \
    -f "$COMPOSE_FILE" --profile obs config --format json >"$TMP_DIR/profile-$profile.json"
  python3 - "$TMP_DIR/profile-$profile.json" "$profile" <<'PY'
import json, sys
ingest = json.load(open(sys.argv[1], encoding="utf-8"))["services"]["ingest"]["environment"]
assert ingest["INGEST_SECURITY_PROFILE"] == sys.argv[2], ingest["INGEST_SECURITY_PROFILE"]
assert "TLS_MTLS" not in ingest, ingest
PY
  if [[ "$profile" == mtls ]]; then
    make --no-print-directory -s -C "$ROOT_DIR" release-config \
      EDITION=core RELEASE_ENV_FILE="$PROFILE_ENV" >/dev/null
    python3 "$ROOT_DIR/development/release/validate_release_inputs.py" --env-file "$PROFILE_ENV" \
      --config "$TMP_DIR/profile-$profile.json" --edition core
  else
    expect_make_failure "INGEST_SECURITY_PROFILE=$profile" \
      make --no-print-directory -s -C "$ROOT_DIR" release-config \
        EDITION=core RELEASE_ENV_FILE="$PROFILE_ENV"
    python3 "$ROOT_DIR/development/release/validate_release_inputs.py" --env-file "$PROFILE_ENV" \
      --config "$TMP_DIR/profile-$profile.json" --edition core 2>"$TMP_DIR/profile.err" && {
      echo "the preflight accepted INGEST_SECURITY_PROFILE=$profile" >&2; exit 1; }
    grep -F "INGEST_SECURITY_PROFILE must be token or mtls" "$TMP_DIR/profile.err" >/dev/null || {
      echo "profile refusal does not name the rule: $(cat "$TMP_DIR/profile.err")" >&2; exit 1; }
  fi
done

NEAR_EXPIRY_CERT_FILE="$TMP_DIR/query-near-expiry.pem"
openssl x509 -req -sha256 -days 1 -in "$TMP_DIR/query.csr" \
  -CA "$CA_FILE" -CAkey "$CA_KEY_FILE" -CAserial "$TMP_DIR/release-ca.srl" \
  -extfile "$QUERY_EXT_FILE" -out "$NEAR_EXPIRY_CERT_FILE" >/dev/null 2>&1
chmod 0644 "$NEAR_EXPIRY_CERT_FILE"
NEAR_EXPIRY_ENV="$TMP_DIR/near-expiry.env"
sed "s#^QUERY_TLS_CERT_FILE=.*#QUERY_TLS_CERT_FILE=$NEAR_EXPIRY_CERT_FILE#" \
  "$VALID_ENV" >"$NEAR_EXPIRY_ENV"
chmod 0600 "$NEAR_EXPIRY_ENV"
expect_make_failure "a server certificate expiring within seven days" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$NEAR_EXPIRY_ENV"

RAW_DATABASE_URL_ENV="$TMP_DIR/raw-database-url.env"
sed 's#^DATABASE_URL=.*#DATABASE_URL=postgresql://robotvault:p@ss:/?#' \
  "$VALID_ENV" >"$RAW_DATABASE_URL_ENV"
chmod 0600 "$RAW_DATABASE_URL_ENV"
expect_make_failure "a DATABASE_URL with raw URI delimiters" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$RAW_DATABASE_URL_ENV"

MALFORMED_ESCAPE_ENV="$TMP_DIR/malformed-database-escape.env"
sed 's#^DATABASE_URL=.*#DATABASE_URL=postgresql://robotvault:p%ZZ@postgres:5432/robotvault#' \
  "$VALID_ENV" >"$MALFORMED_ESCAPE_ENV"
chmod 0600 "$MALFORMED_ESCAPE_ENV"
expect_make_failure "a DATABASE_URL with a malformed percent escape" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MALFORMED_ESCAPE_ENV"

MISMATCHED_DATABASE_URL_ENV="$TMP_DIR/mismatched-database-url.env"
sed 's#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=different-password#' \
  "$VALID_ENV" >"$MISMATCHED_DATABASE_URL_ENV"
chmod 0600 "$MISMATCHED_DATABASE_URL_ENV"
expect_make_failure "a DATABASE_URL whose password disagrees with Postgres" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISMATCHED_DATABASE_URL_ENV"

WRONG_RUNTIME_ROLE_ENV="$TMP_DIR/wrong-runtime-role.env"
sed 's#robot_vault_query:query-db-secret#robot_vault_ingest:query-db-secret#' \
  "$VALID_ENV" >"$WRONG_RUNTIME_ROLE_ENV"
chmod 0600 "$WRONG_RUNTIME_ROLE_ENV"
expect_make_failure "a query DSN using the wrong runtime role" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$WRONG_RUNTIME_ROLE_ENV"

REUSED_DB_PASSWORD_ENV="$TMP_DIR/reused-db-password.env"
sed 's#processor-db-secret-0000000000000001#ingest-db-secret-000000000000000001#' \
  "$VALID_ENV" >"$REUSED_DB_PASSWORD_ENV"
chmod 0600 "$REUSED_DB_PASSWORD_ENV"
expect_make_failure "a PostgreSQL password reused across runtime roles" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$REUSED_DB_PASSWORD_ENV"

SHORT_S3_SECRET_ENV="$TMP_DIR/short-s3-secret.env"
sed 's/^QUERY_S3_SECRET_KEY=.*/QUERY_S3_SECRET_KEY=too-short/' \
  "$VALID_ENV" >"$SHORT_S3_SECRET_ENV"
chmod 0600 "$SHORT_S3_SECRET_ENV"
expect_make_failure "an undersized runtime object-store secret" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$SHORT_S3_SECRET_ENV"

REUSED_S3_SECRET_ENV="$TMP_DIR/reused-s3-secret.env"
sed 's#^GC_S3_SECRET_KEY=.*#GC_S3_SECRET_KEY=ingest-s3-secret-000000000000000001#' \
  "$VALID_ENV" >"$REUSED_S3_SECRET_ENV"
chmod 0600 "$REUSED_S3_SECRET_ENV"
expect_make_failure "an object-store secret reused across runtime users" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$REUSED_S3_SECRET_ENV"

CROSS_SYSTEM_SECRET_ENV="$TMP_DIR/cross-system-secret.env"
sed 's#^QUERY_S3_SECRET_KEY=.*#QUERY_S3_SECRET_KEY=query-db-secret-0000000000000000001#' \
  "$VALID_ENV" >"$CROSS_SYSTEM_SECRET_ENV"
chmod 0600 "$CROSS_SYSTEM_SECRET_ENV"
expect_make_failure "a credential reused across PostgreSQL and the object store" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$CROSS_SYSTEM_SECRET_ENV"

NO_STORE_ROOT_PASSWORD_ENV="$TMP_DIR/no-objectstore-root-password.env"
awk 'index($0, "OBJECTSTORE_ROOT_PASSWORD=") != 1' \
  "$VALID_ENV" >"$NO_STORE_ROOT_PASSWORD_ENV"
chmod 0600 "$NO_STORE_ROOT_PASSWORD_ENV"
expect_make_failure "a missing object-store root password" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$NO_STORE_ROOT_PASSWORD_ENV"

ROOT_REUSED_ENV="$TMP_DIR/objectstore-root-reused.env"
sed 's#^OBJECTSTORE_ROOT_PASSWORD=.*#OBJECTSTORE_ROOT_PASSWORD=gc-s3-secret-00000000000000000000001#' \
  "$VALID_ENV" >"$ROOT_REUSED_ENV"
chmod 0600 "$ROOT_REUSED_ENV"
expect_make_failure "an object-store root password reused as a service secret" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$ROOT_REUSED_ENV"

SHORT_ROOT_ENV="$TMP_DIR/objectstore-root-short.env"
sed 's#^OBJECTSTORE_ROOT_PASSWORD=.*#OBJECTSTORE_ROOT_PASSWORD=short-root-password-0001#' \
  "$VALID_ENV" >"$SHORT_ROOT_ENV"
chmod 0600 "$SHORT_ROOT_ENV"
expect_make_failure "an undersized object-store root password" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$SHORT_ROOT_ENV"

# The development root password is in the repository, so release must refuse it,
# as it refuses the committed development ingest token.
DEV_ROOT_ENV="$TMP_DIR/objectstore-root-dev.env"
DEV_ROOT_PASSWORD="$(cat "$ROOT_DIR/configs/objectstore-root-password.dev")"
sed "s#^OBJECTSTORE_ROOT_PASSWORD=.*#OBJECTSTORE_ROOT_PASSWORD=$DEV_ROOT_PASSWORD#" \
  "$VALID_ENV" >"$DEV_ROOT_ENV"
chmod 0600 "$DEV_ROOT_ENV"
if dev_root_output="$(make --no-print-directory -s -C "$ROOT_DIR" release-config \
  EDITION=core RELEASE_ENV_FILE="$DEV_ROOT_ENV" 2>&1)"; then
  echo "release Make preflight unexpectedly accepted the committed development object-store root password" >&2
  exit 1
fi
grep -F "refuses the committed development object-store root password" <<<"$dev_root_output" >/dev/null || {
  echo "release preflight refused the development root password for another reason:" >&2
  echo "$dev_root_output" >&2
  exit 1
}

ROOT_IS_SERVICE_ENV="$TMP_DIR/objectstore-root-is-service.env"
sed 's#^OBJECTSTORE_ROOT_USER=.*#OBJECTSTORE_ROOT_USER=robot-vault-gc#' \
  "$VALID_ENV" >"$ROOT_IS_SERVICE_ENV"
chmod 0600 "$ROOT_IS_SERVICE_ENV"
expect_make_failure "an object-store root user equal to a service key" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$ROOT_IS_SERVICE_ENV"

# The object-store contract (development/objectstore/CONTRACT.md) on the
# rendered topology: the valid render passes, and each weakened copy fails
# with the reason that names it.
ROBOT_VAULT_EDITION=core SEGMENTATION_ENABLED=false QA_ENABLED=false MEDIA_ENCODE_ENABLED=false \
  clean_compose -p robot-vault-release-config-objectstore --env-file "$VALID_ENV" \
  -f "$COMPOSE_FILE" --profile obs config --format json >"$TMP_DIR/objectstore-base.json"
python3 - "$ROOT_DIR/development/release/validate_release_inputs.py" "$VALID_ENV" \
  "$TMP_DIR/objectstore-base.json" "$TMP_DIR" <<'PY'
import copy
import json
import subprocess
import sys

validator, env_file, base_path, tmp = sys.argv[1:]
base = json.load(open(base_path, encoding="utf-8"))


def run(config):
    path = f"{tmp}/objectstore-mutated.json"
    json.dump(config, open(path, "w", encoding="utf-8"))
    return subprocess.run(
        [sys.executable, validator, "--env-file", env_file, "--config", path, "--edition", "core"],
        capture_output=True, text=True,
    )


ok = run(base)
assert ok.returncode == 0, ok.stderr


def store_env(name, value):
    def mutate(c):
        if value is None:
            del c["services"]["objectstore"]["environment"][name]
        else:
            c["services"]["objectstore"]["environment"][name] = value
    return mutate


def setter(path, value):
    def mutate(c):
        node = c
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return mutate


cases = {
    "durability relaxed": (store_env("RUSTFS_DURABILITY_MODE", "relaxed"), "frozen contract"),
    "drive sync off": (store_env("RUSTFS_DRIVE_SYNC_ENABLE", "false"), "frozen contract"),
    "default 24h expiry": (store_env("RUSTFS_API_STALE_UPLOADS_EXPIRY", None), "frozen contract"),
    "expiry 24h": (store_env("RUSTFS_API_STALE_UPLOADS_EXPIRY", "24h"), "frozen contract"),
    "console on": (store_env("RUSTFS_CONSOLE_ENABLE", "true"), "frozen contract"),
    "root password in env": (store_env("RUSTFS_SECRET_KEY", "x" * 40), "frozen contract"),
    "upstream image": (setter(["services", "objectstore", "image"], "rustfs/rustfs:1.0.6@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a"), "contract image"),
    "store pulls": (setter(["services", "objectstore", "pull_policy"], "always"), "pull_policy"),
    "published port": (setter(["services", "objectstore", "ports"], [{"target": 9000, "published": "9000"}]), "publish"),
    "secret as host file": (setter(["secrets", "objectstore_root_password"], {"file": "/tmp/pw"}), "must come from"),
    "secret to a service": (setter(["services", "gc", "secrets"], [{"source": "objectstore_root_password"}]), "must not receive"),
    "root password env to a service": (
        setter(["services", "gc", "environment", "OBJECTSTORE_ROOT_PASSWORD"], "x" * 40),
        "must not receive",
    ),
    "bootstrap reads a file": (
        setter(["services", "objectstore-access-ready", "environment", "OBJECTSTORE_ROOT_PASSWORD_FILE"], "/x"),
        "RELEASE_ENV_FILE",
    ),
    "bind mount data": (
        setter(["services", "objectstore", "volumes"], [{"type": "bind", "source": "/srv/data", "target": "/data"}]),
        "named volume",
    ),
    "user override": (setter(["services", "objectstore", "user"], "0"), "image user"),
}
for name, (mutate, reason) in cases.items():
    config = copy.deepcopy(base)
    mutate(config)
    result = run(config)
    assert result.returncode != 0, f"validator accepted: {name}"
    assert reason in result.stderr, (name, result.stderr)
PY

DEV_TOKEN_ENV="$TMP_DIR/dev-token.env"
sed 's/^INGEST_AUTH_TOKEN=.*/INGEST_AUTH_TOKEN=dev-ingest-token-change-me/' \
  "$VALID_ENV" >"$DEV_TOKEN_ENV"
chmod 0600 "$DEV_TOKEN_ENV"
expect_make_failure "the committed development ingest token" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$DEV_TOKEN_ENV"

SHORT_TOKEN_ENV="$TMP_DIR/short-token.env"
sed 's/^INGEST_AUTH_TOKEN=.*/INGEST_AUTH_TOKEN=too-short/' \
  "$VALID_ENV" >"$SHORT_TOKEN_ENV"
chmod 0600 "$SHORT_TOKEN_ENV"
expect_make_failure "an undersized release ingest token" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$SHORT_TOKEN_ENV"

SHORT_METRICS_TOKEN_ENV="$TMP_DIR/short-metrics-token.env"
sed 's/^METRICS_AUTH_TOKEN=.*/METRICS_AUTH_TOKEN=too-short/' \
  "$VALID_ENV" >"$SHORT_METRICS_TOKEN_ENV"
chmod 0600 "$SHORT_METRICS_TOKEN_ENV"
expect_make_failure "an undersized release metrics token" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$SHORT_METRICS_TOKEN_ENV"

REUSED_METRICS_TOKEN_ENV="$TMP_DIR/reused-metrics-token.env"
sed 's/^METRICS_AUTH_TOKEN=.*/METRICS_AUTH_TOKEN=release-config-ingest-token-32chars/' \
  "$VALID_ENV" >"$REUSED_METRICS_TOKEN_ENV"
chmod 0600 "$REUSED_METRICS_TOKEN_ENV"
expect_make_failure "a metrics token reused from ingest" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$REUSED_METRICS_TOKEN_ENV"

SHORT_PROVISIONING_TOKEN_ENV="$TMP_DIR/short-provisioning-token.env"
sed 's/^INGEST_PROVISIONING_TOKEN=.*/INGEST_PROVISIONING_TOKEN=too-short/' \
  "$VALID_ENV" >"$SHORT_PROVISIONING_TOKEN_ENV"
chmod 0600 "$SHORT_PROVISIONING_TOKEN_ENV"
expect_make_failure "an undersized release provisioning token" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$SHORT_PROVISIONING_TOKEN_ENV"

REUSED_PROVISIONING_TOKEN_ENV="$TMP_DIR/reused-provisioning-token.env"
sed 's/^INGEST_PROVISIONING_TOKEN=.*/INGEST_PROVISIONING_TOKEN=release-config-ingest-token-32chars/' \
  "$VALID_ENV" >"$REUSED_PROVISIONING_TOKEN_ENV"
chmod 0600 "$REUSED_PROVISIONING_TOKEN_ENV"
expect_make_failure "a provisioning token reused from ingest" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$REUSED_PROVISIONING_TOKEN_ENV"

BAD_HOST_ENV="$TMP_DIR/bad-host.env"
sed 's/^QUERY_PUBLIC_HOST=.*/QUERY_PUBLIC_HOST=https:\/\/vault.example.invalid/' \
  "$VALID_ENV" >"$BAD_HOST_ENV"
chmod 0600 "$BAD_HOST_ENV"
expect_make_failure "a QUERY_PUBLIC_HOST containing a scheme" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$BAD_HOST_ENV"

BAD_TOKEN_URL_ENV="$TMP_DIR/bad-token-url.env"
sed 's#^OIDC_TOKEN_URL=.*#OIDC_TOKEN_URL=http://identity.example.invalid/token#' \
  "$VALID_ENV" >"$BAD_TOKEN_URL_ENV"
chmod 0600 "$BAD_TOKEN_URL_ENV"
expect_make_failure "a plaintext OIDC token endpoint" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$BAD_TOKEN_URL_ENV"

BAD_GC_DRY_RUN_ENV="$TMP_DIR/bad-gc-dry-run.env"
sed 's/^GC_DRY_RUN=.*/GC_DRY_RUN=tru/' \
  "$VALID_ENV" >"$BAD_GC_DRY_RUN_ENV"
chmod 0600 "$BAD_GC_DRY_RUN_ENV"
expect_make_failure "a misspelled GC_DRY_RUN deletion switch" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$BAD_GC_DRY_RUN_ENV"

NEGATIVE_GC_AGE_ENV="$TMP_DIR/negative-gc-age.env"
sed 's/^GC_MIN_AGE_HOURS=.*/GC_MIN_AGE_HOURS=-1/' \
  "$VALID_ENV" >"$NEGATIVE_GC_AGE_ENV"
chmod 0600 "$NEGATIVE_GC_AGE_ENV"
expect_make_failure "a negative GC object age" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$NEGATIVE_GC_AGE_ENV"

NEGATIVE_MPU_AGE_ENV="$TMP_DIR/negative-mpu-age.env"
sed 's/^GC_MPU_MAX_AGE_HOURS=.*/GC_MPU_MAX_AGE_HOURS=-1/' \
  "$VALID_ENV" >"$NEGATIVE_MPU_AGE_ENV"
chmod 0600 "$NEGATIVE_MPU_AGE_ENV"
expect_make_failure "a negative stale-multipart age" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$NEGATIVE_MPU_AGE_ENV"

UNBOUNDED_GC_DELETE_ENV="$TMP_DIR/unbounded-gc-delete.env"
sed 's/^GC_DELETE_LIMIT=.*/GC_DELETE_LIMIT=100001/' \
  "$VALID_ENV" >"$UNBOUNDED_GC_DELETE_ENV"
chmod 0600 "$UNBOUNDED_GC_DELETE_ENV"
expect_make_failure "an unbounded GC delete limit" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$UNBOUNDED_GC_DELETE_ENV"

ZERO_GC_INTERVAL_ENV="$TMP_DIR/zero-gc-interval.env"
sed 's/^GC_INTERVAL_SECONDS=.*/GC_INTERVAL_SECONDS=0/' \
  "$VALID_ENV" >"$ZERO_GC_INTERVAL_ENV"
chmod 0600 "$ZERO_GC_INTERVAL_ENV"
expect_make_failure "a zero GC polling interval" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$ZERO_GC_INTERVAL_ENV"

UNKNOWN_GC_PREFIX_ENV="$TMP_DIR/unknown-gc-prefix.env"
cp "$VALID_ENV" "$UNKNOWN_GC_PREFIX_ENV"
printf '%s\n' 'GC_PREFIXES=raw/,unreviewed/' >>"$UNKNOWN_GC_PREFIX_ENV"
chmod 0600 "$UNKNOWN_GC_PREFIX_ENV"
expect_make_failure "an object prefix outside the reviewed GC policy" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$UNKNOWN_GC_PREFIX_ENV"

ZERO_QUERY_FILE_LIMIT_ENV="$TMP_DIR/zero-query-file-limit.env"
sed 's/^QUERY_MAX_SOURCE_FILES=.*/QUERY_MAX_SOURCE_FILES=0/' \
  "$VALID_ENV" >"$ZERO_QUERY_FILE_LIMIT_ENV"
chmod 0600 "$ZERO_QUERY_FILE_LIMIT_ENV"
expect_make_failure "a zero query source-file limit" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$ZERO_QUERY_FILE_LIMIT_ENV"

TOO_SMALL_QUERY_MEMORY_ENV="$TMP_DIR/too-small-query-memory.env"
sed 's/^QUERY_MEMORY_LIMIT=.*/QUERY_MEMORY_LIMIT=64m/' \
  "$VALID_ENV" >"$TOO_SMALL_QUERY_MEMORY_ENV"
chmod 0600 "$TOO_SMALL_QUERY_MEMORY_ENV"
expect_make_failure "a query memory ceiling below the safe minimum" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$TOO_SMALL_QUERY_MEMORY_ENV"

UNBOUNDED_LOG_ENV="$TMP_DIR/unbounded-log.env"
sed 's/^ROBOT_VAULT_LOG_MAX_SIZE=.*/ROBOT_VAULT_LOG_MAX_SIZE=0m/' \
  "$VALID_ENV" >"$UNBOUNDED_LOG_ENV"
chmod 0600 "$UNBOUNDED_LOG_ENV"
expect_make_failure "a zero container-log size limit" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$UNBOUNDED_LOG_ENV"

ZERO_PROMETHEUS_RETENTION_ENV="$TMP_DIR/zero-prometheus-retention.env"
sed 's/^PROMETHEUS_RETENTION_TIME=.*/PROMETHEUS_RETENTION_TIME=0d/' \
  "$VALID_ENV" >"$ZERO_PROMETHEUS_RETENTION_ENV"
chmod 0600 "$ZERO_PROMETHEUS_RETENTION_ENV"
expect_make_failure "a zero Prometheus retention duration" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$ZERO_PROMETHEUS_RETENTION_ENV"

VALID_ALERTMANAGER_ENV="$TMP_DIR/valid-alertmanager.env"
cp "$VALID_ENV" "$VALID_ALERTMANAGER_ENV"
printf '%s\n' 'ALERTMANAGER_ENDPOINT=https://alerts.example.invalid:9093' >>"$VALID_ALERTMANAGER_ENV"
chmod 0600 "$VALID_ALERTMANAGER_ENV"
make --no-print-directory -s -C "$ROOT_DIR" release-config \
  EDITION=core RELEASE_ENV_FILE="$VALID_ALERTMANAGER_ENV" >/dev/null

INVALID_ALERTMANAGER_ENV="$TMP_DIR/invalid-alertmanager.env"
cp "$VALID_ENV" "$INVALID_ALERTMANAGER_ENV"
printf '%s\n' 'ALERTMANAGER_ENDPOINT=http://alerts.example.invalid:9093/path' >>"$INVALID_ALERTMANAGER_ENV"
chmod 0600 "$INVALID_ALERTMANAGER_ENV"
expect_make_failure "an Alertmanager endpoint with a path" \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$INVALID_ALERTMANAGER_ENV"

# Exercise the Makefile hand-off with a poisoned caller environment. Explicit
# release inputs and EDITION remain authoritative over every topology-changing
# COMPOSE_* variable and the dev-auth/HSTS values.
# POISON_CHECK names the variables that must not reach docker; POISON_EXTRA_ENV
# adds NAME=value poison. An extension appends its own settings to both and
# calls poisoned_release_config again.
POISON_CHECK=(
  DATABASE_URL POSTGRES_PASSWORD OBJECTSTORE_ROOT_USER OBJECTSTORE_ROOT_PASSWORD
  INGEST_DATABASE_URL PROCESSOR_DATABASE_URL EXPORTER_DATABASE_URL QUERY_DATABASE_URL
  GC_DATABASE_URL INGEST_S3_SECRET_KEY PROCESSOR_S3_SECRET_KEY EXPORTER_S3_SECRET_KEY
  QUERY_S3_SECRET_KEY GC_S3_SECRET_KEY INGEST_AUTH_TOKEN INGEST_PROVISIONING_TOKEN
  METRICS_AUTH_TOKEN S3_BUCKET S3_REGION OIDC_ISSUER QUERY_PUBLIC_HOST INGEST_PUBLIC_HOST
  QUERY_TLS_CERT_FILE QUERY_TLS_KEY_FILE QUERY_TLS_CA_FILE INGEST_TLS_CERT_FILE INGEST_TLS_KEY_FILE
  INGEST_TLS_CA_FILE INGEST_SECURITY_PROFILE ROBOT_VAULT_SECRETS_GID TLS_ENABLED TLS_MTLS
  TLS_SERVER_CERT TLS_SERVER_KEY TLS_CA_CERT EXPORTER_MAX_ATTEMPTS DUCKDB_THREADS
  DUCKDB_MEMORY_LIMIT_MB DUCKDB_QUERY_CONCURRENCY DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS
  DUCKDB_QUERY_TIMEOUT_SECONDS QUERY_MAX_SOURCE_RANGE_SECONDS QUERY_MAX_SOURCE_FILES
  QUERY_MAX_SOURCE_ROWS QUERY_MAX_SOURCE_BYTES SQL_CONSOLE_MEMORY_LIMIT_MB GC_MIN_AGE_HOURS
  GC_MPU_MAX_AGE_HOURS GC_DELETE_LIMIT GC_DRY_RUN GC_PREFIXES GC_METRICS_PORT GC_INTERVAL_SECONDS
  ROBOT_VAULT_LOG_MAX_SIZE ROBOT_VAULT_LOG_MAX_FILES FIRST_PARTY_PIDS_LIMIT POSTGRES_SHM_SIZE
  POSTGRES_MEMORY_LIMIT POSTGRES_CPU_LIMIT POSTGRES_PIDS_LIMIT OBJECTSTORE_MEMORY_LIMIT
  OBJECTSTORE_CPU_LIMIT OBJECTSTORE_PIDS_LIMIT OBJECTSTORE_BOOTSTRAP_MEMORY_LIMIT
  OBJECTSTORE_BOOTSTRAP_CPU_LIMIT REDIS_MEMORY_LIMIT REDIS_CPU_LIMIT REDIS_PIDS_LIMIT
  DB_MIGRATE_MEMORY_LIMIT DB_MIGRATE_CPU_LIMIT DB_BOOTSTRAP_MEMORY_LIMIT DB_BOOTSTRAP_CPU_LIMIT
  INGEST_MEMORY_LIMIT INGEST_CPU_LIMIT INGEST_PIDS_LIMIT PROCESSOR_MEMORY_LIMIT PROCESSOR_CPU_LIMIT
  PROCESSOR_PIDS_LIMIT QUERY_MEMORY_LIMIT QUERY_CPU_LIMIT QUERY_PIDS_LIMIT GC_MEMORY_LIMIT
  GC_CPU_LIMIT GC_PIDS_LIMIT PROMETHEUS_MEMORY_LIMIT PROMETHEUS_CPU_LIMIT PROMETHEUS_PIDS_LIMIT
  PROMETHEUS_RETENTION_TIME PROMETHEUS_RETENTION_SIZE
)
POISON_EXTRA_ENV=()
poisoned_release_config() {
POISON_COMPOSE_FILE="$TMP_DIR/not-the-release-compose.yml"
printf '%s\n' 'services: {poison: {image: invalid}}' >"$POISON_COMPOSE_FILE"
CONTROL_POISON_ENV="$TMP_DIR/control-poison.env"
cp "$VALID_ENV" "$CONTROL_POISON_ENV"
printf '%s\n' \
  "COMPOSE_FILE=$POISON_COMPOSE_FILE" \
  'COMPOSE_PROJECT_NAME=poison-from-file' \
  'COMPOSE_PROFILES=pro' \
  "COMPOSE_ENV_FILES=$TMP_DIR/does-not-exist-from-file.env" \
  'COMPOSE_DISABLE_ENV_FILE=0' \
  >>"$CONTROL_POISON_ENV"
chmod 0600 "$CONTROL_POISON_ENV"
CORE_SERVICES="$TMP_DIR/core-services.txt"
REAL_DOCKER="$(command -v docker)"
mkdir -p "$TMP_DIR/poison-bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  "for name in ${POISON_CHECK[*]}; do" \
  '  if [[ -v "$name" ]]; then echo "release environment leaked inherited $name" >&2; exit 97; fi' \
  'done' \
  'exec "$REAL_DOCKER" "$@"' \
  >"$TMP_DIR/poison-bin/docker"
chmod 0755 "$TMP_DIR/poison-bin/docker"
env "${POISON_EXTRA_ENV[@]}" \
  PATH="$TMP_DIR/poison-bin:$PATH" REAL_DOCKER="$REAL_DOCKER" \
  COMPOSE_FILE="$POISON_COMPOSE_FILE" \
  COMPOSE_PROJECT_NAME=poison-project \
  COMPOSE_PROFILES=pro \
  COMPOSE_ENV_FILES="$TMP_DIR/does-not-exist.env" \
  COMPOSE_DISABLE_ENV_FILE=0 \
  POSTGRES_PASSWORD=shell-poison OBJECTSTORE_ROOT_USER=shell-poison OBJECTSTORE_ROOT_PASSWORD=shell-poison \
  INGEST_DATABASE_URL='postgresql://shell:poison@external.invalid:5432/wrong' \
  PROCESSOR_DATABASE_URL='postgresql://shell:poison@external.invalid:5432/wrong' \
  EXPORTER_DATABASE_URL='postgresql://shell:poison@external.invalid:5432/wrong' \
  QUERY_DATABASE_URL='postgresql://shell:poison@external.invalid:5432/wrong' \
  GC_DATABASE_URL='postgresql://shell:poison@external.invalid:5432/wrong' \
  INGEST_S3_SECRET_KEY=shell-poison PROCESSOR_S3_SECRET_KEY=shell-poison \
  EXPORTER_S3_SECRET_KEY=shell-poison QUERY_S3_SECRET_KEY=shell-poison \
  GC_S3_SECRET_KEY=shell-poison \
  INGEST_AUTH_TOKEN=shell-poison INGEST_PROVISIONING_TOKEN=shell-poison \
  METRICS_AUTH_TOKEN=shell-poison \
  OIDC_ISSUER=http://shell-poison.invalid \
  OIDC_AUDIENCE=shell-poison OIDC_JWKS_URL=http://shell-poison.invalid/jwks \
  OIDC_CLIENT_ID=shell-poison OIDC_AUTHORIZATION_URL=http://shell-poison.invalid/auth \
  OIDC_TOKEN_URL=http://shell-poison.invalid/token \
  QUERY_PUBLIC_HOST=https://shell-poison.invalid \
  QUERY_TLS_CERT_FILE="$TMP_DIR/absent-shell-cert" \
  QUERY_TLS_KEY_FILE="$TMP_DIR/absent-shell-key" \
  QUERY_TLS_CA_FILE="$TMP_DIR/absent-shell-ca" \
  DATABASE_URL='postgresql://shell:poison@external.invalid:5432/wrong' \
  S3_BUCKET=shell-poison S3_REGION=shell-poison \
  INGEST_PUBLIC_HOST=shell-poison.invalid \
  INGEST_TLS_CERT_FILE="$TMP_DIR/absent-ingest-cert" \
  INGEST_TLS_KEY_FILE="$TMP_DIR/absent-ingest-key" \
  INGEST_TLS_CA_FILE="$TMP_DIR/absent-ingest-ca" \
  ROBOT_VAULT_SECRETS_GID=1 \
  TLS_ENABLED=false TLS_MTLS=false TLS_SERVER_CERT=shell-poison \
  INGEST_SECURITY_PROFILE=dev \
  TLS_SERVER_KEY=shell-poison TLS_CA_CERT=shell-poison \
  EXPORTER_MAX_ATTEMPTS=999 DUCKDB_THREADS=999 DUCKDB_MEMORY_LIMIT_MB=1 \
  DUCKDB_QUERY_CONCURRENCY=999 DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS=0 \
  DUCKDB_QUERY_TIMEOUT_SECONDS=0 QUERY_MAX_SOURCE_RANGE_SECONDS=0 \
  QUERY_MAX_SOURCE_FILES=0 QUERY_MAX_SOURCE_ROWS=0 QUERY_MAX_SOURCE_BYTES=0 \
  SQL_CONSOLE_MEMORY_LIMIT_MB=1 \
  GC_MIN_AGE_HOURS=0 GC_MPU_MAX_AGE_HOURS=0 GC_DELETE_LIMIT=999999 \
  GC_DRY_RUN=false GC_PREFIXES=poison/ GC_METRICS_PORT=1 GC_INTERVAL_SECONDS=1 \
  ROBOT_VAULT_LOG_MAX_SIZE=0m ROBOT_VAULT_LOG_MAX_FILES=0 \
  FIRST_PARTY_PIDS_LIMIT=1 POSTGRES_SHM_SIZE=1m \
  POSTGRES_MEMORY_LIMIT=1m POSTGRES_CPU_LIMIT=999 POSTGRES_PIDS_LIMIT=1 \
  OBJECTSTORE_MEMORY_LIMIT=1m OBJECTSTORE_CPU_LIMIT=999 OBJECTSTORE_PIDS_LIMIT=1 \
  OBJECTSTORE_BOOTSTRAP_MEMORY_LIMIT=1m OBJECTSTORE_BOOTSTRAP_CPU_LIMIT=999 \
  REDIS_MEMORY_LIMIT=1m REDIS_CPU_LIMIT=999 REDIS_PIDS_LIMIT=1 \
  DB_MIGRATE_MEMORY_LIMIT=1m DB_MIGRATE_CPU_LIMIT=999 \
  DB_BOOTSTRAP_MEMORY_LIMIT=1m DB_BOOTSTRAP_CPU_LIMIT=999 \
  INGEST_MEMORY_LIMIT=1m INGEST_CPU_LIMIT=999 INGEST_PIDS_LIMIT=1 \
  PROCESSOR_MEMORY_LIMIT=1m PROCESSOR_CPU_LIMIT=999 PROCESSOR_PIDS_LIMIT=1 \
  QUERY_MEMORY_LIMIT=1m QUERY_CPU_LIMIT=999 QUERY_PIDS_LIMIT=1 \
  GC_MEMORY_LIMIT=1m GC_CPU_LIMIT=999 GC_PIDS_LIMIT=1 \
  PROMETHEUS_MEMORY_LIMIT=1m PROMETHEUS_CPU_LIMIT=999 PROMETHEUS_PIDS_LIMIT=1 \
  PROMETHEUS_RETENTION_TIME=0d PROMETHEUS_RETENTION_SIZE=0GB \
  SEGMENTATION_ENABLED=true \
  AUTH_MODE=none OIDC_DEV=true ROBOT_VAULT_RELEASE=false \
  OIDC_ALGORITHMS=HS256 OIDC_ROLE_SOURCE=resource_access ENABLE_HSTS=false \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$CONTROL_POISON_ENV" \
    RELEASE_COMPOSE=false RELEASE_PROFILES='--profile pro' \
    RELEASE_EDITION_ENV='ROBOT_VAULT_EDITION=pro SEGMENTATION_ENABLED=true QA_ENABLED=true MEDIA_ENCODE_ENABLED=true' \
    RELEASE_ENV=env >"$CORE_SERVICES"
if grep -Eq '^[[:space:]]*(exporter|poison)$' "$CORE_SERVICES"; then
  echo "EDITION=core inherited a poisoned Compose file/profile" >&2
  exit 1
fi
}
poisoned_release_config

# A shell credential must not fill a hole in the authoritative release file.
MISSING_PASSWORD_ENV="$TMP_DIR/missing-password.env"
awk 'index($0, "POSTGRES_PASSWORD=") != 1' "$VALID_ENV" >"$MISSING_PASSWORD_ENV"
chmod 0600 "$MISSING_PASSWORD_ENV"
expect_make_failure "a shell POSTGRES_PASSWORD overriding RELEASE_ENV_FILE" \
  env POSTGRES_PASSWORD=shell-must-not-win \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISSING_PASSWORD_ENV"

MISSING_RUNTIME_DSN_ENV="$TMP_DIR/missing-runtime-dsn.env"
awk 'index($0, "QUERY_DATABASE_URL=") != 1' \
  "$VALID_ENV" >"$MISSING_RUNTIME_DSN_ENV"
chmod 0600 "$MISSING_RUNTIME_DSN_ENV"
expect_make_failure "a shell runtime DSN filling a RELEASE_ENV_FILE hole" \
  env QUERY_DATABASE_URL='postgresql://robot_vault_query:shell-secret@postgres:5432/robotvault' \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISSING_RUNTIME_DSN_ENV"

MISSING_RUNTIME_S3_ENV="$TMP_DIR/missing-runtime-s3.env"
awk 'index($0, "QUERY_S3_SECRET_KEY=") != 1' \
  "$VALID_ENV" >"$MISSING_RUNTIME_S3_ENV"
chmod 0600 "$MISSING_RUNTIME_S3_ENV"
expect_make_failure "a shell object-store secret filling a RELEASE_ENV_FILE hole" \
  env QUERY_S3_SECRET_KEY=shell-must-not-win \
  make --no-print-directory -s -C "$ROOT_DIR" release-config \
    EDITION=core RELEASE_ENV_FILE="$MISSING_RUNTIME_S3_ENV"

python3 - "$ROOT_DIR/development/release/wait_release_ready.py" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("wait_release_ready", sys.argv[1])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
core_ready, completed = module._required_services("core")
pro_ready, pro_completed = module._required_services("pro")
assert "exporter" not in core_ready
assert pro_ready == core_ready + ("exporter",)
assert completed == pro_completed == (
    "db-migrate",
    "postgres-roles-ready",
    "objectstore-access-ready",
)
PY

# A service that only an edition overlay adds is an orphan of the core files
# after a redeploy to core: `up --remove-orphans` removes it. The Make dry-run
# checks the edition-specific command without touching Docker.
CORE_UP_DRY_RUN="$TMP_DIR/core-release-up.txt"
PRO_UP_DRY_RUN="$TMP_DIR/pro-release-up.txt"
make --no-print-directory -n -C "$ROOT_DIR" release-up \
  EDITION=core RELEASE_ENV_FILE="$VALID_ENV" >"$CORE_UP_DRY_RUN"
make --no-print-directory -n -C "$ROOT_DIR" release-up \
  EDITION=pro RELEASE_ENV_FILE="$VALID_ENV" >"$PRO_UP_DRY_RUN"
for rendered_up in "$CORE_UP_DRY_RUN" "$PRO_UP_DRY_RUN"; do
  grep -F -- 'sh development/release/compose_up.sh 900 docker compose -p ' "$rendered_up" >/dev/null || {
    echo "release-up does not run up through compose_up.sh" >&2; exit 1; }
done
for rendered_up in "$CORE_UP_DRY_RUN" "$PRO_UP_DRY_RUN"; do
  if ! grep -F -- 'db-migrate postgres-roles-ready objectstore-access-ready' \
      "$rendered_up" >/dev/null; then
    echo "release-up does not remove secret-bearing one-shots" >&2
    exit 1
  fi
done
python3 - "$CORE_UP_DRY_RUN" "$PRO_UP_DRY_RUN" <<'PY'
from pathlib import Path
import sys

for rendered_path in map(Path, sys.argv[1:]):
    rendered = rendered_path.read_text(encoding="utf-8")
    cleanup_definition = rendered.index("cleanup_bootstrap()")
    cleanup_trap = rendered.index("trap cleanup_bootstrap EXIT")
    startup = rendered.index("development/release/compose_up.sh", cleanup_trap)
    assert cleanup_definition < cleanup_trap < startup, rendered_path
    # A data volume compose would recreate is refused before `up`
    # (volume_guard.py), and `up` itself fails closed (compose_up.sh, below).
    assert rendered.index("volume_guard.py --project") < cleanup_definition, rendered_path
    assert " --yes" not in rendered and " -y " not in rendered, rendered_path

    cleanup = rendered[cleanup_definition:cleanup_trap]
    assert "rm -sf" in cleanup, rendered_path
    assert "db-migrate postgres-roles-ready objectstore-access-ready" in cleanup, rendered_path
    assert "status=$?" in cleanup and 'exit "$status"' in cleanup, rendered_path
    assert 'if [ "$status" -eq 0 ]; then status=1; fi' in cleanup, rendered_path
    for signal_exit in (
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
    ):
        assert signal_exit in rendered[cleanup_trap:startup], (rendered_path, signal_exit)
PY
# volume_guard.py against a fake `docker volume inspect`: an existing volume
# whose labels, driver or options differ is refused (by name, never by value);
# a missing or matching one passes; an inspect error is not taken as "missing".
mkdir -p "$TMP_DIR/fake-docker"
printf '%s\n' '#!/bin/sh' '[ "$1 $2" = "volume inspect" ] || exit 99' \
  'case "$FAKE_VOLUME" in' \
  '  missing) echo "Error response from daemon: get $3: no such volume" >&2; exit 1;;' \
  '  error) echo "Cannot connect to the Docker daemon" >&2; exit 1;;' \
  '  *) printf "[%s]\n" "$FAKE_VOLUME";;' 'esac' > "$TMP_DIR/fake-docker/docker"
chmod 0755 "$TMP_DIR/fake-docker/docker"
python3 - "$ROOT_DIR/development/release/volume_guard.py" "$TMP_DIR/fake-docker" <<'PY'
import json, os, subprocess, sys
guard, fake = sys.argv[1:]
config = {"volumes": {"pg_data": {"name": "p_pg_data", "labels": {"rv.owner": "x"}},
                      "ext": {"name": "shared", "external": True}}}
def run(volume, cfg=config):
    env = dict(os.environ, PATH=f"{fake}:/usr/bin:/bin", FAKE_VOLUME=volume)
    return subprocess.run([sys.executable, guard, "--project", "p"], input=json.dumps(cfg),
                          capture_output=True, text=True, env=env)
compose = {"com.docker.compose.project": "p", "com.docker.compose.config-hash": "h"}
same = {"Driver": "local", "Labels": {**compose, "rv.owner": "x"}, "Options": None}
assert run("missing").returncode == 0
assert run(json.dumps(same)).returncode == 0, run(json.dumps(same)).stderr
for name, vol, needle in (
    ("planted label", dict(same, Labels={**same["Labels"], "planted": "secret-value"}), "p_pg_data: label planted"),
    ("changed label", dict(same, Labels={**compose, "rv.owner": "y"}), "p_pg_data: label rv.owner"),
    ("driver", dict(same, Driver="nfs"), "p_pg_data: driver"),
    ("option", dict(same, Options={"o": "password=secret-value"}), "p_pg_data: driver option o"),
):
    r = run(json.dumps(vol))
    assert r.returncode == 1 and needle in r.stderr and "REFUSING" in r.stderr, (name, r.stderr)
    assert "secret-value" not in r.stderr + r.stdout, name
r = run("error")
assert r.returncode == 1 and "cannot inspect volume p_pg_data" in r.stderr, r.stderr
r = subprocess.run([sys.executable, guard, "--project", "p"], input="", capture_output=True, text=True)
assert r.returncode == 1 and "no rendered compose config" in r.stderr, r.stderr
print("volume guard checks passed")
PY
# compose_up.sh against a fake docker: compose's "Recreate (data will be
# lost)?" fails the step although compose exits 0 (it keeps the volume on EOF);
# the caller's stdin never reaches compose; exit codes and the timeout hold.
printf '%s\n' '#!/bin/sh' \
  'case "$*" in *" up -d --remove-orphans") ;; *) echo "unexpected args: $*" >&2; exit 98;; esac' \
  'if read -r answer; then echo "compose read stdin: $answer"; exit 42; fi' \
  'case "$FAKE_UP" in' \
  '  prompt) printf "Volume \"p_pg_data\" exists but doesn'"'"'t match configuration in compose file. Recreate (data will be lost)? Container p-x-1 Running\n";;' \
  '  sleep) sleep 5;;' \
  '  fail) echo "compose failed" >&2; exit 3;;' \
  '  *) echo " Container p-x-1 Running";;' 'esac' > "$TMP_DIR/fake-docker/docker"
python3 - "$ROOT_DIR/development/release/compose_up.sh" "$TMP_DIR/fake-docker" <<'PY'
import os, subprocess, sys
script, fake = sys.argv[1:]
src = open(script).read()
assert "--yes" not in src.replace("Never add -y/--yes", "") and " -y " not in src, "compose_up.sh answers prompts"
def run(mode, limit="30"):
    env = dict(os.environ, PATH=f"{fake}:/usr/bin:/bin", FAKE_UP=mode)
    return subprocess.run(["sh", script, limit, "docker", "compose", "-p", "p"], input="y\n",
                          capture_output=True, text=True, env=env, timeout=60)
r = run("ok"); assert r.returncode == 0 and "Running" in r.stdout, (r.returncode, r.stdout, r.stderr)
r = run("prompt")
assert r.returncode == 1 and "REFUSING" in r.stderr and "data will be lost" in r.stdout, (r.returncode, r.stderr)
r = run("fail"); assert r.returncode == 3 and "compose failed" in r.stdout, (r.returncode, r.stdout)
r = run("sleep", limit="1")
assert r.returncode == 124 and "did not finish within 1s" in r.stderr, (r.returncode, r.stderr)
print("compose_up checks passed")
PY
DOWN_DRY_RUN="$TMP_DIR/release-down.txt"
make --no-print-directory -n -C "$ROOT_DIR" release-down \
  EDITION=core RELEASE_ENV_FILE="$VALID_ENV" >"$DOWN_DRY_RUN"
if ! grep -F -- "--profile '*' down --remove-orphans" \
    "$DOWN_DRY_RUN" >/dev/null; then
  echo "release-down can leave containers from an inactive profile running" >&2
  exit 1
fi

# A version tag is not provenance by itself. Every shipped image records the
# source revision, and both tagging and deployment compare it with a clean
# checkout before allowing a release operation.
for dockerfile in \
  "$ROOT_DIR/Dockerfile" \
  "$ROOT_DIR/phases/03-ingest-server-rs/Dockerfile" \
  "$ROOT_DIR/phases/04-processing-pipeline/Dockerfile" \
  "$ROOT_DIR/phases/05-query-service/Dockerfile"; do
  grep -F 'LABEL org.opencontainers.image.revision=$GIT_SHA' "$dockerfile" >/dev/null || {
    echo "missing OCI revision label in $dockerfile" >&2
    exit 1
  }
done
grep -F 'git status --porcelain' "$ROOT_DIR/Makefile" >/dev/null || {
  echo "release targets do not refuse a dirty checkout" >&2
  exit 1
}
grep -F 'org.opencontainers.image.revision' "$ROOT_DIR/Makefile" >/dev/null || {
  echo "release targets do not verify image revision labels" >&2
  exit 1
}
for excluded in '.env' 'certs/' 'secrets/'; do
  grep -Fx "$excluded" "$ROOT_DIR/.dockerignore" >/dev/null || {
    echo ".dockerignore does not exclude host secret input $excluded" >&2
    exit 1
  }
done

# The core image lock (release/core-images.lock.json): an unreleased lock, a
# tag instead of a digest and another version are refused before Docker runs.
python3 - "$ROOT_DIR" "$TMP_DIR" <<'PY'
import json, subprocess, sys
root, tmp = sys.argv[1:]
tool = f"{root}/development/release/core_images.py"
def run(*args):
    return subprocess.run([sys.executable, tool, *args], capture_output=True, text=True,
                          env={"PATH": "/nonexistent"})  # no docker: refusals must come first
committed = f"{root}/release/core-images.lock.json"
r = run("pull", "--lock", committed, "--version", "0.0.0")
assert r.returncode == 1 and "is not filled by a release yet" in r.stderr, r.stderr
good = {"schema": 1, "version": "9.9.9", "source_commit": "a" * 40, "images": {
    n: f"registry.example.invalid/{n}@sha256:" + "b" * 64 for n in
    ("robot-vault-toolkit", "robot-vault-ingest", "robot-vault-core-processor", "robot-vault-core-query", "robot-vault-objectstore")}}
cases = {
    "tag": dict(good, images=dict(good["images"], **{"robot-vault-core-query": "robot-vault-core-query:latest"})),
    "missing": dict(good, images={k: v for k, v in good["images"].items() if k != "robot-vault-ingest"}),
}
for name, lock in cases.items():
    path = f"{tmp}/{name}.lock.json"
    json.dump(lock, open(path, "w"))
    r = run("pull", "--lock", path, "--version", "9.9.9")
    assert r.returncode == 1 and "core-images:" in r.stderr, (name, r.stderr)
path = f"{tmp}/good.lock.json"
json.dump(good, open(path, "w"))
r = run("pull", "--lock", path, "--version", "1.0.0")
assert r.returncode == 1 and "is not this release" in r.stderr, r.stderr
r = run("show", "--lock", path, "robot-vault-core-query")
assert r.returncode == 0 and r.stdout.strip() == good["images"]["robot-vault-core-query"], r
PY

# The live identity cross-check on reported identities: every
# way services can disagree must be named; a consistent set must pass.
python3 - "$ROOT_DIR/development/release/wait_release_ready.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("wrr", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
def ident(edition="pro", core="0.11.0", pro="0.11.0", api=1):
    return {"edition": edition, "core_version": core, "adapter_api": api, "pro_version": pro}
def ui(bundle="pro", core="0.11.0", pro="0.11.0"):
    return m.parse_build_meta(f'<head><meta name="rv-build" content="bundle={bundle} core={core} pro={pro}"><title>')
def pro_set(**over):
    reports = {"query api": ident(), "query ui": ui(), "processor": ident(), "exporter": ident()}
    reports.update(over); return reports
core = {"query api": ident("core", pro=None), "query ui": ui("core", pro="none"), "processor": ident("core", pro=None)}
assert m.identity_mismatches("core", core) == []
assert m.identity_mismatches("pro", pro_set()) == []
cases = {
    "core UI bundle served with a Pro backend": (pro_set(**{"query ui": ui("core", pro="none")}), "query ui: edition 'core'"),
    "mixed Pro builds": (pro_set(processor=ident(pro="9.9.9")), "pro_version differs"),
    "mixed core versions": (pro_set(exporter=ident(core="0.10.0")), "core_version differs"),
    "adapter API differs": (pro_set(processor=ident(api=2)), "adapter_api differs"),
    "Pro image started in core mode": (pro_set(processor=ident("core", pro=None)), "processor: edition 'core'"),
    "no exporter identity line": (pro_set(exporter=None), "exporter: no identity reported"),
    "no UI build identity": (pro_set(**{"query ui": None}), "query ui: no identity reported"),
    "Pro reported in core": ({**core, "processor": ident("core", pro="0.11.0")}, "reports Pro"),
    "wrong edition requested": (core, "expected 'pro'"),
}
for name, (reports, needle) in cases.items():
    expected = "pro" if name != "Pro reported in core" else "core"
    problems = m.identity_mismatches(expected, reports)
    assert any(needle in p for p in problems), (name, problems)
log = 'noise\n{"rv_identity": {"service": "processor", "edition": "core"}}\n{"rv_identity": {"edition": "pro"}}\n'
assert m.parse_identity_line(log) == {"edition": "pro"}
assert m.parse_identity_line("no line") is None and m.parse_build_meta("<head></head>") is None
print("identity cross-check cases passed:", len(cases) + 2)
PY

if [[ -n "$EXTENSION" ]]; then
  # shellcheck source=/dev/null
  source "$EXTENSION"
fi

echo "release contract passed (${EDITIONS[*]} + obs; OIDC/mTLS, immutable non-root runtimes, digest pins, storage, readiness, identity cross-check, core image lock and authoritative inputs enforced)"
