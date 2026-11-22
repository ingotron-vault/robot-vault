"""OIDC authentication, role gating, audit logging, and dev-token routes.

``AUTH_MODE=none`` (the default) keeps deployments without an identity
provider usable.  When OIDC is enabled, every accepted JWT is signature
verified.  The local development issuer is available only when
``OIDC_DEV=true``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from . import settings
from .oidc_verifier import BoundedOIDCVerifier, OIDCVerificationError, OIDCVerifierConfig

logger = logging.getLogger(__name__)

_role_order = {"viewer": 1, "operator": 2, "admin": 3}

# One verifier per active provider policy. Tests patch settings at runtime, so
# the immutable config itself is the cache key; a changed issuer/audience/key
# policy can never inherit the previous provider's cached keys.
_real_verifier: Optional[BoundedOIDCVerifier] = None
_real_verifier_key: Optional[OIDCVerifierConfig] = None
_real_verifier_lock = threading.Lock()

# The development key is generated lazily.  OIDC_DEV is read at request time
# throughout this module (rather than only at import time), which keeps the
# security switch authoritative and makes configuration tests deterministic.
_DEV_PRIVATE_KEY: Optional[str] = None
_DEV_PUBLIC_KEY: Optional[str] = None
_dev_key_lock = threading.Lock()

_REQUEST_USER_RESOLVED = "_robot_vault_auth_user_resolved"
_REQUEST_USER = "_robot_vault_auth_user"


def _ensure_dev_keypair() -> Tuple[Optional[str], Optional[str]]:
    """Return the process-local development keypair when explicitly enabled."""
    global _DEV_PRIVATE_KEY, _DEV_PUBLIC_KEY

    if settings.OIDC_DEV is not True:
        return None, None
    if _DEV_PRIVATE_KEY is not None and _DEV_PUBLIC_KEY is not None:
        return _DEV_PRIVATE_KEY, _DEV_PUBLIC_KEY

    with _dev_key_lock:
        if _DEV_PRIVATE_KEY is None or _DEV_PUBLIC_KEY is None:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa

            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            _DEV_PRIVATE_KEY = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("utf-8")
            _DEV_PUBLIC_KEY = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")

    return _DEV_PRIVATE_KEY, _DEV_PUBLIC_KEY


def _get_real_verifier() -> Optional[BoundedOIDCVerifier]:
    global _real_verifier, _real_verifier_key

    if settings.AUTH_MODE != "oidc" or settings.OIDC_DEV is True:
        return None
    try:
        config = settings.real_oidc_verifier_config()
    except Exception as exc:
        logger.error("invalid real-OIDC verifier configuration: %s", exc)
        return None
    if _real_verifier is not None and _real_verifier_key == config:
        return _real_verifier
    with _real_verifier_lock:
        if _real_verifier is not None and _real_verifier_key == config:
            return _real_verifier
        _real_verifier = BoundedOIDCVerifier(config)
        _real_verifier_key = config
        return _real_verifier


def warm_real_oidc() -> bool:
    """Initialize bounded JWKS state for readiness; never used for liveness."""
    if settings.AUTH_MODE != "oidc" or settings.OIDC_DEV is True:
        return True
    verifier = _get_real_verifier()
    if verifier is None:
        return False
    try:
        verifier.warm()
        return True
    except OIDCVerificationError as exc:
        logger.warning("real-OIDC readiness failed: %s", exc)
        return False


def _extract_roles(claims: dict) -> List[str]:
    roles: List[str] = []
    source = settings.OIDC_ROLE_SOURCE
    value = None
    if source == "claim":
        value = claims.get(settings.OIDC_ROLE_CLAIM)
    elif source == "resource_access":
        client_id = (
            settings.OIDC_RESOURCE_ROLE_CLIENT_ID or settings.OIDC_CLIENT_ID or ""
        ).strip()
        resource_access = claims.get("resource_access")
        if client_id and isinstance(resource_access, dict):
            entry = resource_access.get(client_id)
            if isinstance(entry, dict):
                value = entry.get("roles")
    elif source == "realm_access":
        realm_access = claims.get("realm_access")
        if isinstance(realm_access, dict):
            value = realm_access.get("roles")

    if isinstance(value, list):
        roles.extend(str(role).strip() for role in value if str(role).strip())
    elif isinstance(value, str):
        roles.extend(role.strip() for role in value.split() if role.strip())
    # Only Robot Vault's three named roles affect authorization. Keeping
    # unrelated provider roles out also bounds audit-log cardinality.
    return sorted({role for role in roles if role in _role_order})


def _verify_jwt(token: str) -> Optional[dict]:
    """Verify a JWT against the explicitly enabled dev key or OIDC JWKS."""
    try:
        header = jwt.get_unverified_header(token)  # type: ignore[attr-defined]
    except Exception:
        return None

    # ``dev-key`` is reserved for the process-local RS256 key.  A failed dev
    # verification is final: it must never fall through to signature-free
    # decoding or to an unrelated upstream provider.
    if isinstance(header, dict) and str(header.get("kid")) == "dev-key":
        if settings.OIDC_DEV is not True:
            return None
        _, public_key = _ensure_dev_keypair()
        if not public_key:
            return None
        try:
            return jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=settings.OIDC_AUDIENCE or "robot-vault",
                issuer=settings.OIDC_ISSUER or "http://robot-vault-query:8003/auth/dev",
                leeway=settings.OIDC_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception:
            return None

    verifier = _get_real_verifier()
    if verifier is None:
        return None
    try:
        return dict(verifier.verify(token))
    except OIDCVerificationError:
        return None


class User:
    def __init__(self, sub: str, roles: List[str]):
        self.sub = sub
        self.roles = roles


def get_current_user_optional(request: Request) -> Optional[User]:
    """Resolve a request's bearer token once and reuse the resulting user."""
    if settings.AUTH_MODE != "oidc":
        return None

    if getattr(request.state, _REQUEST_USER_RESOLVED, False):
        return getattr(request.state, _REQUEST_USER, None)

    user: Optional[User] = None
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        claims = _verify_jwt(token)
        if claims:
            user = User(
                sub=str(claims.get("sub") or ""),
                roles=_extract_roles(claims),
            )

    setattr(request.state, _REQUEST_USER, user)
    setattr(request.state, _REQUEST_USER_RESOLVED, True)
    return user


