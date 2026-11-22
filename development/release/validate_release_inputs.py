#!/usr/bin/env python3
"""Validate host-side release inputs without executing an env file.

Compose remains responsible for dotenv parsing and interpolation.  This helper
only inspects the rendered JSON plus filesystem metadata, so credentials are
never evaluated as shell code and never printed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import NoReturn
from urllib.parse import quote, unquote, urlsplit


_PUBLIC_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_PEM_CERTIFICATE_RE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",  # gitleaks:allow (marker string the validator searches for)
    b"-----BEGIN RSA PRIVATE KEY-----",  # gitleaks:allow (marker string the validator searches for)
    b"-----BEGIN EC PRIVATE KEY-----",  # gitleaks:allow (marker string the validator searches for)
    b"-----BEGIN DSA PRIVATE KEY-----",  # gitleaks:allow (marker string the validator searches for)
    b"-----BEGIN OPENSSH PRIVATE KEY-----",  # gitleaks:allow (marker string the validator searches for)
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",  # gitleaks:allow (marker string the validator searches for)
)
_CERTIFICATE_MIN_VALIDITY_SECONDS = 7 * 24 * 60 * 60

# EDITION=pro needs the Pro release overlay, which brings its own release
# checks here; this tree renders the core topology only.
_EDITION_CHECKS = Path(__file__).resolve().parents[2] / "pro" / "deploy" / "validate_pro_release.py"
# The ingest CA certificate inside both ingest and query (TLS_CA_CERT, INGEST_CA_CERT_FILE).
INGEST_CA_TARGET = "/run/robot-vault/tls/ingest-ca.pem"


def fail(message: str) -> NoReturn:
    print(f"release preflight: {message}", file=sys.stderr)
    raise SystemExit(1)


def _edition_checks():
    if not _EDITION_CHECKS.is_file():
        fail(
            "EDITION=pro needs the Pro release overlay and its release checks; "
            "this tree renders the core topology only"
        )
    spec = importlib.util.spec_from_file_location("validate_pro_release", _EDITION_CHECKS)
    if spec is None or spec.loader is None:
        fail(f"could not load the edition release checks: {_EDITION_CHECKS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_env_file(path: Path) -> None:
    if not path.is_file():
        fail(f"RELEASE_ENV_FILE is not a regular file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        fail(
            f"RELEASE_ENV_FILE must not grant group/other permissions "
            f"(expected 0600 or stricter, got {mode:04o}): {path}"
        )


def https_url(name: str, raw: object, *, allow_query: bool = True) -> str:
    value = str(raw or "").strip()
    try:
        parsed = urlsplit(value)
        # Accessing .port performs validation that urlsplit itself defers.
        _ = parsed.port
    except ValueError:
        fail(f"{name} must contain a valid HTTPS host and optional numeric port")
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_query and parsed.query)
        or "\\" in value
        or any(char.isspace() or ord(char) < 0x20 for char in value)
    ):
        suffix = " or query string" if not allow_query else ""
        fail(f"{name} must be an HTTPS URL without credentials, a fragment{suffix}")
    return value


def bind_source(service: dict, target: str) -> Path:
    for volume in service.get("volumes", []):
        if isinstance(volume, dict) and volume.get("target") == target:
            if volume.get("type") != "bind" or not volume.get("read_only"):
                fail(f"{target} must be a read-only bind mount")
            bind = volume.get("bind") or {}
            # Compose canonicalization drops an explicit false (while a
            # short-syntax/default-creating bind renders as true).
            if bind.get("create_host_path", False) is not False:
                fail(f"{target} bind mount must set create_host_path:false")
            source = volume.get("source")
            if not source:
                fail(f"{target} bind mount has no host source")
            return Path(str(source))
    fail(f"release service has no bind mount for {target}")


def validate_tls_file(
    path: Path,
    *,
    label: str,
    private: bool,
    require_other_read: bool = False,
    required_group: int | None = None,
) -> None:
    if not path.is_file():
        fail(f"TLS {label} is not a regular file: {path}")
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if private:
        # Runtime is UID 10001, while deployment keys should remain owned by
        # root (or the operator) on the host.  A single exact supplementary
        # group bridges that bind-mount boundary without making the key
        # world-readable. Group write/execute and every `other` bit remain
        # forbidden.
        if mode not in {0o440, 0o640}:
            fail(
                f"TLS {label} must be owner/group-readable only (expected "
                f"0640 or stricter, got {mode:04o}): {path}"
            )
        if required_group is None or metadata.st_gid != required_group:
            fail(
                f"TLS {label} group {metadata.st_gid} does not match the "
                f"release secrets group {required_group}: {path}"
            )
    elif mode & 0o022:
        fail(f"TLS {label} must not be group/other writable (got {mode:04o}): {path}")
    if require_other_read and not mode & stat.S_IROTH:
        fail(
            f"TLS {label} must be other-readable by the non-root "
            f"Prometheus container (got {mode:04o}): {path}"
        )


def _same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        # The later regular-file validation reports the precise missing or
        # inaccessible path; this comparison must not obscure that error.
        return False


def _openssl(*arguments: str, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ("openssl", *arguments),
            input=input_bytes,
            stdin=None if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        fail("openssl is required to validate release TLS material")
    except subprocess.TimeoutExpired:
        fail("openssl timed out while validating release TLS material")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        fail(f"openssl {' '.join(arguments[:2])} failed: {detail or 'invalid input'}")
    return completed.stdout


def _certificate_blocks(path: Path, *, label: str) -> list[bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        fail(f"could not read TLS {label}: {exc}")
    if any(marker in payload for marker in _PRIVATE_KEY_MARKERS):
        fail(f"TLS {label} must not contain private-key material: {path}")
    blocks = _PEM_CERTIFICATE_RE.findall(payload)
    if not blocks:
        fail(f"TLS {label} contains no PEM certificate: {path}")
    return blocks


def validate_server_identity(
    *, certificate: Path, private_key: Path, ca_bundle: Path, host: str, label: str
) -> None:
    """Validate one deployed TLS identity and its trust chain with OpenSSL."""

    leaf_chain = _certificate_blocks(certificate, label=f"{label} certificate")
    ca_certificates = _certificate_blocks(ca_bundle, label=f"{label} CA bundle")

    # Uvicorn and tonic are not configured with a key-password callback, so an
    # encrypted or malformed key would only crash the container at runtime.
    _openssl("pkey", "-in", str(private_key), "-noout", "-check")
    certificate_public_key = _openssl("x509", "-in", str(certificate), "-pubkey", "-noout")
    private_public_key = _openssl("pkey", "-in", str(private_key), "-pubout")
    if certificate_public_key.strip() != private_public_key.strip():
        fail(f"{label} certificate public key does not match its private key")

    _openssl(
        "x509",
        "-in",
        str(certificate),
        "-noout",
        "-checkend",
        str(_CERTIFICATE_MIN_VALIDITY_SECONDS),
    )
    leaf_text = _openssl("x509", "-in", str(certificate), "-noout", "-text").decode(
        "utf-8", errors="replace"
    )
    if "TLS Web Server Authentication" not in leaf_text:
        fail(f"{label} certificate must explicitly allow TLS Web Server Authentication")
    # `openssl verify -verify_hostname` may fall back to the legacy Common Name
    # when no SAN extension exists. Release identities require an explicit DNS
    # SAN; hostname verification below then proves that SAN covers `host`.
    san_text = _openssl(
        "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"
    ).decode("utf-8", errors="replace")
    if "DNS:" not in san_text:
        fail(f"{label} certificate must contain an explicit DNS subjectAltName")

    # Inspect every trust-bundle certificate, not just the first. A client leaf
    # accidentally supplied as a CA file must never become a trust anchor.
    for index, ca_certificate in enumerate(ca_certificates, start=1):
        _openssl(
            "x509",
            "-noout",
            "-checkend",
            str(_CERTIFICATE_MIN_VALIDITY_SECONDS),
            input_bytes=ca_certificate,
        )
        ca_text = _openssl("x509", "-noout", "-text", input_bytes=ca_certificate).decode(
            "utf-8", errors="replace"
        )
        if "CA:TRUE" not in ca_text:
            fail(f"{label} CA bundle certificate {index} is not a CA")
        # RFC 5280 treats an absent keyUsage extension as unrestricted. If a
        # CA does carry the extension, however, keyCertSign must be asserted.
        if "X509v3 Key Usage" in ca_text and "Certificate Sign" not in ca_text:
            fail(
                f"{label} CA bundle certificate {index} forbids certificate signing"
            )

    verify_arguments = [
        "verify",
        "-x509_strict",
        "-purpose",
        "sslserver",
        "-verify_hostname",
        host,
        "-CAfile",
        str(ca_bundle),
    ]
    intermediate_path: str | None = None
    try:
        if len(leaf_chain) > 1:
            with tempfile.NamedTemporaryFile(prefix="robot-vault-chain-", suffix=".pem", delete=False) as handle:
                for block in leaf_chain[1:]:
                    handle.write(block)
                    handle.write(b"\n")
                intermediate_path = handle.name
            verify_arguments.extend(("-untrusted", intermediate_path))
        verify_arguments.append(str(certificate))
        _openssl(*verify_arguments)
    finally:
        if intermediate_path is not None:
            Path(intermediate_path).unlink(missing_ok=True)


def _strict_uri_component(url_name: str, label: str, encoded: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
        fail(f"{url_name} {label} contains a malformed percent escape")
    try:
        decoded = unquote(encoded, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        fail(f"{url_name} {label} is not valid percent-encoded UTF-8")
    if quote(decoded, safe="") != encoded:
        fail(f"{url_name} {label} must use canonical percent encoding")
    if any(ord(character) < 0x20 for character in decoded):
        fail(f"{url_name} {label} contains a control character")
    return decoded


def _database_url_parts(url_name: str, database_url: object) -> tuple[str, str, str, str]:
    value = str(database_url or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail(f"{url_name} is not a valid PostgreSQL URI")
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != "postgres"
        or port != 5432
        or parsed.query
        or parsed.fragment
        or not parsed.username
        or parsed.password is None
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or parsed.path.count("/") != 1
    ):
        fail(
            f"{url_name} must target postgres:5432 with encoded user, password, "
            "database, and no query or fragment"
        )

    username = _strict_uri_component(url_name, "username", parsed.username)
    password = _strict_uri_component(url_name, "password", parsed.password)
    database = _strict_uri_component(url_name, "database", parsed.path[1:])
    return value, username, password, database


def validate_database_url(services: dict, database_url: object) -> tuple[str, str, str, str]:
    value, username, password, database = _database_url_parts(
        "DATABASE_URL", database_url
    )
    postgres_env = _environment(services.get("postgres", {}), "postgres")
    expected = {
        "username": str(postgres_env.get("POSTGRES_USER") or ""),
        "password": str(postgres_env.get("POSTGRES_PASSWORD") or ""),
        "database": str(postgres_env.get("POSTGRES_DB") or ""),
    }
    actual = {"username": username, "password": password, "database": database}
    for name, expected_value in expected.items():
        if actual[name] != expected_value:
            fail(f"DATABASE_URL {name} does not match the Postgres service configuration")
    return value, username, password, database


def _visible_secret(name: str, value: object, *, minimum: int = 32) -> str:
    secret = str(value or "")
    if len(secret.encode("utf-8")) < minimum or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in secret
    ):
        fail(
            f"release {name} must contain at least {minimum} visible ASCII "
            "characters (use a generated secret)"
        )
    return secret


def _strict_bool(name: str, value: object) -> bool:
    token = str(value or "")
    if token == "true":
        return True
    if token == "false":
        return False
    fail(f"release {name} must be exactly 'true' or 'false'")


def _bounded_int(
    name: str, value: object, *, minimum: int, maximum: int
) -> int:
    token = str(value or "")
    if not re.fullmatch(r"[0-9]+", token):
        fail(f"release {name} must be an integer")
    parsed = int(token, 10)
    if parsed < minimum or parsed > maximum:
        fail(f"release {name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(
    name: str, value: object, *, minimum: float, maximum: float
) -> float:
    token = str(value or "")
    if token != token.strip() or not token:
        fail(f"release {name} must be a finite number")
    try:
        parsed = float(token)
    except ValueError:
        fail(f"release {name} must be a finite number")
    if not math.isfinite(parsed):
        fail(f"release {name} must be a finite number")
    if parsed < minimum or parsed > maximum:
        fail(
            f"release {name} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def validate_gc_settings(service: dict) -> None:
    """Reject deletion-policy typos and unsafe numeric ranges pre-deploy."""
    env = _environment(service, "gc")
    _strict_bool("GC_DRY_RUN", env.get("GC_DRY_RUN"))
    for name in ("GC_MIN_AGE_HOURS", "GC_MPU_MAX_AGE_HOURS"):
        _bounded_float(
            name,
            env.get(name),
            minimum=1 / 3600,
            maximum=87600,
        )
    _bounded_int(
        "GC_DELETE_LIMIT",
        env.get("GC_DELETE_LIMIT"),
        minimum=1,
        maximum=100000,
    )
    _bounded_int(
        "GC_INTERVAL_SECONDS",
        env.get("GC_INTERVAL_SECONDS"),
        minimum=1,
        maximum=604800,
    )
    if str(env.get("METRICS_PORT") or "") != "9108":
        fail("release GC metrics port must be hard-set to 9108")

    allowed_prefixes = {
        "raw/",
        "manifests/",
        "processed/parquet/",
        "cas/files/",
        "processed/media/",
        "qa/reports/",
        "exports/",
    }
    raw_prefixes = str(env.get("GC_PREFIXES") or "")
    prefixes = raw_prefixes.split(",")
    if (
        not raw_prefixes
        or any(not prefix or prefix != prefix.strip() for prefix in prefixes)
        or len(prefixes) != len(set(prefixes))
        or not set(prefixes).issubset(allowed_prefixes)
    ):
        fail(
            "release GC_PREFIXES must be a non-empty, duplicate-free subset "
            "of the seven policy-covered prefixes"
        )


def _rendered_number(
    name: str, value: object, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        fail(f"release {name} must be numeric")
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        fail(f"release {name} must be numeric")
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        fail(f"release {name} must be between {minimum:g} and {maximum:g}")
    return parsed


def validate_resource_guardrails(services: dict, *, extra_services: dict) -> None:
    """Require finite host resource ceilings and bounded container logs."""

    service_names = [
        "postgres",
        "objectstore",
        "objectstore-access-ready",
        "redis",
        "db-migrate",
        "postgres-roles-ready",
        "ingest",
        "processor",
        "query",
        "gc",
        "prometheus",
        *extra_services,
    ]

    for service_name in service_names:
        service = services.get(service_name)
        if not isinstance(service, dict):
            fail(f"release is missing bounded service {service_name}")
        _rendered_number(
            f"{service_name} memory limit",
            service.get("mem_limit"),
            minimum=128 * 1024 * 1024,
            maximum=1024 * 1024 * 1024 * 1024,
        )
        _rendered_number(
            f"{service_name} CPU limit",
            service.get("cpus"),
            minimum=0.1,
            maximum=256,
        )
        _rendered_number(
            f"{service_name} PID limit",
            service.get("pids_limit"),
            minimum=32,
            maximum=1_048_576,
        )
        logging = service.get("logging")
        if not isinstance(logging, dict) or logging.get("driver") != "json-file":
            fail(f"release service {service_name} must use the json-file logger")
        options = logging.get("options")
        if not isinstance(options, dict):
            fail(f"release service {service_name} must bound retained logs")
        max_size = str(options.get("max-size") or "")
        max_files = str(options.get("max-file") or "")
        if re.fullmatch(r"[1-9][0-9]*(?:k|m|g)", max_size) is None:
            fail("release ROBOT_VAULT_LOG_MAX_SIZE must be a positive k/m/g size")
        _bounded_int(
            "ROBOT_VAULT_LOG_MAX_FILES", max_files, minimum=1, maximum=100
        )

    _rendered_number(
        "POSTGRES_SHM_SIZE",
        services["postgres"].get("shm_size"),
        minimum=16 * 1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )
    prometheus_env = _environment(services["prometheus"], "prometheus")
    retention_time = str(prometheus_env.get("PROMETHEUS_RETENTION_TIME") or "")
    retention_size = str(prometheus_env.get("PROMETHEUS_RETENTION_SIZE") or "")
    if re.fullmatch(r"[1-9][0-9]*(?:s|m|h|d|w|y)", retention_time) is None:
        fail("release PROMETHEUS_RETENTION_TIME must be a positive Prometheus duration")
    if re.fullmatch(r"[1-9][0-9]*(?:KB|MB|GB|TB)", retention_size) is None:
        fail("release PROMETHEUS_RETENTION_SIZE must be a positive KB/MB/GB/TB size")


def validate_query_admission_settings(query: dict) -> None:
    """Bound query work."""
    env = _environment(query, "query")
    _bounded_int(
        "DUCKDB_THREADS",
        env.get("DUCKDB_THREADS"),
        minimum=1,
        maximum=256,
    )
    _bounded_int(
        "DUCKDB_MEMORY_LIMIT_MB",
        env.get("DUCKDB_MEMORY_LIMIT_MB"),
        minimum=64,
        maximum=1_048_576,
    )
    _bounded_int(
        "DUCKDB_QUERY_CONCURRENCY",
        env.get("DUCKDB_QUERY_CONCURRENCY"),
        minimum=1,
        maximum=64,
    )
    for name in (
        "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS",
        "DUCKDB_QUERY_TIMEOUT_SECONDS",
        "QUERY_MAX_SOURCE_RANGE_SECONDS",
    ):
        _bounded_float(name, env.get(name), minimum=0.001, maximum=315_360_000)
    _bounded_int(
        "QUERY_MAX_SOURCE_FILES",
        env.get("QUERY_MAX_SOURCE_FILES"),
        minimum=1,
        maximum=1_000_000,
    )
    _bounded_int(
        "QUERY_MAX_SOURCE_ROWS",
        env.get("QUERY_MAX_SOURCE_ROWS"),
        minimum=1,
        maximum=1_000_000_000_000,
    )
    _bounded_int(
        "QUERY_MAX_SOURCE_BYTES",
        env.get("QUERY_MAX_SOURCE_BYTES"),
        minimum=1,
        maximum=1_125_899_906_842_624,
    )
    _bounded_int(
        "SQL_CONSOLE_MEMORY_LIMIT_MB",
        env.get("SQL_CONSOLE_MEMORY_LIMIT_MB"),
        minimum=64,
        maximum=1_048_576,
    )


# The object store, exactly as development/objectstore/CONTRACT.md sections 1-4
# freeze it. A new base digest or setting needs a new gate run and a new contract.
# The image is our own layer on the pinned RustFS (deploy/release-images/objectstore.Dockerfile),
# a local release artifact like the other first-party images.
OBJECTSTORE_IMAGE_RE = re.compile(r"robot-vault-objectstore:[A-Za-z0-9._-]+")
OBJECTSTORE_ENDPOINT = "http://objectstore:9000"
OBJECTSTORE_SECRET = "objectstore_root_password"
OBJECTSTORE_SECRET_PATH = f"/run/secrets/{OBJECTSTORE_SECRET}"
OBJECTSTORE_ENV = {
    "RUSTFS_SECRET_KEY_FILE": OBJECTSTORE_SECRET_PATH,
    "RUSTFS_VOLUMES": "/data",
    "RUSTFS_ADDRESS": "0.0.0.0:9000",
    "RUSTFS_CONSOLE_ENABLE": "false",
    "RUSTFS_DURABILITY_MODE": "strict",
    "RUSTFS_DRIVE_SYNC_ENABLE": "true",
    "RUSTFS_NEW_BUCKET_DURABILITY_MODE": "strict",
    "RUSTFS_PUT_RENAME_EARLY_ACK_ENABLE": "false",
    "RUSTFS_API_STALE_UPLOADS_EXPIRY": "360h",
    "RUSTFS_API_STALE_UPLOADS_CLEANUP_INTERVAL": "6h",
}
OBJECTSTORE_HEALTHCHECK = [
    "CMD", "curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:9000/health/ready",
]
OBJECTSTORE_BOOTSTRAP_CONFIGS = {
    "/release/objectstore_bootstrap.py": ("objectstore_bootstrap", "0444"),
    **{
        f"/release/objectstore-policies/{name}.json": (f"objectstore_policy_{name}", "0444")
        for name in ("ingest", "processor", "exporter", "query", "gc")
    },
}
_FOREIGN_STORE_ENV = re.compile(r"RUSTFS_")


def _secret_sources(service: dict, name: str) -> list[str]:
    sources = []
    for item in service.get("secrets") or []:
        if not isinstance(item, dict):
            fail(f"release service {name} has an invalid Compose secret mount")
        source = str(item.get("source") or "")
        target = str(item.get("target") or source)
        if target not in (source, f"/run/secrets/{source}"):
            fail(f"release service {name} must mount secret {source} at /run/secrets/{source}")
        sources.append(source)
    return sources


def validate_objectstore(config: dict, services: dict) -> None:
    """The pinned store, its frozen settings, and the root password as a file."""

    store = services.get("objectstore")
    if not isinstance(store, dict):
        fail("release is missing the objectstore service")
    if not OBJECTSTORE_IMAGE_RE.fullmatch(str(store.get("image") or "")):
        fail("objectstore must use the contract image robot-vault-objectstore:<version>")
    if store.get("user") not in (None, ""):
        fail("objectstore must run as the image user (do not set user:)")
    if store.get("entrypoint") or store.get("command"):
        fail("objectstore must use the image entrypoint and command")
    env = {key: str(value) for key, value in _environment(store, "objectstore").items()}
    root_user = env.pop("RUSTFS_ACCESS_KEY", "")
    if not root_user:
        fail("objectstore RUSTFS_ACCESS_KEY must be OBJECTSTORE_ROOT_USER")
    if env != OBJECTSTORE_ENV:
        fail(
            "objectstore environment must be exactly the frozen contract "
            "(strict durability, 360h stale-upload expiry, no console, root password file)"
        )
    healthcheck = store.get("healthcheck") or {}
    if healthcheck.get("test") != OBJECTSTORE_HEALTHCHECK:
        fail("objectstore healthcheck must probe /health/ready")
    volumes = [
        (m.get("type"), m.get("source"), m.get("target"))
        for m in store.get("volumes") or []
        if isinstance(m, dict)
    ]
    if volumes != [("volume", "objectstore_data", "/data")]:
        fail("objectstore must keep its data only in the named volume objectstore_data at /data")
    if store.get("ports") or store.get("expose"):
        fail("objectstore must not publish or expose a port (S3 stays on the Compose network)")

    bootstrap_env = _environment(services["objectstore-access-ready"], "objectstore-access-ready")
    if str(bootstrap_env.get("OBJECTSTORE_ROOT_USER") or "") != root_user:
        fail("objectstore-access-ready OBJECTSTORE_ROOT_USER must match the object store")
    if "OBJECTSTORE_ROOT_PASSWORD_FILE" in bootstrap_env:
        fail("objectstore-access-ready must take OBJECTSTORE_ROOT_PASSWORD from RELEASE_ENV_FILE")
    root_password = _visible_secret("OBJECTSTORE_ROOT_PASSWORD", bootstrap_env.get("OBJECTSTORE_ROOT_PASSWORD"))
    if root_password == "dev-objectstore-root-password-change-me":  # configs/objectstore-root-password.dev
        fail("release refuses the committed development object-store root password")

    secret = (config.get("secrets") or {}).get(OBJECTSTORE_SECRET)
    if not isinstance(secret, dict) or secret.get("environment") != "OBJECTSTORE_ROOT_PASSWORD" or secret.get("file"):
        fail(f"Compose secret {OBJECTSTORE_SECRET} must come from OBJECTSTORE_ROOT_PASSWORD")
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        holders = _secret_sources(service, name)
        if name == "objectstore":
            if holders != [OBJECTSTORE_SECRET]:
                fail(f"{name} must mount exactly the {OBJECTSTORE_SECRET} secret")
        elif OBJECTSTORE_SECRET in holders:
            fail(f"{name} must not receive {OBJECTSTORE_SECRET}")
        service_env = service.get("environment") or {}
        if "OBJECTSTORE_ROOT_PASSWORD" in service_env and name != "objectstore-access-ready":
            fail(f"{name} must not receive OBJECTSTORE_ROOT_PASSWORD")
        for key in service_env:
            if _FOREIGN_STORE_ENV.match(str(key)) and name != "objectstore":
                fail(f"{name} must not receive {key}")


def _environment(service: dict, name: str) -> dict:
    env = service.get("environment")
    if not isinstance(env, dict):
        fail(f"release service {name} has no rendered environment mapping")
    return env


def validate_first_party_runtime(services: dict, *, extra_services: dict) -> int:
    """Validate the rendered least-privilege contract and return secrets GID."""

    expected_writable_volumes: dict[str, dict[str, str]] = {
        "db-migrate": {},
        "postgres-roles-ready": {},
        "objectstore-access-ready": {},
        "ingest": {"/app/uploads": "uploads"},
        "processor": {"/var/lib/robot-vault/processor": "processor_work"},
        "query": {"/var/lib/robot-vault/query": "query_work"},
        "gc": {},
        **extra_services,
    }

    for service_name, expected_volumes in expected_writable_volumes.items():
        service = services.get(service_name)
        if not isinstance(service, dict):
            fail(f"release is missing first-party service {service_name}")
        if str(service.get("user") or "") != "10001:10001":
            fail(f"release service {service_name} must run as UID:GID 10001:10001")
        if service.get("read_only") is not True:
            fail(f"release service {service_name} must use a read-only root filesystem")
        if {str(value).upper() for value in service.get("cap_drop", [])} != {"ALL"}:
            fail(f"release service {service_name} must drop all Linux capabilities")
        security_options = {
            str(value).lower().replace("_", "-")
            for value in service.get("security_opt", [])
        }
        if "no-new-privileges:true" not in security_options:
            fail(f"release service {service_name} must forbid new privileges")

        tmpfs = [str(value) for value in service.get("tmpfs", [])]
        if len(tmpfs) != 1 or not tmpfs[0].startswith("/tmp:"):
            fail(f"release service {service_name} must have exactly one /tmp tmpfs")
        tmpfs_options = set(tmpfs[0].split(":", 1)[1].split(","))
        required_options = {"rw", "noexec", "nosuid", "nodev", "size=64m", "mode=1777"}
        if not required_options.issubset(tmpfs_options):
            fail(
                f"release service {service_name} /tmp must be a bounded "
                "noexec,nosuid,nodev tmpfs"
            )

        writable_volumes: dict[str, str] = {}
        for volume in service.get("volumes") or []:
            if not isinstance(volume, dict):
                fail(f"release service {service_name} has an invalid volume entry")
            if volume.get("read_only") is True:
                continue
            target = str(volume.get("target") or "")
            source = str(volume.get("source") or "")
            if volume.get("type") != "volume":
                fail(
                    f"release service {service_name} writable mount {target!r} "
                    "must be a named volume"
                )
            writable_volumes[target] = source
        if writable_volumes != expected_volumes:
            fail(
                f"release service {service_name} writable volumes must be "
                f"{expected_volumes!r} (got {writable_volumes!r})"
            )

        runtime_env = _environment(service, service_name)
        for name, expected in {
            "HOME": "/tmp/robot-vault",
            "XDG_CACHE_HOME": "/tmp/robot-vault/.cache",
            "TMPDIR": "/tmp",
        }.items():
            if str(runtime_env.get(name) or "") != expected:
                fail(f"release service {service_name} {name} must be {expected!r}")

    processor_env = _environment(services["processor"], "processor")
    if processor_env.get("MEDIA_SPOOL_DIR") != "/var/lib/robot-vault/processor":
        fail("release processor MEDIA_SPOOL_DIR must use its writable work volume")
    query_env = _environment(services["query"], "query")
    if query_env.get("DUCKDB_TEMP_DIRECTORY") != "/var/lib/robot-vault/query":
        fail("release query DUCKDB_TEMP_DIRECTORY must use its writable work volume")

    secrets_groups: list[int] = []
    for service_name in ("ingest", "query"):
        values = services[service_name].get("group_add") or []
        if len(values) != 1 or not str(values[0]).isdigit():
            fail(f"release service {service_name} must add one numeric secrets group")
        group_id = int(values[0])
        if group_id <= 0 or group_id > 2_147_483_647:
            fail(
                f"release service {service_name} secrets group must be a "
                "positive 32-bit non-root GID"
            )
        secrets_groups.append(group_id)
    if len(set(secrets_groups)) != 1:
        fail("release ingest and query must use the same secrets group")
    return secrets_groups[0]


def validate_config(path: Path, *, expected_edition: str) -> None:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        services = config["services"]
        query = services["query"]
        ingest = services["ingest"]
        processor = services["processor"]
        prometheus = services["prometheus"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail(f"could not inspect rendered Compose JSON: {exc}")

    if expected_edition not in {"core", "pro"}:
        fail("expected release edition must be core or pro")

    env = _environment(query, "query")
    ingest_env = _environment(ingest, "ingest")
    processor_env = _environment(processor, "processor")
    prometheus_env = _environment(prometheus, "prometheus")
    alertmanager_endpoint = str(prometheus_env.get("ALERTMANAGER_ENDPOINT") or "")
    if alertmanager_endpoint:
        endpoint_match = re.fullmatch(r"https?://([A-Za-z0-9.-]+):([0-9]{1,5})", alertmanager_endpoint)
        if endpoint_match is None:
            fail("ALERTMANAGER_ENDPOINT must be http(s)://hostname:port without a path or credentials")
        host, port_text = endpoint_match.groups()
        if (len(host) > 253 or any(len(label) > 63 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) for label in host.split(".")) or not 1 <= int(port_text) <= 65535):
            fail("ALERTMANAGER_ENDPOINT has an invalid hostname or port")

    public_host = str(env.get("QUERY_PUBLIC_HOST") or "").strip()
    if not _PUBLIC_HOST_RE.fullmatch(public_host):
        fail("QUERY_PUBLIC_HOST must be a DNS hostname without a scheme, port, wildcard, or underscore")
    ingest_public_host = str(ingest_env.get("INGEST_PUBLIC_HOST") or "").strip()
    if not _PUBLIC_HOST_RE.fullmatch(ingest_public_host):
        fail(
            "INGEST_PUBLIC_HOST must be a DNS hostname without a scheme, port, "
            "wildcard, or underscore"
        )

    hard_policy = {
        "ROBOT_VAULT_RELEASE": "true",
        "AUTH_MODE": "oidc",
        "OIDC_DEV": "false",
        "OIDC_ALGORITHMS": "RS256",
        "OIDC_ROLE_SOURCE": "claim",
        "ENABLE_HSTS": "true",
    }
    for name, expected in hard_policy.items():
        if str(env.get(name) or "") != expected:
            fail(f"query {name} must be hard-set to {expected!r} in release")

    ingest_hard_policy = {
        "TLS_ENABLED": "true",
        "TLS_SERVER_CERT": "/run/robot-vault/tls/ingest.crt",
        "TLS_SERVER_KEY": "/run/robot-vault/tls/ingest.key",
        "TLS_CA_CERT": INGEST_CA_TARGET,
    }
    for name, expected in ingest_hard_policy.items():
        if str(ingest_env.get(name) or "") != expected:
            fail(f"ingest {name} must be hard-set to {expected!r} in release")
    # `token` (default) or `mtls`; `dev` and any other value are refused.
    ingest_profile = str(ingest_env.get("INGEST_SECURITY_PROFILE") or "")
    if ingest_profile not in {"token", "mtls"}:
        fail(
            "INGEST_SECURITY_PROFILE must be token or mtls in release "
            f"(got {ingest_profile!r})"
        )
    # Ingest derives TLS_MTLS from the profile; a second switch could only
    # contradict it.
    if "TLS_MTLS" in ingest_env:
        fail("ingest must not set TLS_MTLS in release; INGEST_SECURITY_PROFILE decides it")
    # Robots are told to use this port (INGEST_GRPC_PORT); ingest refuses
    # anything but a bare port, so refuse it here before the stack starts.
    public_port = str(ingest_env.get("INGEST_PUBLIC_PORT") or "")
    if not re.fullmatch(r"[0-9]{1,5}", public_port) or not 1 <= int(public_port) <= 65535:
        fail(f"INGEST_GRPC_PORT must be a bare port in 1-65535 (got {public_port!r})")
    if str(env.get("INGEST_CA_CERT_FILE") or "") != INGEST_CA_TARGET:
        fail(f"query INGEST_CA_CERT_FILE must be hard-set to {INGEST_CA_TARGET!r} in release")
    if "ROBOT_VAULT_RELEASE" in ingest_env:
        fail("ingest must not set ROBOT_VAULT_RELEASE; use INGEST_SECURITY_PROFILE")
    ingest_token = str(ingest_env.get("INGEST_AUTH_TOKEN") or "").strip()
    if not ingest_token:
        fail("release ingest requires a non-empty INGEST_AUTH_TOKEN")
    if ingest_token == "dev-ingest-token-change-me":
        fail("release ingest refuses the committed development token")
    if len(ingest_token.encode("utf-8")) < 32 or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in ingest_token
    ):
        fail(
            "release INGEST_AUTH_TOKEN must contain at least 32 visible ASCII "
            "characters (use a generated secret)"
        )
    provisioning_token = str(
        ingest_env.get("INGEST_PROVISIONING_TOKEN") or ""
    ).strip()
    if len(provisioning_token.encode("utf-8")) < 32 or any(
        ord(character) < 0x21 or ord(character) > 0x7E
        for character in provisioning_token
    ):
        fail(
            "release INGEST_PROVISIONING_TOKEN must contain at least 32 visible "
            "ASCII characters (use a generated secret)"
        )
    if provisioning_token == ingest_token:
        fail("INGEST_PROVISIONING_TOKEN must be distinct from INGEST_AUTH_TOKEN")
    if str(env.get("INGEST_PROVISIONING_TOKEN") or "") != provisioning_token:
        fail(
            "query and ingest must use the same INGEST_PROVISIONING_TOKEN"
        )
    metrics_token = str(env.get("METRICS_AUTH_TOKEN") or "")
    if len(metrics_token.encode("utf-8")) < 32 or any(
        ord(character) < 0x21 or ord(character) > 0x7E
        for character in metrics_token
    ):
        fail(
            "release METRICS_AUTH_TOKEN must contain at least 32 visible ASCII "
            "characters (use a generated secret)"
        )
    if metrics_token == ingest_token:
        fail("METRICS_AUTH_TOKEN must be distinct from INGEST_AUTH_TOKEN")
    if metrics_token == provisioning_token:
        fail("METRICS_AUTH_TOKEN must be distinct from INGEST_PROVISIONING_TOKEN")
    if str(prometheus_env.get("METRICS_AUTH_TOKEN") or "") != metrics_token:
        fail("query and Prometheus must use the same METRICS_AUTH_TOKEN")

    expected_stage_value = "true" if expected_edition == "pro" else "false"
    edition_envs = {
        "query": env,
        "processor": processor_env,
        "prometheus": prometheus_env,
    }
    for service_name, service_env in edition_envs.items():
        if str(service_env.get("ROBOT_VAULT_EDITION") or "") != expected_edition:
            fail(
                f"{service_name} ROBOT_VAULT_EDITION must equal requested "
                f"edition {expected_edition!r}"
            )
    for name in ("SEGMENTATION_ENABLED", "QA_ENABLED", "MEDIA_ENCODE_ENABLED"):
        if str(processor_env.get(name) or "") != expected_stage_value:
            fail(
                f"processor {name} must be {expected_stage_value!r} in "
                f"the {expected_edition} edition"
            )
    # The core files define no exporter; a pro release adds it with the
    # Pro release overlay, whose checks run below.
    edition_checks = None
    extra_services: dict = {}
    if expected_edition == "core":
        if "exporter" in services:
            fail("exporter presence does not match requested edition 'core'")
    else:
        edition_checks = _edition_checks()
        edition_checks.check_topology(services)
        extra_services = edition_checks.EXTRA_SERVICES

    validate_query_admission_settings(query)

    secrets_group = validate_first_party_runtime(
        services, extra_services=extra_services
    )
    validate_resource_guardrails(services, extra_services=extra_services)
    if edition_checks is not None:
        edition_checks.check_services(services, secrets_group=secrets_group)

    https_url("OIDC_ISSUER", env.get("OIDC_ISSUER"), allow_query=False)
    https_url("OIDC_JWKS_URL", env.get("OIDC_JWKS_URL"))
    https_url("OIDC_AUTHORIZATION_URL", env.get("OIDC_AUTHORIZATION_URL"))
    https_url("OIDC_TOKEN_URL", env.get("OIDC_TOKEN_URL"))
    if str(env.get("OIDC_END_SESSION_URL") or "").strip():
        https_url("OIDC_END_SESSION_URL", env.get("OIDC_END_SESSION_URL"))

    client_id = str(env.get("OIDC_CLIENT_ID") or "").strip()
    if not client_id or any(ord(char) < 0x20 for char in client_id):
        fail("OIDC_CLIENT_ID must be non-empty and contain no control characters")

    cert = bind_source(query, "/run/robot-vault/tls/query.crt")
    key = bind_source(query, "/run/robot-vault/tls/query.key")
    ca = bind_source(prometheus, "/run/robot-vault/tls/query-ca.pem")
    ingest_cert = bind_source(ingest, "/run/robot-vault/tls/ingest.crt")
    ingest_key = bind_source(ingest, "/run/robot-vault/tls/ingest.key")
    ingest_ca = bind_source(ingest, INGEST_CA_TARGET)
    # Query serves this file at /ca.crt for robots to check against the pin
    # ingest mints from its own copy; two different files would break every
    # enrollment.
    if not _same_file(bind_source(query, INGEST_CA_TARGET), ingest_ca):
        fail("query and ingest must mount the same INGEST_TLS_CA_FILE")
    if _same_file(cert, key):
        fail("QUERY_TLS_CERT_FILE and QUERY_TLS_KEY_FILE must be different files")
    if _same_file(ca, key):
        fail("QUERY_TLS_CA_FILE must not refer to the private-key file")
    if _same_file(cert, ca):
        fail("QUERY_TLS_CERT_FILE and QUERY_TLS_CA_FILE must be different files")
    if _same_file(ingest_cert, ingest_key):
        fail("INGEST_TLS_CERT_FILE and INGEST_TLS_KEY_FILE must be different files")
    if _same_file(ingest_ca, ingest_key):
        fail("INGEST_TLS_CA_FILE must not refer to the private-key file")
    if _same_file(ingest_cert, ingest_ca):
        fail("INGEST_TLS_CERT_FILE and INGEST_TLS_CA_FILE must be different files")
    validate_tls_file(
        cert, label="certificate", private=False, require_other_read=True
    )
    validate_tls_file(
        key, label="private key", private=True, required_group=secrets_group
    )
    validate_tls_file(ca, label="CA bundle", private=False, require_other_read=True)
    validate_tls_file(
        ingest_cert,
        label="ingest certificate",
        private=False,
        require_other_read=True,
    )
    validate_tls_file(
        ingest_key,
        label="ingest private key",
        private=True,
        required_group=secrets_group,
    )
    validate_tls_file(
        ingest_ca,
        label="ingest CA bundle",
        private=False,
        require_other_read=True,
    )
    validate_server_identity(
        certificate=cert,
        private_key=key,
        ca_bundle=ca,
        host=public_host,
        label="query",
    )
    # One certificate, the root that issued the ingest certificate: robot.sh
    # accepts exactly one PEM block, the pin covers one key, and the
    # `openssl verify` below (no -partial_chain) needs a self-signed anchor.
    if len(_certificate_blocks(ingest_ca, label="ingest CA certificate")) != 1:
        fail(
            "INGEST_TLS_CA_FILE must contain exactly one certificate: the root CA "
            "that issued the ingest certificate"
        )
    validate_server_identity(
        certificate=ingest_cert,
        private_key=ingest_key,
        ca_bundle=ingest_ca,
        host=ingest_public_host,
        label="ingest",
    )

    # Only the migration and role-bootstrap one-shots may see the PostgreSQL
    # owner URI. Every long-running service receives one fixed non-owner role,
    # and all runtime passwords are distinct from one another and the owner.
    if not isinstance(services.get("postgres-roles-ready"), dict):
        fail("release is missing postgres-roles-ready")
    if not isinstance(services.get("objectstore-access-ready"), dict):
        fail("release is missing objectstore-access-ready")
    db_migrate_env = _environment(services["db-migrate"], "db-migrate")
    role_bootstrap_env = _environment(
        services["postgres-roles-ready"], "postgres-roles-ready"
    )
    database_url, owner_username, owner_password, database_name = (
        validate_database_url(services, db_migrate_env.get("DATABASE_URL"))
    )
    if str(db_migrate_env.get("DATABASE_URL") or "") != database_url:
        fail("db-migrate must receive the release PostgreSQL owner URI")
    if str(role_bootstrap_env.get("DATABASE_URL") or "") != database_url:
        fail("postgres-roles-ready must receive the release PostgreSQL owner URI")

    runtime_databases = {
        "ingest": ("INGEST_DATABASE_URL", "robot_vault_ingest"),
        "processor": ("PROCESSOR_DATABASE_URL", "robot_vault_processor"),
        "exporter": ("EXPORTER_DATABASE_URL", "robot_vault_exporter"),
        "query": ("QUERY_DATABASE_URL", "robot_vault_query"),
        "gc": ("GC_DATABASE_URL", "robot_vault_gc"),
    }
    database_passwords = [owner_password]
    for service_name, (variable, expected_username) in runtime_databases.items():
        rendered_url = role_bootstrap_env.get(variable)
        value, username, password, runtime_database = _database_url_parts(
            variable, rendered_url
        )
        if username != expected_username:
            fail(f"{variable} username must be {expected_username!r}")
        if runtime_database != database_name:
            fail(f"{variable} database must match DATABASE_URL")
        if username == owner_username:
            fail(f"{variable} must not reuse the PostgreSQL owner role")
        _visible_secret(f"{variable} password", password)
        database_passwords.append(password)
        if service_name in services:
            service_env = _environment(services[service_name], service_name)
            if str(service_env.get("DATABASE_URL") or "") != value:
                fail(f"{service_name} must receive exactly {variable}")
            if str(service_env.get("DATABASE_URL") or "") == database_url:
                fail(f"{service_name} must not receive the PostgreSQL owner URI")
    if len(database_passwords) != len(set(database_passwords)):
        fail("PostgreSQL owner and runtime passwords must all be distinct")

    # The observability role is opt-in. A non-obs release must not
    # acquire a new mandatory secret, while a configured ops profile must use
    # one distinct fixed login on the same database.
    ops_bootstrap = services.get("ops-roles-ready")
    ops_service = services.get("ops-metrics")
    if isinstance(ops_bootstrap, dict) and isinstance(ops_service, dict):
        ops_bootstrap_env = _environment(ops_bootstrap, "ops-roles-ready")
        ops_env = _environment(ops_service, "ops-metrics")
        ops_url = str(ops_bootstrap_env.get("OPS_DATABASE_URL") or "")
        if ops_url:
            value, username, password, ops_database = _database_url_parts(
                "OPS_DATABASE_URL", ops_url
            )
            if username != "robot_vault_ops" or ops_database != database_name:
                fail("OPS_DATABASE_URL must use robot_vault_ops on the release database")
            _visible_secret("OPS_DATABASE_URL password", password)
            if password in database_passwords:
                fail("OPS_DATABASE_URL password must be distinct from owner and runtime passwords")
            if str(ops_bootstrap_env.get("DATABASE_URL") or "") != database_url:
                fail("ops-roles-ready must receive the owner URI only for role bootstrap")
            if str(ops_env.get("OPS_DATABASE_URL") or "") != value:
                fail("ops-metrics must receive exactly OPS_DATABASE_URL")
        elif str(ops_env.get("OPS_DATABASE_URL") or ""):
            fail("ops-metrics has a credential but ops-roles-ready does not")

    # Exactly two external sockets exist: query HTTPS and ingest mTLS gRPC.
    # The admin probe, databases, metrics and object-store APIs stay private.
    published_targets: dict[str, list[int]] = {}
    for service_name, service in services.items():
        ports = service.get("ports", []) if isinstance(service, dict) else []
        if ports:
            targets: list[int] = []
            for port in ports:
                if not isinstance(port, dict) or "target" not in port:
                    fail(f"release service {service_name} has an invalid published port")
                targets.append(int(port["target"]))
            published_targets[service_name] = targets
    if published_targets != {"ingest": [50051], "query": [8003]}:
        fail(
            "release must publish only ingest gRPC target 50051 and query HTTPS "
            f"target 8003 (got {published_targets!r})"
        )

    # Every first-party image is a local release artifact.  `pull_policy:never`
    # prevents a missing bare image name from falling through to a public
    # registry; Make separately proves each version tag exists before `up`.
    first_party_services = [
        "db-migrate",
        "postgres-roles-ready",
        "objectstore-access-ready",
        "objectstore",
        "ingest",
        "processor",
        "query",
        "gc",
        *extra_services,
    ]
    for service_name in first_party_services:
        service = services.get(service_name)
        if not isinstance(service, dict):
            fail(f"release is missing first-party service {service_name}")
        if service.get("pull_policy") != "never":
            fail(f"release service {service_name} must set pull_policy: never")

    toolkit_image = str(services["db-migrate"].get("image") or "")
    if not re.fullmatch(r"robot-vault-toolkit:[A-Za-z0-9._-]+", toolkit_image):
        fail("db-migrate must use the local versioned toolkit image")
    if services["objectstore-access-ready"].get("image") != toolkit_image:
        fail("objectstore-access-ready must use the same toolkit image as db-migrate")

    # Core runs the local core images (release-up checks their revision or the
    # core image lock); the edition overlay checks its own images.
    if edition_checks is None:
        for service_name, core_name in (
            ("processor", "robot-vault-core-processor"),
            ("query", "robot-vault-core-query"),
        ):
            image = str(services[service_name].get("image") or "")
            if not re.fullmatch(re.escape(core_name) + r":[A-Za-z0-9._-]+", image):
                fail(f"core release {service_name} must use the local {core_name} image (got {image!r})")
    else:
        edition_checks.check_images(services)

    # Registry-hosted dependencies must be content-addressed even when they
    # are hidden behind a profile. An exact tag is useful provenance but can
    # still be moved; the manifest digest is the immutable control.
    third_party_services = (
        "postgres",
        "redis",
        "prometheus",
    )  # objectstore is our own local image, checked by validate_objectstore
    for service_name in third_party_services:
        service = services.get(service_name)
        image = str(service.get("image") or "") if isinstance(service, dict) else ""
        if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image):
            fail(
                f"third-party release service {service_name} must use an "
                "immutable sha256 manifest digest"
            )

    # The object-store bootstrap receives exactly six non-secret reviewed
    # config files and runs the reviewed script. release-up's clean-tree check
    # binds these files to the toolkit image's checkout revision.
    objectstore_bootstrap = services["objectstore-access-ready"]
    rendered_bootstrap_configs: dict[str, tuple[str, str]] = {}
    for item in objectstore_bootstrap.get("configs") or []:
        if not isinstance(item, dict):
            fail("objectstore-access-ready has an invalid Compose config mount")
        rendered_bootstrap_configs[str(item.get("target") or "")] = (
            str(item.get("source") or ""),
            str(item.get("mode") or ""),
        )
    if rendered_bootstrap_configs != OBJECTSTORE_BOOTSTRAP_CONFIGS:
        fail("objectstore-access-ready must mount exactly the reviewed bootstrap and five policies")
    if objectstore_bootstrap.get("entrypoint") != [
        "python3",
        "/release/objectstore_bootstrap.py",
    ] or objectstore_bootstrap.get("command"):
        fail("objectstore-access-ready must execute the reviewed bootstrap config")
    validate_objectstore(config, services)

    # The release wait gate requires a real healthcheck on every long-running
    # service it accepts as ready.  One-shot initialization jobs are verified
    # separately by their successful exit codes.
    ready_services = [
        "postgres",
        "objectstore",
        "redis",
        "ingest",
        "processor",
        "query",
        "gc",
        *extra_services,
    ]
    for service_name in ready_services:
        service = services.get(service_name)
        healthcheck = service.get("healthcheck") if isinstance(service, dict) else None
        if not isinstance(healthcheck, dict) or not healthcheck.get("test"):
            fail(f"release service {service_name} must define a healthcheck")

    # S3 is one deployment-wide contract: bootstrap, GC, writers and readers
    # all use the same bucket and endpoint, so no single override can aim the
    # deleter at a different store from the pipeline.
    bucket = str(env.get("S3_PARQUET_BUCKET") or "")
    if not bucket:
        fail("query S3_PARQUET_BUCKET must be non-empty")
    if (
        len(bucket) < 3
        or len(bucket) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
    ):
        fail("release S3_BUCKET must be a 3-63 character DNS-style bucket name")
    storage_services = ["ingest", "processor", "query", "gc", *extra_services]
    expected_storage = {
        "S3_BUCKET": bucket,
        "S3_ENDPOINT": OBJECTSTORE_ENDPOINT,
        "S3_REGION": str(env.get("S3_REGION") or ""),
        "S3_SECURE": "false",
    }
    if not expected_storage["S3_REGION"]:
        fail("release S3_REGION must be non-empty")
    for service_name in storage_services:
        service_env = _environment(services[service_name], service_name)
        for name, expected in expected_storage.items():
            if str(service_env.get(name) or "") != expected:
                fail(
                    f"{service_name} {name} must match the release storage "
                    f"contract ({expected!r})"
                )
    validate_gc_settings(services["gc"])
    bootstrap_env = _environment(services["objectstore-access-ready"], "objectstore-access-ready")
    for name, expected in {
        "S3_BUCKET": bucket,
        "S3_ENDPOINT": OBJECTSTORE_ENDPOINT,
    }.items():
        if str(bootstrap_env.get(name) or "") != expected:
            fail(f"objectstore-access-ready {name} must match {expected!r}")

    # The root pair is confined to the store and the one-shot bootstrap (the
    # store reads the password only from its secret file; validate_objectstore).
    # Runtime access IDs are fixed, secrets are generated/distinct, and no
    # service receives another service's identity.
    root_access_key = str(bootstrap_env.get("OBJECTSTORE_ROOT_USER") or "")
    root_secret = str(bootstrap_env.get("OBJECTSTORE_ROOT_PASSWORD") or "")

    runtime_storage = {
        "ingest": ("robot-vault-ingest", "INGEST_S3_SECRET_KEY"),
        "processor": ("robot-vault-processor", "PROCESSOR_S3_SECRET_KEY"),
        "exporter": ("robot-vault-exporter", "EXPORTER_S3_SECRET_KEY"),
        "query": ("robot-vault-query", "QUERY_S3_SECRET_KEY"),
        "gc": ("robot-vault-gc", "GC_S3_SECRET_KEY"),
    }
    s3_secrets = [root_secret]
    for service_name, (access_key, secret_variable) in runtime_storage.items():
        if str(bootstrap_env.get(f"{service_name.upper()}_S3_ACCESS_KEY") or "") != access_key:
            fail(f"objectstore-access-ready must fix {service_name} access key to {access_key!r}")
        if access_key == root_access_key:
            fail("OBJECTSTORE_ROOT_USER must differ from every service access key")
        secret = _visible_secret(secret_variable, bootstrap_env.get(secret_variable))
        s3_secrets.append(secret)
        if service_name not in services:
            continue
        service_env = _environment(services[service_name], service_name)
        if str(service_env.get("AWS_ACCESS_KEY_ID") or "") != access_key:
            fail(f"{service_name} must receive only its fixed object-store access key")
        if str(service_env.get("AWS_SECRET_ACCESS_KEY") or "") != secret:
            fail(f"{service_name} must receive only {secret_variable}")
        for forbidden in ("OBJECTSTORE_ROOT_USER", "OBJECTSTORE_ROOT_PASSWORD"):
            if forbidden in service_env:
                fail(f"{service_name} must not receive {forbidden}")
    if len(s3_secrets) != len(set(s3_secrets)):
        fail("object-store root and runtime secret keys must all be distinct")
    if set(database_passwords) & set(s3_secrets):
        fail("PostgreSQL and object-store credentials must not be reused across systems")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--edition", choices=("core", "pro"))
    args = parser.parse_args()

    validate_env_file(args.env_file)
    if args.config is not None:
        if args.edition is None:
            fail("--edition is required with --config")
        validate_config(args.config, expected_edition=args.edition)


if __name__ == "__main__":
    main()
