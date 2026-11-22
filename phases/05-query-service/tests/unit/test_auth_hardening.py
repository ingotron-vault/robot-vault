"""Focused regressions for OIDC signature verification and caching."""
from __future__ import annotations

import asyncio
import importlib
import sys
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from query_service import auth, settings
from query_service.oidc_verifier import OIDCVerifierConfig


def _import_query_service_main():
    """Import the app without requiring Docker's built SPA directory."""
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]

    import fastapi.staticfiles as fastapi_staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    original = fastapi_staticfiles.StaticFiles
    fastapi_staticfiles.StaticFiles = _StubStaticFiles
    try:
        return importlib.import_module("query_service.main")
    finally:
        fastapi_staticfiles.StaticFiles = original


query_main = _import_query_service_main()


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", False)
    monkeypatch.setattr(settings, "OIDC_ISSUER", None)
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", None)
    monkeypatch.setattr(settings, "OIDC_JWKS_URL", None)
    monkeypatch.setattr(settings, "OIDC_ROLE_SOURCE", "claim")
    monkeypatch.setattr(auth, "_real_verifier", None)
    monkeypatch.setattr(auth, "_real_verifier_key", None)


def _request(token: str = "token") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/protected",
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
        }
    )


def test_forged_hs256_dev_key_token_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "attacker",
            "iss": "http://robot-vault-query:8003/auth/dev",
            "aud": "robot-vault",
            "iat": now,
            "exp": now + 3600,
            settings.OIDC_ROLE_CLAIM: ["admin"],
        },
        "attacker-controlled-signing-key-32bytes",
        algorithm="HS256",
        headers={"kid": "dev-key"},
    )

    assert auth._verify_jwt(forged) is None


def test_valid_dev_rs256_token_works_only_when_dev_mode_is_enabled(monkeypatch):
    monkeypatch.setattr(settings, "OIDC_DEV", True)

    issued = auth.dev_token(roles="viewer")
    assert isinstance(issued, dict) and "access_token" in issued
    token = issued["access_token"]
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert auth._verify_jwt(token)[settings.OIDC_ROLE_CLAIM] == ["viewer"]

    monkeypatch.setattr(settings, "OIDC_DEV", False)
    assert auth._verify_jwt(token) is None
    assert auth.dev_token().status_code == 404


def test_real_oidc_verifier_is_cached_across_verifications(monkeypatch):
    verifier_config = OIDCVerifierConfig(
        issuer="https://issuer.example",
        audience="robot-vault",
        jwks_url="https://issuer.example/keys",
    )
    calls = {"clients": 0, "verifications": 0}

    class FakeVerifier:
        def __init__(self, config):
            calls["clients"] += 1
            assert config == verifier_config

        def verify(self, token):
            calls["verifications"] += 1
            return {"sub": "oidc-user"}

    token = jwt.encode(
        {"sub": "oidc-user"},
        "provider-test-signing-secret-32bytes",
        algorithm="HS256",
        headers={"kid": "provider-key"},
    )
    monkeypatch.setattr(settings, "real_oidc_verifier_config", lambda: verifier_config)
    monkeypatch.setattr(auth, "BoundedOIDCVerifier", FakeVerifier)

    assert auth._verify_jwt(token) == {"sub": "oidc-user"}
    assert auth._verify_jwt(token) == {"sub": "oidc-user"}
    assert calls == {"clients": 1, "verifications": 2}


def test_request_user_is_verified_once(monkeypatch):
    calls = 0

    def fake_verify(token):
        nonlocal calls
        calls += 1
        return {"sub": "cached-user", settings.OIDC_ROLE_CLAIM: ["viewer"]}

    monkeypatch.setattr(auth, "_verify_jwt", fake_verify)
    request = _request()

    first = auth.get_current_user_optional(request)
    second = auth.get_current_user_optional(request)

    assert first is second
    assert first.sub == "cached-user"
    assert calls == 1


