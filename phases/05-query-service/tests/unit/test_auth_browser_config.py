"""Browser-facing OIDC config and CSP regressions for the query SPA."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles

from query_service import auth, middleware, settings


@pytest.fixture(autouse=True)
def browser_auth_defaults(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "none")
    monkeypatch.setattr(settings, "OIDC_DEV", False)
    monkeypatch.setattr(settings, "OIDC_ISSUER", None)
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", None)
    monkeypatch.setattr(settings, "OIDC_AUTHORIZATION_URL", None)
    monkeypatch.setattr(settings, "OIDC_TOKEN_URL", None)
    monkeypatch.setattr(settings, "OIDC_END_SESSION_URL", None)
    monkeypatch.setattr(settings, "OIDC_SCOPES", "openid profile email offline_access")


def _client(*, with_middleware: bool = False) -> TestClient:
    app = FastAPI()
    if with_middleware:
        middleware.install(app)
    app.include_router(auth.router)

    @app.get("/ui/test")
    def ui_test():
        return {"ok": True}

    return TestClient(app)


def test_callback_uri_is_the_canonical_built_static_directory(tmp_path):
    callback = tmp_path / "auth" / "callback"
    callback.mkdir(parents=True)
    (callback / "index.html").write_text("callback", encoding="utf-8")
    app = FastAPI()
    app.mount("/ui", StaticFiles(directory=str(tmp_path), html=True), name="ui")
    client = TestClient(app)

    canonical = client.get(settings.OIDC_CALLBACK_PATH, follow_redirects=False)
    assert canonical.status_code == 200
    assert canonical.text == "callback"

    slashless = client.get(settings.OIDC_CALLBACK_PATH.rstrip("/"), follow_redirects=False)
    assert slashless.status_code in {307, 308}
    assert slashless.headers["location"].endswith(settings.OIDC_CALLBACK_PATH)


def _configure_real_oidc(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_ISSUER", "https://idp.example/realms/vault")
    monkeypatch.setattr(settings, "OIDC_CLIENT_ID", "robot-vault-spa")
    monkeypatch.setattr(
        settings,
        "OIDC_AUTHORIZATION_URL",
        "https://idp.example/realms/vault/protocol/openid-connect/auth",
    )
    monkeypatch.setattr(
        settings,
        "OIDC_TOKEN_URL",
        "https://idp.example/realms/vault/protocol/openid-connect/token",
    )


def test_config_reports_none_unless_backend_explicitly_enables_dev(monkeypatch):
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    assert _client().get("/api/v1/auth/config").json() == {"mode": "none"}

    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    response = _client().get("/api/v1/auth/config")
    assert response.status_code == 200
    assert response.json() == {"mode": "dev"}
    assert response.headers["cache-control"] == "no-store"


def test_real_config_exposes_only_public_browser_inputs(monkeypatch):
    _configure_real_oidc(monkeypatch)
    monkeypatch.setattr(
        settings,
        "OIDC_END_SESSION_URL",
        "https://idp.example/realms/vault/protocol/openid-connect/logout",
    )
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", "private-api-audience")
    monkeypatch.setattr(settings, "OIDC_JWKS_URL", "https://keys.example/private/jwks")

    response = _client().get("/api/v1/auth/config")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
        "mode": "oidc",
        "issuer": "https://idp.example/realms/vault",
        "client_id": "robot-vault-spa",
        "authorization_url": "https://idp.example/realms/vault/protocol/openid-connect/auth",
        "token_url": "https://idp.example/realms/vault/protocol/openid-connect/token",
        "end_session_url": "https://idp.example/realms/vault/protocol/openid-connect/logout",
        "scopes": ["openid", "profile", "email", "offline_access"],
        "callback_path": "/ui/auth/callback/",
        "post_logout_path": "/ui/",
    }
    serialized = response.text.lower()
    assert "audience" not in serialized
    assert "jwks" not in serialized
    assert "secret" not in serialized


@pytest.mark.parametrize(
    "missing",
    ["OIDC_CLIENT_ID", "OIDC_AUTHORIZATION_URL", "OIDC_TOKEN_URL"],
)
def test_real_config_fails_closed_when_required_browser_input_is_missing(monkeypatch, missing):
    _configure_real_oidc(monkeypatch)
    monkeypatch.setattr(settings, missing, None)
    response = _client().get("/api/v1/auth/config")
    assert response.status_code == 503
    assert response.json() == {"detail": "OIDC browser login is not configured"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OIDC_ISSUER", "http://idp.example/realms/vault"),
        ("OIDC_TOKEN_URL", "http://idp.example/token"),
        ("OIDC_AUTHORIZATION_URL", "https://idp.example@evil.example/auth"),
        ("OIDC_AUTHORIZATION_URL", "https://idp.example/auth\nX-Evil: yes"),
        ("OIDC_SCOPES", 'openid "broken'),
    ],
)
def test_real_config_rejects_invalid_browser_values(monkeypatch, name, value):
    _configure_real_oidc(monkeypatch)
    monkeypatch.setattr(settings, name, value)
    assert _client().get("/api/v1/auth/config").status_code == 503


def test_real_config_accepts_explicit_https_endpoints_on_provider_specific_origins(monkeypatch):
    _configure_real_oidc(monkeypatch)
    monkeypatch.setattr(settings, "OIDC_AUTHORIZATION_URL", "https://login.example/authorize")
    monkeypatch.setattr(settings, "OIDC_TOKEN_URL", "https://oauth.example/token")
    monkeypatch.setattr(settings, "OIDC_END_SESSION_URL", "https://login.example/logout")
    response = _client().get("/api/v1/auth/config")
    assert response.status_code == 200
    assert response.json()["token_url"] == "https://oauth.example/token"


def test_csp_allows_only_self_and_the_validated_token_origin(monkeypatch):
    _configure_real_oidc(monkeypatch)
    response = _client(with_middleware=True).get("/ui/test")
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    connect = next(part.strip() for part in csp.split(";") if part.strip().startswith("connect-src"))
    assert connect == "connect-src 'self' https://idp.example"
    assert "*" not in connect

    monkeypatch.setattr(settings, "OIDC_TOKEN_URL", "https://tokens.example/token")
    split_origin_response = _client(with_middleware=True).get("/ui/test")
    split_origin_connect = next(
        part.strip()
        for part in split_origin_response.headers["content-security-policy"].split(";")
        if part.strip().startswith("connect-src")
    )
    assert split_origin_connect == "connect-src 'self' https://tokens.example"

    monkeypatch.setattr(settings, "OIDC_TOKEN_URL", None)
    incomplete_response = _client(with_middleware=True).get("/ui/test")
    incomplete_connect = next(
        part.strip()
        for part in incomplete_response.headers["content-security-policy"].split(";")
        if part.strip().startswith("connect-src")
    )
    assert incomplete_connect == "connect-src 'self'"
