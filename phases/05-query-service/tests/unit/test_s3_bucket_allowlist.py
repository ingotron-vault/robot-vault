"""Allowlist the s3api `bucket` param.

`/api/v1/s3/list`, `/presign` and `/download` (`query_service/s3api.py`)
take an optional caller-supplied `bucket`. The `key`/`prefix` is checked
against `ALLOWED_PREFIXES` (`processed/parquet/...`), but that says nothing
about *which bucket*: without an allowlist, a caller could aim this service's
live lake credentials at any other bucket reachable with the same keys, as
long as the requested key started with an allowed prefix.

`_resolve_bucket()`: omitting `bucket` resolves to `S3_PARQUET_BUCKET`; a
`bucket` that names a *different* bucket gets HTTP 400 (`invalid_bucket`),
matching this module's error style (`invalid_prefix`, `missing_key`).

The SPA (`ui-spa/src/pages/Uploads/index.tsx`) never sends `bucket`: it
always omits it, relying on the server's default. `tests/e2e/` does not
call these endpoints at all (it talks to the object store directly via boto3
for setup/teardown). So "omitted -> default bucket" is the *only* case any
real caller exercises, which is why every
"allowed" test below is either an omitted `bucket` or one that names the
configured bucket explicitly (equivalent, and worth pinning down that
explicit-but-correct isn't accidentally rejected too).

Each endpoint's S3 client is stubbed out (`_FakeS3Client`) so these run
without live object store/S3 -- consistent with how this test suite already avoids
live infra (test_bucket_validation.py, test_sql_identifier_allowlists.py).
The stub also records the `Bucket` kwarg each call actually used, so the
"allowed" tests prove the resolved bucket reached the client call, not just
that the endpoint didn't 400.
"""
import importlib
import io
import sys

import pytest
from fastapi.testclient import TestClient


def _import_query_service_main():
    """See test_bucket_validation.py: main.py mounts StaticFiles over the
    SPA build output, which only exists inside the service's Docker image."""
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


qsm = _import_query_service_main()
client = TestClient(qsm.app)

from query_service import s3api  # noqa: E402

CONFIGURED_BUCKET = "robotics-data"
OTHER_BUCKET = "some-other-bucket-the-same-keys-can-reach"


class _FakeBody:
    def iter_chunks(self, chunk_size=1024 * 1024):
        yield b"hello"


class _FakeS3Client:
    """Records every call instead of touching real S3."""

    def __init__(self):
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        return {"Contents": [], "IsTruncated": False, "NextContinuationToken": None}

    def generate_presigned_url(self, **kwargs):
        self.calls.append(("generate_presigned_url", kwargs))
        return "https://example.invalid/presigned-url"

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": _FakeBody(), "ContentType": "application/octet-stream", "ContentLength": 5}


@pytest.fixture
def configured_bucket(monkeypatch):
    monkeypatch.setattr(s3api, "S3_PARQUET_BUCKET", CONFIGURED_BUCKET)  # gitleaks:allow (test value)


@pytest.fixture
def fake_client(monkeypatch):
    fake = _FakeS3Client()
    monkeypatch.setattr(s3api, "_s3_client", lambda endpoint_url=None: fake)
    return fake


# --- /api/v1/s3/list --------------------------------------------------------


def test_list_bucket_omitted_uses_configured_bucket(configured_bucket, fake_client):
    resp = client.get("/api/v1/s3/list", params={"prefix": "processed/parquet/robotA/"})
    assert resp.status_code == 200
    assert fake_client.calls[-1][1]["Bucket"] == CONFIGURED_BUCKET


def test_list_bucket_matching_configured_is_allowed(configured_bucket, fake_client):
    resp = client.get(
        "/api/v1/s3/list",
        params={"prefix": "processed/parquet/robotA/", "bucket": CONFIGURED_BUCKET},
    )
    assert resp.status_code == 200
    assert fake_client.calls[-1][1]["Bucket"] == CONFIGURED_BUCKET


def test_list_bucket_mismatch_rejected_with_400(configured_bucket, fake_client):
    resp = client.get(
        "/api/v1/s3/list",
        params={"prefix": "processed/parquet/robotA/", "bucket": OTHER_BUCKET},
    )
    assert resp.status_code == 400
    assert resp.json().get("detail") == "invalid_bucket"
    # And it must reject BEFORE touching the client -- an aimed-elsewhere
    # bucket must never reach a live S3 call.
    assert fake_client.calls == []


# --- /api/v1/s3/presign -----------------------------------------------------


