"""Static and contract tests for the object store's least-privilege identities.

The policy evaluator `_allows()` is also the oracle of the live IAM probe
(development/objectstore-conformance/iam_probe.py), which checks RustFS against it.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


RELEASE_DIR = Path(__file__).resolve().parent
POLICY_DIR = RELEASE_DIR / "objectstore-policies"
BOOTSTRAP = RELEASE_DIR.parent / "objectstore" / "bootstrap.py"
BUCKET_MARKER = "__ROBOT_VAULT_BUCKET__"
TEST_BUCKET = "release-policy-test"
SERVICES = ("ingest", "processor", "exporter", "query", "gc")
FIXED_IDENTITIES = {
    "INGEST": "robot-vault-ingest",
    "PROCESSOR": "robot-vault-processor",
    "EXPORTER": "robot-vault-exporter",
    "QUERY": "robot-vault-query",
    "GC": "robot-vault-gc",
}
ALLOWED_ACTIONS = {
    "s3:GetBucketLocation",
    "s3:ListBucket",
    "s3:ListBucketMultipartUploads",
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:AbortMultipartUpload",
    "s3:ListMultipartUploadParts",
}
MANAGED_PREFIXES = (
    "raw/",
    "manifests/",
    "processed/parquet/",
    "cas/files/",
    "processed/media/",
    "qa/reports/",
    "exports/",
)


def _list(value):
    return value if isinstance(value, list) else [value]


def _load_policy(service: str) -> dict:
    raw = (POLICY_DIR / f"{service}.json").read_text(encoding="utf-8")
    return json.loads(raw.replace(BUCKET_MARKER, TEST_BUCKET))


def _allows(service: str, action: str, *, key: str | None = None, prefix: str | None = None) -> bool:
    """Small default-deny evaluator for the policy shapes used in this tree."""

    bucket_arn = f"arn:aws:s3:::{TEST_BUCKET}"
    resource = bucket_arn if key is None else f"{bucket_arn}/{key}"
    for statement in _load_policy(service)["Statement"]:
        if statement["Effect"] != "Allow" or action not in _list(statement["Action"]):
            continue
        if not any(fnmatch.fnmatchcase(resource, candidate) for candidate in _list(statement["Resource"])):
            continue
        condition = statement.get("Condition", {})
        prefix_patterns = condition.get("StringLike", {}).get("s3:prefix")
        if prefix_patterns is not None:
            if prefix is None or not any(
                fnmatch.fnmatchcase(prefix, candidate) for candidate in _list(prefix_patterns)
            ):
                continue
        return True
    return False


def test_policy_set_is_exact_and_valid_json():
    assert {path.stem for path in POLICY_DIR.glob("*.json")} == set(SERVICES)
    for service in SERVICES:
        source = (POLICY_DIR / f"{service}.json").read_text(encoding="utf-8")
        assert BUCKET_MARKER in source
        document = json.loads(source)
        assert document["Version"] == "2012-10-17"
        assert document["Statement"]
        assert len({statement["Sid"] for statement in document["Statement"]}) == len(
            document["Statement"]
        )


def test_policies_have_no_admin_or_unscoped_wildcard_permissions():
    for service in SERVICES:
        for statement in _load_policy(service)["Statement"]:
            assert statement["Effect"] == "Allow"
            actions = set(_list(statement["Action"]))
            resources = set(_list(statement["Resource"]))
            assert actions <= ALLOWED_ACTIONS
            assert "s3:*" not in actions
            assert "*" not in resources
            assert all(resource.startswith(f"arn:aws:s3:::{TEST_BUCKET}") for resource in resources)
            assert all("admin" not in action.lower() for action in actions)


@pytest.mark.parametrize(
    ("action", "key", "allowed"),
    (
        ("s3:GetObject", "cas/files/digest", True),
        ("s3:GetObject", "raw/mcap/r/u.mcap", True),
        ("s3:PutObject", "raw/mcap/r/u.mcap", True),
        ("s3:PutObject", "cas/files/digest", True),
        ("s3:PutObject", "manifests/r/u.json", True),
        ("s3:PutObject", "_health/ingest/probe-id", True),
        ("s3:DeleteObject", "_health/ingest/probe-id", True),
        ("s3:DeleteObject", "raw/mcap/r/u.mcap", True),
        ("s3:DeleteObject", "processed/parquet/r/x.parquet", False),
        ("s3:DeleteObject", "cas/files/digest", False),
        ("s3:PutObject", "processed/parquet/r/x.parquet", False),
        ("s3:AbortMultipartUpload", "raw/mcap/r/u.mcap", True),
        ("s3:AbortMultipartUpload", "cas/files/digest", False),
    ),
)
def test_ingest_object_boundary(action, key, allowed):
    assert _allows("ingest", action, key=key) is allowed


def test_ingest_can_distinguish_missing_objects_but_cannot_create_buckets():
    # S3 requires ListBucket to distinguish an absent HEAD/Get object (404)
    # from an inaccessible object (403) during interrupted completion recovery.
    assert _allows("ingest", "s3:ListBucket", prefix="raw/")
    assert not _allows("ingest", "s3:CreateBucket")


@pytest.mark.parametrize(
    ("action", "key", "allowed"),
    (
        ("s3:GetObject", "raw/mcap/r/u.mcap", True),
        ("s3:GetObject", "cas/files/digest", True),
        ("s3:GetObject", "processed/parquet/r/x.parquet", True),
        ("s3:GetObject", "processed/media/r/e/c.mp4", True),
        ("s3:GetObject", "qa/reports/r/e.json", True),
        ("s3:GetObject", "exports/d/file", False),
        ("s3:PutObject", "processed/parquet/r/x.parquet", True),
        ("s3:PutObject", "processed/media/r/e/c.mp4", True),
        ("s3:PutObject", "qa/reports/r/e.json", True),
        ("s3:PutObject", "raw/mcap/r/u.mcap", False),
        ("s3:DeleteObject", "processed/parquet/r/x.parquet", False),
    ),
)
def test_processor_object_boundary(action, key, allowed):
    assert _allows("processor", action, key=key) is allowed


@pytest.mark.parametrize(
    ("action", "key", "allowed"),
    (
        ("s3:GetObject", "raw/mcap/r/u.mcap", True),
        ("s3:GetObject", "cas/files/digest", True),
        ("s3:GetObject", "processed/parquet/r/x.parquet", True),
        ("s3:GetObject", "processed/media/r/e/c.mp4", True),
        ("s3:GetObject", "qa/reports/r/e.json", False),
        ("s3:PutObject", "exports/d/file.parquet", True),
        ("s3:PutObject", "processed/parquet/r/x.parquet", False),
        ("s3:DeleteObject", "exports/d/file.parquet", False),
    ),
)
def test_exporter_object_boundary(action, key, allowed):
    assert _allows("exporter", action, key=key) is allowed


@pytest.mark.parametrize(
    ("action", "key", "allowed"),
    (
        ("s3:GetObject", "raw/mcap/r/u.mcap", True),
        ("s3:GetObject", "cas/files/digest", True),
        ("s3:GetObject", "processed/parquet/r/x.parquet", True),
        ("s3:GetObject", "processed/media/r/e/c.mp4", True),
        ("s3:PutObject", "processed/media/r/e/c.mp4", False),
        ("s3:DeleteObject", "processed/media/r/e/c.mp4", False),
        ("s3:PutObject", "processed/parquet/r/x.parquet", False),
        ("s3:DeleteObject", "processed/parquet/r/x.parquet", False),
    ),
)
def test_query_is_read_only_and_prefix_scoped(action, key, allowed):
    assert _allows("query", action, key=key) is allowed


def test_query_can_list_only_the_parquet_lake():
    assert _allows("query", "s3:ListBucket", prefix="processed/parquet/r/")
    assert not _allows("query", "s3:ListBucket", prefix="raw/")
    assert not _allows("query", "s3:ListBucket", prefix="processed/media/")
    assert not _allows("query", "s3:ListBucket", prefix="processed/media/r/e/")
    assert not _allows("query", "s3:ListBucket", prefix="")


def test_gc_lists_managed_prefixes_and_nothing_else():
    for prefix in MANAGED_PREFIXES:
        assert _allows("gc", "s3:ListBucket", prefix=f"{prefix}example")
    assert not _allows("gc", "s3:ListBucket", prefix="unmanaged/example")
    assert not _allows("gc", "s3:ListBucket", prefix="")


def test_gc_delete_boundary_keeps_cas_fail_closed():
    covered = set(MANAGED_PREFIXES) - {"cas/files/"}
    for prefix in covered:
        assert _allows("gc", "s3:DeleteObject", key=f"{prefix}orphan")
    assert not _allows("gc", "s3:DeleteObject", key="cas/files/orphan")
    assert not _allows("gc", "s3:DeleteObject", key="unmanaged/orphan")
    assert not _allows("gc", "s3:GetObject", key="raw/mcap/r/u.mcap")
    assert not _allows("gc", "s3:PutObject", key="raw/mcap/r/u.mcap")


def test_gc_multipart_discovery_and_abort_contract():
    assert _allows("gc", "s3:ListBucketMultipartUploads")
    for prefix in MANAGED_PREFIXES:
        assert _allows("gc", "s3:AbortMultipartUpload", key=f"{prefix}orphan")
    assert not _allows("gc", "s3:AbortMultipartUpload", key="unmanaged/orphan")


def _bootstrap_env(tmp_path: Path) -> dict[str, str]:
    root_file = tmp_path / "root-password"
    root_file.write_text("root-secret-0000000\n", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "OBJECTSTORE_POLICY_DIR": str(POLICY_DIR),
        # Port 9 (discard): any network call would fail, and these cases must stop before one.
        "S3_ENDPOINT": "http://127.0.0.1:9",
        "S3_BUCKET": TEST_BUCKET,
        "OBJECTSTORE_ROOT_USER": "release-root",
        "OBJECTSTORE_ROOT_PASSWORD_FILE": str(root_file),
    }
    for name, identity in FIXED_IDENTITIES.items():
        env[f"{name}_S3_ACCESS_KEY"] = identity
        env[f"{name}_S3_SECRET_KEY"] = f"{name.lower()}-secret-0000000000"
    return env


def _secrets(env: dict[str, str]) -> list[str]:
    values = [v for k, v in env.items() if k.endswith("_S3_SECRET_KEY") or k == "OBJECTSTORE_ROOT_PASSWORD"]
    return values + ["root-secret-0000000"]


def test_bootstrap_fixes_the_five_identities_and_policy_files():
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'SERVICES = ("ingest", "processor", "exporter", "query", "gc")' in source
    assert 'if access != f"robot-vault-{svc}":' in source
    assert set(FIXED_IDENTITIES.values()) == {f"robot-vault-{svc}" for svc in SERVICES}


def test_bootstrap_never_builds_a_command_line():
    source = BOOTSTRAP.read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "os.exec", "os.spawn", "popen"):
        assert forbidden not in source.lower(), forbidden


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ({"QUERY_S3_SECRET_KEY": "ingest-secret-0000000000"}, "must differ"),
        ({"QUERY_S3_ACCESS_KEY": "operator-picked-name"}, "fixed identifier"),
        ({"S3_BUCKET": "unsafe/Bucket"}, "safe DNS-style"),
        ({"GC_S3_SECRET_KEY": "short"}, "at least 16 characters"),
        ({"INGEST_S3_SECRET_KEY": "two\nlines-0000000000"}, "line break"),
        ({"OBJECTSTORE_ROOT_PASSWORD": "root-secret-0000000"}, "only one of"),
        ({"OBJECTSTORE_ROOT_PASSWORD_FILE": "/nonexistent/root"}, "not readable"),
        ({"S3_ENDPOINT": "file:///etc/passwd"}, "http or https"),
    ),
)
def test_bootstrap_rejects_unsafe_inputs_before_any_request(tmp_path, mutation, expected_error):
    pytest.importorskip("botocore")
    env = _bootstrap_env(tmp_path)
    env.update(mutation)
    result = subprocess.run(
        [sys.executable, str(BOOTSTRAP)], env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 1
    assert expected_error in result.stderr
    assert "not ready" not in result.stderr  # failed on input, not on the network
    for secret in _secrets(env):
        assert secret not in result.stdout + result.stderr