def require_role(min_role: str):
    async def _dep(request: Request):
        # Only the explicit no-auth development mode bypasses enforcement.
        # Matching on the open mode (not `!= "oidc"`) means any future
        # AUTH_MODE value fails CLOSED here until it is deliberately wired
        # into this dependency, instead of silently disabling auth.
        if settings.AUTH_MODE == "none":
            return None

        # JWKS refresh and crypto are synchronous. Keep both off the ASGI event
        # loop; the verifier bounds refresh concurrency and wait time itself.
        user = await run_in_threadpool(get_current_user_optional, request)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_max = max([_role_order.get(r, 0) for r in user.roles] or [0])
        if user_max < _role_order.get(min_role, 99):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep


def audit(request: Request, action: str, extra: Dict[str, object]):
    """Synchronously emit one audit record.

    Protected routes have already resolved and cached the user in
    ``require_role``, so audit logging does not repeat JWT verification.
    """
    try:
        user = get_current_user_optional(request)
        payload = {
            "ts": int(time.time()),
            "ip": request.client.host if request.client else None,
            "user": getattr(user, "sub", None),
            "roles": getattr(user, "roles", None),
            "action": action,
            "path": request.url.path,
            "extra": extra,
        }
        print(json.dumps({"audit": payload}), flush=True)
    except Exception as exc:
        # Audit delivery remains non-blocking for availability, but failure
        # must be visible to operators rather than silently erasing evidence.
        logger.warning(
            "Could not emit audit event for %s (%s)",
            action,
            type(exc).__name__,
        )


router = APIRouter()


@router.get("/api/v1/auth/config")
def browser_auth_config():
    """Publish only the non-secret settings needed by the shipped SPA.

    This route is intentionally open: a user cannot authenticate until the
    browser knows where to start the Authorization Code + PKCE flow.  Client
    secrets, API audience and JWKS verification details are never included.
    """
    try:
        config = settings.browser_oidc_config()
    except ValueError as exc:
        logger.error("invalid browser OIDC configuration: %s", exc)
        return JSONResponse(
            {"detail": "OIDC browser login is not configured"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(config, headers={"Cache-Control": "no-store"})


@router.get("/auth/dev/jwks")
def dev_jwks():
    if settings.OIDC_DEV is not True:
        return PlainTextResponse("not_enabled", status_code=404)
    _, public_key = _ensure_dev_keypair()
    if not public_key:
        return JSONResponse({"detail": "development key unavailable"}, status_code=500)
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(load_pem_public_key(public_key.encode("utf-8"))))
    jwk.update({"kid": "dev-key", "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


@router.get("/auth/dev/token")
def dev_token(roles: str = "admin"):
    if settings.OIDC_DEV is not True:
        return PlainTextResponse("not_enabled", status_code=404)
    try:
        private_key, _ = _ensure_dev_keypair()
        if not private_key:
            raise RuntimeError("development signing key is unavailable")
        now = int(time.time())
        claims = {
            "sub": "dev-user",
            "iss": settings.OIDC_ISSUER or "http://robot-vault-query:8003/auth/dev",
            "aud": settings.OIDC_AUDIENCE or "robot-vault",
            "iat": now,
            "exp": now + 3600,
            settings.OIDC_ROLE_CLAIM: [r for r in roles.split(",") if r],
        }
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "dev-key"})
        return {"access_token": token}
    except Exception:
        logger.exception("failed to issue development token")
        return JSONResponse({"detail": "development token unavailable"}, status_code=500)


@router.get("/api/dev/me")
def dev_me(request: Request):
    if settings.AUTH_MODE != "oidc":
        return {"auth_mode": settings.AUTH_MODE}
    user = get_current_user_optional(request)
    if not user:
        return PlainTextResponse("Unauthorized", status_code=401)
    return {"sub": user.sub, "roles": user.roles}
