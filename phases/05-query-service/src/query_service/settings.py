"""Environment-derived configuration for the query service.

Everything here is read once at import; the rest of the service reads these
values *through this module* (`settings.S3_PARQUET_BUCKET`, not a
`from .settings import S3_PARQUET_BUCKET` copy) so a test can point the
service at a different bucket/endpoint by patching one place.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import redis
from robot_vault_config.edition import (
    DEFAULT_EDITION as DEFAULT_EDITION,
    EDITIONS as EDITIONS,
    read_edition,
)

from .oidc_verifier import OIDCVerifierConfig

logger = logging.getLogger(__name__)

_STRICT_RELEASE_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


def resolve_service_version(
    release_version: Optional[str],
    *,
    component_version: Optional[str] = None,
) -> str:
    """Return release SemVer, or an explicitly component-scoped dev version."""
    if release_version is not None:
        candidate = release_version.strip()
        if not _STRICT_RELEASE_VERSION.fullmatch(candidate):
            raise ValueError("ROBOT_VAULT_VERSION must be strict SemVer X.Y.Z")
        return candidate

    if component_version is None:
        try:
            component_version = importlib_metadata.version(
                "robot-vault-query-service"
            )
        except importlib_metadata.PackageNotFoundError:
            component_version = "0.1.0"
    return f"{component_version}+query-service.dev"


SERVICE_VERSION = resolve_service_version(os.environ.get("ROBOT_VAULT_VERSION"))

DUCKDB_THREADS = int(os.environ.get("DUCKDB_THREADS", "2"))
DUCKDB_TEMP_DIRECTORY = os.environ.get("DUCKDB_TEMP_DIRECTORY")
DUCKDB_MEMORY_LIMIT_MB = int(os.environ.get("DUCKDB_MEMORY_LIMIT_MB", "512"))
DUCKDB_QUERY_CONCURRENCY = int(os.environ.get("DUCKDB_QUERY_CONCURRENCY", "4"))
DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS = float(
    os.environ.get("DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS", "5")
)
DUCKDB_QUERY_TIMEOUT_SECONDS = float(
    os.environ.get("DUCKDB_QUERY_TIMEOUT_SECONDS", "30")
)
# A caller may still omit time bounds for the small, interactive default view.
# When it supplies both bounds, reject windows wider than this before opening
# DuckDB or touching object storage. Thirty-one days keeps a full calendar
# month expressible without making "one month" depend on which month it is.
QUERY_MAX_SOURCE_RANGE_SECONDS = float(
    os.environ.get("QUERY_MAX_SOURCE_RANGE_SECONDS", str(31 * 24 * 60 * 60))
)
QUERY_MAX_SOURCE_FILES = int(os.environ.get("QUERY_MAX_SOURCE_FILES", "5000"))
QUERY_MAX_SOURCE_ROWS = int(os.environ.get("QUERY_MAX_SOURCE_ROWS", "10000000"))
QUERY_MAX_SOURCE_BYTES = int(
    os.environ.get("QUERY_MAX_SOURCE_BYTES", str(20 * 1024 * 1024 * 1024))
)
SQL_CONSOLE_MEMORY_LIMIT_MB = int(
    os.environ.get("SQL_CONSOLE_MEMORY_LIMIT_MB", "512")
)
if DUCKDB_THREADS <= 0:
    raise ValueError("DUCKDB_THREADS must be a positive integer")
if DUCKDB_MEMORY_LIMIT_MB <= 0:
    raise ValueError("DUCKDB_MEMORY_LIMIT_MB must be a positive integer")
if not 1 <= DUCKDB_QUERY_CONCURRENCY <= 64:
    raise ValueError("DUCKDB_QUERY_CONCURRENCY must be between 1 and 64")
if (
    not math.isfinite(DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS)
    or DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS <= 0
):
    raise ValueError(
        "DUCKDB_QUERY_ACQUIRE_TIMEOUT_SECONDS must be finite positive seconds"
    )
if (
    not math.isfinite(DUCKDB_QUERY_TIMEOUT_SECONDS)
    or DUCKDB_QUERY_TIMEOUT_SECONDS <= 0
):
    raise ValueError("DUCKDB_QUERY_TIMEOUT_SECONDS must be finite positive seconds")
if (
    not math.isfinite(QUERY_MAX_SOURCE_RANGE_SECONDS)
    or QUERY_MAX_SOURCE_RANGE_SECONDS <= 0
):
    raise ValueError("QUERY_MAX_SOURCE_RANGE_SECONDS must be finite positive seconds")
if QUERY_MAX_SOURCE_FILES <= 0:
    raise ValueError("QUERY_MAX_SOURCE_FILES must be a positive integer")
if QUERY_MAX_SOURCE_ROWS <= 0:
    raise ValueError("QUERY_MAX_SOURCE_ROWS must be a positive integer")
if QUERY_MAX_SOURCE_BYTES <= 0:
    raise ValueError("QUERY_MAX_SOURCE_BYTES must be a positive integer")
if SQL_CONSOLE_MEMORY_LIMIT_MB <= 0:
    raise ValueError("SQL_CONSOLE_MEMORY_LIMIT_MB must be a positive integer")

# Optional S3/httpfs (DuckDB) for reading Parquet from the object store
S3_PARQUET_BUCKET = os.environ.get("S3_PARQUET_BUCKET")  # e.g., robotics-data
S3_ENDPOINT = os.environ.get("S3_ENDPOINT")  # e.g., http://objectstore:9000
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
S3_SECURE = os.environ.get("S3_SECURE", "false").lower() == "true"
S3_PREFIX_SCOPE = os.environ.get("S3_PREFIX_SCOPE", "")
if S3_PREFIX_SCOPE and not S3_PREFIX_SCOPE.endswith("/"):
    S3_PREFIX_SCOPE = S3_PREFIX_SCOPE + "/"

# Optional Redis cache
REDIS_URL = os.environ.get("REDIS_URL")
REDIS_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("REDIS_CONNECT_TIMEOUT_SECONDS", "1"))
REDIS_SOCKET_TIMEOUT_SECONDS = float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "1"))
if (
    not math.isfinite(REDIS_CONNECT_TIMEOUT_SECONDS)
    or REDIS_CONNECT_TIMEOUT_SECONDS <= 0
    or not math.isfinite(REDIS_SOCKET_TIMEOUT_SECONDS)
    or REDIS_SOCKET_TIMEOUT_SECONDS <= 0
):
    raise ValueError("Redis connect/socket timeouts must be finite positive seconds")
_redis: Optional[redis.Redis] = None
if REDIS_URL:
    try:
        _redis = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _redis = None
        # Never include the URL or exception text: both can contain Redis
        # credentials. This is a one-time startup signal; runtime failures are
        # counted and rate-limited by the HTTP middleware.
        logger.warning(
            "Configured Redis client could not be initialized (%s); using "
            "in-process rate limiting and disabling Redis response caching",
            type(exc).__name__,
        )

TIME_INDEX_CACHE_SIZE_MB = int(os.environ.get("TIME_INDEX_CACHE_SIZE_MB", "512"))

AUTH_MODE = os.environ.get("AUTH_MODE", "none").lower()  # none|oidc
OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE")
OIDC_JWKS_URL = os.environ.get("OIDC_JWKS_URL")
OIDC_ROLE_CLAIM = os.environ.get("OIDC_ROLE_CLAIM", "roles")
OIDC_DEV = os.environ.get("OIDC_DEV", "false").lower() == "true"
# Public-client settings used by the browser.  These are deliberately
# separate from OIDC_AUDIENCE/JWKS: an API audience is not necessarily an
# OAuth client id, and the browser never needs to know the key-discovery URL.
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
OIDC_AUTHORIZATION_URL = os.environ.get("OIDC_AUTHORIZATION_URL")
OIDC_TOKEN_URL = os.environ.get("OIDC_TOKEN_URL")
OIDC_END_SESSION_URL = os.environ.get("OIDC_END_SESSION_URL")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email offline_access")
# Verification policy is deliberately narrow by default. Operators may opt in
# to another asymmetric algorithm explicitly; symmetric/none algorithms are
# rejected by OIDCVerifierConfig.
OIDC_ALGORITHMS: Tuple[str, ...] = tuple(
    value.strip()
    for value in os.environ.get("OIDC_ALGORITHMS", "RS256").split(",")
    if value.strip()
)
OIDC_ROLE_SOURCE = os.environ.get("OIDC_ROLE_SOURCE", "claim").strip().lower()
OIDC_RESOURCE_ROLE_CLIENT_ID = os.environ.get("OIDC_RESOURCE_ROLE_CLIENT_ID")
OIDC_LEEWAY_SECONDS = int(os.environ.get("OIDC_LEEWAY_SECONDS", "30"))
OIDC_MAX_TOKEN_LIFETIME_SECONDS = int(
    os.environ.get("OIDC_MAX_TOKEN_LIFETIME_SECONDS", "86400")
)
OIDC_JWKS_TIMEOUT_SECONDS = float(os.environ.get("OIDC_JWKS_TIMEOUT_SECONDS", "3"))
OIDC_JWKS_CACHE_SECONDS = float(os.environ.get("OIDC_JWKS_CACHE_SECONDS", "300"))
OIDC_JWKS_STALE_IF_ERROR_SECONDS = float(
    os.environ.get("OIDC_JWKS_STALE_IF_ERROR_SECONDS", "60")
)
OIDC_JWKS_REFRESH_COOLDOWN_SECONDS = float(
    os.environ.get("OIDC_JWKS_REFRESH_COOLDOWN_SECONDS", "15")
)
OIDC_NEGATIVE_KID_SECONDS = float(os.environ.get("OIDC_NEGATIVE_KID_SECONDS", "30"))
OIDC_JWKS_REFRESH_LOCK_TIMEOUT_SECONDS = float(
    os.environ.get("OIDC_JWKS_REFRESH_LOCK_TIMEOUT_SECONDS", "0.1")
)
OIDC_MAX_JWKS_BYTES = int(os.environ.get("OIDC_MAX_JWKS_BYTES", "1048576"))
OIDC_MAX_JWKS_KEYS = int(os.environ.get("OIDC_MAX_JWKS_KEYS", "128"))
RELEASE_MODE = os.environ.get("ROBOT_VAULT_RELEASE", "false").lower() == "true"
ENABLE_HSTS = os.environ.get("ENABLE_HSTS", "false").lower() == "true"
METRICS_AUTH_TOKEN = os.environ.get("METRICS_AUTH_TOKEN")

# StaticFiles(html=True) serves the built callback as a directory index and
# redirects the slashless path. Register the canonical trailing-slash URI with
# the provider so the OAuth code/state survive no redirect contract mismatch.
OIDC_CALLBACK_PATH = "/ui/auth/callback/"
OIDC_POST_LOGOUT_PATH = "/ui/"

# The CA that issued the ingest server certificate, served verbatim by
# `GET /ca.crt`. Robots pin it (the SPKI digest travels inside the enrollment
# code) before they send the code to ingest. The release mounts the same host
# file that ingest uses as `TLS_CA_CERT`.
INGEST_CA_CERT_FILE = os.environ.get(
    "INGEST_CA_CERT_FILE", "/run/robot-vault/tls/ingest-ca.pem"
)

# The robot bootstrap script and the agent tarball, built into the query image
# and served without authentication by `GET /robot` and
# `GET /robot/agent-linux-amd64.tar.gz`.
ROBOT_BOOTSTRAP_DIR = os.environ.get("ROBOT_BOOTSTRAP_DIR", "/opt/robot-vault/robot")

# Provisioning remains on ingest's private admin listener.  This is
# deliberately not a general-purpose outbound URL: allowing a query-side
# environment override to name an arbitrary host would turn the provisioning
# token into a SSRF credential.
INGEST_PROVISIONING_UPSTREAM = os.environ.get(
    "INGEST_PROVISIONING_UPSTREAM", "http://ingest:8080"
)
INGEST_PROVISIONING_TOKEN = os.environ.get("INGEST_PROVISIONING_TOKEN")


def provisioning_upstream_url() -> str:
    """Return the one permitted internal provisioning origin, or refuse it.

    The browser never chooses this destination and neither browser headers nor
    its bearer token cross this boundary.  Keep the validation here so startup
    validation and the request-time fail-closed guard share one definition.
    """
    raw = (INGEST_PROVISIONING_UPSTREAM or "").strip()
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("INGEST_PROVISIONING_UPSTREAM has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "ingest"
        or port != 8080
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "INGEST_PROVISIONING_UPSTREAM must be exactly the private "
            "http://ingest:8080 origin"
        )
    return "http://ingest:8080"


def provisioning_token_configured() -> bool:
    """Whether the query-side copy of the provisioning token is safe to send."""
    token = INGEST_PROVISIONING_TOKEN or ""
    return (
        len(token.encode("utf-8")) >= 32
        and all(0x21 <= ord(char) <= 0x7E for char in token)
    )


def _https_oidc_url(name: str, raw: Optional[str]) -> str:
    """Validate one public real-OIDC URL and return its stripped value."""
    value = (raw or "").strip()
    if not value:
        raise ValueError(f"{name} is required when real OIDC browser login is enabled")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} must not contain control characters")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTPS URL without credentials or a fragment")
    return value


def _url_origin(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    elif host:
        host = host.encode("idna").decode("ascii")
    parsed_port = parsed.port
    port = f":{parsed_port}" if parsed_port is not None and parsed_port != 443 else ""
    return f"{parsed.scheme.lower()}://{host.lower()}{port}"


def browser_oidc_config() -> Dict[str, object]:
    """Return the non-secret browser auth contract, or raise if unsafe.

    The release SPA is a public OAuth client: it has a client id but never a
    client secret.  Keeping this validation in one place also lets the CSP
    and the JSON config endpoint agree on the sole cross-origin host to which
    the browser may send a refresh token.
    """
    if AUTH_MODE not in {"none", "oidc"}:
        raise ValueError(f"AUTH_MODE={AUTH_MODE!r} is invalid; expected 'none' or 'oidc'")
    if AUTH_MODE == "none":
        return {"mode": "none"}
    if OIDC_DEV is True:
        return {"mode": "dev"}

    issuer = _https_oidc_url("OIDC_ISSUER", OIDC_ISSUER)
    if urlsplit(issuer).query:
        raise ValueError("OIDC_ISSUER must not contain a query string")
    authorization_url = _https_oidc_url("OIDC_AUTHORIZATION_URL", OIDC_AUTHORIZATION_URL)
    token_url = _https_oidc_url("OIDC_TOKEN_URL", OIDC_TOKEN_URL)
    end_session_url = (
        _https_oidc_url("OIDC_END_SESSION_URL", OIDC_END_SESSION_URL)
        if (OIDC_END_SESSION_URL or "").strip()
        else None
    )
    client_id = (OIDC_CLIENT_ID or "").strip()
    if not client_id or any(ord(char) < 0x20 or ord(char) == 0x7F for char in client_id):
        raise ValueError("OIDC_CLIENT_ID is required and must not contain control characters")

    scopes: List[str] = []
    for scope in (OIDC_SCOPES or "").split():
        if any(
            ord(char) < 0x21 or ord(char) > 0x7E or char in {'"', "\\"}
            for char in scope
        ):
            raise ValueError("OIDC_SCOPES contains an invalid OAuth scope token")
        if scope and scope not in scopes:
            scopes.append(scope)
    if "openid" not in scopes:
        scopes.insert(0, "openid")

    config: Dict[str, object] = {
        "mode": "oidc",
        "issuer": issuer,
        "client_id": client_id,
        "authorization_url": authorization_url,
        "token_url": token_url,
        "scopes": scopes,
        "callback_path": OIDC_CALLBACK_PATH,
        "post_logout_path": OIDC_POST_LOGOUT_PATH,
    }
    if end_session_url:
        config["end_session_url"] = end_session_url
    return config


def browser_oidc_token_origin() -> Optional[str]:
    """The exact CSP origin allowed for public-client token exchange."""
    config = browser_oidc_config()
    if config.get("mode") != "oidc":
        return None
    return _url_origin(str(config["token_url"]))


def real_oidc_verifier_config() -> OIDCVerifierConfig:
    """Build the immutable trust policy used for real provider tokens."""
    return OIDCVerifierConfig(
        issuer=str(OIDC_ISSUER or ""),
        audience=str(OIDC_AUDIENCE or ""),
        jwks_url=str(OIDC_JWKS_URL or ""),
        algorithms=OIDC_ALGORITHMS,
        leeway_seconds=OIDC_LEEWAY_SECONDS,
        max_token_lifetime_seconds=OIDC_MAX_TOKEN_LIFETIME_SECONDS,
        jwks_timeout_seconds=OIDC_JWKS_TIMEOUT_SECONDS,
        jwks_cache_seconds=OIDC_JWKS_CACHE_SECONDS,
        jwks_stale_if_error_seconds=OIDC_JWKS_STALE_IF_ERROR_SECONDS,
        refresh_cooldown_seconds=OIDC_JWKS_REFRESH_COOLDOWN_SECONDS,
        negative_kid_seconds=OIDC_NEGATIVE_KID_SECONDS,
        refresh_lock_timeout_seconds=OIDC_JWKS_REFRESH_LOCK_TIMEOUT_SECONDS,
        max_jwks_bytes=OIDC_MAX_JWKS_BYTES,
        max_jwks_keys=OIDC_MAX_JWKS_KEYS,
    ).validated()


def validate_auth_configuration() -> None:
    """Refuse startup when authentication would silently fail open or stall."""
    if AUTH_MODE not in {"none", "oidc"}:
        raise ValueError(f"AUTH_MODE={AUTH_MODE!r} is invalid; expected 'none' or 'oidc'")
    if RELEASE_MODE and AUTH_MODE != "oidc":
        raise ValueError("ROBOT_VAULT_RELEASE requires AUTH_MODE=oidc")
    if AUTH_MODE == "none":
        return
    if RELEASE_MODE and OIDC_DEV:
        raise ValueError("ROBOT_VAULT_RELEASE forbids OIDC_DEV")
    if RELEASE_MODE and (
        not METRICS_AUTH_TOKEN
        or len(METRICS_AUTH_TOKEN.encode("utf-8")) < 32
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in METRICS_AUTH_TOKEN)
    ):
        raise ValueError(
            "ROBOT_VAULT_RELEASE requires METRICS_AUTH_TOKEN with at least "
            "32 visible ASCII characters"
        )
    if RELEASE_MODE and not provisioning_token_configured():
        raise ValueError(
            "ROBOT_VAULT_RELEASE requires INGEST_PROVISIONING_TOKEN with at least "
            "32 visible ASCII characters"
        )
    if RELEASE_MODE:
        provisioning_upstream_url()
    if OIDC_ROLE_SOURCE not in {"claim", "resource_access", "realm_access"}:
        raise ValueError(
            "OIDC_ROLE_SOURCE must be claim, resource_access, or realm_access"
        )
    if OIDC_ROLE_SOURCE == "claim" and not (OIDC_ROLE_CLAIM or "").strip():
        raise ValueError("OIDC_ROLE_CLAIM is required for OIDC_ROLE_SOURCE=claim")
    if OIDC_ROLE_SOURCE == "resource_access" and not (
        (OIDC_RESOURCE_ROLE_CLIENT_ID or "").strip() or (OIDC_CLIENT_ID or "").strip()
    ):
        raise ValueError(
            "OIDC_RESOURCE_ROLE_CLIENT_ID or OIDC_CLIENT_ID is required for resource_access roles"
        )
    if OIDC_DEV:
        return
    real_oidc_verifier_config()
    # The shipped application always includes the SPA, so a real provider
    # without its public-client contract is not a usable deployment.
    browser_oidc_config()

PROMETHEUS_BASE = os.environ.get("PROMETHEUS_BASE", "http://prometheus:9090")

# --- MCP server at /mcp (docs/MCP.md) ----------------------------------------
# Read once when the app is built (like the edition): changing one means
# recreating the service. AUTH_MODE stays live; see query_service/mcp.
def _csv(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


MCP_ENABLED = os.environ.get("MCP_ENABLED", "true").lower() == "true"
MCP_WRITE_TOOLS = os.environ.get("MCP_WRITE_TOOLS", "false").lower() == "true"
MCP_SQL_ENABLED = os.environ.get("MCP_SQL_ENABLED", "true").lower() == "true"
MCP_ALLOW_NO_AUTH = os.environ.get("MCP_ALLOW_NO_AUTH", "false").lower() == "true"
MCP_TOOLS_DENY = frozenset(_csv("MCP_TOOLS_DENY"))
MCP_ALLOWED_ORIGINS = _csv("MCP_ALLOWED_ORIGINS")
MCP_INGRESS_RATE = int(os.environ.get("MCP_INGRESS_RATE", "240"))  # per peer per minute
MCP_MAX_RESPONSE_BYTES = int(os.environ.get("MCP_MAX_RESPONSE_BYTES", str(1024 * 1024)))
MCP_MAX_FIELD_BYTES = int(os.environ.get("MCP_MAX_FIELD_BYTES", str(64 * 1024)))
# The URL clients use for this service (scheme://host[:port], no path). It names
# the protected resource, the allowed Host, and absolute download-ticket URLs.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/") or None

# --- Edition -----------------------------------------------------------------
#
# `ROBOT_VAULT_EDITION` selects which product surface a deployment exposes:
#
#   core -- the platform: upload -> integrity -> Parquet -> query/UI.
#   pro  -- all of the above plus episode segmentation, the QA
#           engine, media encoding, dataset cuts and the exporter.
#
# The authoritative gates are (a) this service, which does not import or mount
# the Pro routers in core (`main.create_app` passes `EDITION` to `load_pro`;
# a test builds a core app with `monkeypatch.setattr(settings, "EDITION", ...)`), (b) compose topology, which does not start the exporter in
# core, and (c) the docs. The processor's pro stages are gated by the flags
# they already own (`QA_ENABLED`, `MEDIA_ENCODE_ENABLED`), set consistently
# with this value by compose -- see deploy/docker-compose.release.yml.
#
# Deliberately validated hard rather than defaulted-on-typo: an unrecognised
# value cannot be resolved safely in either direction. Falling back to "pro"
# would ship pro surfaces to a core deployment; falling back to "core" would
# silently delete a Pro deployment's features. A refusal at import is loud, is
# visible in `docker logs` on the first start, and cannot be mistaken for
# working.
EDITION = read_edition()


def s3_enabled() -> bool:
    return bool(S3_PARQUET_BUCKET and S3_ENDPOINT and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY)


def cache_get(key: str) -> Optional[object]:
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None


def cache_set(key: str, value, ttl: int = 30) -> None:
    if not _redis:
        return
    try:
        _redis.setex(key, ttl, json.dumps(value))
    except Exception:
        pass


def parse_iso_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an API timestamp as a UTC instant, rejecting malformed input.

    Offset-less values follow the lake contract and mean UTC; they must never
    be interpreted through the host timezone. A supplied-but-invalid filter is
    a client error, not permission to silently broaden the query.
    """
    if not value:
        return None
    v = value.strip()
    # Normalize common variants: trailing 'Z' (UTC) and optional milliseconds
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(v)
    except (TypeError, ValueError) as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Format an instant as ISO-8601 UTC with a trailing `Z`.

    This is the one formatter for every timestamp the API returns.

    - An aware datetime is converted to UTC first. Postgres returns
      TIMESTAMPTZ values in the session time zone, so a server with
      `TimeZone=Europe/Berlin` gives `+01:00` values. Appending `Z` without
      the conversion produced `...+01:00Z`, which browsers cannot parse.
    - A naive datetime is taken to be UTC. DuckDB sessions run with
      `TimeZone='UTC'` and the lake stores UTC, so a naive value from them is
      already UTC. This matches `parse_iso_ts`, which reads an offset-less
      input as UTC.
    - `None` stays `None`. Any other type is a programming error and raises
      `TypeError`, rather than being passed through as an unparseable string.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"iso_utc expects a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def datetime_to_epoch_ns(value: datetime) -> int:
    """Convert a datetime to exact epoch nanoseconds without a float."""
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    delta = normalized - _UTC_EPOCH
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )
