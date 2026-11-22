"""Bounded, rotation-aware verification for real OIDC access tokens.

PyJWT's ``PyJWKClient`` is intentionally not used here.  In the versions the
query image supports it performs blocking, unbounded JWKS reads and lets an
attacker force repeated refreshes with random ``kid`` values.  This module
keeps the network boundary explicit: short timeouts, a size/key cap, one
refresh at a time, a negative-key cache, and an exact algorithm policy.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWK
from jwt.exceptions import InvalidSignatureError


class OIDCConfigurationError(ValueError):
    """The configured real-OIDC trust boundary is incomplete or unsafe."""


class OIDCVerificationError(ValueError):
    """A token or provider response could not be verified safely."""


def _expected_kty(algorithm: str) -> str:
    return "RSA" if algorithm.startswith("RS") else "EC" if algorithm.startswith("ES") else "OKP"


def _algorithm_matches_jwk(algorithm: str, raw: Mapping[str, Any]) -> bool:
    if raw.get("kty") != _expected_kty(algorithm):
        return False
    if algorithm.startswith("ES"):
        expected_curve = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}[algorithm]
        return raw.get("crv") == expected_curve
    if algorithm == "EdDSA":
        return raw.get("crv") in {"Ed25519", "Ed448"}
    return True


def validated_https_url(name: str, raw: Optional[str], *, issuer: bool = False) -> str:
    """Return a normalized-enough HTTPS URL or reject it.

    Paths and query strings are preserved for provider endpoints.  Issuer
    identifiers may contain a path but not a query, matching their use as an
    exact JWT ``iss`` value and as an OIDC discovery base.
    """
    value = (raw or "").strip()
    if not value:
        raise OIDCConfigurationError(f"{name} is required")
    try:
        parsed = urlsplit(value)
        port = parsed.port  # force malformed ports to raise
    except ValueError as exc:
        raise OIDCConfigurationError(f"{name} is not a valid URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (issuer and parsed.query)
        or port is not None and not (1 <= port <= 65535)
    ):
        suffix = " without credentials, fragments, or a query" if issuer else " without credentials or fragments"
        raise OIDCConfigurationError(f"{name} must be an HTTPS URL{suffix}")
    return value


@dataclass(frozen=True)
class OIDCVerifierConfig:
    issuer: str
    audience: str
    jwks_url: str
    algorithms: Tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 30
    max_token_lifetime_seconds: int = 86_400
    jwks_timeout_seconds: float = 3.0
    jwks_cache_seconds: float = 300.0
    jwks_stale_if_error_seconds: float = 60.0
    refresh_cooldown_seconds: float = 15.0
    negative_kid_seconds: float = 30.0
    refresh_lock_timeout_seconds: float = 0.1
    max_jwks_bytes: int = 1_048_576
    max_jwks_keys: int = 128

    def validated(self) -> "OIDCVerifierConfig":
        validated_https_url("OIDC_ISSUER", self.issuer, issuer=True)
        validated_https_url("OIDC_JWKS_URL", self.jwks_url)
        if not self.audience or any(ord(char) < 0x20 for char in self.audience):
            raise OIDCConfigurationError("OIDC_AUDIENCE is required and must not contain control characters")
        if not self.algorithms or len(set(self.algorithms)) != len(self.algorithms):
            raise OIDCConfigurationError("OIDC_ALGORITHMS must be a non-empty, duplicate-free allowlist")
        supported = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
        if any(algorithm not in supported for algorithm in self.algorithms):
            raise OIDCConfigurationError(
                "OIDC_ALGORITHMS contains an unsupported value; symmetric and 'none' algorithms are forbidden"
            )
        numeric_values = {
            "OIDC_LEEWAY_SECONDS": self.leeway_seconds,
            "OIDC_MAX_TOKEN_LIFETIME_SECONDS": self.max_token_lifetime_seconds,
            "OIDC_JWKS_TIMEOUT_SECONDS": self.jwks_timeout_seconds,
            "OIDC_JWKS_CACHE_SECONDS": self.jwks_cache_seconds,
            "OIDC_JWKS_STALE_IF_ERROR_SECONDS": self.jwks_stale_if_error_seconds,
            "OIDC_JWKS_REFRESH_COOLDOWN_SECONDS": self.refresh_cooldown_seconds,
            "OIDC_NEGATIVE_KID_SECONDS": self.negative_kid_seconds,
            "OIDC_JWKS_REFRESH_LOCK_TIMEOUT_SECONDS": self.refresh_lock_timeout_seconds,
            "OIDC_MAX_JWKS_BYTES": self.max_jwks_bytes,
            "OIDC_MAX_JWKS_KEYS": self.max_jwks_keys,
        }
        for name, value in numeric_values.items():
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise OIDCConfigurationError(f"{name} must be a finite positive number")
        return self


@dataclass(frozen=True)
class _CachedKeys:
    fetched_at: float
    # A JWK may omit ``alg``.  In that case the second tuple item is None and
    # the header's allowed algorithm is supplied explicitly to PyJWK.
    keys: Mapping[Tuple[str, Optional[str]], Mapping[str, Any]]


class BoundedOIDCVerifier:
    """Verify one provider's tokens with bounded, single-flight JWKS I/O."""

    def __init__(self, config: OIDCVerifierConfig):
        self.config = config.validated()
        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._cache: Optional[_CachedKeys] = None
        self._negative_kids: Dict[str, float] = {}
        self._last_forced_refresh = 0.0

    def warm(self) -> None:
        """Fetch and validate the provider key set for startup/readiness."""
        self._refresh(force=False, require_fresh=True)

    def verify(self, token: str) -> Mapping[str, Any]:
        """Return verified claims or raise ``OIDCVerificationError``."""
        if not isinstance(token, str) or not token or len(token) > 64 * 1024:
            raise OIDCVerificationError("token is empty or too large")
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise OIDCVerificationError("token header is invalid") from exc
        if not isinstance(header, dict):
            raise OIDCVerificationError("token header is invalid")
        kid = header.get("kid")
        algorithm = header.get("alg")
        if not isinstance(kid, str) or not kid or len(kid) > 256 or any(ord(c) < 0x20 for c in kid):
            raise OIDCVerificationError("token kid is missing or invalid")
        if not isinstance(algorithm, str) or algorithm not in self.config.algorithms:
            raise OIDCVerificationError("token algorithm is not allowed")
        if header.get("crit") not in (None, []):
            raise OIDCVerificationError("critical JWT headers are not supported")

        jwk = self._key_for(kid, algorithm)
        try:
            return self._decode(token, jwk, algorithm)
        except InvalidSignatureError as first_error:
            # A provider may rotate key material while retaining the kid.  One
            # cooldown-limited forced refresh supports that without turning a
            # bad signature into an attacker-controlled network request.
            refreshed = self._refresh(force=True, require_fresh=False)
            replacement = self._find_key(refreshed, kid, algorithm)
            if replacement is None or replacement == jwk:
                raise OIDCVerificationError("token signature is invalid") from first_error
            try:
                return self._decode(token, replacement, algorithm)
            except Exception as exc:
                raise OIDCVerificationError("token verification failed after key refresh") from exc
        except Exception as exc:
            raise OIDCVerificationError("token verification failed") from exc

    def _decode(
        self,
        token: str,
        jwk_data: Mapping[str, Any],
        algorithm: str,
    ) -> Mapping[str, Any]:
        declared_algorithm = jwk_data.get("alg")
        if declared_algorithm is not None and declared_algorithm != algorithm:
            raise OIDCVerificationError("JWT and JWK algorithms do not agree")
        if not _algorithm_matches_jwk(algorithm, jwk_data):
            raise OIDCVerificationError("JWT algorithm and JWK key type do not agree")
        try:
            key = PyJWK.from_dict(dict(jwk_data), algorithm=algorithm).key
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except InvalidSignatureError:
            raise
        except Exception as exc:
            raise OIDCVerificationError("token claims are invalid") from exc
        if not isinstance(claims, dict):
            raise OIDCVerificationError("token claims are invalid")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise OIDCVerificationError("token subject is empty")
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if (
            isinstance(issued_at, bool)
            or isinstance(expires_at, bool)
            or not isinstance(issued_at, (int, float))
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(issued_at))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= float(issued_at)
            or float(expires_at) - float(issued_at) > self.config.max_token_lifetime_seconds
            or float(issued_at) > time.time() + self.config.leeway_seconds
        ):
            raise OIDCVerificationError("token lifetime is invalid")
        return claims

    def _key_for(self, kid: str, algorithm: str) -> Mapping[str, Any]:
        now = time.monotonic()
        with self._state_lock:
            cache = self._cache
            negative_until = self._negative_kids.get(kid, 0.0)
        if cache is not None and now - cache.fetched_at < self.config.jwks_cache_seconds:
            found = self._find_key(cache, kid, algorithm)
            if found is not None:
                return found
            if negative_until > now:
                raise OIDCVerificationError("token kid is not known")

        refreshed = self._refresh(force=cache is not None, require_fresh=cache is None)
        found = self._find_key(refreshed, kid, algorithm)
        if found is not None:
            return found
        with self._state_lock:
            self._negative_kids[kid] = time.monotonic() + self.config.negative_kid_seconds
            if len(self._negative_kids) > 256:
                oldest = sorted(self._negative_kids.items(), key=lambda item: item[1])[:64]
                for old_kid, _ in oldest:
                    self._negative_kids.pop(old_kid, None)
        raise OIDCVerificationError("token kid is not known")

    def _find_key(
        self,
        cache: _CachedKeys,
        kid: str,
        algorithm: str,
    ) -> Optional[Mapping[str, Any]]:
        return cache.keys.get((kid, algorithm)) or cache.keys.get((kid, None))

    def _refresh(self, *, force: bool, require_fresh: bool) -> _CachedKeys:
        now = time.monotonic()
        with self._state_lock:
            current = self._cache
            if not force and current is not None and now - current.fetched_at < self.config.jwks_cache_seconds:
                return current
            if force and current is not None and now - self._last_forced_refresh < self.config.refresh_cooldown_seconds:
                return current

        acquired = self._refresh_lock.acquire(timeout=self.config.refresh_lock_timeout_seconds)
        if not acquired:
            with self._state_lock:
                current = self._cache
            if self._stale_is_usable(current, require_fresh=require_fresh):
                return current
            raise OIDCVerificationError("OIDC key refresh is already in progress")
        try:
            now = time.monotonic()
            with self._state_lock:
                current = self._cache
                if not force and current is not None and now - current.fetched_at < self.config.jwks_cache_seconds:
                    return current
                if force and current is not None and now - self._last_forced_refresh < self.config.refresh_cooldown_seconds:
                    return current
                if force:
                    self._last_forced_refresh = now
            fetched = _CachedKeys(fetched_at=time.monotonic(), keys=self._fetch_keys())
            with self._state_lock:
                self._cache = fetched
                self._negative_kids = {
                    kid: expiry
                    for kid, expiry in self._negative_kids.items()
                    if expiry > time.monotonic()
                    and not any(key_kid == kid for key_kid, _ in fetched.keys)
                }
            return fetched
        except Exception:
            if self._stale_is_usable(current, require_fresh=require_fresh):
                return current
            raise
        finally:
            self._refresh_lock.release()

    def _stale_is_usable(
        self,
        current: Optional[_CachedKeys],
        *,
        require_fresh: bool,
    ) -> bool:
        if current is None or require_fresh:
            return False
        maximum_age = self.config.jwks_cache_seconds + self.config.jwks_stale_if_error_seconds
        return time.monotonic() - current.fetched_at <= maximum_age

    def _fetch_keys(self) -> Mapping[Tuple[str, Optional[str]], Mapping[str, Any]]:
        timeout = httpx.Timeout(self.config.jwks_timeout_seconds)
        body = bytearray()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                with client.stream(
                    "GET",
                    self.config.jwks_url,
                    headers={"Accept": "application/json"},
                ) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            length = int(content_length)
                            if length < 0 or length > self.config.max_jwks_bytes:
                                raise OIDCVerificationError("JWKS response is too large")
                        except ValueError as exc:
                            raise OIDCVerificationError("JWKS Content-Length is invalid") from exc
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.config.max_jwks_bytes:
                            raise OIDCVerificationError("JWKS response is too large")
        except OIDCVerificationError:
            raise
        except Exception as exc:
            raise OIDCVerificationError("JWKS request failed") from exc
        try:
            document = json.loads(bytes(body))
        except Exception as exc:
            raise OIDCVerificationError("JWKS response is not valid JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise OIDCVerificationError("JWKS response has no keys array")
        raw_keys: Sequence[Any] = document["keys"]
        if not raw_keys or len(raw_keys) > self.config.max_jwks_keys:
            raise OIDCVerificationError("JWKS key count is empty or exceeds the configured cap")

        parsed: Dict[Tuple[str, Optional[str]], Mapping[str, Any]] = {}
        seen_kids = set()
        for raw in raw_keys:
            if not isinstance(raw, dict):
                raise OIDCVerificationError("JWKS contains a non-object key")
            kid = raw.get("kid")
            algorithm = raw.get("alg")
            if not isinstance(kid, str) or not kid or len(kid) > 256:
                raise OIDCVerificationError("JWKS contains a key without a valid kid")
            if algorithm is not None and algorithm not in self.config.algorithms:
                # Ignore explicitly unrelated signing algorithms; providers
                # often publish keys for more than one client/policy.
                continue
            if raw.get("use") not in (None, "sig"):
                continue
            key_ops = raw.get("key_ops")
            if key_ops is not None and (not isinstance(key_ops, list) or "verify" not in key_ops):
                continue
            candidate_algorithms = (
                [algorithm]
                if isinstance(algorithm, str)
                else [
                    candidate
                    for candidate in self.config.algorithms
                    if _algorithm_matches_jwk(candidate, raw)
                ]
            )
            if not candidate_algorithms or any(
                not _algorithm_matches_jwk(candidate, raw)
                for candidate in candidate_algorithms
            ):
                raise OIDCVerificationError("JWKS signing key type/curve does not match its algorithm")
            try:
                # Readiness must prove the advertised material is usable, not
                # merely that the response has a keys-shaped JSON object.
                # Parsing every viable algorithm also catches malformed
                # modulus/point bytes before a customer attempts first login.
                for candidate in candidate_algorithms:
                    public_key = PyJWK.from_dict(dict(raw), algorithm=candidate).key
                    if candidate.startswith("RS") and getattr(public_key, "key_size", 0) < 2048:
                        raise OIDCVerificationError("JWKS RSA signing key is smaller than 2048 bits")
            except OIDCVerificationError:
                raise
            except Exception as exc:
                raise OIDCVerificationError("JWKS contains malformed signing key material") from exc
            identity = (kid, algorithm)
            if kid in seen_kids:
                raise OIDCVerificationError("JWKS contains an ambiguous duplicate kid")
            seen_kids.add(kid)
            parsed[identity] = dict(raw)
        if not parsed:
            raise OIDCVerificationError("JWKS has no usable signing keys")
        return parsed