def test_require_role_offloads_synchronous_verification(monkeypatch):
    calls = []
    expected = auth.User("thread-user", ["viewer"])

    async def fake_run_in_threadpool(func, *args):
        calls.append((func, args))
        return expected

    monkeypatch.setattr(auth, "run_in_threadpool", fake_run_in_threadpool)
    result = asyncio.run(auth.require_role("viewer")(_request()))

    assert result is expected
    assert len(calls) == 1
    assert calls[0][0] is auth.get_current_user_optional
    assert calls[0][1][0].url.path == "/protected"


def test_invalid_auth_mode_is_rejected_instead_of_disabling_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "odic")
    monkeypatch.setattr(settings, "RELEASE_MODE", False)
    with pytest.raises(ValueError, match="AUTH_MODE"):
        settings.validate_auth_configuration()


def test_release_marker_forbids_disabled_or_development_auth(monkeypatch):
    monkeypatch.setattr(settings, "RELEASE_MODE", True)
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    with pytest.raises(ValueError, match="requires AUTH_MODE=oidc"):
        settings.validate_auth_configuration()

    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    with pytest.raises(ValueError, match="forbids OIDC_DEV"):
        settings.validate_auth_configuration()


def test_release_requires_a_strong_metrics_scrape_token(monkeypatch):
    monkeypatch.setattr(settings, "RELEASE_MODE", True)
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", False)
    monkeypatch.setattr(settings, "METRICS_AUTH_TOKEN", "short")
    with pytest.raises(ValueError, match="METRICS_AUTH_TOKEN"):
        settings.validate_auth_configuration()


def test_only_configured_role_source_is_trusted(monkeypatch):
    claims = {
        "roles": ["viewer"],
        "realm_access": {"roles": ["admin"]},
        "resource_access": {"robot-vault-spa": {"roles": ["operator"]}},
    }
    monkeypatch.setattr(settings, "OIDC_ROLE_SOURCE", "claim")
    assert auth._extract_roles(claims) == ["viewer"]

    monkeypatch.setattr(settings, "OIDC_ROLE_SOURCE", "resource_access")
    monkeypatch.setattr(settings, "OIDC_RESOURCE_ROLE_CLIENT_ID", "robot-vault-spa")
    assert auth._extract_roles(claims) == ["operator"]


def test_readiness_combines_oidc_and_postgres_observations(monkeypatch):
    client = TestClient(query_main.app)
    monkeypatch.setattr(query_main, "_postgres_is_ready", lambda: True)
    # Core readiness; the Pro repository tests the Pro signing check.
    monkeypatch.setattr(query_main.app.state, "pro", None)

    monkeypatch.setattr(query_main.auth_mod, "warm_real_oidc", lambda: False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "oidc": False,
            "postgres": True,
            "dataset_signing": True,
        },
    }

    monkeypatch.setattr(query_main.auth_mod, "warm_real_oidc", lambda: True)
    assert client.get("/readyz").json() == {
        "status": "ready",
        "checks": {
            "oidc": True,
            "postgres": True,
            "dataset_signing": True,
        },
    }

    monkeypatch.setattr(query_main, "_postgres_is_ready", lambda: False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {
            "oidc": True,
            "postgres": False,
            "dataset_signing": True,
        },
    }


def test_postgres_readiness_is_bounded_and_release_requires_a_dsn(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "RELEASE_MODE", False)
    assert query_main._postgres_is_ready() is True
    monkeypatch.setattr(settings, "RELEASE_MODE", True)
    assert query_main._postgres_is_ready() is False

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            assert statement == "SELECT 1"

        def fetchone(self):
            return (1,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    def connect(dsn, *, connect_timeout, options):
        calls.append((dsn, connect_timeout, options))
        return Connection()

    monkeypatch.setenv("DATABASE_URL", "postgresql://release.invalid/vault")
    monkeypatch.setattr(query_main.psycopg, "connect", connect)
    assert query_main._postgres_is_ready() is True
    assert calls == [
        ("postgresql://release.invalid/vault", 3, "-c statement_timeout=3000")
    ]

    monkeypatch.setattr(
        query_main.psycopg,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database down")),
    )
    assert query_main._postgres_is_ready() is False
