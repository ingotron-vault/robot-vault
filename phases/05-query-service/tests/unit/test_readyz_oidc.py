"""`/readyz` observes the operator's OIDC provider.

The real verifier, the real `/readyz` and a real admin route run here; only
the network to the IdP (httpx.Client inside oidc_verifier) and the verifier's
monotonic clock are fakes.

Ways this could go wrong, each covered below:
- a provider that cannot be reached at cold start reports ready;
- a malformed key set reports ready;
- an outage hides behind the cache forever (readiness never drops);
- signed-in users are cut off at once during an outage (no stale grace);
- stale keys are accepted past the grace window;
- readiness does not recover when the provider returns;
- AUTH_MODE=none or OIDC_DEV fetches a key set anyway.
"""
from __future__ import annotations

import importlib
import json
import time
import types

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from query_service import auth, oidc_verifier, settings

ISSUER = "https://idp.example/realms/robots"
AUDIENCE = "robot-vault"
JWKS_URL = "https://idp.example/realms/robots/keys"
CACHE_SECONDS = 300.0
STALE_SECONDS = 60.0


def _import_query_service_main():
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
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_JWK = {**json.loads(RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}


class FakeIdP:
    """The provider's JWKS endpoint as seen through httpx.Client."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.fetches = 0

    def client(self, **_kwargs):
        idp = self

        class _Response:
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                if idp.mode == "malformed":
                    yield b'{"keys": [{"kid": "k1", "kty": "RSA", "alg": "RS256", "n": "!!", "e": "AQAB"}]}'
                else:
                    yield json.dumps({"keys": [PUBLIC_JWK]}).encode()

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, method, url, headers):
                assert (method, url) == ("GET", JWKS_URL)
                idp.fetches += 1
                if idp.mode == "down":
                    raise httpx.ConnectError("connection refused")
                if idp.mode == "forbidden":
                    raise AssertionError("this mode must not contact the IdP")
                return _Response()

        return _Client()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def idp(monkeypatch, tmp_path) -> FakeIdP:
    fake = FakeIdP()
    monkeypatch.setattr(oidc_verifier.httpx, "Client", fake.client)
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", False)
    monkeypatch.setattr(settings, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(settings, "OIDC_JWKS_URL", JWKS_URL)
    monkeypatch.setattr(settings, "OIDC_ROLE_SOURCE", "claim")
    monkeypatch.setattr(settings, "OIDC_ROLE_CLAIM", "roles")
    monkeypatch.setattr(settings, "OIDC_JWKS_CACHE_SECONDS", CACHE_SECONDS)
    monkeypatch.setattr(settings, "OIDC_JWKS_STALE_IF_ERROR_SECONDS", STALE_SECONDS)
    monkeypatch.setattr(settings, "ROBOT_BOOTSTRAP_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "_real_verifier", None)
    monkeypatch.setattr(auth, "_real_verifier_key", None)
    monkeypatch.setattr(query_main, "_postgres_is_ready", lambda: True)
    monkeypatch.setattr(query_main.app.state, "pro", None)
    return fake


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(oidc_verifier, "time", types.SimpleNamespace(monotonic=fake.monotonic, time=time.time))
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(query_main.app, raise_server_exceptions=False)


def _admin_token() -> str:
    now = int(time.time())
    claims = {"sub": "admin-1", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 3000, "roles": ["admin"]}
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "k1"})


def _admin_status(client: TestClient) -> int:
    return client.get(
        "/api/admin/robots/bootstrap", headers={"Authorization": f"Bearer {_admin_token()}"}
    ).status_code


def _ready(client: TestClient):
    response = client.get("/readyz")
    return response.status_code, response.json()["checks"]["oidc"]


def test_unreachable_provider_at_cold_start_is_not_ready(idp, clock, client):
    idp.mode = "down"
    assert _ready(client) == (503, False)
    assert idp.fetches == 1


def test_malformed_key_set_is_not_ready(idp, clock, client):
    idp.mode = "malformed"
    assert _ready(client) == (503, False)


def test_valid_key_set_is_ready_and_admin_token_works(idp, clock, client):
    assert _ready(client) == (200, True)
    assert _admin_status(client) == 200


def test_outage_drops_readiness_after_cache_and_tokens_after_grace_then_recovers(idp, clock, client):
    assert _ready(client) == (200, True)
    idp.mode = "down"

    clock.now += CACHE_SECONDS - 1  # inside the cache: nothing to observe yet
    assert _ready(client) == (200, True)
    assert _admin_status(client) == 200

    clock.now += 2  # cache expired: readiness needs a fresh fetch
    assert _ready(client) == (503, False)
    assert _admin_status(client) == 200, "a known key must keep working during the stale grace"

    clock.now += STALE_SECONDS  # past cache + grace
    assert _admin_status(client) == 401, "stale keys must not be used past the grace window"

    idp.mode = "ok"
    assert _ready(client) == (200, True)
    assert _admin_status(client) == 200


@pytest.mark.parametrize("mode, dev", [("none", False), ("oidc", True)])
def test_modes_without_a_real_provider_never_fetch(idp, clock, client, monkeypatch, mode, dev):
    idp.mode = "forbidden"
    monkeypatch.setattr(settings, "AUTH_MODE", mode)
    monkeypatch.setattr(settings, "OIDC_DEV", dev)
    assert _ready(client) == (200, True)
    assert idp.fetches == 0
