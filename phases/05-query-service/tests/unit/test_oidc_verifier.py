"""Adversarial tests for the bounded real-OIDC verifier."""
from __future__ import annotations

import json
import threading
import time
from typing import Dict

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from query_service.oidc_verifier import (
    BoundedOIDCVerifier,
    OIDCConfigurationError,
    OIDCVerificationError,
    OIDCVerifierConfig,
)


ISSUER = "https://identity.example/realms/robot-vault"
AUDIENCE = "robot-vault-api"
JWKS_URL = "https://identity.example/realms/robot-vault/keys"


def config(**overrides) -> OIDCVerifierConfig:
    values = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": JWKS_URL,
        "refresh_cooldown_seconds": 0.01,
        "negative_kid_seconds": 0.05,
    }
    values.update(overrides)
    return OIDCVerifierConfig(**values)


def keypair(kid: str = "key-1"):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, public_jwk


def token(private, *, kid: str = "key-1", **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": "operator-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "roles": ["operator"],
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


def install_keys(monkeypatch, verifier: BoundedOIDCVerifier, *documents: Dict):
    calls = []

    def fetch():
        index = min(len(calls), len(documents) - 1)
        calls.append(index)
        return documents[index]

    monkeypatch.setattr(verifier, "_fetch_keys", fetch)
    return calls


def mapping(jwk: Dict):
    return {(str(jwk["kid"]), str(jwk["alg"])): jwk}


def test_real_oidc_configuration_requires_https():
    with pytest.raises(OIDCConfigurationError, match="HTTPS"):
        config(jwks_url="http://identity.example/keys").validated()
    with pytest.raises(OIDCConfigurationError, match="HTTPS"):
        config(issuer="http://identity.example").validated()


def test_valid_rsa_token_and_mandatory_claims(monkeypatch):
    private, jwk = keypair()
    verifier = BoundedOIDCVerifier(config())
    install_keys(monkeypatch, verifier, mapping(jwk))

    assert verifier.verify(token(private))["sub"] == "operator-1"
    for missing in ("exp", "iat", "iss", "aud", "sub"):
        now = int(time.time())
        claims = {
            "sub": "operator-1",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
        }
        claims.pop(missing)
        malformed = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "key-1"})
        with pytest.raises(OIDCVerificationError):
            verifier.verify(malformed)


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.example"},
        {"aud": "another-api"},
        {"sub": ""},
        {"iat": int(time.time()) - 200_000, "exp": int(time.time()) + 300},
    ],
)
def test_claim_boundary_is_exact(monkeypatch, overrides):
    private, jwk = keypair()
    verifier = BoundedOIDCVerifier(config())
    install_keys(monkeypatch, verifier, mapping(jwk))

    with pytest.raises(OIDCVerificationError):
        verifier.verify(token(private, **overrides))


def test_algorithm_and_jwk_declaration_must_agree(monkeypatch):
    private, jwk = keypair()
    jwk["alg"] = "RS512"
    verifier = BoundedOIDCVerifier(config(algorithms=("RS256", "RS512")))
    # Deliberately index the provider response under the requested algorithm
    # to exercise the verifier's independent raw-JWK declaration check.
    install_keys(monkeypatch, verifier, {("key-1", "RS256"): jwk})

    with pytest.raises(OIDCVerificationError):
        verifier.verify(token(private))


def test_same_kid_rotation_refreshes_once(monkeypatch):
    old_private, old_jwk = keypair("stable-kid")
    new_private, new_jwk = keypair("stable-kid")
    verifier = BoundedOIDCVerifier(config(refresh_cooldown_seconds=0.001))
    calls = install_keys(monkeypatch, verifier, mapping(old_jwk), mapping(new_jwk))
    verifier.warm()

    assert verifier.verify(token(new_private, kid="stable-kid"))["sub"] == "operator-1"
    assert len(calls) == 2
    # The old key and variable exist to make it explicit this is same-kid key
    # material rotation, rather than merely adding a second kid.
    assert old_private.private_numbers() != new_private.private_numbers()


def test_unknown_kids_do_not_stampede_the_provider(monkeypatch):
    private, jwk = keypair("known")
    verifier = BoundedOIDCVerifier(config(refresh_cooldown_seconds=60))
    calls = install_keys(monkeypatch, verifier, mapping(jwk), mapping(jwk))
    verifier.warm()

    for index in range(25):
        with pytest.raises(OIDCVerificationError):
            verifier.verify(token(private, kid=f"unknown-{index}"))
    # One initial warm plus at most one forced refresh for the entire cooldown,
    # not one outbound request per attacker-controlled kid.
    assert len(calls) <= 2


def test_cold_refresh_is_single_flight_and_waiters_fail_fast(monkeypatch):
    _, jwk = keypair()
    verifier = BoundedOIDCVerifier(config(refresh_lock_timeout_seconds=0.01))
    entered = threading.Event()
    release = threading.Event()

    def slow_fetch():
        entered.set()
        assert release.wait(1)
        return mapping(jwk)

    monkeypatch.setattr(verifier, "_fetch_keys", slow_fetch)
    errors = []

    thread = threading.Thread(target=lambda: verifier.warm(), daemon=True)
    thread.start()
    assert entered.wait(1)
    with pytest.raises(OIDCVerificationError, match="already in progress"):
        verifier.warm()
    release.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert errors == []


def test_jwks_response_size_is_bounded(monkeypatch):
    verifier = BoundedOIDCVerifier(config(max_jwks_bytes=16))

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b'{"keys":['
            yield b'"far too large"]}'

    class Client:
        def __init__(self, *, timeout, follow_redirects):
            assert follow_redirects is False
            assert timeout is not None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("query_service.oidc_verifier.httpx.Client", Client)
    with pytest.raises(OIDCVerificationError, match="too large"):
        verifier.warm()


def test_readiness_rejects_structurally_malformed_signing_material(monkeypatch):
    verifier = BoundedOIDCVerifier(config())
    document = json.dumps({
        "keys": [{
            "kid": "broken-rsa",
            "alg": "RS256",
            "kty": "RSA",
            "use": "sig",
            "e": "AQAB",
            "n": "not-valid-base64url!",
        }],
    }).encode()

    class Response:
        headers = {"Content-Length": str(len(document))}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield document

    class Client:
        def __init__(self, *, timeout, follow_redirects):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("query_service.oidc_verifier.httpx.Client", Client)
    with pytest.raises(OIDCVerificationError, match="malformed"):
        verifier.warm()