def test_presign_bucket_omitted_uses_configured_bucket(configured_bucket, fake_client):
    resp = client.post(
        "/api/v1/s3/presign",
        json={"key": "processed/parquet/robotA/part-0.parquet"},
    )
    assert resp.status_code == 200
    assert fake_client.calls[-1][1]["Params"]["Bucket"] == CONFIGURED_BUCKET


def test_presign_bucket_matching_configured_is_allowed(configured_bucket, fake_client):
    resp = client.post(
        "/api/v1/s3/presign",
        json={"key": "processed/parquet/robotA/part-0.parquet", "bucket": CONFIGURED_BUCKET},
    )
    assert resp.status_code == 200
    assert fake_client.calls[-1][1]["Params"]["Bucket"] == CONFIGURED_BUCKET


def test_presign_bucket_mismatch_rejected_with_400(configured_bucket, fake_client):
    resp = client.post(
        "/api/v1/s3/presign",
        json={"key": "processed/parquet/robotA/part-0.parquet", "bucket": OTHER_BUCKET},
    )
    assert resp.status_code == 400
    assert resp.json().get("detail") == "invalid_bucket"
    assert fake_client.calls == []


@pytest.mark.parametrize("expires", [0, -1, 3601, 1.5, True, "1.5", "nope"])
def test_presign_rejects_invalid_or_overlong_expiry(configured_bucket, fake_client, expires):
    resp = client.post(
        "/api/v1/s3/presign",
        json={"key": "processed/parquet/robotA/part-0.parquet", "expires": expires},
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "invalid_expires"}
    assert fake_client.calls == []


@pytest.mark.parametrize("expires", [1, 3600, "60"])
def test_presign_accepts_bounded_integer_expiry(configured_bucket, fake_client, expires):
    resp = client.post(
        "/api/v1/s3/presign",
        json={"key": "processed/parquet/robotA/part-0.parquet", "expires": expires},
    )
    assert resp.status_code == 200
    expected = int(expires)
    assert fake_client.calls[-1][1]["ExpiresIn"] == expected


def test_public_endpoint_is_signed_directly_never_rewritten(configured_bucket, monkeypatch):
    fake = _FakeS3Client()
    endpoints = []

    def build(endpoint=None):
        endpoints.append(endpoint)
        return fake

    monkeypatch.setattr(s3api, "S3_PUBLIC_ENDPOINT", "https://objects.example")
    monkeypatch.setattr(s3api, "_s3_client", build)
    resp = client.post(
        "/api/v1/s3/presign",
        json={"key": "processed/parquet/robotA/part-0.parquet"},
    )
    assert resp.status_code == 200
    assert endpoints == ["https://objects.example"]
    assert resp.json()["url"] == "https://example.invalid/presigned-url"


# --- /api/v1/s3/download ----------------------------------------------------


def test_download_bucket_omitted_uses_configured_bucket(configured_bucket, fake_client):
    resp = client.get(
        "/api/v1/s3/download",
        params={"key": "processed/parquet/robotA/part-0.parquet"},
    )
    assert resp.status_code == 200
    assert fake_client.calls[-1][1]["Bucket"] == CONFIGURED_BUCKET


def test_download_bucket_matching_configured_is_allowed(configured_bucket, fake_client):
    resp = client.get(
        "/api/v1/s3/download",
        params={"key": "processed/parquet/robotA/part-0.parquet", "bucket": CONFIGURED_BUCKET},
    )
    assert resp.status_code == 200
    assert fake_client.calls[-1][1]["Bucket"] == CONFIGURED_BUCKET


def test_download_bucket_mismatch_rejected_with_400(configured_bucket, fake_client):
    resp = client.get(
        "/api/v1/s3/download",
        params={"key": "processed/parquet/robotA/part-0.parquet", "bucket": OTHER_BUCKET},
    )
    assert resp.status_code == 400
    assert resp.json().get("detail") == "invalid_bucket"
    assert fake_client.calls == []


def test_download_filename_header_is_bounded_and_injection_safe(configured_bucket, fake_client):
    resp = client.get(
        "/api/v1/s3/download",
        params={"key": "processed/parquet/robotA/report\r\nX-Evil: yes-ß.parquet"},
    )
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "filename*=UTF-8''" in disposition
    assert "%0D%0A" in disposition
    assert "%C3%9F" in disposition


# --- the helper directly ----------------------------------------------------


def test_resolve_bucket_helper_directly(configured_bucket):
    assert s3api._resolve_bucket(None) == CONFIGURED_BUCKET
    assert s3api._resolve_bucket("") == CONFIGURED_BUCKET
    assert s3api._resolve_bucket(CONFIGURED_BUCKET) == CONFIGURED_BUCKET
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        s3api._resolve_bucket(OTHER_BUCKET)
    assert exc_info.value.status_code == 400
