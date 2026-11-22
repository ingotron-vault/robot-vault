"""Object-store client contract (development/objectstore/CONTRACT.md section 3).

Every boto3 client of the query service and the GC signs with SigV4 and uses path style.
Presigned URLs too: Garage and Versity accept only SigV4, and RustFS honours both, so SigV2
would hide a drift until a store change. The live check (a URL signed for the public host:
Range 206, tampered 403) is `development/objectstore-conformance/rustfs_contract_probe.py`.
"""
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "phases/05-query-service/src"))

import s3_gc  # noqa: E402
from query_service import s3api, uploads  # noqa: E402

ENV = {
    "S3_ENDPOINT": "http://objectstore:9000",
    "AWS_ACCESS_KEY_ID": "robot-vault-query",
    "AWS_SECRET_ACCESS_KEY": "x" * 24,
}


@pytest.fixture
def clients(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(s3api, "S3_ENDPOINT", ENV["S3_ENDPOINT"])
    monkeypatch.setattr(s3api, "S3_ACCESS_KEY_ID", ENV["AWS_ACCESS_KEY_ID"])
    monkeypatch.setattr(s3api, "S3_SECRET_ACCESS_KEY", ENV["AWS_SECRET_ACCESS_KEY"])
    monkeypatch.setattr(uploads, "_S3_ENDPOINT", ENV["S3_ENDPOINT"])
    monkeypatch.setattr(uploads, "_S3_ACCESS_KEY_ID", ENV["AWS_ACCESS_KEY_ID"])
    monkeypatch.setattr(uploads, "_S3_SECRET_ACCESS_KEY", ENV["AWS_SECRET_ACCESS_KEY"])
    return {
        "query s3api": s3api._s3_client(),
        "query uploads": uploads._s3_client_for_uploads(),
        "gc": s3_gc._make_s3_client(),
    }


@pytest.mark.parametrize("name", ["query s3api", "query uploads", "gc"])
def test_presigned_url_is_sigv4_and_path_style(clients, name):
    # `client.meta.config.signature_version` reads "s3v4" even when presigning falls back to
    # SigV2 (botocore 1.43), so check the URL a client really produces.
    url = clients[name].generate_presigned_url(
        "get_object", Params={"Bucket": "rv", "Key": "processed/parquet/a.parquet"}, ExpiresIn=60
    )
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "objectstore:9000"
    assert parts.path == "/rv/processed/parquet/a.parquet"  # path style
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "X-Amz-Signature" in query and "Signature" not in query
    assert "us-east-1" in query["X-Amz-Credential"][0]


def test_presigned_url_for_the_public_host(clients):
    url = s3api._s3_client("https://objectstore-public:9000").generate_presigned_url(
        "get_object", Params={"Bucket": "rv", "Key": "k"}, ExpiresIn=60
    )
    assert urlsplit(url).netloc == "objectstore-public:9000"
    assert parse_qs(urlsplit(url).query)["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
