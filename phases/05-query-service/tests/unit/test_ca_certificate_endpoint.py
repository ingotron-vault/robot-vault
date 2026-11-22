"""`GET /ca.crt` -- the ingest CA that robots pin before they enroll.

Requirements:
- **Unauthenticated and un-guarded.** Registered next to `/healthz` and
  `/readyz`, *not* through a router carrying `dependencies=viewer_guard`.
- Serves the file at `INGEST_CA_CERT_FILE` verbatim as
  `application/x-pem-file` with `Cache-Control: no-store`; 503 if the file
  is unreadable.
- A misconfigured `INGEST_CA_CERT_FILE` must be a 503, never a key leak. The
  route is unauthenticated by design, so "serves the configured file
  verbatim" is only safe while the configured file is provably a certificate.
"""
from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from query_service import settings

CERT_PEM = b"""-----BEGIN CERTIFICATE-----
MIIBkTCB+wIJAKl1nSg2wq5PMA0GCSqGSIb3DQEBCwUAMBQxEjAQBgNVBAMMCWlu
Z290cm9uMB4XDTI2MDgxNzAwMDAwMFoXDTM2MDgxNDAwMDAwMFowFDESMBAGA1UE
-----END CERTIFICATE-----
"""


def _import_query_service_main():
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]
    import fastapi.staticfiles as staticfiles

    class _StubStaticFiles:
        def __init__(self, *args, **kwargs):
            pass

        async def __call__(self, scope, receive, send):  # pragma: no cover
            raise AssertionError("static files are not used in this test")

    original = staticfiles.StaticFiles
    staticfiles.StaticFiles = _StubStaticFiles
    try:
        import query_service.main as module
    finally:
        staticfiles.StaticFiles = original
    return module


qsm = _import_query_service_main()
client = TestClient(qsm.app, raise_server_exceptions=False)


def test_a_present_ca_is_served_verbatim_with_the_pem_content_type(tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(CERT_PEM)
    monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", str(ca))

    response = client.get("/ca.crt")

    assert response.status_code == 200
    assert response.content == CERT_PEM, "the CA was not served byte-for-byte"
    assert response.headers["content-type"].startswith("application/x-pem-file")
    assert response.headers["cache-control"] == "no-store"


def test_an_absent_or_unconfigured_ca_is_a_bounded_503(tmp_path, monkeypatch):
    for value in (str(tmp_path / "missing.crt"), "", "   ", str(tmp_path)):
        monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", value)
        response = client.get("/ca.crt")
        assert response.status_code == 503, value
        assert response.json() == {"error": "ca_certificate_unavailable"}
        # An unauthenticated route must not leak the path or the OS error.
        assert str(tmp_path) not in response.text
        assert "Traceback" not in response.text


def test_the_route_needs_no_authentication_in_either_auth_mode(tmp_path, monkeypatch):
    """A robot fetches this to pin the CA before it has any credential at all."""
    ca = tmp_path / "ca.crt"
    ca.write_bytes(CERT_PEM)
    monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", str(ca))

    for mode in ("none", "oidc"):
        monkeypatch.setattr(settings, "AUTH_MODE", mode)
        monkeypatch.setattr(settings, "OIDC_DEV", True)
        response = client.get("/ca.crt")
        assert response.status_code == 200, mode
        assert "www-authenticate" not in {k.lower() for k in response.headers}

    # And a data route in the same process DOES demand a token in oidc mode,
    # so the comparison above is a real difference and not an inert app.
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    assert client.get("/api/v1/robots/list").status_code == 401


def test_a_misconfigured_path_is_a_503_and_never_a_key_leak(tmp_path, monkeypatch):
    key = tmp_path / "leaf.key"
    key.write_bytes(
        b"-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n"  # gitleaks:allow (fake key)
    )
    monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", str(key))
    response = client.get("/ca.crt")
    assert response.status_code == 503
    assert b"PRIVATE KEY" not in response.content

    both = tmp_path / "bundle.pem"
    both.write_bytes(CERT_PEM + b"-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", str(both))
    assert client.get("/ca.crt").status_code == 503

    garbage = tmp_path / "notes.txt"
    garbage.write_bytes(b"this is not a certificate\n")
    monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", str(garbage))
    assert client.get("/ca.crt").status_code == 503


def test_an_oversized_file_is_refused_rather_than_streamed(tmp_path, monkeypatch):
    huge = tmp_path / "huge.crt"
    huge.write_bytes(CERT_PEM + b"#" * (qsm.CA_CERT_MAX_BYTES + 1))
    monkeypatch.setattr(settings, "INGEST_CA_CERT_FILE", str(huge))
    assert client.get("/ca.crt").status_code == 503


def test_the_default_path_is_the_release_ingest_ca_mount():
    assert settings.INGEST_CA_CERT_FILE == "/run/robot-vault/tls/ingest-ca.pem"
